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

        # Prefer the queue (wakes in-process waiters) when wired; fall
        # back to the store directly otherwise.
        target = state.approval_queue or store
        try:
            updated = await target.decide(call_id, decision)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "decide_failed", "message": str(exc)},
            ) from exc
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_found",
                    "resource": "approval",
                    "id": call_id,
                },
            )
        return {"id": call_id, "decision": decision.value}

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
