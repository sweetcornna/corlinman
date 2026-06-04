"""``corlinman-shadow-tester`` CLI entry point.

Thin wrapper that loads a corlinman config, opens ``evolution.sqlite``,
builds a :class:`~corlinman_shadow_tester.ShadowRunner` with the three
built-in simulators registered, and runs one
:meth:`ShadowRunner.run_once` pass.  Designed to be invoked as a
subprocess job from a scheduler — same shape as ``corlinman-auto-rollback``.

Config is read from ``[evolution.shadow]`` inside the corlinman TOML::

    [evolution.shadow]
    enabled = true          # default false → no-op + exit 0
    kb_path = "/data/kb.sqlite"
    eval_set_dir = "/data/evolution/eval_sets"  # optional
    # data_dir = "/data"   # fallback when kb_path / eval_set_dir are absent

When ``enabled = false`` (the default) the CLI prints a no-op notice and
exits 0 — safe to leave in a crontab before the operator is ready to
enable shadow-testing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from corlinman_evolution_store import EvolutionStore, ProposalsRepo

from corlinman_shadow_tester.runner import RunSummary, ShadowRunner
from corlinman_shadow_tester.simulator import (
    MemoryOpSimulator,
    SkillUpdateSimulator,
    TagRebalanceSimulator,
)

logger = logging.getLogger("corlinman_shadow_tester")

PROG_NAME = "corlinman-shadow-tester"


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG_NAME,
        description=(
            "ShadowTester — shadow-runs pending medium/high-risk "
            "EvolutionProposals against an in-process eval set before "
            "they reach the operator queue."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run-once",
        help=(
            "Run one shadow pass: claim pending medium/high-risk proposals, "
            "replay each eval case against a tempdir copy of kb.sqlite, "
            "write shadow_done results back."
        ),
    )
    run.add_argument(
        "--config",
        type=Path,
        required=True,
        help=(
            "Path to the corlinman config (corlinman.toml). Reads "
            "[evolution.shadow].enabled, [evolution.shadow].kb_path, "
            "[evolution.shadow].eval_set_dir, and [server].data_dir."
        ),
    )
    run.add_argument(
        "--evolution-db",
        type=Path,
        default=None,
        help=(
            "Override the evolution.sqlite path derived from config. "
            "Useful for running against a test DB."
        ),
    )
    run.add_argument(
        "--max-proposals",
        type=int,
        default=None,
        help="Per-run cap on proposals processed per kind (default: 10).",
    )
    run.add_argument(
        "--json",
        action="store_true",
        help="Emit the run summary as JSON on stdout.",
    )
    run.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable INFO logging.",
    )
    return parser


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _load_config(path: Path) -> dict[str, Any]:
    """Best-effort TOML load — propagates FileNotFoundError /
    tomllib.TOMLDecodeError so the CLI exits with a clean message."""
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _shadow_section(raw: dict[str, Any]) -> dict[str, Any]:
    """Return ``[evolution.shadow]`` as a dict, empty if absent."""
    evolution = raw.get("evolution", {})
    if not isinstance(evolution, dict):
        return {}
    shadow = evolution.get("shadow", {})
    return shadow if isinstance(shadow, dict) else {}


def _resolve_data_dir(raw: dict[str, Any]) -> Path:
    """Resolve the data directory via the three-probe pattern used elsewhere
    in the gateway:

    1. ``CORLINMAN_DATA_DIR`` env var.
    2. ``[server].data_dir`` in the TOML.
    3. Hard-coded ``/data`` fallback (mirrors Rust binary).
    """
    env_dir = os.environ.get("CORLINMAN_DATA_DIR")
    if env_dir:
        return Path(env_dir)

    server = raw.get("server", {})
    if isinstance(server, dict):
        data_dir = server.get("data_dir")
        if isinstance(data_dir, str) and data_dir:
            return Path(data_dir)

    return Path("/data")


def _resolve_evolution_db_path(raw: dict[str, Any], override: Path | None) -> Path:
    """Resolve evolution.sqlite — explicit override wins, then
    ``[evolution.observer].db_path``, then ``<data_dir>/evolution.sqlite``."""
    if override is not None:
        return override

    evolution = raw.get("evolution", {})
    if isinstance(evolution, dict):
        observer = evolution.get("observer", {})
        if isinstance(observer, dict):
            db_path = observer.get("db_path")
            if isinstance(db_path, str) and db_path:
                return Path(db_path)

    return _resolve_data_dir(raw) / "evolution.sqlite"


def _resolve_kb_path(shadow: dict[str, Any], raw: dict[str, Any]) -> Path:
    """Resolve the production ``kb.sqlite`` path.

    Priority:
    1. ``[evolution.shadow].kb_path``
    2. ``<data_dir>/kb.sqlite``
    """
    kb_path = shadow.get("kb_path")
    if isinstance(kb_path, str) and kb_path:
        return Path(kb_path)
    return _resolve_data_dir(raw) / "kb.sqlite"


def _resolve_eval_set_dir(shadow: dict[str, Any], raw: dict[str, Any]) -> Path:
    """Resolve the eval-set root directory.

    Priority:
    1. ``[evolution.shadow].eval_set_dir``
    2. ``<data_dir>/evolution/eval_sets``
    """
    eval_set_dir = shadow.get("eval_set_dir")
    if isinstance(eval_set_dir, str) and eval_set_dir:
        return Path(eval_set_dir)
    return _resolve_data_dir(raw) / "evolution" / "eval_sets"


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------


def _summary_to_dict(summary: RunSummary) -> dict[str, int]:
    return asdict(summary)


def _print_summary(summary: RunSummary, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_summary_to_dict(summary), indent=2))
        return
    print(f"proposals_claimed:    {summary.proposals_claimed}")
    print(f"proposals_completed:  {summary.proposals_completed}")
    print(f"proposals_failed:     {summary.proposals_failed}")
    print(f"cases_run:            {summary.cases_run}")
    print(f"errors:               {summary.errors}")


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _run_once_async(
    *,
    config_path: Path,
    evolution_db_override: Path | None,
    max_proposals: int | None,
) -> RunSummary:
    raw = _load_config(config_path)
    shadow = _shadow_section(raw)

    if not shadow.get("enabled", False):
        print(
            "shadow-tester: [evolution.shadow].enabled = false — "
            "no-op pass. Set enabled = true to activate shadow testing.",
            file=sys.stderr,
        )
        return RunSummary()

    evolution_db = _resolve_evolution_db_path(raw, evolution_db_override)
    kb_path = _resolve_kb_path(shadow, raw)
    eval_set_dir = _resolve_eval_set_dir(shadow, raw)

    logger.info(
        "shadow-tester: opening evolution.sqlite at %s (kb=%s, eval_set_dir=%s)",
        evolution_db,
        kb_path,
        eval_set_dir,
    )

    store = await EvolutionStore.open(evolution_db)
    try:
        proposals = ProposalsRepo(store.conn)
        runner = ShadowRunner(
            proposals=proposals,
            kb_path=kb_path,
            eval_set_dir=eval_set_dir,
        )
        # Register the three built-in simulators.
        runner.register_simulator(MemoryOpSimulator())
        runner.register_simulator(TagRebalanceSimulator())
        runner.register_simulator(SkillUpdateSimulator())

        if max_proposals is not None:
            runner.with_max_proposals_per_run(max_proposals)

        summary = await runner.run_once()
    finally:
        await store.close()

    logger.info(
        "shadow-tester: run-once complete (claimed=%d completed=%d "
        "failed=%d cases=%d errors=%d)",
        summary.proposals_claimed,
        summary.proposals_completed,
        summary.proposals_failed,
        summary.cases_run,
        summary.errors,
    )
    return summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run-once":
        logging.basicConfig(
            level=logging.INFO if args.verbose else logging.WARNING,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        try:
            summary = asyncio.run(
                _run_once_async(
                    config_path=args.config,
                    evolution_db_override=args.evolution_db,
                    max_proposals=args.max_proposals,
                )
            )
        except SystemExit:
            raise
        except FileNotFoundError as exc:
            logger.error("shadow-tester: %s", exc)
            return 2
        except tomllib.TOMLDecodeError as exc:
            logger.error("shadow-tester: failed to parse config TOML: %s", exc)
            return 2
        _print_summary(summary, as_json=args.json)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2  # parser.error exits, but appease type-checkers.


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
