"""``/admin/approvals*`` — tool-approval queue admin endpoints.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/approvals.rs``.

Three routes (all behind :func:`require_admin_dependency`):

* ``GET  /admin/approvals?include_decided=false`` — JSON list backed by
  :class:`corlinman_providers.plugins.ApprovalStore`.
* ``POST /admin/approvals/{call_id}/decide`` — record an approve / deny
  decision and wake any in-process waiter via
  :class:`~corlinman_providers.plugins.ApprovalQueue`.
* ``GET  /admin/approvals/stream`` — Server-Sent Events feed of fresh
  ``pending`` / ``decided`` rows. Uses Starlette's
  :class:`fastapi.responses.StreamingResponse` because the Python
  ``ApprovalQueue`` doesn't ship a broadcast bus — we poll the store
  every ``poll_interval`` seconds and emit deltas.

When ``state.approval_store`` is ``None`` every route returns
**503 ``approvals_disabled``**, mirroring the Rust gate.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from corlinman_server.gateway.routes_admin_a._approvals_lib import (
    ApprovalOut,
    DecideBody,
    _approvals_disabled,
    _list_decided,
    _record_to_out,
    _require_store,
    _sse_iter,
    _wire_decision,
)
from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/approvals*``."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/approvals",
        response_model=list[ApprovalOut],
        summary="List pending (and optionally decided) approvals",
    )
    async def list_approvals(
        state: Annotated[AdminState, Depends(get_admin_state)],
        include_decided: Annotated[bool, Query()] = False,
    ) -> list[ApprovalOut]:
        # When the approval gate isn't wired, an EMPTY queue is the correct
        # operator view — there are no approvals to act on. Returning 200 []
        # (rather than 503) keeps the admin dashboard's periodic poll quiet
        # instead of spamming the browser console with a 503 on every tick.
        # The mutating routes (decide/decide-all) still 503: you can't act on
        # a gate that doesn't exist.
        if state.approval_store is None:
            return []
        store = _require_store(state)
        try:
            if include_decided:
                # The Python store doesn't ship a single "list everything"
                # method — fall back to two queries. The pending list is
                # the operator's primary view; the decided trickle is
                # informational so we tolerate the second round-trip.
                pending = await store.pending()
                rows = list(pending)
                # ``ApprovalStore`` doesn't expose a list-all helper
                # publicly; opportunistically use the underlying
                # connection when present so the operator sees both.
                rows.extend(await _list_decided(store))
            else:
                rows = await store.pending()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "storage_error", "message": str(exc)},
            ) from exc
        return [_record_to_out(rec) for rec in rows]

    @r.post(
        "/admin/approvals/{call_id}/decide",
        summary="Approve or deny a pending tool call",
    )
    async def decide_approval(
        call_id: str,
        body: DecideBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, str]:
        store = _require_store(state)
        # Resolve the ApprovalDecision enum lazily so a missing
        # ``corlinman_providers`` install doesn't break imports.
        try:
            from corlinman_providers.plugins import ApprovalDecision
        except ImportError as exc:  # pragma: no cover — providers always installed
            raise _approvals_disabled() from exc

        decision = (
            ApprovalDecision.ALLOW if body.approve else ApprovalDecision.DENY
        )

        # Resolve the PENDING row(s) for this call — provider call ids
        # collide across concurrent streams (composite keys everywhere,
        # W3-4 review fix), so a bare id may be ambiguous.
        try:
            candidates = await store.pending_for_call(call_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "decide_failed", "message": str(exc)},
            ) from exc
        if body.session_key:
            candidates = [
                r for r in candidates if r.session_key == body.session_key
            ]
        if not candidates:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_found",
                    "resource": "approval",
                    "id": call_id,
                },
            )
        if len(candidates) > 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "ambiguous_call_id",
                    "id": call_id,
                    "sessions": [r.session_key for r in candidates],
                    "message": "pass session_key to pick one",
                },
            )
        record = candidates[0]

        # DELIVER FIRST, persist after (W3-4 review fix): recording
        # "approved" for a call no stream will ever consume told the
        # operator a lie. Boot-time reconcile_orphaned() already swept
        # restart orphans to timeout, so an undeliverable pending row
        # here means its stream is tearing down right now — mark it
        # expired instead of approved.
        delivered = False
        try:
            from corlinman_server.gateway.services.approval_broker import (  # noqa: PLC0415
                get_approval_broker,
            )

            delivered = await get_approval_broker().decide(
                call_id,
                approved=body.approve,
                scope="once",
                deny_message=body.reason or "",
                session_key=record.session_key,
            )
        except Exception:  # noqa: BLE001 — treated as undeliverable
            delivered = False

        target = state.approval_queue or store
        if not delivered:
            try:
                await target.decide(
                    call_id,
                    ApprovalDecision.TIMEOUT,
                    reason="expired: no live stream for this approval",
                    session_key=record.session_key,
                )
            except Exception:  # noqa: BLE001 — expiry sweep is best-effort
                pass
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={
                    "error": "approval_expired",
                    "id": call_id,
                    "message": (
                        "no live stream is waiting on this approval; "
                        "the row was marked timeout"
                    ),
                },
            )
        try:
            updated = await target.decide(
                call_id,
                decision,
                reason=body.reason,
                session_key=record.session_key,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "decide_failed", "message": str(exc)},
            ) from exc
        if not updated:
            # The broker delivered but the row vanished mid-flight — the
            # stream's own persistence already recorded the decision.
            import logging  # noqa: PLC0415

            logging.getLogger(__name__).info(
                "admin approvals: row already decided call_id=%s", call_id
            )
        return {"id": call_id, "decision": _wire_decision(decision.value) or ""}

    @r.get(
        "/admin/approvals/stream",
        summary="SSE stream of pending / decided approval events",
    )
    async def stream_approvals(
        request: Request,
        state: Annotated[AdminState, Depends(get_admin_state)],
        poll_interval: Annotated[float, Query(ge=0.05, le=10.0)] = 0.5,
    ) -> StreamingResponse:
        store = _require_store(state)
        return StreamingResponse(
            _sse_iter(store, request, poll_interval=poll_interval),
            media_type="text/event-stream",
        )

    return r


__all__ = [
    "ApprovalOut",
    "DecideBody",
    "router",
]
