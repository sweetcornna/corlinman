"""``/admin/authz/grants*`` — durable-grant admin surface (W3-4).

Lists and revokes the ``always`` grants the
:class:`corlinman_agent.authz.grants.GrantStore` keeps in
``<data_dir>/authz/grants.sqlite3``. Two routes, both behind the admin
session gate:

* ``GET    /admin/authz/grants``  — every durable grant, newest first.
* ``DELETE /admin/authz/grants``  — revoke one grant by its exact key.

Cross-process contract: this runs in the *gateway* process while the
grants are consumed by the *agent* process. Both sides share the SQLite
file; a revocation here bumps the file's mtime, and the agent-side
GrantStore re-stats the file on every permission check, so the revoked
grant stops matching at the agent's next tool call (≤ next turn). See
the GrantStore docstring for the invalidation trade-offs.

A fresh ``GrantStore`` is built per request (reads go straight to
SQLite) — the gateway keeps no long-lived mirror that could go stale.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)

# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class GrantOut(BaseModel):
    """One durable (``always``) grant row.

    ``created_at`` is ``None`` for rows that exist only in the agent
    process's memory (SQLite write failed there) — visible for honesty,
    not revocable from here.
    """

    tenant: str
    surface: str
    user_id: str
    tool: str
    arg_digest: str
    created_at: float | None = None


class GrantKey(BaseModel):
    """``DELETE /admin/authz/grants`` body — the exact grant key."""

    tenant: str
    surface: str = ""
    user_id: str = ""
    tool: str
    arg_digest: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _grant_store(state: AdminState) -> Any:
    """A per-request GrantStore over the shared grants DB."""
    from corlinman_agent.authz.grants import GrantStore  # noqa: PLC0415

    return GrantStore(state.data_dir)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/authz/grants*``."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/authz/grants",
        response_model=list[GrantOut],
        summary="List durable (always) authorization grants",
    )
    async def list_grants(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> list[GrantOut]:
        try:
            rows = _grant_store(state).list_always()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "storage_error", "message": str(exc)},
            ) from exc
        return [GrantOut(**row) for row in rows]

    @r.delete(
        "/admin/authz/grants",
        summary="Revoke one durable grant by its exact key",
    )
    async def revoke_grant(
        body: GrantKey,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, Any]:
        try:
            existed = _grant_store(state).revoke_always_entry(
                tenant=body.tenant,
                surface=body.surface,
                user_id=body.user_id,
                tool=body.tool,
                arg_digest=body.arg_digest,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "storage_error", "message": str(exc)},
            ) from exc
        if not existed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_found",
                    "resource": "grant",
                    "tool": body.tool,
                },
            )
        return {"ok": True}

    return r


__all__ = ["GrantKey", "GrantOut", "router"]
