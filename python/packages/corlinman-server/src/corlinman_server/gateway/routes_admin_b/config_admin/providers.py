"""``/admin/providers*`` — provider registry CRUD.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/providers.rs``.

Routes:

* ``GET    /admin/providers``              — list every declared slot
  (kind, api-key source, ``params_schema``).
* ``POST   /admin/providers``              — upsert a provider slot.
* ``PATCH  /admin/providers/{name}``       — partial update.
* ``DELETE /admin/providers/{name}``       — refused with 409 when an
  alias or the ``[embedding]`` block still references it.
* ``POST   /admin/providers/{name}/test``  — zero-cost connectivity probe
  (W1.1). Returns ``{ok, latency_ms, error?, models_count?}``.
* ``GET    /admin/providers/{name}/models``— list models exposed by a
  provider (W1.1). 30s in-memory cache for openai-shape proxies;
  hardcoded catalogs for anthropic / google / mock.
* ``GET    /admin/providers/kinds``        — descriptor list of every
  registered :class:`ProviderKind` (W1.1) — ``{kinds: [{kind, label,
  description, params_schema}]}``.

JSON-schema for ``params`` is pulled lazily from
``corlinman_providers`` (sibling package) so the Python source stays the
single source of truth — mirrors the Rust note that "Python wins" on
schema drift.
"""

from __future__ import annotations

import time
from typing import Any

from corlinman_providers.specs import list_supported_kinds
from fastapi import APIRouter, Depends
from fastapi import Path as FPath
from fastapi.responses import JSONResponse, Response

from corlinman_server.gateway.core.config_mutation import (
    publish_config_mutation as _publish_config_mutation_core,
)
from corlinman_server.gateway.core.config_mutation import (
    write_config_atomic as _write_config_atomic,
)
from corlinman_server.gateway.routes_admin_b.config_admin._providers_lib import (
    _BUILTIN_SLOTS,
    _FISH_TTS_MODELS,
    _HARDCODED_MODELS,
    _KIND_LABELS,
    _MODELS_CACHE,
    _MODELS_CACHE_TTL_SECONDS,
    _SLUG_RE,
    Capabilities,
    CustomListOut,
    CustomProviderCreate,
    CustomProviderPatch,
    CustomProviderView,
    KindDescriptor,
    ListOut,
    ProviderModelProbe,
    ProviderPatch,
    ProviderUpsert,
    ProviderView,
    _autobind_default_alias,
    _bad,
    _clear_models_cache,
    _custom_view_from_entry,
    _find_alias_refs,
    _fish_tts_reference_id,
    _is_known_kind,
    _kind_capabilities,
    _normalize_kind,
    _params_schema_for,
    _persist,
    _provider_tts_backend,
    _query_provider_models,
    _query_provider_models_with_retry,
    _redact,
    _remove_model_refs,
    _resolve_api_key,
    _view_from_entry,
    _zero_cost_probe_kind,
)
from corlinman_server.gateway.routes_admin_b.state import (
    config_snapshot,
    get_admin_state,
    require_admin,
)


def _py_config_writer():
    from corlinman_server.gateway.lifecycle import write_py_config  # noqa: PLC0415

    return write_py_config


async def _publish_config_mutation(state: Any, cfg: dict[str, Any]) -> None:
    await _publish_config_mutation_core(
        state,
        cfg,
        py_config_writer=_py_config_writer(),
    )

# Re-export the moved wire models / helpers so external importers (and tests)
# that do ``from ...config_admin.providers import <name>`` keep working after
# the god-file split into ``_providers_lib``. Names not referenced directly by
# ``router()`` (e.g. ``_clear_models_cache``, accessed only by tests) are
# listed here so they are not pruned as unused re-imports.
__all__ = [
    "_BUILTIN_SLOTS",
    "_HARDCODED_MODELS",
    "_KIND_LABELS",
    "_MODELS_CACHE",
    "_MODELS_CACHE_TTL_SECONDS",
    "_SLUG_RE",
    "Capabilities",
    "CustomListOut",
    "CustomProviderCreate",
    "CustomProviderPatch",
    "CustomProviderView",
    "KindDescriptor",
    "ListOut",
    "ProviderModelProbe",
    "ProviderPatch",
    "ProviderUpsert",
    "ProviderView",
    "_autobind_default_alias",
    "_bad",
    "_clear_models_cache",
    "_custom_view_from_entry",
    "_find_alias_refs",
    "_is_known_kind",
    "_kind_capabilities",
    "_normalize_kind",
    "_params_schema_for",
    "_persist",
    "_query_provider_models",
    "_query_provider_models_with_retry",
    "_redact",
    "_resolve_api_key",
    "_view_from_entry",
    "_zero_cost_probe_kind",
    "router",
]

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "providers"])

    @r.get("/admin/providers", response_model=ListOut)
    async def list_providers():
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        providers: list[ProviderView] = []
        if isinstance(providers_cfg, dict):
            for name, entry in providers_cfg.items():
                if isinstance(entry, dict):
                    providers.append(_view_from_entry(str(name), entry))
        providers.sort(key=lambda p: p.name)
        kinds = [
            KindDescriptor(
                kind=k, params_schema=_params_schema_for(k), capabilities=_kind_capabilities(k)
            )
            for k in list_supported_kinds()
        ]
        return ListOut(providers=providers, kinds=kinds)

    @r.post("/admin/providers")
    async def upsert_provider(body: ProviderUpsert):
        if not body.name:
            return _bad("invalid_name", "provider name must be non-empty")
        normalized_kind = _normalize_kind(body.kind)
        if not _is_known_kind(normalized_kind):
            return _bad("invalid_kind", f"unknown provider kind: {body.kind}")
        state = get_admin_state()
        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = dict(providers.get(body.name) or {})
            existing["kind"] = normalized_kind
            if body.enabled is not None:
                existing["enabled"] = body.enabled
            elif "enabled" not in existing:
                existing["enabled"] = True
            if body.base_url is not None:
                existing["base_url"] = body.base_url
            if body.api_key is not None:
                existing["api_key"] = body.api_key
            if body.params is not None:
                existing["params"] = body.params
            elif "params" not in existing:
                existing["params"] = {}
            providers[body.name] = existing
            cfg["providers"] = providers
            if bool(existing.get("enabled", True)):
                cfg = await _autobind_default_alias(cfg, body.name, existing)
            err = await _persist(
                state,
                cfg,
                py_config_writer=_py_config_writer(),
            )
            if err is not None:
                return err
        return {"status": "ok", "provider": _view_from_entry(body.name, existing).model_dump()}

    @r.post("/admin/providers/probe-models")
    async def probe_provider_models(body: ProviderModelProbe) -> Any:
        """List models for a draft provider config without persisting it.

        Used by the Add/Edit provider dialog. This intentionally avoids
        touching the TOML-backed ``providers`` map; it builds a transient
        in-memory config and reuses the same safe model-discovery path as
        ``GET /admin/providers/{name}/models``.
        """
        normalized_kind = _normalize_kind(body.kind)
        if not _is_known_kind(normalized_kind):
            return _bad("invalid_kind", f"unknown provider kind: {body.kind}")

        entry: dict[str, Any] = {
            "kind": normalized_kind,
            "enabled": True,
            "params": dict(body.params or {}),
        }
        if body.base_url is not None:
            entry["base_url"] = body.base_url
        if body.api_key is not None:
            entry["api_key"] = dict(body.api_key)
        elif body.existing_name:
            providers_cfg = config_snapshot().get("providers") or {}
            existing = (
                providers_cfg.get(body.existing_name)
                if isinstance(providers_cfg, dict)
                else None
            )
            existing_api_key = (
                existing.get("api_key") if isinstance(existing, dict) else None
            )
            if (
                isinstance(existing_api_key, dict)
                and isinstance(existing_api_key.get("value"), str)
                and existing_api_key.get("value")
            ):
                entry["api_key"] = {"value": existing_api_key["value"]}
            elif isinstance(existing_api_key, str) and existing_api_key:
                entry["api_key"] = {"value": existing_api_key}

        probe_strategy = _zero_cost_probe_kind(normalized_kind)
        if probe_strategy in ("mock", "hardcoded"):
            return {"models": list(_HARDCODED_MODELS.get(normalized_kind, []))}
        if probe_strategy != "openai_models":
            return {
                "models": [],
                "error": f"kind {normalized_kind!r} has no model-discovery endpoint",
            }

        draft_name = "__draft__"
        result = await _query_provider_models_with_retry(
            draft_name,
            {"providers": {draft_name: entry}},
        )
        api_key = _resolve_api_key(entry)
        if not result.get("ok"):
            err = _redact(str(result.get("error") or "upstream_error"), api_key)
            return {"models": [], "error": err}
        models = [
            {"id": mid, "display_name": mid}
            for mid in (result.get("models") or [])
            if isinstance(mid, str)
        ]
        return {"models": models}

    @r.patch("/admin/providers/{name}")
    async def patch_provider(name: str, body: ProviderPatch):
        state = get_admin_state()
        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(name)
            if existing is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "resource": "provider", "id": name},
                )
            entry = dict(existing)
            if body.kind is not None:
                normalized_kind = _normalize_kind(body.kind)
                if not _is_known_kind(normalized_kind):
                    return _bad("invalid_kind", f"unknown provider kind: {body.kind}")
                entry["kind"] = normalized_kind
            if body.enabled is not None:
                entry["enabled"] = body.enabled
            if body.base_url is not None:
                entry["base_url"] = body.base_url
            if body.api_key is not None:
                entry["api_key"] = body.api_key
            if body.params is not None:
                entry["params"] = body.params
            providers[name] = entry
            cfg["providers"] = providers
            if bool(entry.get("enabled", True)):
                cfg = await _autobind_default_alias(cfg, name, entry)
            err = await _persist(
                state,
                cfg,
                py_config_writer=_py_config_writer(),
            )
            if err is not None:
                return err
        return {"status": "ok", "provider": _view_from_entry(name, entry).model_dump()}

    @r.delete("/admin/providers/{name}")
    async def delete_provider(name: str):
        state = get_admin_state()
        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            if name not in providers:
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "resource": "provider", "id": name},
                )
            alias_refs = _find_alias_refs(cfg, name)
            emb = cfg.get("embedding") or {}
            emb_ref = emb.get("provider") == name
            if alias_refs or emb_ref:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "provider_in_use",
                        "alias_refs": alias_refs,
                        "embedding_uses": emb_ref,
                    },
                )
            providers.pop(name)
            cfg["providers"] = providers
            cfg = _remove_model_refs(cfg, name)
            err = await _persist(
                state,
                cfg,
                py_config_writer=_py_config_writer(),
            )
            if err is not None:
                return err
        return {"status": "ok", "removed": name}

    # -----------------------------------------------------------------
    # W-B1 — custom-provider CRUD
    #
    # Operators add ad-hoc providers via the admin UI by submitting
    # ``{slug, kind, base_url, api_key, params}``. The endpoint writes a
    # ``[providers.<slug>]`` block tagged ``params.custom = true`` — that
    # marker is the load-bearing distinction between user-added entries
    # (manageable through this surface) and built-in slots
    # (anthropic / openai / google / mock — owned by the credentials
    # surface). See ``docs/PLAN_PROVIDER_AUTH.md`` §1.2.
    # -----------------------------------------------------------------

    @r.get("/admin/providers/kinds")
    async def list_provider_kinds() -> dict[str, Any]:
        """W1.1 — descriptor list of every registered provider kind.

        Returns ``{kinds: [{kind, label, description, params_schema}]}``
        where ``params_schema`` is the per-kind adapter
        :meth:`params_schema` value resolved through ``_params_schema_for``.
        Order is the same alphabetical order as
        :func:`list_supported_kinds`.
        """
        items: list[dict[str, Any]] = []
        for kind in list_supported_kinds():
            label, description = _KIND_LABELS.get(
                kind, (kind.replace("_", " ").title(), "")
            )
            items.append(
                {
                    "kind": kind,
                    "label": label,
                    "description": description,
                    "params_schema": _params_schema_for(kind),
                }
            )
        return {"kinds": items}

    @r.get("/admin/providers/custom", response_model=CustomListOut)
    async def list_custom_providers() -> CustomListOut:
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        items: list[CustomProviderView] = []
        if isinstance(providers_cfg, dict):
            for slug, entry in providers_cfg.items():
                if not isinstance(entry, dict):
                    continue
                params = entry.get("params") or {}
                if not (isinstance(params, dict) and params.get("custom") is True):
                    continue
                items.append(_custom_view_from_entry(str(slug), entry))
        items.sort(key=lambda v: v.slug)
        return CustomListOut(providers=items)

    @r.post("/admin/providers/custom")
    async def create_custom_provider(body: CustomProviderCreate):
        if not _SLUG_RE.match(body.slug):
            return _bad("invalid_slug", "slug must match ^[a-z0-9][a-z0-9_-]{0,31}$")
        if body.slug in _BUILTIN_SLOTS:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "builtin_slot",
                    "message": f"slug {body.slug!r} is reserved for a built-in provider",
                    "slug": body.slug,
                },
            )
        normalized_kind = _normalize_kind(body.kind)
        if not _is_known_kind(normalized_kind):
            return _bad("invalid_kind", f"unknown provider kind: {body.kind}")

        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(status_code=503, content={"error": "config_path_unset"})

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            if body.slug in providers:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "slug_exists",
                        "message": f"provider {body.slug!r} already exists",
                        "slug": body.slug,
                    },
                )
            entry: dict[str, Any] = {
                "kind": normalized_kind,
                "enabled": True,
            }
            if body.base_url is not None:
                entry["base_url"] = body.base_url
            if body.api_key is not None:
                entry["api_key"] = dict(body.api_key)
            params = dict(body.params or {})
            params["custom"] = True
            entry["params"] = params

            providers[body.slug] = entry
            cfg["providers"] = providers
            if _provider_tts_backend(entry) != "fish":
                cfg = await _autobind_default_alias(cfg, body.slug, entry)
            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err
            await _publish_config_mutation(state, cfg)

        view = _custom_view_from_entry(body.slug, entry)
        return JSONResponse(status_code=201, content=view.model_dump())

    @r.patch("/admin/providers/custom/{slug}")
    async def patch_custom_provider(
        body: CustomProviderPatch,
        slug: str = FPath(..., min_length=1),
    ):
        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(status_code=503, content={"error": "config_path_unset"})

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(slug)
            if not isinstance(existing, dict):
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "resource": "provider", "id": slug},
                )
            params = existing.get("params") or {}
            if not (isinstance(params, dict) and params.get("custom") is True):
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": "not_custom",
                        "message": f"provider {slug!r} is not a custom slot",
                        "id": slug,
                    },
                )

            entry = dict(existing)
            if body.kind is not None:
                normalized_kind = _normalize_kind(body.kind)
                if not _is_known_kind(normalized_kind):
                    return _bad("invalid_kind", f"unknown provider kind: {body.kind}")
                entry["kind"] = normalized_kind
            if body.base_url is not None:
                entry["base_url"] = body.base_url
            if body.api_key is not None:
                entry["api_key"] = dict(body.api_key)
            if body.params is not None:
                merged_params = dict(body.params)
                merged_params["custom"] = True
                entry["params"] = merged_params
            else:
                # Make sure the marker survives even if a caller dropped
                # the params block from a prior write.
                existing_params = dict(entry.get("params") or {})
                existing_params["custom"] = True
                entry["params"] = existing_params

            providers[slug] = entry
            cfg["providers"] = providers
            if _provider_tts_backend(entry) == "fish":
                cfg = _remove_model_refs(cfg, slug)
            elif bool(entry.get("enabled", True)):
                cfg = await _autobind_default_alias(cfg, slug, entry)
            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err
            await _publish_config_mutation(state, cfg)

        view = _custom_view_from_entry(slug, entry)
        return JSONResponse(status_code=200, content=view.model_dump())

    @r.delete("/admin/providers/custom/{slug}")
    async def delete_custom_provider(slug: str = FPath(..., min_length=1)):
        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(status_code=503, content={"error": "config_path_unset"})

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(slug)
            if not isinstance(existing, dict):
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "resource": "provider", "id": slug},
                )
            params = existing.get("params") or {}
            if not (isinstance(params, dict) and params.get("custom") is True):
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": "not_custom",
                        "message": f"provider {slug!r} is not a custom slot",
                        "id": slug,
                    },
                )
            providers.pop(slug)
            cfg["providers"] = providers
            cfg = _remove_model_refs(cfg, slug)
            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err
            await _publish_config_mutation(state, cfg)

        return Response(status_code=204)

    @r.post("/admin/providers/{name}/test")
    async def test_provider(name: str) -> dict[str, Any]:
        """W1.1 — zero-cost connectivity probe for a configured provider.

        Returns ``{ok: bool, latency_ms: int, error?: str,
        models_count?: int}``. Strategy per kind:

        * ``mock``                     — instant ok, ``models_count=1``.
        * openai / openai-compatible   — ``GET <base>/v1/models``.
        * anthropic / google / etc.    — no free upstream probe; return
                                         ``ok=True`` with a hardcoded
                                         catalog count to signal "config
                                         shape is valid" without burning
                                         tokens. The UI can label this
                                         as "configured" rather than
                                         "verified live".
        * unknown                      — ``ok=False`` with diagnostic
                                         error.

        Every error message is run through :func:`_redact` so the api key
        never leaks into the response (or, by extension, the access log).
        Caps total latency at 5s via httpx timeout.
        """
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        entry = providers_cfg.get(name)

        # Resolve kind. Codex is special-cased — it has no config entry.
        if entry is None and name != "codex":
            return {
                "ok": False,
                "latency_ms": 0,
                "error": "provider_not_found",
            }

        if name == "codex":
            kind = "codex"
        else:
            kind = _normalize_kind(str((entry or {}).get("kind") or "openai_compatible"))

        probe_strategy = _zero_cost_probe_kind(kind)
        api_key = _resolve_api_key(entry or {})

        if _provider_tts_backend(entry if isinstance(entry, dict) else {}) == "fish":
            if not api_key:
                return {
                    "ok": False,
                    "latency_ms": 0,
                    "error": "fish_audio_api_key_missing",
                }
            if not _fish_tts_reference_id(entry if isinstance(entry, dict) else {}):
                return {
                    "ok": False,
                    "latency_ms": 0,
                    "error": "fish_audio_reference_id_missing",
                }
            return {
                "ok": True,
                "latency_ms": 0,
                "models_count": len(_FISH_TTS_MODELS),
                "note": "Fish Audio TTS provider; /v1/models probe skipped",
            }

        if probe_strategy == "mock":
            return {"ok": True, "latency_ms": 0, "models_count": 1}

        if probe_strategy == "openai_models":
            # Reuse the legacy helper, then reshape with a 5s cap.
            import asyncio as _asyncio

            t0 = time.monotonic()
            try:
                result = await _asyncio.wait_for(
                    _query_provider_models(name, cfg), timeout=5.0
                )
            except TimeoutError:
                latency_ms = int((time.monotonic() - t0) * 1000)
                return {"ok": False, "latency_ms": latency_ms, "error": "timeout"}
            latency_ms = int(result.get("latency_ms") or 0)
            if result.get("ok"):
                return {
                    "ok": True,
                    "latency_ms": latency_ms,
                    "models_count": len(result.get("models") or []),
                }
            err = _redact(str(result.get("error") or "upstream_error"), api_key)
            return {"ok": False, "latency_ms": latency_ms, "error": err}

        if probe_strategy == "hardcoded":
            # No free upstream probe — surface as ok so the operator sees
            # green for a well-formed config; the dropdown below will
            # serve the canned catalog. NOT a real liveness check.
            return {
                "ok": True,
                "latency_ms": 0,
                "models_count": len(_HARDCODED_MODELS.get(kind, [])),
                "note": "no zero-cost upstream probe; config-shape only",
            }

        return {
            "ok": False,
            "latency_ms": 0,
            "error": f"no zero-cost probe; configure provider kind {kind!r} to enable testing",
        }

    @r.get("/admin/providers/{name}/models")
    async def list_provider_models(name: str) -> dict[str, Any]:
        """W1.1 — list models a provider exposes.

        Returns ``{models: [{id, display_name?, created_at?}]}``. For
        openai-shape providers we proxy ``GET <base>/v1/models`` with a
        30s in-memory cache. For providers with a known fixed catalog
        (anthropic, google, mock) we serve the canned list from
        :data:`_HARDCODED_MODELS`. On transient upstream failures we
        retry and then fall back to the most recent cached success for
        that provider (if any), marked with ``stale=true``.
        """
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        entry = providers_cfg.get(name)

        if entry is None and name != "codex":
            return {"models": [], "error": "provider_not_found"}

        if name == "codex":
            kind = "codex"
        else:
            kind = _normalize_kind(str((entry or {}).get("kind") or "openai_compatible"))

        if _provider_tts_backend(entry if isinstance(entry, dict) else {}) == "fish":
            return {"models": list(_FISH_TTS_MODELS)}

        probe_strategy = _zero_cost_probe_kind(kind)

        if probe_strategy in ("mock", "hardcoded"):
            return {"models": list(_HARDCODED_MODELS.get(kind, []))}

        if probe_strategy != "openai_models":
            return {
                "models": [],
                "error": f"kind {kind!r} has no model-discovery endpoint",
            }

        # Cache lookup.
        now = time.monotonic()
        cached = _MODELS_CACHE.get(name)
        if cached is not None and cached[0] > now:
            return dict(cached[1])

        result = await _query_provider_models_with_retry(name, cfg)
        api_key = _resolve_api_key(entry or {})
        if not result.get("ok"):
            err = _redact(str(result.get("error") or "upstream_error"), api_key)
            if cached is not None:
                stale_payload = dict(cached[1])
                stale_payload["stale"] = True
                stale_payload["warning"] = err
                return stale_payload
            # Don't cache failures — operator likely just fixed the key.
            return {"models": [], "error": err}

        models = [
            {"id": mid, "display_name": mid}
            for mid in (result.get("models") or [])
            if isinstance(mid, str)
        ]
        payload: dict[str, Any] = {"models": models}
        _MODELS_CACHE[name] = (now + _MODELS_CACHE_TTL_SECONDS, payload)
        return dict(payload)

    return r
