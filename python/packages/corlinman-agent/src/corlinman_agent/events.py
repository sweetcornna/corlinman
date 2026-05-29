"""Typed event stream for the reasoning loop (W1.1 — task observability).

This module defines the event taxonomy that replaces the sparse 4-event
yield (``TokenEvent``/``ToolCallEvent``/``DoneEvent``/``ErrorEvent``)
with a part-based model adapted from Claude Code's ``content_block`` and
opencode's ``Part`` discriminated union.

Every :class:`EventEnvelope` carries a monotonic ``sequence`` within its
``turn_id``, a wall-clock ``timestamp_ms``, and exactly one typed
:class:`Event` payload. The envelope's :meth:`EventEnvelope.to_json`
helper produces the wire shape consumed downstream by:

* the journal writer (``turn_events`` table — W1.2);
* the SSE replay endpoint (``GET /admin/sessions/{key}/turns/{turn_id}/events`` — W1.3);
* the live SSE stream (``GET /admin/sessions/{key}/events/live`` — W1.3).

Out-of-scope here:

* ``Cancelling`` / ``ToolStateHeartbeat`` are emitted by W3.1 (runner pool).
* ``SubagentSpawned`` / ``SubagentEvent`` / ``SubagentCompleted`` are
  emitted by W3.2 (subagent supervisor).

We define the dataclasses for every event type so downstream code can
import them today; only a subset are wired to fire from the reasoning
loop in this slice.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from threading import Lock
from typing import Any, Literal, Protocol

# --------------------------------------------------------------------------
# Event payload dataclasses (the discriminated union)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class TurnStart:
    """Start of a single turn — the first event in every turn stream."""

    model: str
    user_text_preview: str
    system_message_preview: str


BlockType = Literal["text", "reasoning", "tool_use"]


@dataclass(slots=True)
class BlockStart:
    """A new content block begins. Mirrors Claude Code's
    ``content_block_start``."""

    index: int
    block_type: BlockType
    tool_name: str | None = None
    tool_call_id: str | None = None


@dataclass(slots=True)
class TextDelta:
    """Incremental text content delta for a ``text`` block."""

    index: int
    text: str
    cumulative_len: int | None = None


@dataclass(slots=True)
class ReasoningDelta:
    """Incremental ``reasoning`` block delta (Anthropic ``thinking_delta``
    / OpenAI ``reasoning`` summary)."""

    index: int
    text: str
    signature: str | None = None


@dataclass(slots=True)
class ToolInputDelta:
    """Incremental tool-call arguments delta for a ``tool_use`` block.

    ``partial_json`` is a raw fragment of the JSON arguments string —
    the concatenation of all deltas for a given ``index`` is valid JSON.
    """

    index: int
    partial_json: str


@dataclass(slots=True)
class BlockStop:
    """A content block ends. Mirrors Claude Code's ``content_block_stop``."""

    index: int
    elapsed_ms: int


@dataclass(slots=True)
class ToolStateRunning:
    """Tool dispatch has started — emitted just before the plugin runs.

    The runner pool (W3.1) may re-emit / move this event; for now it
    fires from the reasoning loop the moment the model's tool-call
    fragments are fully aggregated.
    """

    tool_call_id: str
    tool_name: str
    args_json: str
    started_at_ms: int


@dataclass(slots=True)
class ToolStateHeartbeat:
    """Periodic keepalive for a long-running tool (every 10s).

    Emitted by W3.1 (runner pool) — not by the reasoning loop.
    Defined here so downstream consumers (channel adapters, SSE) can
    import a stable type today.
    """

    tool_call_id: str
    elapsed_ms: int
    stdout_tail: str | None = None


@dataclass(slots=True)
class ToolStateCompleted:
    """Tool dispatch finished. ``result_summary`` is capped at <= 4 KB."""

    tool_call_id: str
    result_summary: str
    result_json_ref: str | None = None
    elapsed_ms: int = 0
    is_error: bool = False


@dataclass(slots=True)
class SubagentSpawned:
    """Parent agent spawned a child subagent. Emitted by W3.2."""

    parent_session_key: str
    child_session_key: str
    child_agent_id: str
    depth: int
    prompt_preview: str


@dataclass(slots=True)
class SubagentEvent:
    """A child subagent emitted an event — bubbled up to the parent
    stream tagged with the child's session key. Recursive: ``envelope``
    is a complete child :class:`EventEnvelope`."""

    child_session_key: str
    envelope: EventEnvelope


@dataclass(slots=True)
class SubagentCompleted:
    """A child subagent finished. Emitted by W3.2."""

    child_session_key: str
    finish_reason: str
    tool_calls_made: int
    elapsed_ms: int
    summary: str


@dataclass(slots=True)
class Cancelling:
    """First-poll cancel feedback. Emitted by W3.1 (cancel.py)."""

    reason: str


@dataclass(slots=True)
class TurnComplete:
    """Terminal success event for a turn."""

    finish_reason: str
    usage: Mapping[str, int]
    elapsed_ms: int
    estimated_cost_usd: float | None = None
    cost_status: str | None = None


@dataclass(slots=True)
class TurnErrored:
    """Terminal error event for a turn."""

    reason: str
    message: str
    elapsed_ms: int


# --------------------------------------------------------------------------
# Discriminated union + envelope
# --------------------------------------------------------------------------

Event = (
    TurnStart
    | BlockStart
    | TextDelta
    | ReasoningDelta
    | ToolInputDelta
    | BlockStop
    | ToolStateRunning
    | ToolStateHeartbeat
    | ToolStateCompleted
    | SubagentSpawned
    | SubagentEvent
    | SubagentCompleted
    | Cancelling
    | TurnComplete
    | TurnErrored
)
"""Discriminator: ``type(event).__name__`` (e.g. ``'TextDelta'``).

Used as the ``event_type`` tag in :meth:`EventEnvelope.to_json` and the
journal's ``turn_events.event_type`` column.
"""


def _event_type_name(event: Event) -> str:
    """Return the discriminator tag for ``event``."""

    return type(event).__name__


def _event_payload_to_dict(event: Event) -> dict[str, Any]:
    """Serialise an event dataclass to a plain dict.

    Recurses through nested :class:`EventEnvelope` (for
    :class:`SubagentEvent.envelope`) and any embedded dataclasses so the
    output is pure JSON-friendly primitives — ready for ``json.dumps``.
    """

    if not is_dataclass(event):
        # Defensive: callers always pass a dataclass instance, but if a
        # raw dict slips through, pass it back unchanged.
        return dict(event)  # type: ignore[arg-type]

    out: dict[str, Any] = {}
    for f in fields(event):
        value = getattr(event, f.name)
        out[f.name] = _serialise(value)
    return out


def _serialise(value: Any) -> Any:
    """Convert any nested value into a JSON-friendly primitive."""

    if isinstance(value, EventEnvelope):
        return value.to_json()
    if is_dataclass(value) and not isinstance(value, type):
        return _event_payload_to_dict(value)  # type: ignore[arg-type]
    if isinstance(value, Mapping):
        return {str(k): _serialise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialise(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        # Defensive — none of our event fields are bytes today, but
        # surface as a hex string rather than crashing the JSON encoder.
        return bytes(value).hex()
    return value


@dataclass(slots=True)
class EventEnvelope:
    """The single wire-shape the gateway emits to all consumers.

    Every event the agent / runner / subagent supervisor produces is
    wrapped in an envelope with:

    * ``turn_id`` for correlation across rounds;
    * ``session_key`` for fan-out to per-session subscribers;
    * ``sequence`` monotonic per turn (0-indexed);
    * ``timestamp_ms`` wall clock at emission;
    * ``event`` — the typed payload (one of the union members above).
    """

    turn_id: str
    session_key: str
    sequence: int
    timestamp_ms: int
    event: Event

    def to_json(self) -> dict[str, Any]:
        """Return the wire shape consumed by W1.2 / W1.3.

        Shape::

            {
                "turn_id": "...",
                "session_key": "...",
                "sequence": 0,
                "timestamp_ms": 1716500000000,
                "event_type": "TextDelta",
                "payload": {"index": 0, "text": "hi", "cumulative_len": 2},
            }
        """

        return {
            "turn_id": self.turn_id,
            "session_key": self.session_key,
            "sequence": self.sequence,
            "timestamp_ms": self.timestamp_ms,
            "event_type": _event_type_name(self.event),
            "payload": _event_payload_to_dict(self.event),
        }


# --------------------------------------------------------------------------
# Emitter Protocol + test helper
# --------------------------------------------------------------------------


class EventEmitter(Protocol):
    """Sink for :class:`EventEnvelope` instances.

    The gateway tees one of these into every :class:`ReasoningLoop` /
    subagent / runner — production wiring lands an
    emit-and-persist-and-fanout impl in W1.3; tests use
    :class:`MockEventEmitter`.

    Two emission surfaces:

    * :meth:`emit` — caller supplies a fully-formed :class:`EventEnvelope`.
      The reasoning loop uses this (W1.1) because it already owns a
      per-turn sequence counter.
    * :meth:`emit_event` — caller hands in the raw ``event`` plus the
      ``turn_id`` / ``session_key`` correlation pair, and the emitter
      assigns the monotonic sequence + wall-clock timestamp itself. The
      tool dispatcher (W3.1) uses this so two emit sites for the same
      turn don't race on a shared counter.
    """

    async def emit(self, envelope: EventEnvelope) -> None:
        """Persist / fan out ``envelope``. Must not raise on the hot path."""

        ...

    async def emit_event(
        self,
        turn_id: str,
        session_key: str,
        event: Event,
    ) -> None:
        """Build an envelope for ``event`` (assigning sequence +
        timestamp) and emit. Must not raise on the hot path."""

        ...


class MockEventEmitter:
    """Test-only emitter that captures every envelope.

    Records every emit into :attr:`envelopes` in arrival order. Lets a
    unit test assert the full event sequence for a deterministic turn.

    Maintains a per-``turn_id`` monotonic sequence counter so
    :meth:`emit_event` produces stable envelopes without coordination
    from the caller.
    """

    def __init__(self) -> None:
        self.envelopes: list[EventEnvelope] = []
        self._sequence_by_turn: dict[str, int] = defaultdict(int)
        self._lock = Lock()

    async def emit(self, envelope: EventEnvelope) -> None:
        self.envelopes.append(envelope)

    async def emit_event(
        self,
        turn_id: str,
        session_key: str,
        event: Event,
    ) -> None:
        with self._lock:
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

    # Convenience helpers ---------------------------------------------------

    @property
    def event_types(self) -> list[str]:
        """Discriminator tags in arrival order — handy for sequence
        assertions."""

        return [_event_type_name(env.event) for env in self.envelopes]

    def events_of(self, event_cls: type) -> list[Any]:
        """Return every captured payload of type ``event_cls`` (in order)."""

        return [env.event for env in self.envelopes if isinstance(env.event, event_cls)]


__all__ = [
    "BlockStart",
    "BlockStop",
    "BlockType",
    "Cancelling",
    "Event",
    "EventEmitter",
    "EventEnvelope",
    "MockEventEmitter",
    "ReasoningDelta",
    "SubagentCompleted",
    "SubagentEvent",
    "SubagentSpawned",
    "TextDelta",
    "ToolInputDelta",
    "ToolStateCompleted",
    "ToolStateHeartbeat",
    "ToolStateRunning",
    "TurnComplete",
    "TurnErrored",
    "TurnStart",
]
