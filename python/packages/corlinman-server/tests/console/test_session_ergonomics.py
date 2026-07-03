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

    async def get_session_turn_ids(self, session_key: str, limit: int = 50) -> list[int]:
        # The rebuild target (30) is one of this session's turns — the
        # ownership probe must pass for the happy-path test.
        return [30, 20, 10]

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


class _RichJournal(_FakeJournal):
    """Fake journal with recency metadata + turn rows for the Codex-fix tests."""

    def __init__(
        self,
        rows: list[Any] | None = None,
        *,
        turn_ids: list[int] | None = None,
        turn_pages: list[list[dict[str, Any]]] | None = None,
        fail_on_turn: Any = None,
    ) -> None:
        super().__init__([])
        self.rows = rows or []
        self.turn_ids = turn_ids or []
        self.turn_pages = list(turn_pages or [])
        self.fail_on_turn = fail_on_turn
        self.cursor_seen: list[str] = []
        self.limits_seen: list[int] = []

    async def list_session_summaries(self, limit: int = 20) -> list[Any]:
        self.limits_seen.append(limit)
        return self.rows[:limit]

    async def get_session_turn_ids(self, session_key: str, limit: int = 50) -> list[int]:
        return self.turn_ids[:limit]

    async def list_session_turns(
        self, session_key: str, *, limit: int = 50, before_turn_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.cursor_seen.append(str(before_turn_id))
        return self.turn_pages.pop(0) if self.turn_pages else []

    async def _load_messages(self, turn_id: Any) -> list[dict[str, Any]]:
        if self.fail_on_turn is not None and turn_id == self.fail_on_turn:
            raise RuntimeError("simulated journal read failure")
        return [
            {"role": "user", "content": f"u{turn_id}"},
            {"role": "assistant", "content": f"a{turn_id}"},
        ]


def _summary(key: str, last_seen: int, *, pinned: bool = False) -> Any:
    return SimpleNamespace(session_key=key, last_seen_at_ms=last_seen, pinned=pinned)


async def test_latest_session_key_ignores_pinned_ordering() -> None:
    """--continue must resume TRUE recency, not the pinned-first sort
    (Codex PR#105): the backend lists pinned sessions ahead of newer
    unpinned ones, so row 0 is wrong whenever any pin exists."""
    journal = _RichJournal(
        rows=[
            _summary("console:pinned-old", 100, pinned=True),
            _summary("console:fresh", 200),
        ]
    )
    app = _app(journal)
    assert await app.latest_session_key() == "console:fresh"


async def test_match_session_keys_pages_beyond_first_page() -> None:
    """A fragment match hiding beyond the first 50 rows must still count
    toward uniqueness (Codex PR#105) — otherwise /resume 'proves'
    uniqueness on one page and resumes the wrong session."""
    keys = [f"console:filler-{i:03d}" for i in range(49)]
    keys.insert(3, "console:proj-a")
    keys.append("console:proj-b")  # row 51 — outside the first page
    journal = _RichJournal(rows=[_summary(k, 1000 - i) for i, k in enumerate(keys)])
    app = _app(journal)
    matches = await app.match_session_keys("proj-")
    assert matches == ["console:proj-a", "console:proj-b"]  # ambiguous, both seen


async def test_replay_window_before_keeps_window_on_failure() -> None:
    """A journal read failure mid-replay must leave the window UNTOUCHED
    (Codex PR#105) — the old code cleared it first, then reported
    'window unchanged'."""
    journal = _RichJournal(
        turn_ids=[20, 10],
        turn_pages=[
            [
                {"turn_id": 20, "started_at_ms": 2000},
                {"turn_id": 10, "started_at_ms": 1000},
            ]
        ],
        fail_on_turn=20,
    )
    app = _app(journal)
    app.session.window.extend([{"role": "user", "content": "precious"}])
    assert await app.replay_window_before(30) is None
    assert [m["content"] for m in app.session.window] == ["precious"]


async def test_replay_window_before_rejects_foreign_turn_id() -> None:
    """A checkpoint turn id that does not belong to THIS session must
    degrade (None), not truncate the window to an unrelated cutoff
    (Codex PR#105 — checkpoints are global across sessions)."""
    journal = _RichJournal(
        turn_ids=[10, 20],
        turn_pages=[[{"turn_id": 10, "started_at_ms": 1000}]],
    )
    app = _app(journal)
    app.session.window.extend([{"role": "user", "content": "precious"}])
    assert await app.replay_window_before(999) is None
    assert [m["content"] for m in app.session.window] == ["precious"]


async def test_replay_window_before_stub_backend_degrades() -> None:
    """A backend whose list_session_turns is a stub (returns []) while the
    session demonstrably has turns must degrade (None), not clear the
    window and report success (Codex PR#105 — Postgres stub)."""
    journal = _RichJournal(turn_ids=[10], turn_pages=[[]])
    app = _app(journal)
    app.session.window.extend([{"role": "user", "content": "precious"}])
    assert await app.replay_window_before(10) is None
    assert [m["content"] for m in app.session.window] == ["precious"]


async def test_replay_window_before_pages_past_first_page() -> None:
    """>50 prior turns must ALL replay (Codex PR#105) — the rebuild pages
    with the before_turn_id cursor until exhausted."""
    page1 = [
        {"turn_id": 100 - i, "started_at_ms": (100 - i) * 10} for i in range(50)
    ]  # newest-first 100..51
    page2 = [{"turn_id": 50, "started_at_ms": 500}, {"turn_id": 49, "started_at_ms": 490}]
    journal = _RichJournal(
        turn_ids=[101],
        turn_pages=[page1, page2],
    )
    app = _app(journal)
    replayed = await app.replay_window_before(101)
    assert replayed == 52 * 2
    # Cursor walked: original target, then the oldest row of the full
    # page; the short second page ends the walk.
    assert journal.cursor_seen == ["101", "51"]
    assert app.session.window[0]["content"] == "u49"
    assert app.session.window[-1]["content"] == "a100"


async def test_replay_window_before_numeric_tiebreak() -> None:
    """Same-millisecond turns tie-break NUMERICALLY (Codex PR#105) — a
    string sort would order turn 10 before turn 9."""
    journal = _RichJournal(
        turn_ids=[11],
        turn_pages=[
            [
                {"turn_id": 10, "started_at_ms": 1000},
                {"turn_id": 9, "started_at_ms": 1000},
            ]
        ],
    )
    app = _app(journal)
    assert await app.replay_window_before(11) == 4
    assert [m["content"] for m in app.session.window] == ["u9", "a9", "u10", "a10"]
