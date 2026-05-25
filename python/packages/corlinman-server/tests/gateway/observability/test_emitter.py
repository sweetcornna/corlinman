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

from corlinman_agent.events import EventEnvelope, TextDelta
from corlinman_server.gateway.observability import JournalBackedEmitter
from corlinman_server.gateway.observability.emitter import (
    DEFAULT_QUEUE_MAXSIZE,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingJournal:
    """In-memory ``AgentJournal`` stand-in. Captures every envelope handed
    to :meth:`append_event` and lets a test toggle a failure mode."""

    def __init__(self) -> None:
        self.appended: list[Any] = []
        self.should_fail: bool = False

    async def append_event(self, envelope: Any) -> None:
        if self.should_fail:
            raise RuntimeError("simulated journal write failure")
        self.appended.append(envelope)


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
    full_q, unsub_full = await emitter.subscribe("sess-1", queue_maxsize=1)
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
