"""``/admin/personas*`` + ``/admin/channels/{channel}/humanlike`` — persona registry CRUD.

A *persona* is a named system_prompt block. When a channel's
``humanlike`` toggle is on with a chosen ``persona_id``, the channel
handler prepends the persona's ``system_prompt`` (plus its
W7 emoji block) to the chat request so the agent talks "in character".

Routes mount behind :func:`require_admin_dependency`. The persona store
lives at :class:`AdminState.persona_store` (a
:class:`corlinman_server.persona.PersonaStore`); the channels-config
TOML editor lives at :class:`AdminState.channels_config` /
``channels_writer`` exactly like the existing ``/admin/channels/qq/
keywords`` route.

Five persona-CRUD endpoints + generic humanlike-toggle endpoints:

* ``GET    /admin/personas``                            — list every persona
* ``GET    /admin/personas/{id}``                       — fetch one (404 missing)
* ``POST   /admin/personas``                            — create (201)
* ``PATCH  /admin/personas/{id}``                       — partial update (404 missing)
* ``DELETE /admin/personas/{id}``                       — remove (404 missing/builtin)
* ``GET    /admin/channels/{channel}/humanlike``        — read live toggle
* ``PUT    /admin/channels/{channel}/humanlike``        — write toggle + persist

The humanlike routes are generalised to all five humanlike-capable
channels: ``qq``, ``telegram``, ``discord``, ``slack``, ``feishu``.
Unknown ``{channel}`` slugs 404 ``unknown_channel``.
"""

from __future__ import annotations

import inspect
import re
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)
from corlinman_server.persona import (
    ALLOWED_MIMES,
    AssetKind,
    AssetMimeRejected,
    AssetQuotaExceeded,
    AssetRecord,
    AssetTooLarge,
    Persona,
    PersonaAssetStore,
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


class AssetOut(BaseModel):
    """Wire view of one persona asset record."""

    id: str
    persona_id: str
    kind: Literal["emoji", "reference"]
    label: str
    file_name: str
    mime: str
    size_bytes: int
    sha256: str
    created_at_ms: int
    # Convenience URL the UI uses directly in <img src=…>.
    url: str

    @classmethod
    def from_record(cls, r: AssetRecord) -> "AssetOut":
        return cls(
            id=r.id,
            persona_id=r.persona_id,
            kind=r.kind,
            label=r.label,
            file_name=r.file_name,
            mime=r.mime,
            size_bytes=r.size_bytes,
            sha256=r.sha256,
            created_at_ms=r.created_at_ms,
            url=f"/admin/personas/{r.persona_id}/assets/{r.id}",
        )


class AssetListOut(BaseModel):
    assets: list[AssetOut]


# Slot label naming rule — same shape as persona ids. Forbidding
# slashes / dot-segments stops a malicious caller from prying open
# the persona dir structure via crafted labels.
_LABEL_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9_-]{1,64}$")


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


def _asset_store(state: AdminState) -> PersonaAssetStore:
    store = state.persona_asset_store
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "persona_asset_store_missing",
                "message": "gateway booted without a persona asset store",
            },
        )
    return store


async def _require_persona(
    persona_store: PersonaStore, persona_id: str
) -> None:
    """Raise 404 ``persona_not_found`` if the row doesn't exist. Used
    by every asset route so a typo in ``persona_id`` fails fast before
    we touch the asset store."""
    if await persona_store.get(persona_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "persona_not_found", "id": persona_id},
        )


def _validate_label(label: str) -> str:
    label = (label or "").strip()
    if not _LABEL_PATTERN.match(label):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_label",
                "message": "label must match [a-z0-9_-], 1-64 chars",
            },
        )
    return label


def _validate_kind(kind: str) -> AssetKind:
    if kind not in ("emoji", "reference"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_kind",
                "message": "kind must be 'emoji' or 'reference'",
            },
        )
    return kind  # type: ignore[return-value]


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
    dict when the channel or the sub-section is missing.

    Kept for the legacy QQ-only routes; new code routes through
    :func:`_channel_humanlike_block` which takes the channel name."""
    return _channel_humanlike_block(state, "qq")


#: Channels that support the humanlike system-prompt injection. WeChat
#: Official + QQ Official are intentionally excluded — the former is
#: webhook-only and doesn't currently surface a persona path, and the
#: latter does its own per-platform message formatting that doesn't sit
#: alongside the spinner / footer machinery this initiative depends on.
SUPPORTED_HUMANLIKE_CHANNELS: frozenset[str] = frozenset(
    {"qq", "telegram", "discord", "slack", "feishu"}
)


def _validate_channel_name(channel: str) -> str:
    """Reject channel slugs that aren't in :data:`SUPPORTED_HUMANLIKE_CHANNELS`
    with a 404 ``unknown_channel`` — matches the rest of the gateway's
    "unknown {thing}" error envelope shape."""
    if channel not in SUPPORTED_HUMANLIKE_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "unknown_channel",
                "channel": channel,
                "supported": sorted(SUPPORTED_HUMANLIKE_CHANNELS),
            },
        )
    return channel


def _channel_humanlike_block(
    state: AdminState, channel: str
) -> dict[str, Any]:
    """Read the live ``[channels.{channel}.humanlike]`` block. Returns
    an empty dict when the channel or the sub-section is missing — same
    "missing == disabled" semantics the resolver in channels_runtime
    relies on so a half-configured TOML never crashes the GET path."""
    cfg = state.channels_config or {}
    section = cfg.get(channel)
    if not isinstance(section, dict):
        return {}
    hl = section.get("humanlike")
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
        # Best-effort asset cleanup BEFORE the persona row goes — if
        # the asset store isn't wired or the cleanup raises, we still
        # remove the persona row so an operator can recover from a
        # half-broken deploy.
        if state.persona_asset_store is not None:
            try:
                await state.persona_asset_store.delete_all(persona_id)
            except Exception:  # noqa: BLE001 — never block deletion
                pass
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

    # ----- Persona asset CRUD (W1 Persona Studio) ---------------------

    @r.get(
        "/admin/personas/{persona_id}/assets",
        response_model=AssetListOut,
        summary="List a persona's emoji + reference assets",
    )
    async def list_persona_assets(
        persona_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
        kind: str | None = None,
    ) -> AssetListOut:
        await _require_persona(_persona_store(state), persona_id)
        assets = await _asset_store(state).list(
            persona_id, kind=_validate_kind(kind) if kind else None
        )
        return AssetListOut(
            assets=[AssetOut.from_record(a) for a in assets]
        )

    @r.post(
        "/admin/personas/{persona_id}/assets",
        response_model=AssetOut,
        status_code=status.HTTP_201_CREATED,
        summary="Upload one asset (emoji or reference image)",
    )
    async def upload_persona_asset(
        persona_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
        kind: Annotated[str, Form()],
        label: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
    ) -> AssetOut:
        persona_store = _persona_store(state)
        asset_store = _asset_store(state)
        await _require_persona(persona_store, persona_id)
        kind_v = _validate_kind(kind)
        label_v = _validate_label(label)

        mime = (file.content_type or "").lower()
        # Drop any charset / parameter suffix — Telegram-uploaded
        # images can land as ``image/jpeg; charset=binary``.
        mime = mime.split(";", 1)[0].strip()
        if mime not in ALLOWED_MIMES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={
                    "error": "unsupported_mime",
                    "received": mime,
                    "allowed": sorted(ALLOWED_MIMES),
                },
            )

        bytes_ = await file.read()
        try:
            record = await asset_store.put(
                persona_id,
                kind_v,
                label_v,
                bytes_=bytes_,
                mime=mime,
                file_name=(file.filename or f"{label_v}.bin")[:200],
            )
        except AssetMimeRejected as exc:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={"error": "unsupported_mime", "message": str(exc)},
            ) from exc
        except AssetTooLarge as exc:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={"error": "asset_too_large", "message": str(exc)},
            ) from exc
        except AssetQuotaExceeded as exc:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={"error": "quota_exceeded", "message": str(exc)},
            ) from exc
        return AssetOut.from_record(record)

    @r.get(
        "/admin/personas/{persona_id}/assets/{asset_id}",
        summary="Serve one asset blob (cacheable; ETag = sha256)",
    )
    async def serve_persona_asset(
        persona_id: str,
        asset_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> Response:
        asset_store = _asset_store(state)
        record = await asset_store.get_by_id(asset_id)
        if record is None or record.persona_id != persona_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "asset_not_found", "id": asset_id},
            )
        path = asset_store.path_for(record)
        if not path.is_file():
            # Metadata says the row exists but the blob got deleted
            # behind our back (manual ``rm``). 404 is the right shape
            # because a re-upload would heal it.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "asset_blob_missing", "id": asset_id},
            )
        return FileResponse(
            path,
            media_type=record.mime,
            filename=record.file_name,
            headers={
                "ETag": f'"{record.sha256}"',
                "Cache-Control": "public, max-age=86400, immutable",
            },
        )

    @r.delete(
        "/admin/personas/{persona_id}/assets/{asset_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Remove one asset",
    )
    async def delete_persona_asset(
        persona_id: str,
        asset_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> Response:
        asset_store = _asset_store(state)
        record = await asset_store.get_by_id(asset_id)
        if record is None or record.persona_id != persona_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "asset_not_found", "id": asset_id},
            )
        removed = await asset_store.delete_by_id(asset_id)
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "asset_not_found", "id": asset_id},
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ----- Generic per-channel humanlike toggle (W7) ------------------

    @r.get(
        "/admin/channels/{channel}/humanlike",
        response_model=HumanlikeOut,
        summary="Read a channel's humanlike toggle",
    )
    async def get_channel_humanlike(
        channel: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> HumanlikeOut:
        channel = _validate_channel_name(channel)
        block = _channel_humanlike_block(state, channel)
        return HumanlikeOut(
            enabled=bool(block.get("enabled", False)),
            persona_id=block.get("persona_id"),
        )

    @r.put(
        "/admin/channels/{channel}/humanlike",
        response_model=HumanlikeOut,
        summary="Set a channel's humanlike toggle (persists to config)",
    )
    async def put_channel_humanlike(
        channel: str,
        body: HumanlikeIn,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> HumanlikeOut:
        channel = _validate_channel_name(channel)
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
                    detail={
                        "error": "persona_not_found",
                        "id": body.persona_id,
                    },
                )

        writer = _channels_writer(state)
        cfg = state.channels_config or {}
        section = cfg.get(channel)
        # Auto-stub a missing section so an operator can flip the toggle
        # on for a channel they're about to configure — the channel
        # won't actually start until ``enabled=true`` lands on the
        # parent ``[channels.{channel}]`` table, so the stub is harmless
        # but lets the wizard write the humanlike block first.
        if not isinstance(section, dict):
            section = {}
            cfg[channel] = section
        section["humanlike"] = {
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


__all__ = ["router", "SUPPORTED_HUMANLIKE_CHANNELS"]
