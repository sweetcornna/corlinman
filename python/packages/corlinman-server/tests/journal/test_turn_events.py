"""Tests for W1.2 — turn_events timeline in the agent journal.

Covers:

- round-trip append / load with sequence ordering
- batch insert path
- resume / catch-up via ``iter_events(start_sequence=)``
- empty-turn load semantics
- migration idempotency (re-open does not error)
- W1.2 aggregate column population by :meth:`AgentJournal.complete_turn`
- ``get_session_turn_ids`` ordering + limit
- ``update_turn_cost`` late-binding update

Async tests use the project-wide ``pytest-asyncio`` ``auto`` mode (set in
the root ``pyproject.toml``), so ``async def test_*`` works directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from corlinman_server.agent_journal import AgentJournal


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def journal(tmp_path: Path) -> AgentJournal:
    j = await AgentJournal.open(tmp_path / "journal.sqlite")
    yield j
    await j.close()


@dataclass
class _Envelope:
    """Minimal stand-in for the W1.1 EventEnvelope dataclass.

    Mirrors the field shape the storage projector cares about: turn_id,
    session_key, sequence, event_type, payload, timestamp_ms. Lives here
    (not in conftest) so this test file is self-contained — the real
    EventEnvelope ships in corlinman-agent under a parallel wave.
    """

    turn_id: int
    sequence: int
    event_type: str
    payload: dict[str, Any]
    timestamp_ms: int
    session_key: str = "sess-test"


def _envelope(
    turn_id: int,
    sequence: int,
    event_type: str = "TextDelta",
    *,
    text: str | None = None,
    timestamp_ms: int | None = None,
) -> _Envelope:
    payload: dict[str, Any] = {"index": 0}
    if text is not None:
        payload["text"] = text
    return _Envelope(
        turn_id=turn_id,
        sequence=sequence,
        event_type=event_type,
        payload=payload,
        timestamp_ms=timestamp_ms if timestamp_ms is not None else 1_700_000_000_000 + sequence,
    )


# ---------------------------------------------------------------------------
# Append + load round trip
# ---------------------------------------------------------------------------


async def test_append_and_load_round_trip(journal: AgentJournal) -> None:
    """1000 events: every one comes back in sequence order with payload intact."""
    tid = await journal.begin_turn("sess-1", "stream a lot")
    assert tid is not None

    for seq in range(1000):
        await journal.append_event(_envelope(tid, seq, text=f"tok-{seq}"))

    events = await journal.load_events(tid)
    assert len(events) == 1000
    # ORDER BY sequence ASC — every neighbour pair is strictly increasing.
    assert all(events[i]["sequence"] < events[i + 1]["sequence"] for i in range(999))
    # Payload round-trips as parsed JSON.
    assert events[0]["payload"]["text"] == "tok-0"
    assert events[999]["payload"]["text"] == "tok-999"
    # Discriminator preserved verbatim.
    assert events[42]["event_type"] == "TextDelta"


async def test_append_events_batch(journal: AgentJournal) -> None:
    """100 events in one batch land identically to 100 single appends."""
    tid = await journal.begin_turn("sess-batch", "batch path")
    assert tid is not None

    batch = [_envelope(tid, seq, text=f"b-{seq}") for seq in range(100)]
    await journal.append_events_batch(batch)

    events = await journal.load_events(tid)
    assert len(events) == 100
    assert [e["sequence"] for e in events] == list(range(100))
    assert events[50]["payload"]["text"] == "b-50"


async def test_append_events_batch_empty_is_noop(journal: AgentJournal) -> None:
    """Empty input does not write — and does not raise."""
    tid = await journal.begin_turn("sess-empty-batch", "no events")
    assert tid is not None
    await journal.append_events_batch([])
    assert await journal.load_events(tid) == []


async def test_append_event_idempotent_on_duplicate_pk(
    journal: AgentJournal,
) -> None:
    """Re-inserting the same (turn_id, sequence) lands once (INSERT OR IGNORE)."""
    tid = await journal.begin_turn("sess-dup", "dup test")
    assert tid is not None
    await journal.append_event(_envelope(tid, 0, text="first"))
    await journal.append_event(_envelope(tid, 0, text="second"))
    events = await journal.load_events(tid)
    assert len(events) == 1
    # First write wins — INSERT OR IGNORE keeps the original.
    assert events[0]["payload"]["text"] == "first"


async def test_append_event_accepts_dict_payload(journal: AgentJournal) -> None:
    """The dict-shaped envelope path works for SSE replay round-trips."""
    tid = await journal.begin_turn("sess-dict", "dict shape")
    assert tid is not None
    await journal.append_event(
        {
            "turn_id": tid,
            "sequence": 7,
            "event_type": "ToolStateRunning",
            "payload": {"tool_call_id": "c1", "tool_name": "bash"},
            "timestamp_ms": 1_700_000_001_234,
        }
    )
    events = await journal.load_events(tid)
    assert len(events) == 1
    assert events[0]["event_type"] == "ToolStateRunning"
    assert events[0]["payload"]["tool_name"] == "bash"


# ---------------------------------------------------------------------------
# iter_events resume / catch-up
# ---------------------------------------------------------------------------


async def test_iter_events_resume(journal: AgentJournal) -> None:
    """Catch-up from start_sequence=500 yields exactly the back half."""
    tid = await journal.begin_turn("sess-iter", "resume mid-turn")
    assert tid is not None
    await journal.append_events_batch(
        [_envelope(tid, seq, text=f"t-{seq}") for seq in range(1000)]
    )

    seen: list[int] = []
    async for e in journal.iter_events(tid, start_sequence=500):
        seen.append(e["sequence"])
    # > start_sequence (strict) — so sequence 500 itself is excluded.
    assert seen == list(range(501, 1000))


async def test_iter_events_default_skips_only_negative_sentinel(
    journal: AgentJournal,
) -> None:
    """Catch-up with start_sequence=-1 yields every event including seq=0.

    ``iter_events`` semantics: ``sequence > start_sequence`` (strict).
    Reconnecting clients pass their last-seen sequence; brand-new
    consumers pass ``-1`` to opt into the entire timeline.
    """
    tid = await journal.begin_turn("sess-iter-all", "stream all")
    assert tid is not None
    await journal.append_events_batch(
        [_envelope(tid, seq) for seq in range(50)]
    )
    count = 0
    async for _ in journal.iter_events(tid, start_sequence=-1):
        count += 1
    assert count == 50
    # Default (start_sequence=0) skips just the first event.
    count = 0
    async for _ in journal.iter_events(tid):
        count += 1
    assert count == 49


# ---------------------------------------------------------------------------
# Empty / unknown turn
# ---------------------------------------------------------------------------


async def test_load_events_empty_turn(journal: AgentJournal) -> None:
    """A turn with no events returns ``[]`` (not None, not error)."""
    tid = await journal.begin_turn("sess-empty", "no events")
    assert tid is not None
    assert await journal.load_events(tid) == []


async def test_load_events_unknown_turn_returns_empty(
    journal: AgentJournal,
) -> None:
    """Loading events for a non-existent turn_id is best-effort empty."""
    assert await journal.load_events(99999999) == []


# ---------------------------------------------------------------------------
# Migration idempotency
# ---------------------------------------------------------------------------


async def test_migration_idempotent(tmp_path: Path) -> None:
    """Running ``open`` twice on the same file is a no-op the second time."""
    db = tmp_path / "journal.sqlite"
    j1 = await AgentJournal.open(db)
    tid = await j1.begin_turn("sess-mig", "first open")
    assert tid is not None
    await j1.append_event(_envelope(tid, 0, text="hello"))
    await j1.close()

    # Re-open — schema migration code path runs again; must not raise.
    j2 = await AgentJournal.open(db)
    events = await j2.load_events(tid)
    assert len(events) == 1
    # New writes on the re-opened DB succeed.
    await j2.append_event(_envelope(tid, 1, text="world"))
    events = await j2.load_events(tid)
    assert [e["payload"]["text"] for e in events] == ["hello", "world"]
    await j2.close()


# ---------------------------------------------------------------------------
# complete_turn populates W1.2 aggregate columns
# ---------------------------------------------------------------------------


async def test_turn_finalize_populates_columns(
    journal: AgentJournal, tmp_path: Path
) -> None:
    """After ``complete_turn`` the aggregate columns are non-null + correct."""
    tid = await journal.begin_turn("sess-final", "do work")
    assert tid is not None

    # 2 tool calls + 1 reasoning event with ~5 tokens.
    await journal.append_message(tid, "user", "do work")
    await journal.append_message(tid, "assistant", "thinking…")
    await journal.append_message(tid, "tool", "{}", tool_call_id="t1")
    await journal.append_message(tid, "tool", "{}", tool_call_id="t2")
    await journal.append_event(
        _envelope(tid, 0, event_type="ReasoningDelta", text="one two three four five")
    )

    await journal.complete_turn(tid)

    # Read aggregates back via the raw sqlite path — the facade doesn't
    # expose them yet (the admin endpoint will, in W1.3).
    db_path = journal._path  # backend-specific shim, fine for tests
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT elapsed_ms, tool_call_count, reasoning_token_count, "
            "estimated_cost_usd, cost_status FROM turns WHERE turn_id = ?",
            (tid,),
        )
        row = await cur.fetchone()
        await cur.close()
    assert row is not None
    elapsed_ms, tool_count, reasoning_tokens, cost_usd, cost_status = row
    assert elapsed_ms is not None and elapsed_ms >= 0
    assert tool_count == 2
    assert reasoning_tokens == 5
    # cost columns are NULL until the gateway flushes the _CostMeter.
    assert cost_usd is None
    assert cost_status is None


async def test_update_turn_cost_writes_columns(journal: AgentJournal) -> None:
    """``update_turn_cost`` lets the gateway flush late-arriving cost data."""
    tid = await journal.begin_turn("sess-cost", "needs cost")
    assert tid is not None
    await journal.complete_turn(tid)
    await journal.update_turn_cost(
        tid, estimated_cost_usd=0.0123, cost_status="estimated"
    )

    async with aiosqlite.connect(journal._path) as conn:
        cur = await conn.execute(
            "SELECT estimated_cost_usd, cost_status FROM turns WHERE turn_id = ?",
            (tid,),
        )
        row = await cur.fetchone()
        await cur.close()
    assert row is not None
    cost_usd, cost_status = row
    assert cost_usd == pytest.approx(0.0123)
    assert cost_status == "estimated"


async def test_update_turn_cost_partial(journal: AgentJournal) -> None:
    """Passing ``None`` for a field leaves that column untouched."""
    tid = await journal.begin_turn("sess-cost-partial", "partial")
    assert tid is not None
    await journal.complete_turn(tid)
    await journal.update_turn_cost(
        tid, estimated_cost_usd=0.05, cost_status="confident"
    )
    # Overwrite only the status; cost stays put.
    await journal.update_turn_cost(
        tid, estimated_cost_usd=None, cost_status="unknown"
    )

    async with aiosqlite.connect(journal._path) as conn:
        cur = await conn.execute(
            "SELECT estimated_cost_usd, cost_status FROM turns WHERE turn_id = ?",
            (tid,),
        )
        row = await cur.fetchone()
        await cur.close()
    assert row is not None
    assert row[0] == pytest.approx(0.05)
    assert row[1] == "unknown"


# ---------------------------------------------------------------------------
# Session-level helpers
# ---------------------------------------------------------------------------


async def test_get_session_turn_ids_orders_recent_first(
    journal: AgentJournal,
) -> None:
    """Most-recent turn comes first, capped by ``limit``."""
    import asyncio

    sess = "sess-listing"
    tids: list[int] = []
    for i in range(3):
        tid = await journal.begin_turn(sess, f"task-{i}")
        assert tid is not None
        tids.append(tid)
        await asyncio.sleep(0.005)  # ensure distinct started_at_ms

    listed = await journal.get_session_turn_ids(sess, limit=5)
    # DESC order — youngest first.
    assert listed[0] == tids[-1]
    assert listed[-1] == tids[0]
    assert len(listed) == 3


async def test_get_session_turn_ids_respects_limit(journal: AgentJournal) -> None:
    sess = "sess-limit"
    for i in range(5):
        await journal.begin_turn(sess, f"t-{i}")
    listed = await journal.get_session_turn_ids(sess, limit=2)
    assert len(listed) == 2


async def test_get_session_turn_ids_empty_session(journal: AgentJournal) -> None:
    assert await journal.get_session_turn_ids("does-not-exist") == []
    assert await journal.get_session_turn_ids("", limit=10) == []


# ---------------------------------------------------------------------------
# Cross-cutting: events isolated per turn
# ---------------------------------------------------------------------------


async def test_events_isolated_per_turn(journal: AgentJournal) -> None:
    """Events for turn A don't leak into turn B's load_events."""
    t1 = await journal.begin_turn("sess-iso", "turn one")
    t2 = await journal.begin_turn("sess-iso", "turn two")
    assert t1 is not None and t2 is not None
    await journal.append_event(_envelope(t1, 0, text="A"))
    await journal.append_event(_envelope(t2, 0, text="B"))
    a = await journal.load_events(t1)
    b = await journal.load_events(t2)
    assert len(a) == 1 and a[0]["payload"]["text"] == "A"
    assert len(b) == 1 and b[0]["payload"]["text"] == "B"


async def test_payload_json_handles_unserializable_fallback(
    journal: AgentJournal,
) -> None:
    """A dataclass-shaped payload (no native JSON) round-trips via __dict__."""

    @dataclass
    class FakeEvent:
        text: str
        index: int

    tid = await journal.begin_turn("sess-ds", "dataclass payload")
    assert tid is not None
    await journal.append_event(
        _Envelope(
            turn_id=tid,
            sequence=0,
            event_type="TextDelta",
            payload={"event": FakeEvent(text="hi", index=2)},
            timestamp_ms=1,
        )
    )
    events = await journal.load_events(tid)
    assert len(events) == 1
    # FakeEvent serialised through the dataclass default; ``payload``
    # round-trips to a dict whose ``event`` key holds the field values.
    inner = events[0]["payload"]["event"]
    assert inner == {"text": "hi", "index": 2}
    # Ensure the JSON is well-formed (sanity belt-and-braces).
    _ = json.dumps(events[0]["payload"])
