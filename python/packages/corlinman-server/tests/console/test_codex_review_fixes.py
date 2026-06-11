"""Regression tests for the PR #88 Codex review findings."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from corlinman_server.console.app import ConsoleApp
from corlinman_server.console.brain import BrainSession
from corlinman_server.console.commands import dispatch
from corlinman_server.console.events import ConsoleEvent, TextDelta, TurnDone
from corlinman_server.console.render import Renderer
from corlinman_server.console.router import ModelRouter
from rich.console import Console


class RecordingBrain:
    """Yields one canned turn, recording the model each call used."""

    descriptor = "recording"

    def __init__(self) -> None:
        self.models_used: list[str] = []

    def run_turn(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        session_key: str,
        cancel: asyncio.Event,
    ) -> AsyncIterator[ConsoleEvent]:
        self.models_used.append(model)

        async def _gen() -> AsyncIterator[ConsoleEvent]:
            yield TextDelta(text="ok")
            yield TurnDone()

        return _gen()

    async def aclose(self) -> None:  # pragma: no cover - protocol filler
        pass


def _make_app(tmp_path: Path, *, auto_route: bool) -> tuple[ConsoleApp, RecordingBrain]:
    brain = RecordingBrain()
    session = BrainSession(brain=brain, model="big-model")
    router = ModelRouter(
        default_model="big-model",
        small_fast_model="small-model",
        auto_route=auto_route,
    )
    renderer = Renderer(Console(file=io.StringIO(), force_terminal=False))
    app = ConsoleApp(
        session=session,
        renderer=renderer,
        router=router,
        data_dir=tmp_path,
        embedded=True,
    )
    return app, brain


async def test_auto_route_downgrades_simple_turns_by_default(tmp_path: Path) -> None:
    app, brain = _make_app(tmp_path, auto_route=True)
    await app.run_turn("hi there?")
    assert brain.models_used == ["small-model"]


async def test_explicit_model_disables_auto_downgrade(tmp_path: Path) -> None:
    """--model / /model must win over auto-routing (Codex P2)."""
    app, brain = _make_app(tmp_path, auto_route=True)
    app.model_explicit = True
    await app.run_turn("hi there?")
    assert brain.models_used == ["big-model"]


async def test_model_command_marks_choice_explicit(tmp_path: Path) -> None:
    app, brain = _make_app(tmp_path, auto_route=True)
    await dispatch(app, "/model hand-picked")
    assert app.model_explicit is True
    await app.run_turn("hi there?")
    assert brain.models_used == ["hand-picked"]


async def test_open_journal_absent_sqlite_returns_none(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """SQLite backend with no journal file → None (no phantom sessions);
    the Postgres-configured path must NOT be gated on the local file."""
    monkeypatch.delenv("CORLINMAN_JOURNAL_BACKEND", raising=False)
    app, _ = _make_app(tmp_path, auto_route=False)
    assert await app._open_journal() is None  # noqa: SLF001
