"""Session ergonomics (ABSORB_MATRIX Dim 11): --continue + fuzzy /resume.

Drives ``ConsoleApp.latest_session_key`` / ``match_session_keys`` against a
stubbed journal (recency-ordered ``list_session_summaries``, matching the real
backend's MAX(started_at_ms) DESC contract).
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from corlinman_server.console.app import ConsoleApp
from corlinman_server.console.brain import BrainSession
from corlinman_server.console.render import Renderer
from corlinman_server.console.router import ModelRouter
from rich.console import Console


class _IdleBrain:
    descriptor = "stub"

    async def aclose(self) -> None:  # pragma: no cover
        pass


class _FakeJournal:
    def __init__(self, keys: list[str]) -> None:
        self._keys = keys
        self.closed = False

    async def list_session_summaries(self, limit: int = 20) -> list[Any]:
        return [SimpleNamespace(session_key=k) for k in self._keys[:limit]]

    async def close(self) -> None:
        self.closed = True


def _app(journal: _FakeJournal | None, *, embedded: bool = True) -> ConsoleApp:
    app = ConsoleApp(
        session=BrainSession(brain=_IdleBrain(), model="m"),
        renderer=Renderer(Console(file=io.StringIO(), force_terminal=False)),
        router=ModelRouter(default_model="m", small_fast_model=None, auto_route=False),
        data_dir=Path("/nonexistent"),
        embedded=embedded,
    )

    async def _open() -> Any:
        return journal

    app._open_journal = _open  # type: ignore[method-assign]
    return app


async def test_latest_session_key_returns_newest() -> None:
    journal = _FakeJournal(["console:new", "console:old"])  # newest-first order
    app = _app(journal)
    assert await app.latest_session_key() == "console:new"
    assert journal.closed is True  # journal handle released


async def test_latest_session_key_none_when_empty_or_attach() -> None:
    assert await _app(_FakeJournal([])).latest_session_key() is None
    assert await _app(None).latest_session_key() is None  # no journal
    assert await _app(_FakeJournal(["k"]), embedded=False).latest_session_key() is None


async def test_match_session_keys_exact_beats_substring() -> None:
    app = _app(_FakeJournal(["console:abc", "console:abcd"]))
    assert await app.match_session_keys("console:abc") == ["console:abc"]


async def test_match_session_keys_substring_recency_ordered() -> None:
    app = _app(_FakeJournal(["console:b-new", "console:b-old", "tg:1"]))
    assert await app.match_session_keys("b-") == ["console:b-new", "console:b-old"]
    assert await app.match_session_keys("nope") == []


class _TurnJournal(_FakeJournal):
    """Fake journal with turns for the turn-keyed rewind rebuild."""

    def __init__(self) -> None:
        super().__init__([])
        self.cursor_seen: list[str] = []

    async def list_session_turns(
        self, session_key: str, *, limit: int = 50, before_turn_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.cursor_seen.append(str(before_turn_id))
        # Deliberately newest-first (the backend contract) to prove the
        # rebuild re-sorts oldest-first.
        return [
            {"turn_id": 20, "started_at_ms": 2000},
            {"turn_id": 10, "started_at_ms": 1000},
        ]

    async def _load_messages(self, turn_id: Any) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": f"u{turn_id}"},
            {"role": "assistant", "content": f"a{turn_id}"},
            {"role": "tool", "content": "ignored"},
        ]


async def test_replay_window_before_rebuilds_oldest_first() -> None:
    journal = _TurnJournal()
    app = _app(journal)
    app.session.window.extend([{"role": "user", "content": "stale"}])

    replayed = await app.replay_window_before(30)

    assert journal.cursor_seen == ["30"]  # strictly-before cursor forwarded
    assert replayed == 4  # 2 turns × (user + assistant); tool msg filtered
    assert [m["content"] for m in app.session.window] == ["u10", "a10", "u20", "a20"]
    assert journal.closed is True


async def test_replay_window_before_none_paths() -> None:
    assert await _app(None).replay_window_before(5) is None  # no journal
    assert await _app(_TurnJournal(), embedded=False).replay_window_before(5) is None
