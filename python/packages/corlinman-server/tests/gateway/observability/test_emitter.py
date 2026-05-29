"""Tests for :class:`JournalBackedEmitter` (W1.3 fan-out tee).

Exercises the five guarantees the gateway relies on:

1. Every emit writes to the journal (``append_event``).
2. Every emit fans out to in-process subscribers keyed by ``session_key``.
3. A subscriber whose queue is full does not break the emitter — the
   warning is logged and the event is dropped for that subscriber.
4. Unsubscribing removes the queue from the fan-out set immediately.
5. A journal write failure does not prevent the live fan-out — the
   subscriber still gets the envelope.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from corlinman_agent.events import (
    EventEnvelope,
    ReasoningDelta,
    TextDelta,
    ToolStateCompleted,
    ToolStateRunning,
    TurnComplete,
    TurnErrored,
)
from corlinman_server.gateway.observability import JournalBackedEmitter
from corlinman_server.gateway.observability.emitter import (
    DEFAULT_QUEUE_MAXSIZE,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingJournal:
    """In-memory ``AgentJournal`` stand-in. Captures every envelope handed
    to :meth:`append_event` and lets a test toggle a failure mode.

    Has no ``append_events_batch`` — exercises the single-shot fallback
    path the emitter takes for a duck-typed journal that predates the
    batch API.
    """

    def __init__(self) -> None:
        self.appended: list[Any] = []
        self.should_fail: bool = False
        # One commit per single-shot append_event call.
        self.commit_count: int = 0

    async def append_event(self, envelope: Any) -> None:
        if self.should_fail:
            raise RuntimeError("simulated journal write failure")
        self.appended.append(envelope)
        self.commit_count += 1


class _CountingBatchJournal:
    """Journal stub exposing BOTH ``append_event`` and
    ``append_events_batch`` so a test can prove how many *commits*
    (durable transactions) the emitter triggers.

    ``commit_count`` increments once per ``append_event`` (one row / one
    commit) and once per ``append_events_batch`` (N rows / one commit) —
    matching the real :class:`SqliteJournalBackend`, where every
    ``append_event`` issues its own ``commit()`` and every
    ``append_events_batch`` folds N inserts into one ``BEGIN
    IMMEDIATE``/``COMMIT``.
    """

    def __init__(self) -> None:
        self.appended: list[Any] = []
        self.commit_count: int = 0
        self.batch_calls: int = 0
        self.single_calls: int = 0

    async def append_event(self, envelope: Any) -> None:
        self.appended.append(envelope)
        self.single_calls += 1
        self.commit_count += 1

    async def append_events_batch(self, envelopes: Any) -> None:
        batch = list(envelopes)
        if not batch:
            return
        self.appended.extend(batch)
        self.batch_calls += 1
        self.commit_count += 1


def _envelope(
    *,
    session_key: str = "sess-1",
    turn_id: str = "turn-1",
    sequence: int = 0,
    text: str = "hi",
) -> EventEnvelope:
    return EventEnvelope(
        turn_id=turn_id,
        session_key=session_key,
        sequence=sequence,
        timestamp_ms=1_700_000_000_000 + sequence,
        event=TextDelta(index=0, text=text),
    )


# ---------------------------------------------------------------------------
# (1) Journal write
# ---------------------------------------------------------------------------


async def test_emit_writes_to_journal() -> None:
    """Every successful emit lands one row in the journal."""
    journal = _RecordingJournal()
    emitter = JournalBackedEmitter(journal)

    await emitter.emit(_envelope(sequence=0))
    await emitter.emit(_envelope(sequence=1, text="there"))

    assert len(journal.appended) == 2
    assert journal.appended[0].sequence == 0
    assert journal.appended[1].sequence == 1
    assert journal.appended[1].event.text == "there"


# ---------------------------------------------------------------------------
# (2) Live fan-out
# ---------------------------------------------------------------------------


async def test_emit_fans_out_to_subscriber() -> None:
    """A subscriber for the matching session_key receives the envelope."""
    journal = _RecordingJournal()
    emitter = JournalBackedEmitter(journal)

    queue, unsubscribe = await emitter.subscribe("sess-1")
    try:
        env = _envelope(session_key="sess-1", sequence=42)
        await emitter.emit(env)
        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received is env
    finally:
        await unsubscribe()


async def test_emit_skips_unrelated_session() -> None:
    """A subscriber on session A does not receive session B's events."""
    journal = _RecordingJournal()
    emitter = JournalBackedEmitter(journal)

    queue, unsubscribe = await emitter.subscribe("sess-A")
    try:
        await emitter.emit(_envelope(session_key="sess-B", sequence=0))
        # No delivery to the unrelated subscriber.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)
    finally:
        await unsubscribe()


# ---------------------------------------------------------------------------
# (3) Backpressure / overflow
# ---------------------------------------------------------------------------


async def test_subscriber_overflow_logs_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full subscriber queue is logged + dropped; the next subscriber
    still gets the envelope."""
    journal = _RecordingJournal()
    emitter = JournalBackedEmitter(journal)

    # Open one subscriber with a 1-slot queue to make overflow easy.
    _full_q, unsub_full = await emitter.subscribe("sess-1", queue_maxsize=1)
    healthy_q, unsub_healthy = await emitter.subscribe("sess-1")
    try:
        # Fill the small queue.
        await emitter.emit(_envelope(session_key="sess-1", sequence=0))
        # Drain the healthy subscriber's first delivery so it can accept
        # the next one without interference.
        await asyncio.wait_for(healthy_q.get(), timeout=1.0)

        with caplog.at_level(logging.WARNING):
            # Second emit — overflow path on the full subscriber.
            await emitter.emit(_envelope(session_key="sess-1", sequence=1))

        # The healthy subscriber still got the second envelope.
        delivered = await asyncio.wait_for(healthy_q.get(), timeout=1.0)
        assert delivered.sequence == 1

        # The journal saw BOTH writes regardless of overflow.
        assert [e.sequence for e in journal.appended] == [0, 1]
    finally:
        await unsub_full()
        await unsub_healthy()


# ---------------------------------------------------------------------------
# (4) Unsubscribe
# ---------------------------------------------------------------------------


async def test_unsubscribe_stops_delivery() -> None:
    """After unsubscribe, the queue receives no further envelopes."""
    journal = _RecordingJournal()
    emitter = JournalBackedEmitter(journal)

    queue, unsubscribe = await emitter.subscribe("sess-1")
    await emitter.emit(_envelope(session_key="sess-1", sequence=0))
    _ = await asyncio.wait_for(queue.get(), timeout=1.0)

    await unsubscribe()

    # Subsequent emit must not touch the queue.
    await emitter.emit(_envelope(session_key="sess-1", sequence=1))
    assert queue.qsize() == 0
    assert emitter.subscriber_count("sess-1") == 0


# ---------------------------------------------------------------------------
# (5) Journal failure isolation
# ---------------------------------------------------------------------------


async def test_journal_failure_does_not_break_emit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing ``append_event`` is logged but does NOT prevent the
    envelope from reaching live subscribers."""
    journal = _RecordingJournal()
    journal.should_fail = True
    emitter = JournalBackedEmitter(journal)

    queue, unsubscribe = await emitter.subscribe("sess-1")
    try:
        with caplog.at_level(logging.ERROR):
            await emitter.emit(_envelope(session_key="sess-1", sequence=0))

        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received.sequence == 0
    finally:
        await unsubscribe()


# ---------------------------------------------------------------------------
# (6) Default queue capacity is sensible (sanity)
# ---------------------------------------------------------------------------


async def test_default_queue_capacity_is_documented() -> None:
    """The advertised default cap must match the value the docstring
    documents in the SSE backpressure note."""
    assert DEFAULT_QUEUE_MAXSIZE == 512


# ---------------------------------------------------------------------------
# (7) G5 — per-token sync-commit hot path: buffer TextDeltas, flush in batches
# ---------------------------------------------------------------------------


def _envelope_for(event: Any, *, session_key: str, turn_id: str, sequence: int) -> EventEnvelope:
    return EventEnvelope(
        turn_id=turn_id,
        session_key=session_key,
        sequence=sequence,
        timestamp_ms=1_700_000_000_000 + sequence,
        event=event,
    )


async def test_text_deltas_are_batched_not_per_commit() -> None:
    """A 200-token reply (200 TextDeltas) followed by a terminal event
    must NOT cost 200 durable commits. The deltas are buffered and
    flushed in batches; the terminal event forces a final flush. ALL
    events are still persisted, in order, exactly once.
    """
    journal = _CountingBatchJournal()
    emitter = JournalBackedEmitter(journal)

    n = 200
    for i in range(n):
        await emitter.emit(
            _envelope_for(
                TextDelta(index=0, text=f"t{i}"),
                session_key="sess-1",
                turn_id="turn-1",
                sequence=i,
            )
        )
    # Terminal event closes the turn.
    await emitter.emit(
        _envelope_for(
            TurnComplete(finish_reason="stop", usage={}, elapsed_ms=10),
            session_key="sess-1",
            turn_id="turn-1",
            sequence=n,
        )
    )

    # All N+1 events persisted, in order, exactly once.
    assert len(journal.appended) == n + 1
    assert [e.sequence for e in journal.appended] == list(range(n + 1))
    # And the durable commit count is DRASTICALLY reduced vs. 201.
    # Buffer threshold batches the deltas; the terminal forces a flush.
    assert journal.commit_count < n // 4, (
        f"expected far fewer than {n} commits, got {journal.commit_count}"
    )
    assert journal.batch_calls >= 1


async def test_terminal_event_forces_final_flush_no_loss() -> None:
    """Fewer deltas than the size threshold must still be persisted —
    the terminal event guarantees a final flush so nothing is lost at
    turn end."""
    journal = _CountingBatchJournal()
    emitter = JournalBackedEmitter(journal)

    # Three deltas — below any sane flush threshold; they would sit in
    # the buffer indefinitely without the terminal flush.
    for i in range(3):
        await emitter.emit(
            _envelope_for(
                TextDelta(index=0, text=f"t{i}"),
                session_key="sess-1",
                turn_id="turn-x",
                sequence=i,
            )
        )
    # Before terminal, buffered deltas are NOT yet durable.
    assert len(journal.appended) == 0

    await emitter.emit(
        _envelope_for(
            TurnComplete(finish_reason="stop", usage={}, elapsed_ms=1),
            session_key="sess-1",
            turn_id="turn-x",
            sequence=3,
        )
    )

    # All four events flushed, in order.
    assert [e.sequence for e in journal.appended] == [0, 1, 2, 3]


async def test_errored_terminal_forces_final_flush() -> None:
    """A turn that ERRORS must still flush its buffered deltas — the
    error path is not allowed to lose the partial reply."""
    journal = _CountingBatchJournal()
    emitter = JournalBackedEmitter(journal)

    for i in range(5):
        await emitter.emit(
            _envelope_for(
                TextDelta(index=0, text=f"t{i}"),
                session_key="sess-1",
                turn_id="turn-err",
                sequence=i,
            )
        )
    await emitter.emit(
        _envelope_for(
            TurnErrored(reason="boom", message="kaboom", elapsed_ms=2),
            session_key="sess-1",
            turn_id="turn-err",
            sequence=5,
        )
    )

    assert [e.sequence for e in journal.appended] == [0, 1, 2, 3, 4, 5]


async def test_important_event_flushes_preceding_deltas_in_order() -> None:
    """A tool frame mid-stream must flush the deltas buffered before it
    BEFORE it is persisted — the journal must stay strictly ordered
    (subscriber-must-see-promptly events are not reordered behind
    buffered deltas)."""
    journal = _CountingBatchJournal()
    emitter = JournalBackedEmitter(journal)

    # Two deltas, then a tool-running frame (must-see-promptly), then a
    # tool-completed frame, then a terminal.
    await emitter.emit(
        _envelope_for(TextDelta(index=0, text="a"), session_key="s", turn_id="t", sequence=0)
    )
    await emitter.emit(
        _envelope_for(TextDelta(index=0, text="b"), session_key="s", turn_id="t", sequence=1)
    )
    await emitter.emit(
        _envelope_for(
            ToolStateRunning(
                tool_call_id="c1", tool_name="bash", args_json="{}", started_at_ms=1
            ),
            session_key="s",
            turn_id="t",
            sequence=2,
        )
    )
    # After the tool frame, the two deltas + the tool frame must be durable.
    assert [e.sequence for e in journal.appended] == [0, 1, 2]

    await emitter.emit(
        _envelope_for(
            ToolStateCompleted(tool_call_id="c1", result_summary="ok"),
            session_key="s",
            turn_id="t",
            sequence=3,
        )
    )
    await emitter.emit(
        _envelope_for(
            TurnComplete(finish_reason="stop", usage={}, elapsed_ms=1),
            session_key="s",
            turn_id="t",
            sequence=4,
        )
    )
    assert [e.sequence for e in journal.appended] == [0, 1, 2, 3, 4]


async def test_per_turn_buffers_do_not_interleave() -> None:
    """The shared emitter must buffer per turn_id — flushing turn A's
    terminal must not pull in turn B's still-buffered deltas, and each
    turn's events stay grouped/ordered."""
    journal = _CountingBatchJournal()
    emitter = JournalBackedEmitter(journal)

    # Interleave two turns' deltas.
    await emitter.emit(
        _envelope_for(TextDelta(index=0, text="A0"), session_key="sA", turn_id="A", sequence=0)
    )
    await emitter.emit(
        _envelope_for(TextDelta(index=0, text="B0"), session_key="sB", turn_id="B", sequence=0)
    )
    await emitter.emit(
        _envelope_for(TextDelta(index=0, text="A1"), session_key="sA", turn_id="A", sequence=1)
    )
    # Terminal for A only.
    await emitter.emit(
        _envelope_for(
            TurnComplete(finish_reason="stop", usage={}, elapsed_ms=1),
            session_key="sA",
            turn_id="A",
            sequence=2,
        )
    )

    a_events = [e for e in journal.appended if e.turn_id == "A"]
    b_events = [e for e in journal.appended if e.turn_id == "B"]
    # A fully flushed in order.
    assert [e.sequence for e in a_events] == [0, 1, 2]
    # B's single delta is still buffered (not yet durable).
    assert b_events == []

    # Close B and confirm it flushes independently.
    await emitter.emit(
        _envelope_for(
            TurnComplete(finish_reason="stop", usage={}, elapsed_ms=1),
            session_key="sB",
            turn_id="B",
            sequence=1,
        )
    )
    b_events = [e for e in journal.appended if e.turn_id == "B"]
    assert [e.sequence for e in b_events] == [0, 1]


async def test_reasoning_deltas_are_also_batched() -> None:
    """ReasoningDelta is high-frequency (one per thinking-token chunk)
    and must be batched too — same hot path as TextDelta."""
    journal = _CountingBatchJournal()
    emitter = JournalBackedEmitter(journal)

    n = 100
    for i in range(n):
        await emitter.emit(
            _envelope_for(
                ReasoningDelta(index=0, text=f"r{i}"),
                session_key="s",
                turn_id="t",
                sequence=i,
            )
        )
    await emitter.emit(
        _envelope_for(
            TurnComplete(finish_reason="stop", usage={}, elapsed_ms=1),
            session_key="s",
            turn_id="t",
            sequence=n,
        )
    )
    assert len(journal.appended) == n + 1
    assert journal.commit_count < n // 4


async def test_deltas_still_fan_out_immediately_despite_batching() -> None:
    """Batching the DURABLE write must not delay the realtime SSE
    fan-out — a live subscriber receives each delta as it is emitted,
    before any journal flush."""
    journal = _CountingBatchJournal()
    emitter = JournalBackedEmitter(journal)

    queue, unsubscribe = await emitter.subscribe("sess-1")
    try:
        env = _envelope_for(
            TextDelta(index=0, text="live"),
            session_key="sess-1",
            turn_id="turn-1",
            sequence=0,
        )
        await emitter.emit(env)
        # Delivered to the live subscriber even though it's still
        # buffered for the durable store.
        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received is env
        # Not yet flushed to the journal (buffered).
        assert journal.appended == []
    finally:
        await unsubscribe()


async def test_fallback_journal_without_batch_api_still_persists() -> None:
    """A duck-typed journal lacking ``append_events_batch`` must still
    persist every event (single-shot fallback). Correctness over perf
    for legacy stubs."""
    journal = _RecordingJournal()  # no append_events_batch
    emitter = JournalBackedEmitter(journal)

    for i in range(5):
        await emitter.emit(
            _envelope_for(
                TextDelta(index=0, text=f"t{i}"),
                session_key="s",
                turn_id="t",
                sequence=i,
            )
        )
    await emitter.emit(
        _envelope_for(
            TurnComplete(finish_reason="stop", usage={}, elapsed_ms=1),
            session_key="s",
            turn_id="t",
            sequence=5,
        )
    )
    assert [e.sequence for e in journal.appended] == [0, 1, 2, 3, 4, 5]
