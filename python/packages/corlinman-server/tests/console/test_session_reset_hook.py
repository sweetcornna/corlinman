"""Dim 9 — ``session_reset`` fires on the console session boundary
(/new, /clear); attach mode / no runner degrades silently."""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_server.console.brain import BrainSession
from corlinman_server.console.commands import dispatch
from corlinman_server.console.render import Renderer
from corlinman_server.console.router import ModelRouter
from rich.console import Console

pytestmark = pytest.mark.asyncio


class _RecordingRunner:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def run_event_async(self, event, payload=None, ctx=None):
        self.events.append((event, dict(ctx or {})))
        return SimpleNamespace(allow=True)


class _HookBrain:
    descriptor = "stub brain with hooks"

    def __init__(self, runner: Any) -> None:
        self._runner = runner

    def get_hook_runner(self) -> Any:
        return self._runner

    def run_turn(self, **_kw: Any) -> Any:  # pragma: no cover — unused
        raise AssertionError("commands must not run turns")

    async def aclose(self) -> None:  # pragma: no cover — unused
        pass


class _BareBrain:
    descriptor = "stub brain without hooks"

    def run_turn(self, **_kw: Any) -> Any:  # pragma: no cover — unused
        raise AssertionError("commands must not run turns")

    async def aclose(self) -> None:  # pragma: no cover — unused
        pass


class StubApp:
    def __init__(self, brain: Any) -> None:
        self.session = BrainSession(brain=brain, model="big")
        self.renderer = Renderer(
            Console(file=io.StringIO(), force_terminal=False)
        )
        self.router = ModelRouter(
            default_model="big", small_fast_model="small", auto_route=False
        )
        self.running = True


async def test_new_and_clear_fire_session_reset() -> None:
    runner = _RecordingRunner()
    app = StubApp(_HookBrain(runner))

    await dispatch(app, "/new")
    await dispatch(app, "/clear")

    events = [e for e, _ in runner.events]
    assert events == ["session_reset", "session_reset"]
    # ctx carries the fresh session key.
    assert all(ctx.get("session_key") for _, ctx in runner.events)


async def test_session_reset_degrades_without_runner() -> None:
    app = StubApp(_BareBrain())
    out = await dispatch(app, "/new")
    assert "new session" in str(out)
