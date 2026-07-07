"""C1 (#108 item 2) — process-wide subagent event tail on the journal.

``load_subagent_events_since`` is the storage half of the gateway's
global journal tail: a rowid-cursored read of ONLY the subagent
lifecycle event types, so the LiveSubagentRegistry can be fed without an
open per-session SSE stream. ``latest_event_rowid`` seeds the cursor at
boot (tail-only — history is not replayed into the registry).
"""

from __future__ import annotations

from pathlib import Path

from corlinman_server.agent_journal import AgentJournal


def _env(turn_id: int, seq: int, event_type: str, payload: dict) -> dict:
    return {
        "turn_id": turn_id,
        "sequence": seq,
        "event_type": event_type,
        "payload": payload,
        "timestamp_ms": 1_700_000_000_000 + seq,
    }


async def test_latest_event_rowid_zero_on_fresh_db(tmp_path: Path) -> None:
    journal = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
    try:
        assert await journal.latest_event_rowid() == 0
    finally:
        await journal.close()


async def test_tail_returns_only_subagent_events(tmp_path: Path) -> None:
    journal = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
    try:
        t = await journal.begin_turn("corlinman:tail", "q")
        base = await journal.latest_event_rowid()
        await journal.append_event(_env(t, 0, "TurnStart", {"model": "m"}))
        await journal.append_event(_env(t, 1, "TextDelta", {"text": "hi"}))
        await journal.append_event(
            _env(t, 2, "SubagentSpawned", {"child_session_key": "c1"})
        )
        await journal.append_event(
            _env(t, 3, "SubagentEvent", {"child_session_key": "c1"})
        )
        await journal.append_event(
            _env(t, 4, "SubagentCompleted", {"child_session_key": "c1"})
        )

        cursor, rows = await journal.load_subagent_events_since(base)
        assert [r["event_type"] for r in rows] == [
            "SubagentSpawned",
            "SubagentEvent",
            "SubagentCompleted",
        ]
        # Rows carry the observe_journal_event contract: parsed payload
        # dict + timestamp_ms, same shape as ``load_events``.
        assert rows[0]["payload"] == {"child_session_key": "c1"}
        assert isinstance(rows[0]["timestamp_ms"], int)
        # Cursor advanced past ALL rows (including the non-subagent ones)
        # so the next poll never rescans them.
        assert cursor > base
        cursor2, rows2 = await journal.load_subagent_events_since(cursor)
        assert rows2 == []
        assert cursor2 == cursor
    finally:
        await journal.close()


async def test_tail_cursor_resumes_mid_stream(tmp_path: Path) -> None:
    journal = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
    try:
        t = await journal.begin_turn("corlinman:tail2", "q")
        base = await journal.latest_event_rowid()
        await journal.append_event(
            _env(t, 0, "SubagentSpawned", {"child_session_key": "a"})
        )
        cursor, rows = await journal.load_subagent_events_since(base)
        assert len(rows) == 1
        await journal.append_event(
            _env(t, 1, "SubagentCompleted", {"child_session_key": "a"})
        )
        cursor2, rows2 = await journal.load_subagent_events_since(cursor)
        assert [r["event_type"] for r in rows2] == ["SubagentCompleted"]
        assert cursor2 > cursor
    finally:
        await journal.close()


async def test_tail_respects_limit(tmp_path: Path) -> None:
    journal = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
    try:
        t = await journal.begin_turn("corlinman:tail3", "q")
        base = await journal.latest_event_rowid()
        for i in range(5):
            await journal.append_event(
                _env(t, i, "SubagentSpawned", {"child_session_key": f"c{i}"})
            )
        cursor, rows = await journal.load_subagent_events_since(base, limit=2)
        assert len(rows) == 2
        # The bounded page still advances the cursor only to what it
        # returned, so the remainder arrives on the next call.
        cursor2, rows2 = await journal.load_subagent_events_since(cursor, limit=10)
        assert len(rows2) == 3
        assert cursor2 > cursor
    finally:
        await journal.close()
