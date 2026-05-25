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
    SubagentEvent,
)

logger = structlog.get_logger(__name__)


# Bounded per-subscriber queue. Sized to absorb a couple of seconds of
# TextDelta bursts on a fast turn (~100 tokens/s) without dropping; a
# subscriber that lags beyond this window is genuinely behind and the
# client should reconnect via Last-Event-ID for catch-up.
DEFAULT_QUEUE_MAXSIZE: int = 512


class JournalBackedEmitter:
    """Tee :class:`EventEnvelope` to the journal and to live subscribers.

    The same instance is shared by every :class:`ReasoningLoop` /
    :class:`RunnerPool` / subagent supervisor the gateway constructs;
    the gateway lifecycle wires one at boot and threads it through.
    """

    def __init__(self, journal: Any) -> None:
        """Wrap ``journal`` (typically an
        :class:`corlinman_server.agent_journal.AgentJournal`).

        Typed loosely so a test can hand in a duck-typed stub exposing
        just ``append_event(envelope)``.
        """
        self._journal = journal
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
        """
        # (1) durable write first — replay is the source of truth.
        try:
            await self._journal.append_event(envelope)
        except Exception:  # noqa: BLE001 — never bubble up
            logger.exception(
                "emitter.journal_write_failed",
                turn_id=envelope.turn_id,
                sequence=envelope.sequence,
            )

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


__all__ = ["JournalBackedEmitter", "BubbleEmitter", "DEFAULT_QUEUE_MAXSIZE"]
