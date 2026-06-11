"""Console fallback to the cross-surface (channels) command registry."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.console.brain import BrainSession
from corlinman_server.console.commands import TurnRequest, dispatch
from corlinman_server.console.render import Renderer
from corlinman_server.console.router import ModelRouter
from rich.console import Console


class _IdleBrain:
    descriptor = "stub brain"

    def run_turn(self, **_kw: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("dispatch tests must not run turns")

    async def aclose(self) -> None:  # pragma: no cover - unused
        pass


class StubApp:
    def __init__(self) -> None:
        self.session = BrainSession(brain=_IdleBrain(), model="big")
        self.renderer = Renderer(Console(file=io.StringIO(), force_terminal=False))
        self.router = ModelRouter(default_model="big")
        self.running = True

    def known_models(self) -> list[str]:
        return []


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    return tmp_path


async def test_local_commands_still_win() -> None:
    """/model is console-local (richer) — must NOT fall through to the
    channel handler."""
    app = StubApp()
    text = await dispatch(app, "/model")
    assert isinstance(text, str)
    assert "usage: /model" in text  # console wording, not the channel reply


async def test_shared_handler_command_works() -> None:
    """/whoami exists only in the channels registry — the console reaches
    it through the fallback with a synthetic console binding."""
    app = StubApp()
    text = await dispatch(app, "/whoami")
    assert isinstance(text, str)
    assert "channel: console" in text


async def test_shared_prelude_command_returns_turn_request() -> None:
    """Wizard commands (/persona) come back as a TurnRequest the REPL
    sends through the brain."""
    app = StubApp()
    result = await dispatch(app, "/persona")
    assert isinstance(result, TurnRequest)
    assert result.content  # the wizard prelude, not the literal "/persona"
    assert result.content != "/persona"


async def test_unknown_still_hints() -> None:
    app = StubApp()
    text = await dispatch(app, "/definitely-not-a-command")
    assert isinstance(text, str)
    assert "unknown command" in text
