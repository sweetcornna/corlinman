"""``/admin/personas/use-default`` — first-run "use default persona" hook.

This module is the admin_b half of the persona surface. The full
persona CRUD lives in ``routes_admin_a.personas`` (Persona Studio); this
file only adds the **B5** endpoint required by the first-run wizard so
the wizard can pick the built-in ``grantley`` persona without leaving
``routes_admin_b``.

B5 — ``POST /admin/personas/use-default`` (see
``docs/PLAN_FIRST_RUN_WIZARD.md``):

* Idempotent: ensure the built-in ``grantley`` persona row exists. The
  gateway lifecycle already auto-seeds it via
  :func:`corlinman_server.persona.seed_builtin_personas` — this endpoint
  re-runs the seeder defensively so a test harness that didn't run the
  full lifespan still gets a usable row.
* Marks the row as "active" — there is no global active-persona slot in
  the data model today (humanlike toggle is per-channel), so this is a
  no-op affirmation. The response payload is the load-bearing surface;
  the FE wizard records the choice and the channels surface picks up the
  persona id when the operator enables humanlike on a channel.

The route is also reachable from the onboard wizard's B3 branch via the
:func:`ensure_default_persona_active` worker so the side effects happen
exactly once per request.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.state import require_admin

# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class UseDefaultResponse(BaseModel):
    """B5 response payload."""

    status: str = "ok"
    persona_id: str = "grantley"


# ---------------------------------------------------------------------------
# Worker — also called by onboard.py B3 default-branch
# ---------------------------------------------------------------------------


async def ensure_default_persona_active() -> str:
    """Idempotently ensure the built-in ``grantley`` persona exists.

    Resolves the persona store from the admin_a state (where the
    gateway lifecycle opened it) and runs
    :func:`seed_builtin_personas` — which is a no-op when the row is
    already present. Returns the persona id so callers (the wizard, the
    ``/use-default-persona`` slash command, etc.) can echo it in their
    own response payloads.

    Raises an :class:`HTTPException` ``503`` when the persona store
    isn't wired (degraded boot). The seeder itself never raises on the
    idempotent path; a fresh-insert failure surfaces as ``500``.
    """
    from corlinman_server.persona import (
        DEFAULT_GRANTLEY_ID,
        seed_builtin_personas,
    )

    # The persona store lives on the admin_a state slot; the gateway
    # lifecycle populates it after :class:`PersonaStore.open` resolves.
    # We resolve through admin_a so the same singleton the admin_a CRUD
    # routes use is the one we mutate here.
    try:
        from corlinman_server.gateway.routes_admin_a.state import (
            get_admin_state as _get_admin_a_state,
        )
    except Exception as exc:  # pragma: no cover — admin_a missing
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "persona_store_missing",
                "message": "routes_admin_a unavailable",
            },
        ) from exc

    try:
        state_a = _get_admin_a_state()
    except RuntimeError as exc:
        # admin_a state not yet installed (e.g. degraded boot, or a
        # test harness that only built admin_b). Surface as 503 so the
        # FE wizard can show a clean "persona store not ready" message.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "persona_store_missing",
                "message": "routes_admin_a state not installed",
            },
        ) from exc
    store = getattr(state_a, "persona_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "persona_store_missing",
                "message": "gateway booted without a persona store",
            },
        )

    # Seeder is idempotent — existing row wins so operator edits stick.
    try:
        await seed_builtin_personas(store)
    except Exception as exc:  # noqa: BLE001 — surface as 500
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "seed_failed",
                "message": str(exc),
            },
        ) from exc

    # Affirmation read so the response payload reflects the actual row
    # rather than a hard-coded literal — catches the (rare) case where
    # the persona id constant drifted out from under us.
    row = await store.get(DEFAULT_GRANTLEY_ID)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "seed_failed",
                "message": (
                    "default persona missing after seed; check "
                    "default_grantley.md alongside the persona package"
                ),
            },
        )
    return row.id


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """B5 sub-router. Mirrors the admin_b convention: every route here
    runs under :func:`require_admin`."""
    r = APIRouter(
        dependencies=[Depends(require_admin)],
        tags=["admin", "personas"],
    )

    @r.post(
        "/admin/personas/use-default",
        response_model=UseDefaultResponse,
        summary="Activate the built-in grantley persona (idempotent)",
    )
    async def use_default_persona(
        _body: Annotated[dict | None, None] = None,
    ) -> UseDefaultResponse:
        # Body is reserved for future flags (e.g. ``{ scope: "channel:qq" }``)
        # — accept ``{}`` today so the FE can post an empty JSON object.
        del _body
        persona_id = await ensure_default_persona_active()
        return UseDefaultResponse(persona_id=persona_id)

    return r


__all__ = ["ensure_default_persona_active", "router"]
