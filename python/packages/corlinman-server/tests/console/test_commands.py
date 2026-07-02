"""Slash command registry + dispatch against a stub app."""

from __future__ import annotations

import io
from typing import Any

from corlinman_server.console.brain import BrainSession
from corlinman_server.console.commands import dispatch, registry
from corlinman_server.console.render import Renderer
from corlinman_server.console.router import ModelRouter
from rich.console import Console


class _IdleBrain:
    descriptor = "stub brain"

    def run_turn(self, **_kw: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("commands must not run turns")

    async def aclose(self) -> None:  # pragma: no cover - unused
        pass


class StubApp:
    """The slice of ConsoleApp the command handlers touch."""

    def __init__(self) -> None:
        self.session = BrainSession(brain=_IdleBrain(), model="big")
        self.renderer = Renderer(Console(file=io.StringIO(), force_terminal=False))
        self.router = ModelRouter(
            default_model="big", small_fast_model="small", auto_route=False
        )
        self.running = True
        self._models = ["alias-a", "alias-b"]

    def known_models(self) -> list[str]:
        return self._models

    async def list_sessions(self) -> list[str] | None:
        return ["console:aaa  (4 msgs)  hello"]

    async def resume_session(self, key: str) -> int | None:
        self.session.reset(session_key=key)
        return 2


async def test_help_lists_every_command() -> None:
    app = StubApp()
    text = await dispatch(app, "/help") or ""
    for cmd in registry():
        assert f"/{cmd.name}" in text


async def test_unknown_command_is_a_hint_not_a_crash() -> None:
    app = StubApp()
    text = await dispatch(app, "/nope") or ""
    assert "unknown command" in text


async def test_bare_slash_shows_help() -> None:
    app = StubApp()
    text = await dispatch(app, "/") or ""
    assert "commands:" in text


async def test_model_switch_updates_session_and_router() -> None:
    app = StubApp()
    await dispatch(app, "/model gpt-x")
    assert app.session.model == "gpt-x"
    assert app.router.default_model == "gpt-x"


async def test_model_show_includes_known_aliases() -> None:
    app = StubApp()
    text = await dispatch(app, "/model") or ""
    assert "big" in text and "alias-a" in text


async def test_new_rotates_session() -> None:
    app = StubApp()
    old = app.session.session_key
    text = await dispatch(app, "/new") or ""
    assert app.session.session_key != old
    assert app.session.session_key in text


async def test_resume_switches_key() -> None:
    app = StubApp()
    text = await dispatch(app, "/resume console:bbb") or ""
    assert app.session.session_key == "console:bbb"
    assert "2 message(s) replayed" in text


async def test_resume_requires_key() -> None:
    app = StubApp()
    text = await dispatch(app, "/resume") or ""
    assert "usage:" in text


async def test_progress_validates_mode() -> None:
    app = StubApp()
    bad = await dispatch(app, "/progress nope") or ""
    assert "usage:" in bad
    await dispatch(app, "/progress verbose")
    assert app.renderer.tool_progress == "verbose"


async def test_verbose_toggles() -> None:
    app = StubApp()
    await dispatch(app, "/verbose")
    assert app.renderer.tool_progress == "verbose"
    await dispatch(app, "/verbose")
    assert app.renderer.tool_progress == "new"


async def test_quit_stops_the_loop() -> None:
    app = StubApp()
    assert await dispatch(app, "/quit") is None
    assert app.running is False


async def test_alias_dispatch() -> None:
    app = StubApp()
    assert await dispatch(app, "/q") is None
    assert app.running is False


async def test_status_mentions_brain_and_model() -> None:
    app = StubApp()
    text = await dispatch(app, "/status") or ""
    assert "stub brain" in text and "big" in text


async def test_init_returns_turn_request_with_codebase_analysis_prompt() -> None:
    """/init resolves to a TurnRequest (run through the brain), not printed text,
    instructing the agent to analyze the repo and write CORLINMAN.md."""
    from corlinman_server.console.commands import TurnRequest

    app = StubApp()
    reply = await dispatch(app, "/init")
    assert isinstance(reply, TurnRequest)
    assert "CORLINMAN.md" in reply.content
    assert "build" in reply.content.lower() and "test" in reply.content.lower()
