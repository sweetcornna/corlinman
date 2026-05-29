"""``JournalBackedEmitter`` + ``BubbleEmitter`` — gateway-wide event tee.

W1.3 introduced :class:`JournalBackedEmitter`. W3.2 layers
:class:`BubbleEmitter` on top so child-subagent events can be folded
into the *parent's* event stream without giving the child its own
journal-backed write path. See :class:`BubbleEmitter` for the
rationale.

:class:`JournalBackedEmitter` conforms to
:class:`corlinman_agent.events.EventEmitter`. Every emit:

1. Persists the envelope to the per-turn journal (durable; powers the
   ``/admin/sessions/{key}/turns/{turn_id}/events`` replay route).
2. Fans the envelope out to every live in-process subscriber keyed by
   ``session_key`` (powers the ``/admin/sessions/{key}/events/live`` SSE
   stream).

Design notes
------------

**Persist before fan-out.** A journal failure is logged but never
propagated — emit() must not raise on the hot path. The fan-out runs
afterwards so a slow subscriber doesn't keep events out of the durable
store, and a journal write failure doesn't deny live observers the
event the agent already produced.

**Batched durable writes (G5).** The reasoning loop emits one
streaming delta per token chunk; persisting each with its own
``append_event`` cost one ``commit()`` per row under the backend's
global write-lock. We instead buffer the high-frequency,
non-correctness-gating delta events per-turn and flush them via
``append_events_batch`` (one commit per batch) on a size threshold, on
any important event (which is flushed *behind* the buffered deltas to
keep the journal ordered), and always at turn end. Only the durable
write is deferred — the realtime fan-out still fires for every event
immediately, so live SSE is unaffected. See :meth:`_persist`.

**Per-session fan-out.** Subscribers are scoped to a single
``session_key``. The set is keyed by string so a client watching
session X never receives session Y events. Lookup happens under an
:class:`asyncio.Lock` so the dict can't be mutated mid-fan-out — the
list-of-queues snapshot is taken inside the lock, the actual
``put_nowait`` calls run outside it.

**Backpressure.** Each subscriber owns a bounded
:class:`asyncio.Queue` (``maxsize=512``). On overflow we log a warning
and drop the event for that subscriber. The client can resync via
``Last-Event-ID`` to fetch the missed events from the journal — the
durable record is the source of truth, the in-process queue is the
realtime nicety. This mirrors the opencode SSE pattern (drop + client
reconnect with last id rather than blocking the producer).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from corlinman_agent.events import (
    Event,
    EventEmitter,
    EventEnvelope,
    ReasoningDelta,
    SubagentEvent,
    TextDelta,
    ToolInputDelta,
)

logger = structlog.get_logger(__name__)


# Bounded per-subscriber queue. Sized to absorb a couple of seconds of
# TextDelta bursts on a fast turn (~100 tokens/s) without dropping; a
# subscriber that lags beyond this window is genuinely behind and the
# client should reconnect via Last-Event-ID for catch-up.
DEFAULT_QUEUE_MAXSIZE: int = 512


# G5 — per-token sync-commit hot path. The reasoning loop emits one
# streaming delta per token chunk; persisting each with its own
# ``append_event`` cost one ``commit()`` per row under the backend's
# global write-lock (~6ms each per the backend note), so a 200-token
# reply serialized ~200 commits against every other session. We buffer
# only the *high-frequency, non-gating* delta events and flush them in
# batches via ``append_events_batch`` (one ``BEGIN IMMEDIATE``/``COMMIT``
# for the whole batch).
#
# Only these delta types are deferrable. Everything else — turn
# lifecycle, tool frames, block boundaries, subagent frames, cancel —
# is "important": a subscriber / the cost+resume logic must see it
# promptly, so it is never buffered. When an important event arrives we
# first flush any deltas buffered ahead of it (to keep the journal
# strictly ordered), then persist the important event itself.
_DEFERRABLE_EVENT_TYPES: tuple[type, ...] = (
    TextDelta,
    ReasoningDelta,
    ToolInputDelta,
)

# Flush a turn's delta buffer once it reaches this many events even if no
# important/terminal event has arrived yet. Bounds both the unflushed
# (crash-window) event count and per-batch memory; small enough that a
# crash loses at most this many of the lowest-value (re-derivable from
# the realtime stream) delta events, large enough to collapse ~an order
# of magnitude of commits on a normal reply.
DEFAULT_FLUSH_THRESHOLD: int = 32


class JournalBackedEmitter:
    """Tee :class:`EventEnvelope` to the journal and to live subscribers.

    The same instance is shared by every :class:`ReasoningLoop` /
    :class:`RunnerPool` / subagent supervisor the gateway constructs;
    the gateway lifecycle wires one at boot and threads it through.
    """

    def __init__(
        self,
        journal: Any,
        *,
        flush_threshold: int = DEFAULT_FLUSH_THRESHOLD,
    ) -> None:
        """Wrap ``journal`` (typically an
        :class:`corlinman_server.agent_journal.AgentJournal`).

        Typed loosely so a test can hand in a duck-typed stub exposing
        just ``append_event(envelope)``. If the journal also exposes
        ``append_events_batch(envelopes)`` (the W1.2 backend API), the
        emitter buffers high-frequency streaming deltas and flushes them
        one batch / one commit instead of one commit per token; a journal
        without that method falls back to per-event ``append_event``
        (correct, just not batched).

        ``flush_threshold`` caps how many delta events sit unflushed in a
        turn's buffer before a size-triggered flush — bounding both the
        crash window and per-batch memory.
        """
        self._journal = journal
        # Whether the journal supports the batch write API. Cached at
        # construction so the hot path doesn't re-probe every emit.
        self._has_batch_api: bool = callable(
            getattr(journal, "append_events_batch", None)
        )
        self._flush_threshold = max(1, flush_threshold)
        # turn_id -> ordered list of buffered deferrable envelopes awaiting
        # a batch flush. Keyed per-turn (not global) because the emitter
        # is shared across every session/turn; flushing turn A must not
        # entangle turn B's still-open buffer, and each turn's final flush
        # is driven by its own terminal event. Guarded by ``_buffer_lock``
        # so two emits for the same turn can't lose/duplicate a buffered
        # event or double-flush.
        self._delta_buffers: dict[str, list[EventEnvelope]] = defaultdict(list)
        self._buffer_lock = asyncio.Lock()
        # session_key -> set of queues. ``set`` so unsubscribe is O(1)
        # and so a single client opening two SSE streams against the
        # same session gets two distinct entries (Queue identity is the
        # disambiguator).
        self._subscribers: dict[str, set[asyncio.Queue[EventEnvelope]]] = {}
        self._lock = asyncio.Lock()
        # Per-turn monotonic sequence counter — owned by the emitter so
        # multiple emit sites (reasoning loop + tool dispatcher) for the
        # same turn never race on a shared counter. ``emit_event``
        # consults this map; ``emit`` (which takes a fully-formed
        # envelope) leaves it alone. Guarded by an asyncio.Lock-free
        # threading lock — ``emit_event`` is async but the increment is
        # a few ns; we don't want to await holding a sequence lock.
        self._sequence_by_turn: dict[str, int] = defaultdict(int)
        self._sequence_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # EventEmitter protocol
    # ------------------------------------------------------------------

    async def emit(self, envelope: EventEnvelope) -> None:
        """Persist ``envelope`` then hand it to every live subscriber.

        Conforms to :class:`corlinman_agent.events.EventEmitter`: never
        raises on the hot path. Logs at WARN on a journal write failure
        and at WARN on a subscriber queue overflow.

        Durable-write batching (G5): high-frequency streaming deltas
        (:data:`_DEFERRABLE_EVENT_TYPES`) are buffered per-turn and
        flushed via :meth:`_flush_turn` in batches — on the size
        threshold, on an important/terminal event, and always at turn
        end. Important events flush the turn's buffered deltas *first*
        so the journal stays strictly ordered, then persist themselves.
        Only the *durable write* is deferred — the realtime fan-out
        below still happens for every event the instant it is emitted,
        so live SSE subscribers see every token immediately.
        """
        # (1) durable write — batched for deltas, prompt for everything
        # that gates correctness (turn lifecycle, tool frames, ...).
        await self._persist(envelope)

        # (2) snapshot subscribers under the lock; deliver outside it
        # so a slow consumer can't stall the producer holding the lock.
        async with self._lock:
            queues = list(self._subscribers.get(envelope.session_key, ()))

        for queue in queues:
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                logger.warning(
                    "emitter.subscriber_overflow",
                    session_key=envelope.session_key,
                    turn_id=envelope.turn_id,
                    sequence=envelope.sequence,
                    detail=(
                        "subscriber queue full; dropping event — "
                        "client should reconnect with Last-Event-ID "
                        "to catch up from the journal"
                    ),
                )

    # ------------------------------------------------------------------
    # Durable-write path (G5 batching)
    # ------------------------------------------------------------------

    async def _persist(self, envelope: EventEnvelope) -> None:
        """Route ``envelope`` to the durable store with delta batching.

        Deferrable deltas accumulate in the turn's buffer (flushed once
        it crosses :attr:`_flush_threshold`). Any other event is
        "important": flush the turn's buffered deltas to preserve order,
        then persist the important event itself (and, when it is a
        turn-terminal event, drop the empty per-turn buffer so the dict
        can't grow without bound under session churn).

        Falls back to per-event ``append_event`` when the journal has no
        batch API — correctness over throughput for legacy stubs.
        """
        is_deferrable = self._has_batch_api and isinstance(
            envelope.event, _DEFERRABLE_EVENT_TYPES
        )

        if is_deferrable:
            async with self._buffer_lock:
                buf = self._delta_buffers[envelope.turn_id]
                buf.append(envelope)
                if len(buf) >= self._flush_threshold:
                    await self._flush_locked(envelope.turn_id)
            return

        # Important / non-deferrable event. Flush whatever deltas were
        # buffered before it (order preservation), then persist it
        # promptly via its own write.
        async with self._buffer_lock:
            await self._flush_locked(envelope.turn_id)
            if self._is_terminal(envelope.event):
                # Turn is over — reap the (now-empty) buffer entry.
                self._delta_buffers.pop(envelope.turn_id, None)
        await self._append_event_safely(envelope)

    async def _flush_locked(self, turn_id: str) -> None:
        """Flush ``turn_id``'s buffered deltas in one batch. Caller MUST
        hold :attr:`_buffer_lock`. No-op when the buffer is empty.

        The buffer slice is swapped out *before* the await so a
        concurrent emit for the same turn (which also needs the lock,
        so this is serialized) never sees a half-flushed list, and a
        batch-write failure is logged but never re-raised (hot-path
        contract). On failure the events are dropped from the durable
        store — the realtime stream already delivered them and the
        client can resync via Last-Event-ID, mirroring the existing
        ``append_event`` failure posture.
        """
        buf = self._delta_buffers.get(turn_id)
        if not buf:
            return
        batch = buf
        # Reset to a fresh empty buffer for this turn before awaiting.
        self._delta_buffers[turn_id] = []
        try:
            await self._journal.append_events_batch(batch)
        except Exception:  # noqa: BLE001 — never bubble up on the hot path
            logger.exception(
                "emitter.journal_batch_write_failed",
                turn_id=turn_id,
                count=len(batch),
            )

    async def _append_event_safely(self, envelope: EventEnvelope) -> None:
        """Single-shot durable write — replay is the source of truth.

        Used for important/non-deferrable events and as the fallback
        when the journal exposes no batch API. Never raises.
        """
        try:
            await self._journal.append_event(envelope)
        except Exception:  # noqa: BLE001 — never bubble up
            logger.exception(
                "emitter.journal_write_failed",
                turn_id=envelope.turn_id,
                sequence=envelope.sequence,
            )

    @staticmethod
    def _is_terminal(event: Event) -> bool:
        """Whether ``event`` ends the turn (final flush + buffer reap).

        Matched by class name so this stays correct even if the import
        of the event union shifts; the gateway only ever emits one of
        these two terminal events per turn.
        """
        return type(event).__name__ in ("TurnComplete", "TurnErrored")

    async def emit_event(
        self,
        turn_id: str,
        session_key: str,
        event: Event,
    ) -> None:
        """Build an envelope for ``event`` (with sequence + timestamp
        assigned here) and emit. Callers that don't already own a
        per-turn sequence counter — notably the tool dispatcher (W3.1) —
        use this so two emit sites for the same turn don't race.

        Per :class:`EventEmitter`'s contract: never raises on the hot
        path. Logs and swallows any internal failure.
        """
        async with self._sequence_lock:
            seq = self._sequence_by_turn[turn_id]
            self._sequence_by_turn[turn_id] = seq + 1
        envelope = EventEnvelope(
            turn_id=turn_id,
            session_key=session_key,
            sequence=seq,
            timestamp_ms=time.time_ns() // 1_000_000,
            event=event,
        )
        await self.emit(envelope)

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        session_key: str,
        *,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ) -> tuple[
        asyncio.Queue[EventEnvelope],
        Callable[[], Awaitable[None]],
    ]:
        """Register a live subscriber for ``session_key``.

        Returns the subscriber's bounded :class:`asyncio.Queue` and an
        async ``unsubscribe`` callable. The route handler typically:

        1. ``queue, unsubscribe = await emitter.subscribe(key)``
        2. Drain any catch-up events from the journal.
        3. Loop ``envelope = await queue.get()`` and emit SSE frames.
        4. ``await unsubscribe()`` in the ``finally`` clause.

        The subscribe-before-catch-up ordering is load-bearing: any
        envelope emitted between the catch-up read and the live loop
        ends up in ``queue`` instead of being lost.
        """
        queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=queue_maxsize)
        async with self._lock:
            self._subscribers.setdefault(session_key, set()).add(queue)

        async def _unsubscribe() -> None:
            async with self._lock:
                queues = self._subscribers.get(session_key)
                if queues is None:
                    return
                queues.discard(queue)
                if not queues:
                    # Reap empty sets so the dict doesn't grow without
                    # bound under high session churn.
                    self._subscribers.pop(session_key, None)

        return queue, _unsubscribe

    # ------------------------------------------------------------------
    # Diagnostics — used by tests + future /admin debug routes.
    # ------------------------------------------------------------------

    def subscriber_count(self, session_key: str) -> int:
        """Return the live-subscriber count for ``session_key`` (0 when
        none). Snapshot read — racy under concurrent subscribe/unsubscribe
        but good enough for ``/admin`` diagnostics.
        """
        queues = self._subscribers.get(session_key)
        return 0 if queues is None else len(queues)


class BubbleEmitter:
    """Wrap a child agent's emits so they surface on the parent's stream.

    W3.2 — the child subagent runs in its own :class:`ReasoningLoop` /
    runner pool but the operator's admin UI watches the *parent's* SSE
    stream. To make the child's tool calls / reasoning / text visible
    without giving the child its own ``session_key`` subscription, the
    supervisor hands the child agent a :class:`BubbleEmitter` whose
    every ``emit`` / ``emit_event`` rewrites the envelope into a
    :class:`corlinman_agent.events.SubagentEvent` *under the parent's
    turn_id / session_key*. The frontend then re-renders the wrapped
    inner envelope as a nested timeline beneath the spawning tool call
    (see ``ui/components/sessions/subagent-tree.tsx``).

    Conforms to :class:`corlinman_agent.events.EventEmitter` — the same
    duck-typed interface the reasoning loop already takes.

    Why a wrapper, not a dual-emitter:
        Two emitters would race on monotonic sequence assignment under
        the same parent turn_id. The wrapper keeps the parent's emitter
        as the *single* sequence authority — every bubbled event gets
        the parent's next free sequence — so the SSE stream stays
        strictly ordered.

    Why ``parent_turn_id`` stays static:
        The child's runs may span the parent's turn boundary in theory,
        but the supervisor today always awaits the child to completion
        inside the parent's turn. Pinning the turn id at construction
        is the simpler contract; if multi-turn lifetimes show up later
        the wrapper can lift the turn id from the wrapped envelope
        instead.
    """

    __slots__ = (
        "_child_session_key",
        "_parent",
        "_parent_session_key",
        "_parent_turn_id",
    )

    def __init__(
        self,
        parent: EventEmitter,
        parent_turn_id: str,
        parent_session_key: str,
        child_session_key: str,
    ) -> None:
        self._parent = parent
        self._parent_turn_id = parent_turn_id
        self._parent_session_key = parent_session_key
        self._child_session_key = child_session_key

    async def emit(self, envelope: EventEnvelope) -> None:
        """Forward ``envelope`` as a :class:`SubagentEvent` to the parent.

        The wrapped inner envelope is preserved verbatim — the frontend
        recovers it via ``payload.envelope`` and feeds it back through
        the same reducer scoped to the child's sub-tree.
        """
        wrapped = SubagentEvent(
            child_session_key=self._child_session_key,
            envelope=envelope,
        )
        await self._parent.emit_event(
            self._parent_turn_id,
            self._parent_session_key,
            wrapped,
        )

    async def emit_event(
        self,
        turn_id: str,  # noqa: ARG002 — child-side turn_id ignored; parent owns ordering
        session_key: str,  # noqa: ARG002 — child-side session_key ignored
        event: Event,
    ) -> None:
        """Wrap ``event`` in a :class:`SubagentEvent` and forward upward.

        The child caller's ``turn_id`` / ``session_key`` are ignored
        deliberately — we want every bubbled envelope to land on the
        parent's stream under the parent's turn id so the consumer sees
        one ordered sequence.
        """
        # Build a synthetic inner envelope so the wrapped payload still
        # round-trips through the wire shape. The inner sequence is 0
        # because the parent's emit_event will assign the *outer*
        # sequence (the one the SSE / journal care about); the inner
        # value is informational only.
        inner = EventEnvelope(
            turn_id=self._parent_turn_id,
            session_key=self._child_session_key,
            sequence=0,
            timestamp_ms=time.time_ns() // 1_000_000,
            event=event,
        )
        await self.emit(inner)


__all__ = [
    "DEFAULT_FLUSH_THRESHOLD",
    "DEFAULT_QUEUE_MAXSIZE",
    "BubbleEmitter",
    "JournalBackedEmitter",
]
