"""Dataclasses crossing the :class:`MemoryKernel` facade."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class KernelScope:
    """Who a memory belongs to. The hard isolation boundary.

    ``scope_user_id`` is the canonical corlinman-identity user id once
    W2 wires the resolver; until then callers pass the raw channel
    sender (or ``None`` for agent-scoped memory). ``persona_id`` of
    ``""`` means shared across personas.
    """

    tenant_id: str = "default"
    scope_user_id: str | None = None
    persona_id: str = ""


@dataclass
class Observation:
    """One completed turn, queued for sleep-time reconciliation."""

    session_key: str
    user_text: str
    reply_text: str
    ts_ms: int
    tenant_id: str = "default"
    channel: str | None = None
    channel_user_id: str | None = None
    scope_user_id: str | None = None
    persona_id: str = ""
    # Row id — set when hydrated from the queue, None before insert.
    id: str | None = None


@dataclass
class MemoryItem:
    """A hydrated ``mk_items`` row (the fields recall consumers need)."""

    id: str
    text: str
    kind: str
    source: str
    scope: KernelScope = field(default_factory=KernelScope)
    visibility: str = "private"
    risk: str = "low"
    confidence: float = 0.6
    importance: float = 0.5
    trust: float = 0.5
    utility: float = 0.5
    valid_from_ms: int = 0
    valid_to_ms: int | None = None
    recorded_at_ms: int = 0
    score: float = 0.0
