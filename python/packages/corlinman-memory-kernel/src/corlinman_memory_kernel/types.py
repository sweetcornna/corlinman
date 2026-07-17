"""Dataclasses crossing the :class:`MemoryKernel` facade."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple


class LedgerEntry(NamedTuple):
    """One injected memory in a turn's recall ledger.

    A named shape (not a bare tuple) because the fields cross the
    servicer↔kernel package boundary and three of them are numbers — a
    positional swap would insert silently and poison the trust-loop
    analytics built on the ledger.
    """

    item_id: str
    lane: str
    rank: int
    score: float
    shown_chars: int


def scope_namespace(
    tenant_id: str, user_id: str, persona_id: str = ""
) -> str:
    """Canonical per-user notes namespace: ``facts/{tenant}/{user}/{persona}``.

    ``_`` stands in for the unbound persona so the segment count stays
    fixed. This is the single definition of the scheme — the servicer's
    scope helper and the identity merge re-homing both build from here.
    """
    return f"facts/{tenant_id}/{user_id}/{persona_id or '_'}"


def user_namespace_prefix(tenant_id: str, user_id: str) -> str:
    """Prefix covering every persona namespace of one user (for merges)."""
    return f"facts/{tenant_id}/{user_id}"


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
    # EPA affect vector (W6); salience 0 = affect-neutral / unstamped.
    affect_e: float = 0.0
    affect_p: float = 0.0
    affect_a: float = 0.0
    affect_salience: float = 0.0
