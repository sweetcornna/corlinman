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
from typing import Annotated, Literal

import structlog
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

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)

# Module-level wire shapes, constants, and helpers were extracted to the
# sibling ``_personas_lib`` module. Re-import every name ``router()`` and its
# handlers reference so this file stays focused on the route core.
from corlinman_server.gateway.routes_admin_a.studio._personas_lib import (
    SUPPORTED_HUMANLIKE_CHANNELS,
    AssetLabelPatch,
    AssetListOut,
    AssetOut,
    CreateBody,
    DecayOut,
    DiaryEntryOut,
    DiaryOut,
    HumanlikeIn,
    HumanlikeOut,
    LifeSeedsIn,
    LifeSeedsOut,
    LifeStateOut,
    LifeStatePatch,
    ListOut,
    OkOut,
    PatchBody,
    PersonaOut,
    _asset_store,
    _avatar_url_for,
    _channel_humanlike_block,
    _channels_writer,
    _life_state_db_path,
    _model_bindings_plain,
    _parse_iso_ms,
    _persona_store,
    _require_persona,
    _validate_channel_name,
    _validate_kind,
    _validate_label,
)
from corlinman_server.persona import (
    ALLOWED_MIMES,
    AssetMimeRejected,
    AssetQuotaExceeded,
    AssetTooLarge,
    Persona,
    PersonaError,
    PersonaExists,
    PersonaProtected,
)
from corlinman_server.persona.asset_store import (
    AssetExists,
    AssetNotFound,
)

logger = structlog.get_logger(__name__)


def _py_config_writer():
    from corlinman_server.gateway.lifecycle import write_py_config  # noqa: PLC0415

    return write_py_config


async def _refresh_sidecar_provider_registry_after_model_bindings() -> None:
    """Best-effort refresh for persona model/provider binding saves."""
    try:
        from corlinman_server.gateway.core.config_mutation import (  # noqa: PLC0415
            publish_config_mutation,
        )
        from corlinman_server.gateway.routes_admin_b import (  # noqa: PLC0415
            state as admin_b_state,
        )

        b_state = getattr(admin_b_state, "_state", None)
        if b_state is None:
            return
        lock = getattr(b_state, "admin_write_lock", None)
        if lock is None:
            cfg = dict(admin_b_state.config_snapshot(b_state))
            await publish_config_mutation(
                b_state,
                cfg,
                py_config_writer=_py_config_writer(),
            )
            return
        async with lock:
            cfg = dict(admin_b_state.config_snapshot(b_state))
            await publish_config_mutation(
                b_state,
                cfg,
                py_config_writer=_py_config_writer(),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "persona.model_bindings.py_config_refresh_failed",
            error=str(exc),
        )


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
        # ``persona_asset_store`` may be ``None`` on a degraded boot — the
        # avatar resolver returns ``None`` for every persona in that case
        # rather than 503ing the whole list.
        asset_store = state.persona_asset_store
        out: list[PersonaOut] = []
        for p in rows:
            avatar = await _avatar_url_for(asset_store, p.id)
            out.append(PersonaOut.from_row(p, avatar_url=avatar))
        return ListOut(personas=out)

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
        avatar = await _avatar_url_for(state.persona_asset_store, persona_id)
        return PersonaOut.from_row(p, avatar_url=avatar)

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
            model_bindings=_model_bindings_plain(body.model_bindings) or {},
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
        if body.model_bindings:
            await _refresh_sidecar_provider_registry_after_model_bindings()
        avatar = await _avatar_url_for(state.persona_asset_store, persona.id)
        return PersonaOut.from_row(persona, avatar_url=avatar)

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
                model_bindings=_model_bindings_plain(body.model_bindings),
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
        if body.model_bindings is not None:
            await _refresh_sidecar_provider_registry_after_model_bindings()
        avatar = await _avatar_url_for(state.persona_asset_store, persona_id)
        return PersonaOut.from_row(persona, avatar_url=avatar)

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
        description: Annotated[str | None, Form()] = None,
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
                description=description,
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

    @r.patch(
        "/admin/personas/{persona_id}/assets/{asset_id}",
        response_model=AssetOut,
        summary="Edit one asset's slot label and/or description",
    )
    async def patch_persona_asset(
        persona_id: str,
        asset_id: str,
        body: AssetLabelPatch,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> AssetOut:
        asset_store = _asset_store(state)
        if body.label is None and body.description is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "empty_patch",
                    "message": "provide label and/or description",
                },
            )
        # Confirm the asset both exists AND belongs to this persona before
        # the edit — same path-confusion guard the serve/delete routes use.
        record = await asset_store.get_by_id(asset_id)
        if record is None or record.persona_id != persona_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "asset_not_found", "id": asset_id},
            )
        updated = record
        try:
            if body.label is not None:
                label = _validate_label(body.label)
                updated = await asset_store.relabel_by_id(asset_id, label)
            if body.description is not None:
                updated = await asset_store.set_description_by_id(
                    asset_id, body.description
                )
        except AssetNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "asset_not_found", "id": asset_id},
            ) from exc
        except AssetExists as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "duplicate_label", "label": body.label},
            ) from exc
        return AssetOut.from_record(updated)

    # ----- Persona-liveness: life-state / diary / seeds / decay (R3) ---

    @r.get(
        "/admin/personas/{persona_id}/life-state",
        response_model=LifeStateOut,
        summary="Read a persona's runtime life-state row",
    )
    async def get_life_state(
        persona_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> LifeStateOut:
        await _require_persona(_persona_store(state), persona_id)
        # Lazy-import the runtime persona-STATE store — it lives in the
        # separately-packaged ``corlinman_persona`` distribution (the
        # ``agent_state.sqlite`` DB), NOT the gateway-internal PersonaStore.
        from corlinman_persona.store import (
            DEFAULT_TENANT_ID,
        )
        from corlinman_persona.store import (
            PersonaStore as StateStore,
        )

        db = _life_state_db_path(state)
        async with StateStore(db) as store:
            row = await store.get(persona_id, tenant_id=DEFAULT_TENANT_ID)
        if row is None:
            # No row yet → contract defaults (mood neutral, fatigue 0, …).
            return LifeStateOut(
                mood="neutral",
                fatigue=0.0,
                recent_topics=[],
                state_json={},
                updated_at_ms=0,
            )
        return LifeStateOut(
            mood=row.mood,
            fatigue=row.fatigue,
            recent_topics=list(row.recent_topics),
            state_json=row.state_json,
            updated_at_ms=row.updated_at_ms,
        )

    @r.patch(
        "/admin/personas/{persona_id}/life-state",
        response_model=LifeStateOut,
        summary="Upsert a persona's runtime life-state (manual seed/override)",
    )
    async def patch_life_state(
        persona_id: str,
        body: LifeStatePatch,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> LifeStateOut:
        await _require_persona(_persona_store(state), persona_id)
        from corlinman_persona.state import PersonaState
        from corlinman_persona.store import (
            DEFAULT_TENANT_ID,
        )
        from corlinman_persona.store import (
            PersonaStore as StateStore,
        )

        db = _life_state_db_path(state)
        async with StateStore(db) as store:
            current = await store.get(persona_id, tenant_id=DEFAULT_TENANT_ID)
            base = current or PersonaState(agent_id=persona_id)
            merged = PersonaState(
                agent_id=persona_id,
                mood=body.mood if body.mood is not None else base.mood,
                fatigue=(
                    body.fatigue if body.fatigue is not None else base.fatigue
                ),
                recent_topics=(
                    list(body.recent_topics)
                    if body.recent_topics is not None
                    else list(base.recent_topics)
                ),
                # ``upsert`` stamps "now" because we pass 0; this is a manual
                # edit so a fresh timestamp is correct.
                updated_at_ms=0,
                state_json=base.state_json,
            )
            await store.upsert(merged, tenant_id=DEFAULT_TENANT_ID)
            row = await store.get(persona_id, tenant_id=DEFAULT_TENANT_ID)
        # ``row`` is never None right after a successful upsert.
        assert row is not None
        return LifeStateOut(
            mood=row.mood,
            fatigue=row.fatigue,
            recent_topics=list(row.recent_topics),
            state_json=row.state_json,
            updated_at_ms=row.updated_at_ms,
        )

    @r.get(
        "/admin/personas/{persona_id}/diary",
        response_model=DiaryOut,
        summary="Read a persona's private diary tail",
    )
    async def get_diary(
        persona_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
        limit: int = 50,
    ) -> DiaryOut:
        await _require_persona(_persona_store(state), persona_id)
        from corlinman_persona.store import (
            DEFAULT_TENANT_ID,
        )
        from corlinman_persona.store import (
            PersonaStore as StateStore,
        )

        # Clamp the tail to a sane window so a huge ``limit`` can't fan out.
        tail = max(0, min(int(limit), 500))
        db = _life_state_db_path(state)
        async with StateStore(db) as store:
            row = await store.get(persona_id, tenant_id=DEFAULT_TENANT_ID)
        diary_raw = []
        if row is not None:
            raw = row.state_json.get("diary")
            if isinstance(raw, list):
                diary_raw = raw
        # Newest-last tail. Each record may carry ``entry`` (agent tool
        # shape) or ``text`` (operator-seeded shape); ``ts`` is ISO or int.
        sliced = diary_raw[-tail:] if tail else []
        entries: list[DiaryEntryOut] = []
        for rec in sliced:
            if not isinstance(rec, dict):
                continue
            text = str(rec.get("text") or rec.get("entry") or "")
            entries.append(
                DiaryEntryOut(ts=_parse_iso_ms(rec.get("ts")), text=text)
            )
        return DiaryOut(entries=entries)

    @r.get(
        "/admin/personas/{persona_id}/life-seeds",
        response_model=LifeSeedsOut,
        summary="Read a persona's effective event-seed pack (as YAML)",
    )
    async def get_life_seeds(
        persona_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> LifeSeedsOut:
        await _require_persona(_persona_store(state), persona_id)
        # Reuse the agent-side resolution so the admin view matches exactly
        # what ``persona_life_event_seed`` draws from at runtime (server may
        # import agent — see the import-linter layering contract).
        import yaml
        from corlinman_agent.persona.life import (
            _load_bundled_seeds,
            _override_seed_path,
            _resolve_seed_library,
        )

        data_dir = state.data_dir
        library = _resolve_seed_library(persona_id, data_dir)
        source: Literal["override", "bundled", "generic"]
        if data_dir is not None and _override_seed_path(
            persona_id, data_dir
        ).is_file():
            source = "override"
        elif _load_bundled_seeds(persona_id) is not None:
            source = "bundled"
        else:
            source = "generic"
        text = yaml.safe_dump(library, allow_unicode=True, sort_keys=False)
        return LifeSeedsOut(yaml=text, source=source)

    @r.put(
        "/admin/personas/{persona_id}/life-seeds",
        response_model=OkOut,
        summary="Write a persona's operator event-seed override (YAML)",
    )
    async def put_life_seeds(
        persona_id: str,
        body: LifeSeedsIn,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> OkOut:
        await _require_persona(_persona_store(state), persona_id)
        if state.data_dir is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "data_dir_missing",
                    "message": "gateway booted without a writable data dir",
                },
            )
        import yaml
        from corlinman_agent.persona.life import (
            _override_seed_path,
            _valid_persona_slug,
        )

        # Guard the slug before it's interpolated into a filename — mirrors
        # the agent-side ``persona_life_set_seeds`` write path.
        if not _valid_persona_slug(persona_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_persona_id",
                    "message": "persona_id must be a slug ([a-z0-9_-])",
                },
            )
        try:
            parsed = yaml.safe_load(body.yaml)
        except yaml.YAMLError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_yaml", "message": str(exc)},
            ) from exc
        # A valid-but-non-mapping body (a bare scalar / list) isn't a usable
        # seed pack — reject it the same way an unparseable body is.
        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_yaml",
                    "message": "seed pack must be a YAML mapping of "
                    "{category: [strings]}",
                },
            )
        path = _override_seed_path(persona_id, state.data_dir)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write via tmp+replace — same pattern the agent tool uses
            # so a crash mid-write never leaves a half-written override.
            tmp = path.with_suffix(".yaml.tmp")
            tmp.write_text(body.yaml, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "write_failed", "message": str(exc)},
            ) from exc
        return OkOut(ok=True)

    @r.post(
        "/admin/personas/{persona_id}/reset-to-default",
        response_model=OkOut,
        summary="Re-seed a built-in persona body from its default markdown",
    )
    async def reset_to_default(
        persona_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> OkOut:
        store = _persona_store(state)
        persona = await store.get(persona_id)
        if persona is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "persona_not_found", "id": persona_id},
            )
        if not persona.is_builtin:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "not_builtin",
                    "id": persona_id,
                    "message": "reset-to-default only applies to built-in "
                    "personas",
                },
            )
        # Today the sole built-in is ``grantley``; re-seed its body from the
        # shipped markdown. Future builtins would branch on ``persona_id``.
        from corlinman_server.persona import (
            DEFAULT_GRANTLEY_DISPLAY_NAME,
            DEFAULT_GRANTLEY_ID,
            DEFAULT_GRANTLEY_SUMMARY,
            load_default_grantley_body,
        )

        if persona_id != DEFAULT_GRANTLEY_ID:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "no_default_body",
                    "id": persona_id,
                    "message": "no shipped default body for this built-in",
                },
            )
        await store.update(
            persona_id,
            display_name=DEFAULT_GRANTLEY_DISPLAY_NAME,
            short_summary=DEFAULT_GRANTLEY_SUMMARY,
            system_prompt=load_default_grantley_body(),
        )
        return OkOut(ok=True)

    @r.post(
        "/admin/personas/{persona_id}/decay",
        response_model=DecayOut,
        summary="Run the mood/fatigue decay sweep for one persona's row",
    )
    async def run_decay(
        persona_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> DecayOut:
        await _require_persona(_persona_store(state), persona_id)
        import time

        from corlinman_persona.decay import DecayConfig, apply_decay
        from corlinman_persona.state import PersonaState
        from corlinman_persona.store import (
            DEFAULT_TENANT_ID,
        )
        from corlinman_persona.store import (
            PersonaStore as StateStore,
        )

        db = _life_state_db_path(state)
        now_ms = int(time.time() * 1000)
        config = DecayConfig()
        changed = 0
        async with StateStore(db) as store:
            row = await store.get(persona_id, tenant_id=DEFAULT_TENANT_ID)
            if row is not None:
                hours = max(0.0, (now_ms - row.updated_at_ms) / 3_600_000.0)
                decayed = apply_decay(row, hours, config)
                new_state = PersonaState(
                    agent_id=decayed.agent_id,
                    mood=decayed.mood,
                    fatigue=decayed.fatigue,
                    recent_topics=decayed.recent_topics,
                    # Stamp "now" so a re-run doesn't double-count elapsed time.
                    updated_at_ms=now_ms,
                    state_json=decayed.state_json,
                )
                await store.upsert(new_state, tenant_id=DEFAULT_TENANT_ID)
                if (
                    new_state.mood != row.mood
                    or new_state.fatigue != row.fatigue
                    or new_state.recent_topics != row.recent_topics
                ):
                    changed = 1
        return DecayOut(rows_changed=changed)

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


__all__ = ["SUPPORTED_HUMANLIKE_CHANNELS", "router"]
