"""``/admin/personas*`` + ``/admin/channels/qq/humanlike`` — persona registry CRUD.

A *persona* is a named system_prompt block. When the QQ OneBot channel's
``humanlike`` toggle is on with a chosen ``persona_id``, the channel
handler prepends the persona's ``system_prompt`` to the chat request so
the agent talks "in character".

Routes mount behind :func:`require_admin_dependency`. The persona store
lives at :class:`AdminState.persona_store` (a
:class:`corlinman_server.persona.PersonaStore`); the channels-config
TOML editor lives at :class:`AdminState.channels_config` /
``channels_writer`` exactly like the existing ``/admin/channels/qq/
keywords`` route.

Five persona-CRUD endpoints + two humanlike-toggle endpoints:

* ``GET    /admin/personas``                       — list every persona
* ``GET    /admin/personas/{id}``                  — fetch one (404 missing)
* ``POST   /admin/personas``                       — create (201)
* ``PATCH  /admin/personas/{id}``                  — partial update (404 missing)
* ``DELETE /admin/personas/{id}``                  — remove (404 missing/builtin)
* ``GET    /admin/channels/qq/humanlike``          — read live toggle
* ``PUT    /admin/channels/qq/humanlike``          — write toggle + persist
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)
from corlinman_server.persona import (
    Persona,
    PersonaError,
    PersonaExists,
    PersonaProtected,
    PersonaStore,
)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class PersonaOut(BaseModel):
    id: str
    display_name: str
    short_summary: str
    system_prompt: str
    is_builtin: bool
    created_at_ms: int
    updated_at_ms: int

    @classmethod
    def from_row(cls, p: Persona) -> "PersonaOut":
        return cls(
            id=p.id,
            display_name=p.display_name,
            short_summary=p.short_summary,
            system_prompt=p.system_prompt,
            is_builtin=p.is_builtin,
            created_at_ms=p.created_at_ms,
            updated_at_ms=p.updated_at_ms,
        )


class ListOut(BaseModel):
    personas: list[PersonaOut]


class CreateBody(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=200)
    short_summary: str = Field(default="", max_length=500)
    system_prompt: str = Field(min_length=1, max_length=200_000)


class PatchBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    short_summary: str | None = Field(default=None, max_length=500)
    system_prompt: str | None = Field(default=None, max_length=200_000)


class HumanlikeOut(BaseModel):
    enabled: bool
    persona_id: str | None


class HumanlikeIn(BaseModel):
    enabled: bool
    persona_id: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persona_store(state: AdminState) -> PersonaStore:
    store = state.persona_store
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "persona_store_missing",
                "message": "gateway booted without a persona store",
            },
        )
    return store


def _channels_writer(state: AdminState) -> Any:
    if state.channels_config is None or state.channels_writer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "channels_writer_missing",
                "message": "no writable channels config wired",
            },
        )
    return state.channels_writer


def _qq_humanlike_block(state: AdminState) -> dict[str, Any]:
    """Read the live ``[channels.qq.humanlike]`` block. Returns an empty
    dict when the channel or the sub-section is missing."""
    cfg = state.channels_config or {}
    qq = cfg.get("qq")
    if not isinstance(qq, dict):
        return {}
    hl = qq.get("humanlike")
    return hl if isinstance(hl, dict) else {}


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Persona CRUD + QQ humanlike toggle."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/personas",
        response_model=ListOut,
        summary="List every persona",
    )
    async def list_personas(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> ListOut:
        store = _persona_store(state)
        rows = await store.list()
        return ListOut(personas=[PersonaOut.from_row(p) for p in rows])

    @r.get(
        "/admin/personas/{persona_id}",
        response_model=PersonaOut,
        summary="Fetch one persona",
    )
    async def get_persona(
        persona_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> PersonaOut:
        store = _persona_store(state)
        p = await store.get(persona_id)
        if p is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "persona_not_found", "id": persona_id},
            )
        return PersonaOut.from_row(p)

    @r.post(
        "/admin/personas",
        response_model=PersonaOut,
        status_code=status.HTTP_201_CREATED,
        summary="Create a custom persona",
    )
    async def create_persona(
        body: CreateBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> PersonaOut:
        store = _persona_store(state)
        # Build a Persona dataclass — the store stamps timestamps + clears
        # is_builtin internally on the create() path.
        from corlinman_server.persona.store import _now_ms  # type: ignore[attr-defined]

        now = _now_ms()
        candidate = Persona(
            id=body.id,
            display_name=body.display_name,
            short_summary=body.short_summary,
            system_prompt=body.system_prompt,
            is_builtin=False,
            created_at_ms=now,
            updated_at_ms=now,
        )
        try:
            persona = await store.create(candidate)
        except PersonaExists as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "persona_exists", "id": body.id},
            ) from exc
        return PersonaOut.from_row(persona)

    @r.patch(
        "/admin/personas/{persona_id}",
        response_model=PersonaOut,
        summary="Update an existing persona",
    )
    async def patch_persona(
        persona_id: str,
        body: PatchBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> PersonaOut:
        store = _persona_store(state)
        try:
            persona = await store.update(
                persona_id,
                display_name=body.display_name,
                short_summary=body.short_summary,
                system_prompt=body.system_prompt,
            )
        except PersonaProtected as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "persona_protected", "id": persona_id},
            ) from exc
        except PersonaError as exc:
            # Base PersonaError fires when the row is missing (the store
            # intentionally avoided a dedicated NotFound subclass — see
            # the docstring on PersonaStore.update).
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "persona_not_found", "id": persona_id},
            ) from exc
        return PersonaOut.from_row(persona)

    @r.delete(
        "/admin/personas/{persona_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Delete a custom persona (builtins refused)",
    )
    async def delete_persona(
        persona_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> Response:
        store = _persona_store(state)
        try:
            removed = await store.delete(persona_id)
        except PersonaProtected as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "persona_builtin", "id": persona_id},
            ) from exc
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "persona_not_found", "id": persona_id},
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ----- QQ humanlike toggle ----------------------------------------

    @r.get(
        "/admin/channels/qq/humanlike",
        response_model=HumanlikeOut,
        summary="Read the QQ humanlike toggle",
    )
    async def get_qq_humanlike(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> HumanlikeOut:
        block = _qq_humanlike_block(state)
        return HumanlikeOut(
            enabled=bool(block.get("enabled", False)),
            persona_id=block.get("persona_id"),
        )

    @r.put(
        "/admin/channels/qq/humanlike",
        response_model=HumanlikeOut,
        summary="Set the QQ humanlike toggle (persists to config)",
    )
    async def put_qq_humanlike(
        body: HumanlikeIn,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> HumanlikeOut:
        if body.enabled and not body.persona_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "persona_id_required",
                    "message": "enabled=true requires a persona_id",
                },
            )
        # Verify the persona exists when set (catches typos before persist).
        if body.persona_id:
            store = _persona_store(state)
            if await store.get(body.persona_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "persona_not_found", "id": body.persona_id},
                )

        writer = _channels_writer(state)
        cfg = state.channels_config or {}
        qq = cfg.get("qq")
        if not isinstance(qq, dict):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "channel_not_configured",
                    "message": "no [channels.qq] section in config",
                },
            )
        qq["humanlike"] = {
            "enabled": bool(body.enabled),
            "persona_id": body.persona_id,
        }

        try:
            ret = writer(cfg)
            if inspect.isawaitable(ret):
                await ret
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "write_failed", "message": str(exc)},
            ) from exc

        return HumanlikeOut(
            enabled=bool(body.enabled),
            persona_id=body.persona_id,
        )

    return r


__all__ = ["router"]
