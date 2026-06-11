"""``corlinman console`` — launch the interactive agent console.

See ``docs/PLAN_CLI_CONSOLE.md`` and :mod:`corlinman_server.console`.

Examples::

    corlinman console                       # embedded full-agent REPL
    corlinman console "总结一下今天的日志"     # REPL, first turn pre-filled
    corlinman console -p "1+1等于几" | cat    # one-shot, stdout = answer only
    corlinman console --attach http://127.0.0.1:6005   # client of a gateway
    corlinman console --model gpt-4o-mini --session console:abc123
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click

try:  # Python 3.11+ stdlib
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - older interpreters
    import tomli as tomllib  # type: ignore[no-redef]

from corlinman_server.cli._common import default_config_path, resolve_data_dir
from corlinman_server.console.render import TOOL_PROGRESS_MODES

__all__ = ["console"]


def _quiet_logging() -> None:
    """Route server-plane logs away from the console UI.

    The embedded servicer logs via structlog (PrintLogger → stdout by
    default) and gRPC chats on stderr at INFO — both would interleave
    with the streamed answer. The renderer *is* the UI here, so logs go
    to stderr at WARNING; ``--print`` keeps stdout answer-only.
    """
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)
    try:
        import structlog  # noqa: PLC0415

        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        )
    except Exception:  # noqa: BLE001 — logging polish is best-effort
        pass


def _load_config(data_dir: Path) -> dict[str, Any]:
    """Best-effort ``config.toml`` read — a missing/broken file gives an
    empty dict (the console then runs on env-key provider fallback)."""
    path = default_config_path(data_dir)
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError):
        return {}


@click.command("console")
@click.argument("prompt", nargs=-1)
@click.option(
    "--attach",
    metavar="URL",
    default=None,
    help="Attach to a running gateway instead of hosting the brain in-process.",
)
@click.option("--model", default=None, help="Model id or alias for this run.")
@click.option(
    "--session",
    "session_key",
    default=None,
    help="Session key to continue (default: fresh console:<id> key).",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    default=None,
    help="Data dir (default: $CORLINMAN_DATA_DIR or ~/.corlinman).",
)
@click.option(
    "-p",
    "--print",
    "print_mode",
    is_flag=True,
    help="Non-interactive: run PROMPT once, write only the answer to stdout.",
)
@click.option(
    "--tool-progress",
    type=click.Choice(TOOL_PROGRESS_MODES),
    default="new",
    show_default=True,
    help="Tool-call progress display mode.",
)
def console(
    prompt: tuple[str, ...],
    attach: str | None,
    model: str | None,
    session_key: str | None,
    data_dir: str | None,
    print_mode: bool,
    tool_progress: str,
) -> None:
    """Interactive agent console (REPL) — the CLI face of the corlinman brain.

    Hosts the full agent in-process by default (tools, subagents, memory,
    journal — identical to production wiring), or attaches to a running
    gateway with --attach. PROMPT, when given, is sent as the first turn.
    """
    from corlinman_server.console import run_console  # noqa: PLC0415 — heavy import

    _quiet_logging()
    resolved_data_dir = resolve_data_dir(Path(data_dir) if data_dir else None)
    config = _load_config(resolved_data_dir)
    prompt_text = " ".join(prompt).strip() or None

    try:
        code = asyncio.run(
            run_console(
                data_dir=resolved_data_dir,
                config=config,
                model=model,
                attach=attach,
                session_key=session_key,
                prompt=prompt_text,
                print_mode=print_mode,
                tool_progress=tool_progress,
            )
        )
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
