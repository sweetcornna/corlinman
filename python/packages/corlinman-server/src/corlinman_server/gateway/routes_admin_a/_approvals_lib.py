"""Wire models and helpers for :mod:`.approvals`.

Extracted verbatim from ``approvals.py`` to keep that route module small.
Holds the wire shapes (:class:`ApprovalOut`, :class:`DecideBody`) and the
store / SSE helpers that ``router()`` and its handlers call. Importing this
module must not pull in ``approvals.py`` (no cycle).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
)

# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class ApprovalOut(BaseModel):
    """Flat JSON shape returned to the UI. Mirrors the Rust
    ``ApprovalOut`` envelope projected onto the Python
    :class:`~corlinman_providers.plugins.ApprovalRecord` shape.

    Field naming follows the Python side's ``ApprovalRecord``
    (``call_id``, ``args_preview``, ``created_at``) — the Rust side's
    flat ``SqliteStore::PendingApproval`` shape carried different
    column names (``id``, ``args_json``, ``requested_at``). UI clients
    of the Python plane should consume this Python-native shape; the
    ``ui/lib/api.ts`` ``Approval`` interface maps 1:1 onto it (W3-4).

    ``decision`` is normalized to the wire vocabulary
    ``approved`` / ``denied`` / ``timeout`` (store-internal values are
    ``allow`` / ``deny`` / ``timeout``). ``decision_reason`` carries the
    decider's rationale; ``reason`` stays the request-side "why this
    needed approval".
    """

    call_id: str
    plugin: str
    tool: str
    session_key: str
    args_preview: str
    reason: str
    created_at: float
    decision: str | None = None
    decided_at: float | None = None
    decision_reason: str | None = None


class DecideBody(BaseModel):
    """``POST /admin/approvals/{call_id}/decide`` body.

    ``approve = True`` maps to :class:`ApprovalDecision.ALLOW`;
    ``approve = False`` to :class:`ApprovalDecision.DENY`. ``reason``
    (W3-4) is persisted into the row's ``decision_reason`` column and
    forwarded to the parked stream as the deny message."""

    approve: bool
    reason: str | None = None
    #: Disambiguates when the same provider call_id is parked by MORE
    #: than one concurrent stream (index-style ids collide). Optional —
    #: required only when the route reports 409 ambiguous.
    session_key: str | None = None


#: Store-internal decision values → the wire vocabulary the UI renders.
_WIRE_DECISION = {
    "allow": "approved",
    "deny": "denied",
    "timeout": "timeout",
    "prompt": "prompt",
}


def _wire_decision(value: str | None) -> str | None:
    """Normalize a store decision value for the wire (identity fallback)."""
    if value is None:
        return None
    return _WIRE_DECISION.get(value, value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _approvals_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "approvals_disabled",
            "message": "approval gate is not configured on this gateway",
        },
    )


def _require_store(state: AdminState) -> Any:
    """Return ``state.approval_store`` or raise the 503 envelope."""
    store = state.approval_store
    if store is None:
        raise _approvals_disabled()
    return store


def _record_to_out(record: Any) -> ApprovalOut:
    """Convert a :class:`ApprovalRecord` to the wire envelope."""
    decision = getattr(record, "decision", None)
    decided_at = getattr(record, "decided_at", None)
    return ApprovalOut(
        call_id=record.call_id,
        plugin=record.plugin,
        tool=record.tool,
        session_key=record.session_key,
        args_preview=record.args_preview,
        reason=record.reason,
        created_at=float(record.created_at),
        decision=_wire_decision(decision.value if decision is not None else None),
        decided_at=(float(decided_at) if decided_at is not None else None),
        decision_reason=getattr(record, "decision_reason", None),
    )


async def _list_decided(store: Any) -> list[Any]:
    """Decided rows via the store's public ``decided()`` API (W3-4).

    Returns an empty list when the store predates the API or the query
    fails, so ``include_decided=true`` degrades gracefully rather than
    raising. The old implementation reflected on the private
    ``store._conn`` and re-implemented the SQL — that path is gone.
    """
    decided = getattr(store, "decided", None)
    if not callable(decided):
        return []
    try:
        return list(await decided())
    except Exception:  # pragma: no cover — informational only
        return []


async def _sse_iter(
    store: Any, request: Request, *, poll_interval: float
) -> AsyncIterator[bytes]:
    """Poll-based SSE feed.

    The Rust side uses a broadcast bus on the ``ApprovalGate``; the
    Python ApprovalQueue doesn't expose one yet. We poll the store
    every ``poll_interval`` seconds and emit two event kinds matching
    the Rust ``ApprovalEvent::{Pending,Decided}`` enum:

    * ``data: {"kind": "pending", "approval": {...}}\\n\\n`` — when a
      row appears in ``pending()``.
    * ``data: {"kind": "decided", "id": ..., "decision": ...,
      "reason": ...}\\n\\n`` — when a previously pending row gains a
      decision. ``reason`` is the decider's rationale (W3-4), ``null``
      when none was given.

    Drops out cleanly when the client disconnects.
    """
    seen_pending: set[str] = set()
    seen_decided: set[str] = set()

    # Seed: emit the current pending backlog so a fresh subscriber
    # doesn't miss the queue. ``await store.pending()`` is cheap.
    backlog = await store.pending()
    for rec in backlog:
        seen_pending.add(rec.call_id)
        payload: dict[str, Any] = {
            "kind": "pending",
            "approval": _record_to_out(rec).model_dump(),
        }
        yield _sse_frame(payload)

    while True:
        if await request.is_disconnected():
            return
        try:
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            return
        try:
            pending = await store.pending()
        except Exception as exc:  # surface as an ``lag`` frame and bail
            yield _sse_frame({"kind": "lag", "error": str(exc)}, event="lag")
            return

        current_ids = {rec.call_id for rec in pending}
        # New pending rows.
        for rec in pending:
            if rec.call_id not in seen_pending:
                seen_pending.add(rec.call_id)
                payload = {
                    "kind": "pending",
                    "approval": _record_to_out(rec).model_dump(),
                }
                yield _sse_frame(payload)

        # Rows that *were* pending and no longer are = newly decided.
        newly_decided = seen_pending - current_ids - seen_decided
        for call_id in newly_decided:
            seen_decided.add(call_id)
            try:
                record = await store.get(call_id)
            except Exception:
                record = None
            decision = (
                getattr(getattr(record, "decision", None), "value", None)
                if record is not None
                else None
            )
            payload = {
                "kind": "decided",
                "id": call_id,
                "decision": _wire_decision(decision),
                "reason": getattr(record, "decision_reason", None),
            }
            yield _sse_frame(payload)


def _sse_frame(payload: dict[str, Any], *, event: str | None = None) -> bytes:
    """Encode a single ``data:`` line (optionally with an ``event:``
    label). Mirrors the Rust ``SseEvent::default().data(...)`` shape."""
    lines: list[str] = []
    if event is not None:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(payload, separators=(',', ':'))}")
    lines.append("")  # terminating blank line — required by the SSE spec
    return ("\n".join(lines) + "\n").encode("utf-8")
