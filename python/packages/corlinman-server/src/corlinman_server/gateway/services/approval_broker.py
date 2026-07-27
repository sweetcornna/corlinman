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
    """(session_key, call_id) → backend-stream registry + decide().

    W3-4: when an :class:`~corlinman_providers.plugins.ApprovalStore` is
    attached (:meth:`attach_store`), the broker mirrors its registry into
    the durable ``pending_approvals`` table — a row on register, a
    decision update on decide, and a ``timeout`` update when a stream
    ends with the approval still parked. All persistence is best-effort:
    a broken store never blocks or fails the live decision path.
    """

    def __init__(self, store: Any | None = None) -> None:
        self._lock = threading.RLock()
        self._pending: dict[tuple[str, str], PendingApproval] = {}
        self._store: Any | None = store
        # Fire-and-forget persistence tasks; retained so they aren't GC'd
        # mid-flight and can be awaited by tests via ``drain_persistence``.
        self._persist_tasks: set[asyncio.Task[Any]] = set()
        # Per-key pending-row INSERT task. Decision / timeout UPDATEs only
        # touch rows that exist, so they must be sequenced AFTER the
        # insert — every update path awaits this task first.
        self._insert_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}

    def attach_store(self, store: Any | None) -> None:
        """Attach (or detach with ``None``) the durable approvals store."""
        self._store = store

    async def drain_persistence(self) -> None:
        """Await every in-flight persistence task (tests / shutdown)."""
        tasks = list(self._persist_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule(self, coro: Any) -> asyncio.Task[Any] | None:
        """Run a persistence coroutine on the current loop, detached."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        task = loop.create_task(coro)
        self._persist_tasks.add(task)
        task.add_done_callback(self._persist_tasks.discard)
        return task

    async def _await_insert(self, key: tuple[str, str]) -> None:
        """Wait for the pending-row INSERT of ``key`` (ordering barrier)."""
        with self._lock:
            task = self._insert_tasks.get(key)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _persist_pending(self, entry: PendingApproval) -> None:
        store = self._store
        if store is None:
            return
        try:
            from corlinman_providers.plugins import ApprovalRequest  # noqa: PLC0415

            await store.insert(
                ApprovalRequest(
                    call_id=entry.call_id,
                    plugin=entry.plugin,
                    tool=entry.tool,
                    args_preview=entry.args_preview_json,
                    session_key=entry.session_key,
                    reason=entry.reason,
                    created_at=entry.registered_at,
                )
            )
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            log.warning(
                "approval_broker.persist_pending_failed call_id=%s err=%s",
                entry.call_id,
                exc,
            )

    async def _persist_decision(
        self, key: tuple[str, str], *, approved: bool, reason: str = ""
    ) -> None:
        store = self._store
        if store is None:
            return
        await self._await_insert(key)
        try:
            from corlinman_providers.plugins import ApprovalDecision  # noqa: PLC0415

            decision = ApprovalDecision.ALLOW if approved else ApprovalDecision.DENY
            await store.decide(
                key[1], decision, reason=reason or None, session_key=key[0]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "approval_broker.persist_decision_failed call_id=%s err=%s",
                key[1],
                exc,
            )

    async def _persist_timeout(self, key: tuple[str, str], reason: str) -> None:
        store = self._store
        if store is None:
            return
        await self._await_insert(key)
        try:
            from corlinman_providers.plugins import ApprovalDecision  # noqa: PLC0415

            # ``decide`` only touches rows whose decision is still NULL, so
            # a row already decided by a human is never downgraded.
            await store.decide(
                key[1],
                ApprovalDecision.TIMEOUT,
                reason=reason,
                session_key=key[0],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "approval_broker.persist_timeout_failed call_id=%s err=%s",
                key[1],
                exc,
            )

    def register(self, entry: PendingApproval) -> None:
        key = _key(entry.session_key, entry.call_id)
        with self._lock:
            self._pending[key] = entry
        task = self._schedule(self._persist_pending(entry))
        if task is not None:
            with self._lock:
                self._insert_tasks[key] = task

            def _drop_insert_task(
                done: asyncio.Task[Any],
                k: tuple[str, str] = key,
            ) -> None:
                # Identity-checked: a re-registration of the same key
                # replaced the entry with a NEWER insert task — popping
                # blindly would remove that barrier and re-open the
                # UPDATE-before-INSERT race (W3-4 review fix).
                with self._lock:
                    if self._insert_tasks.get(k) is done:
                        self._insert_tasks.pop(k, None)

            task.add_done_callback(_drop_insert_task)

    def unregister(self, call_id: str, session_key: str = "") -> None:
        key = _key(session_key, call_id)
        with self._lock:
            entry = self._pending.pop(key, None)
        if entry is not None:
            # The stream died with the approval still parked — nobody can
            # answer it any more. Mark the durable row ``timeout`` so the
            # audit trail distinguishes it from a human deny.
            self._schedule(
                self._persist_timeout(
                    key, "stream ended before a decision arrived"
                )
            )

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
            # The human answered but the turn never saw it — record it as
            # a timeout, not as an (unenforced) approval/denial.
            await self._persist_timeout(
                _key(session_key, call_id),
                "stream died before the decision could be delivered",
            )
            return False
        await self._persist_decision(
            _key(session_key, call_id), approved=approved, reason=deny_message or ""
        )
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
