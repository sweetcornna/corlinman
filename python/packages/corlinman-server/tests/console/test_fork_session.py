"""E3 --fork-session at the console layer.

Two seams:

- :meth:`ConsoleApp.fork_from` — the embedded-only journal-lifecycle
  wrapper that ``run_console`` calls before resuming a forked key. Driven
  against a REAL tmp journal (fork is I/O, not logic worth stubbing), plus
  the attach-mode degrade path.
- the click command + ``run_console`` signature carry the flag, so the CLI
  plumbing that reaches the wrapper is present.
"""

from __future__ import annotations

import inspect
import io
from pathlib import Path
from typing import Any

from corlinman_server.agent_journal import AgentJournal
from corlinman_server.console.app import ConsoleApp, run_console
from corlinman_server.console.brain import BrainSession
from corlinman_server.console.render import Renderer
from corlinman_server.console.router import ModelRouter
from rich.console import Console


class _IdleBrain:
    descriptor = "stub"

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _app(
    path: Path | None, *, embedded: bool = True, has_journal: bool = True
) -> ConsoleApp:
    """A ConsoleApp whose ``_open_journal`` mints a FRESH handle per call.

    ``fork_from`` (like ``resume_session``) opens then closes its own
    journal handle, so the opener must hand back a new one each time — a
    single shared handle would be closed out from under the test's own
    verification reads. ``path=None`` / ``has_journal=False`` simulates the
    "no journal available" degrade.
    """
    app = ConsoleApp(
        session=BrainSession(brain=_IdleBrain(), model="m"),
        renderer=Renderer(Console(file=io.StringIO(), force_terminal=False)),
        router=ModelRouter(default_model="m", small_fast_model=None, auto_route=False),
        data_dir=Path("/nonexistent"),
        embedded=embedded,
    )

    async def _open() -> Any:
        if not has_journal or path is None:
            return None
        return await AgentJournal.open(path)

    app._open_journal = _open  # type: ignore[method-assign]
    return app


async def test_fork_from_copies_completed_history(tmp_path: Path) -> None:
    path = tmp_path / "agent_journal.sqlite"
    src = "console:src"
    seed = await AgentJournal.open(path)
    try:
        t = await seed.begin_turn(src, "hello")
        assert t is not None
        await seed.append_message(t, "user", "hello")
        await seed.append_message(t, "assistant", "hi")
        await seed.complete_turn(t)
    finally:
        await seed.close()

    app = _app(path)
    copied = await app.fork_from(src, "console:dst")
    assert copied == 1

    # Source untouched, fork populated — verify through a fresh handle.
    verify = await AgentJournal.open(path)
    try:
        assert len(await verify.get_session_turn_ids(src, limit=50)) == 1
        assert len(await verify.get_session_turn_ids("console:dst", limit=50)) == 1
    finally:
        await verify.close()


async def test_fork_from_is_zero_in_attach_mode(tmp_path: Path) -> None:
    app = _app(tmp_path / "agent_journal.sqlite", embedded=False)
    # Attach mode owns no local journal — fork degrades to 0 (the caller
    # then resumes the source key unforked).
    assert await app.fork_from("console:src", "console:dst") == 0


async def test_fork_from_is_zero_without_journal() -> None:
    app = _app(None, has_journal=False)
    assert await app.fork_from("console:src", "console:dst") == 0


def test_cli_and_run_console_expose_fork_flag() -> None:
    """The click command carries --fork-session and run_console accepts it."""
    from corlinman_server.cli.console import console

    names = {p.name for p in console.params}
    assert "fork_session" in names
    assert "fork_session" in inspect.signature(run_console).parameters
