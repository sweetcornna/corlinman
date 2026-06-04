"""``evolution.engine_run_once`` builtin — R8 PASSIVE slice (L2).

Wraps :meth:`corlinman_evolution_engine.engine.EvolutionEngine.run_once`
in the scheduler-builtin contract so the gateway's cron loop can fire the
daily evolution pass on its own cadence — *in-process*, not by shelling
out to ``corlinman-evolution-engine run-once``. Without this nothing ever
schedules the engine, so clustered signals never become PENDING proposals
in the operator queue.

This is the PASSIVE half of the loop: ``run_once`` only mints
``status=pending`` proposals from clustered signals. It never applies,
rolls back, or touches a live target — those are later, separately-gated
phases. The whole job is wired behind ``[evolution.engine] enabled`` which
defaults OFF.

Behaviour matrix (mirrors
:mod:`corlinman_server.scheduler.builtins.evolution_darwin_curate` and
:mod:`corlinman_server.scheduler.builtins.persona_decay`):

* No ``data_dir`` reachable from ``app_state`` → builtin returns
  ``{"ok": False, "reason": "data_dir_unavailable"}``. Same envelope
  shape so the scheduler history surfaces *why* the run skipped rather
  than logging a stack trace.
* ``corlinman_evolution_engine`` not importable (stubbed test fixture,
  partial install) → ``{"ok": False, "reason": "deps_unavailable: ..."}``.
* SQLite locked / permission denied / engine raised → caught, returned as
  ``{"ok": False, "reason": "run_failed: ..."}``.

The actual signals → clusters → handlers → persist pipeline lives in
:class:`~corlinman_evolution_engine.engine.EvolutionEngine`; this wrapper
only resolves the ``evolution.sqlite`` / ``kb.sqlite`` paths off
``app_state``, reads the optional ``[evolution.budget]`` config section,
and forwards a small report.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from corlinman_server.scheduler.builtins.registry import (
    BuiltinContext,
    register_builtin,
)

_logger = logging.getLogger(
    "corlinman_server.scheduler.builtins.evolution_engine_run_once"
)


#: Builtin name used in ``JobAction.run_tool(plugin="evolution", tool="engine_run_once")``.
#: The dot-joined form matches the scheduler's ``<plugin>.<tool>`` convention.
EVOLUTION_ENGINE_RUN_ONCE_BUILTIN_NAME: str = "evolution.engine_run_once"


__all__ = [
    "EVOLUTION_ENGINE_RUN_ONCE_BUILTIN_NAME",
    "_evolution_engine_run_once_action",
]


def _resolve_data_dir(context: BuiltinContext) -> Path | None:
    """Find the gateway's writable data dir on ``app_state``.

    Same three-probe pattern :func:`_evolution_darwin_curate_action`'s
    resolver uses — falls through ``app_state`` → ``admin_state`` so a
    degraded boot that only landed one of them still discovers the path.
    """
    for owner in (context.app_state, context.admin_state):
        if owner is None:
            continue
        raw = getattr(owner, "data_dir", None)
        if raw is None:
            continue
        if isinstance(raw, Path):
            return raw
        return Path(str(raw))
    return None


def _resolve_config(context: BuiltinContext) -> Any:
    """Return the gateway config (a plain dict) off ``app_state``.

    The live config snapshot rides on ``AppState.config``; a degraded
    boot before the watcher attached one leaves it ``None``, which the
    budget reader below tolerates by falling back to the disabled
    default.
    """
    for owner in (context.app_state, context.admin_state):
        if owner is None:
            continue
        cfg = getattr(owner, "config", None)
        if cfg is not None:
            return cfg
    return None


def _build_budget(config: Any) -> Any:
    """Read ``[evolution.budget]`` off the plain-dict gateway config.

    Mirrors :func:`corlinman_evolution_engine.cli._load_budget_config`'s
    section shape but sources the values from the already-loaded gateway
    config dict rather than re-parsing a TOML file. Missing section /
    non-dict config → the disabled-by-default :class:`BudgetConfig`.
    """
    from corlinman_evolution_engine.engine import BudgetConfig  # noqa: PLC0415

    if not isinstance(config, dict):
        return BudgetConfig()
    evolution = config.get("evolution")
    if not isinstance(evolution, dict):
        return BudgetConfig()
    section = evolution.get("budget")
    if not isinstance(section, dict):
        return BudgetConfig()
    per_kind_raw = section.get("per_kind", {})
    per_kind: dict[str, int] = {}
    if isinstance(per_kind_raw, dict):
        for k, v in per_kind_raw.items():
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool):
                per_kind[k] = v
    return BudgetConfig(
        enabled=bool(section.get("enabled", False)),
        weekly_total=int(section.get("weekly_total", 15)),
        per_kind=per_kind,
    )


async def _evolution_engine_run_once_action(
    context: BuiltinContext,
) -> dict[str, Any]:
    """Run one ``EvolutionEngine.run_once`` pass against the gateway's
    ``evolution.sqlite`` + ``kb.sqlite`` and return a small report the
    scheduler history persists verbatim.

    PASSIVE only: the engine mints ``status=pending`` proposals from
    clustered signals. Nothing is applied. This wrapper resolves paths
    off ``app_state``, builds the :class:`EngineConfig` (with the
    optional budget gate), and forwards the run counts.
    """
    data_dir = _resolve_data_dir(context)
    if data_dir is None:
        return {"ok": False, "reason": "data_dir_unavailable"}

    # Lazy imports — ``corlinman_evolution_engine`` isn't on every test
    # fixture's PYTHONPATH (gateway-side tests stub the scheduler) and we'd
    # rather degrade than crash on a deep import chain.
    try:
        from corlinman_evolution_engine.engine import (  # noqa: PLC0415
            EngineConfig,
            EvolutionEngine,
        )
    except ImportError as exc:
        return {"ok": False, "reason": f"deps_unavailable: {exc}"}

    evolution_db = data_dir / "evolution.sqlite"
    kb_db = data_dir / "kb.sqlite"

    try:
        budget = _build_budget(_resolve_config(context))
        # ``enabled_kinds`` is left at the EngineConfig default (all
        # handlers) — the PASSIVE slice only mints proposals; the high-risk
        # kinds stay gated downstream by the shadow tester (L3).
        cfg = EngineConfig(
            db_path=evolution_db,
            kb_path=kb_db,
            budget=budget,
        )
        summary = await EvolutionEngine(cfg).run_once()
    except Exception as exc:  # noqa: BLE001 - never raise out of builtin
        _logger.warning(
            "scheduler.builtin.evolution_engine_run_once.failed",
            extra={"error": repr(exc)},
        )
        return {"ok": False, "reason": f"run_failed: {exc!r}"}

    return {
        "ok": True,
        "evolution_db": str(evolution_db),
        "kb_db": str(kb_db),
        "signals_loaded": summary.signals_loaded,
        "clusters_found": summary.clusters_found,
        "proposals_written": summary.proposals_written,
        "skipped_existing": summary.skipped_existing,
        "truncated_by_cap": summary.truncated_by_cap,
        "skipped_by_budget": summary.skipped_by_budget,
        "proposals_skipped_budget": summary.proposals_skipped_budget,
        "proposals_by_kind": dict(summary.proposals_by_kind),
        "elapsed_seconds": summary.elapsed_seconds,
    }


# Module-load-time registration so the package ``__init__`` import is
# all that's required to wire the builtin. Tests that monkeypatch the
# registry can simply replace
# ``BUILTIN_ACTIONS[EVOLUTION_ENGINE_RUN_ONCE_BUILTIN_NAME]`` without
# redoing this dance.
register_builtin(
    EVOLUTION_ENGINE_RUN_ONCE_BUILTIN_NAME,
    _evolution_engine_run_once_action,
)
