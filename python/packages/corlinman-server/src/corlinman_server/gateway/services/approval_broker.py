"""Gateway approval broker — routes decisions back to parked tool calls.

W3-3 of the unified-authorization plan. When the agent emits an
``AwaitingApproval`` server frame, :mod:`.chat_service` registers the
``call_id`` here together with the stream's outbound ``tx`` queue. Any
decider — the web ``POST /v1/chat/completions/{turn}/approve`` route, a
channel reply loop, admin tooling — then answers through
:meth:`ApprovalBroker.decide`, which feeds the matching
``ApprovalDecision`` client frame into the right backend stream.

Process-global singleton on purpose: the web route, the channel runtime
and the chat service all live in the gateway process and must share one
registry or a decision could never find its stream. Entries are removed
on decision and force-cleared by ``chat_service`` when a stream ends, so
a stream that dies with a pending approval cannot leak its queue.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from corlinman_grpc._generated.corlinman.v1 import agent_pb2

__all__ = [
    "ApprovalBroker",
    "PendingApproval",
    "get_approval_broker",
    "reset_approval_broker",
]

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingApproval:
    """One parked tool call awaiting a human decision."""

    call_id: str
    tx: asyncio.Queue[Any]
    plugin: str = ""
    tool: str = ""
    args_preview_json: str = ""
    reason: str = ""
    #: The parked stream's session — the decision-authorization scope.
    #: Deciders must present the same key: knowing a session's key is the
    #: same capability bar as posting messages into it, and without the
    #: check ANY authenticated caller could decide ANY pending approval
    #: process-wide (review finding, W3-3).
    session_key: str = ""
    registered_at: float = field(default_factory=time.time)


def _key(session_key: str, call_id: str) -> tuple[str, str]:
    """Registry key. Composite on purpose — provider tool-call ids are
    NOT globally unique (index-style ``call_0`` ids collide across
    concurrent streams), so a bare call_id key would let one stream's
    registration clobber another's."""
    return (session_key or "", call_id)


class ApprovalBroker:
    """(session_key, call_id) → backend-stream registry + decide()."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: dict[tuple[str, str], PendingApproval] = {}

    def register(self, entry: PendingApproval) -> None:
        with self._lock:
            self._pending[_key(entry.session_key, entry.call_id)] = entry

    def unregister(self, call_id: str, session_key: str = "") -> None:
        with self._lock:
            self._pending.pop(_key(session_key, call_id), None)

    def pending_ids(self) -> list[str]:
        with self._lock:
            return [call_id for (_s, call_id) in self._pending]

    def get(self, call_id: str, session_key: str = "") -> PendingApproval | None:
        with self._lock:
            return self._pending.get(_key(session_key, call_id))

    async def decide(
        self,
        call_id: str,
        *,
        approved: bool,
        scope: str = "once",
        deny_message: str = "",
        session_key: str = "",
    ) -> bool:
        """Feed a decision to the parked stream.

        ``session_key`` scopes the lookup: a decider must know the parked
        stream's session (the same capability posting into that session
        requires). Returns ``False`` when the (session, call_id) pair is
        unknown — already decided, timed out agent-side, never existed,
        or the caller presented the wrong session — so callers surface
        "this approval has expired" instead of a silent no-op.
        """
        with self._lock:
            entry = self._pending.pop(_key(session_key, call_id), None)
        if entry is None:
            return False
        frame = agent_pb2.ClientFrame(
            approval=agent_pb2.ApprovalDecision(
                call_id=call_id,
                approved=approved,
                scope=scope or "once",
                deny_message=deny_message or "",
            )
        )
        try:
            await entry.tx.put(frame)
        except Exception as exc:  # noqa: BLE001 — dead stream ≙ expired approval
            log.warning(
                "approval_broker.decision_delivery_failed call_id=%s err=%s",
                call_id,
                exc,
            )
            return False
        log.info(
            "approval_broker.decided call_id=%s approved=%s scope=%s",
            call_id,
            approved,
            scope or "once",
        )
        return True


_BROKER: ApprovalBroker | None = None
_BROKER_LOCK = threading.RLock()


def get_approval_broker() -> ApprovalBroker:
    """The process-global broker (created on first use)."""
    global _BROKER
    with _BROKER_LOCK:
        if _BROKER is None:
            _BROKER = ApprovalBroker()
        return _BROKER


def reset_approval_broker() -> None:
    """Drop the global broker (tests)."""
    global _BROKER
    with _BROKER_LOCK:
        _BROKER = None
