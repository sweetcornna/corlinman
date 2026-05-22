"""``/admin/providers*`` — provider registry CRUD.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/providers.rs``.

Routes:

* ``GET    /admin/providers``              — list every declared slot
  (kind, api-key source, ``params_schema``).
* ``POST   /admin/providers``              — upsert a provider slot.
* ``PATCH  /admin/providers/{name}``       — partial update.
* ``DELETE /admin/providers/{name}``       — refused with 409 when an
  alias or the ``[embedding]`` block still references it.

JSON-schema for ``params`` is pulled lazily from
``corlinman_providers`` (sibling package) so the Python source stays the
single source of truth — mirrors the Rust note that "Python wins" on
schema drift.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Path as FPath
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.onboard import _write_config_atomic
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class Capabilities(BaseModel):
    chat: bool = True
    embedding: bool = True


class ProviderView(BaseModel):
    name: str
    kind: str
    enabled: bool
    base_url: str | None = None
    api_key_source: str = "unset"
    api_key_env_name: str | None = None
    params: dict[str, Any] = {}
    params_schema: dict[str, Any] = {}
    capabilities: Capabilities = Capabilities()


class KindDescriptor(BaseModel):
    kind: str
    params_schema: dict[str, Any] = {}
    capabilities: Capabilities = Capabilities()


class ListOut(BaseModel):
    providers: list[ProviderView]
    kinds: list[KindDescriptor]


class ApiKeyEnv(BaseModel):
    env: str


class ApiKeyValue(BaseModel):
    value: str


class ProviderUpsert(BaseModel):
    name: str
    kind: str
    enabled: bool | None = None
    base_url: str | None = None
    api_key: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


class ProviderPatch(BaseModel):
    kind: str | None = None
    enabled: bool | None = None
    base_url: str | None = None
    api_key: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_KNOWN_KINDS = (
    "openai",
    "anthropic",
    "google",
    "openai-compatible",
    "deepseek",
    "glm",
    "qwen",
    "declarative",
)


def _kind_capabilities(kind: str) -> Capabilities:
    if kind == "anthropic":
        return Capabilities(chat=True, embedding=False)
    return Capabilities(chat=True, embedding=True)


def _params_schema_for(kind: str) -> dict[str, Any]:
    """Lazy lookup of ``corlinman_providers`` schema. Empty dict on miss."""
    try:
        from corlinman_providers import specs  # noqa: PLC0415

        getter = getattr(specs, "params_schema_for", None)
        if getter is not None:
            schema = getter(kind)
            if isinstance(schema, dict):
                return schema
    except (ImportError, AttributeError, Exception):  # noqa: BLE001
        pass
    return {"type": "object", "additionalProperties": True}


def _view_from_entry(name: str, entry: dict[str, Any]) -> ProviderView:
    api_key = entry.get("api_key")
    if api_key is None:
        source, env_name = "unset", None
    elif isinstance(api_key, dict) and "env" in api_key:
        source, env_name = "env", str(api_key["env"])
    elif isinstance(api_key, dict) and "value" in api_key:
        source, env_name = "value", None
    else:
        source, env_name = "value", None
    kind = str(entry.get("kind") or "openai-compatible").lower()
    return ProviderView(
        name=name,
        kind=kind,
        enabled=bool(entry.get("enabled", True)),
        base_url=entry.get("base_url"),
        api_key_source=source,
        api_key_env_name=env_name,
        params=dict(entry.get("params") or {}),
        params_schema=_params_schema_for(kind),
        capabilities=_kind_capabilities(kind),
    )


def _alias_target(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("model", ""))
    return ""


def _alias_provider(entry: Any) -> str | None:
    if isinstance(entry, dict):
        return entry.get("provider")
    return None


def _find_alias_refs(cfg: dict[str, Any], slot: str) -> list[str]:
    aliases = (cfg.get("models") or {}).get("aliases") or {}
    out: list[str] = []
    for name, entry in aliases.items():
        if _alias_provider(entry) == slot:
            out.append(str(name))
    return out


def _bad(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": code, "message": message})


async def _persist(state: AdminState, cfg: dict[str, Any]) -> JSONResponse | None:
    if state.config_path is None:
        return JSONResponse(status_code=503, content={"error": "config_path_unset"})
    try:
        try:
            import tomli_w  # noqa: PLC0415
        except ImportError:  # pragma: no cover
            import toml as tomli_w  # type: ignore  # noqa: PLC0415
        serialised = tomli_w.dumps(cfg)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"error": "serialise_failed", "message": str(exc)},
        )
    path = state.config_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".new")
        tmp.write_text(serialised, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={"error": "write_failed", "message": str(exc)},
        )
    return None


# ---------------------------------------------------------------------------
# W-B1 — custom-provider wire models + helpers
#
# These live alongside the legacy provider-slot CRUD above but address a
# different operator story: the "Add custom provider" form in
# ``ui/(admin)/providers``. The marker ``params.custom = true`` is what
# separates user-added blocks from built-in slots so the credentials UI
# can show them under their own group. See ``docs/PLAN_PROVIDER_AUTH.md``
# §1.2 for the on-disk shape.
# ---------------------------------------------------------------------------


# Lazy import to avoid a hard dependency cycle at module load — the
# providers package is a sibling and may be reshuffled. ImportError
# bubbles up as a 500 the first time a caller hits the kinds endpoint,
# which is the desired loud failure for a missing wire-up.
from corlinman_providers.specs import list_supported_kinds  # noqa: E402


# Slug regex pinned by the plan — lowercase ascii + digits, optionally
# separated by ``-`` or ``_``; 1–32 chars; first char alphanumeric.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


# Built-in slots managed by the credentials surface (or hardwired
# elsewhere). Operators cannot squat on these via the custom-provider
# endpoint — they must use ``/admin/credentials`` to configure them so
# the well-known UX (env-ref hints, masked previews) keeps working.
_BUILTIN_SLOTS: frozenset[str] = frozenset(
    {"anthropic", "openai", "google", "mock"}
)


class _ApiKeyEnvRef(BaseModel):
    env: str


class _ApiKeyValueRef(BaseModel):
    value: str


class CustomProviderView(BaseModel):
    """Read-side projection of one ``params.custom = true`` block."""

    slug: str
    kind: str
    base_url: str | None = None
    has_api_key: bool = False
    params: dict[str, Any] = {}


class CustomListOut(BaseModel):
    providers: list[CustomProviderView]


class CustomKindsOut(BaseModel):
    kinds: list[str]


class CustomProviderCreate(BaseModel):
    slug: str
    kind: str
    base_url: str | None = None
    api_key: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


class CustomProviderPatch(BaseModel):
    kind: str | None = None
    base_url: str | None = None
    api_key: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


def _custom_view_from_entry(slug: str, entry: dict[str, Any]) -> CustomProviderView:
    """Project a stored ``[providers.<slug>]`` block to the wire view.

    ``has_api_key`` follows the same masking convention as
    ``credentials._resolve_field_view``: any of literal string / ``{value=…}``
    / ``{env=…}`` shapes count as "set". We deliberately do NOT echo the
    literal back — the operator must re-paste to rotate (matches the
    paste-only edit story of the credentials UI).
    """
    api_key = entry.get("api_key")
    has_api_key = False
    if isinstance(api_key, str):
        has_api_key = bool(api_key)
    elif isinstance(api_key, dict):
        if "env" in api_key:
            has_api_key = bool(api_key.get("env"))
        elif "value" in api_key:
            has_api_key = bool(api_key.get("value"))
        else:
            has_api_key = bool(api_key)
    return CustomProviderView(
        slug=slug,
        kind=str(entry.get("kind") or "openai_compatible"),
        base_url=entry.get("base_url"),
        has_api_key=has_api_key,
        params=dict(entry.get("params") or {}),
    )


# ---------------------------------------------------------------------------
# Provider model-discovery helpers (module-level so tests can import them)
# ---------------------------------------------------------------------------

_OPENAI_COMPATIBLE_KINDS: frozenset[str] = frozenset(
    {
        "openai",
        "openai_compatible",
        "openai-compatible",
        "codex",
        "groq",
        "qwen",
        "glm",
        "deepseek",
    }
)


async def _query_provider_models(
    name: str, cfg: dict[str, Any]
) -> dict[str, Any]:
    """Query ``/v1/models`` for a provider and return a result dict.

    Returns ``{"ok": bool, "models": list[str], "latency_ms": int, "error": str|null}``.
    For OpenAI-compatible providers, calls ``<base_url>/v1/models`` with the
    configured API key. For the ``codex`` provider, reads the token from
    ``~/.codex/auth.json`` and queries ``https://api.openai.com/v1/models``.
    """
    import os
    import time as _time

    import httpx as _httpx

    providers_cfg = cfg.get("providers") or {}
    entry = providers_cfg.get(name)

    # Special handling for the auto-injected codex provider (no entry in config).
    is_codex = name == "codex"
    if entry is None and not is_codex:
        return {"ok": False, "models": [], "latency_ms": 0, "error": "provider_not_found"}

    if is_codex:
        # Read token from ~/.codex/auth.json
        try:
            from corlinman_providers._codex_oauth import (  # noqa: PLC0415
                load_codex_credential,
            )

            cred = load_codex_credential()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "models": [], "latency_ms": 0, "error": str(exc)}
        if cred is None:
            return {
                "ok": False,
                "models": [],
                "latency_ms": 0,
                "error": "codex_auth_not_found",
            }
        api_key = cred.access_token
        base_url = "https://api.openai.com"
    else:
        entry_dict = dict(entry) if isinstance(entry, dict) else {}
        kind = str(entry_dict.get("kind") or "openai_compatible").lower().replace("-", "_")
        if kind not in _OPENAI_COMPATIBLE_KINDS:
            return {
                "ok": False,
                "models": [],
                "latency_ms": 0,
                "error": f"kind '{kind}' does not support /v1/models probe",
            }
        raw_key = entry_dict.get("api_key")
        if isinstance(raw_key, dict):
            if "value" in raw_key:
                api_key = str(raw_key["value"])
            elif "env" in raw_key:
                api_key = os.environ.get(str(raw_key["env"]), "")
            else:
                api_key = ""
        elif isinstance(raw_key, str):
            api_key = raw_key
        else:
            api_key = ""
        raw_base = entry_dict.get("base_url") or "https://api.openai.com"
        base_url = str(raw_base).rstrip("/")

    url = base_url.rstrip("/") + "/v1/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    t0 = _time.monotonic()
    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
        latency_ms = int((_time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            return {
                "ok": False,
                "models": [],
                "latency_ms": latency_ms,
                "error": f"HTTP {resp.status_code}",
            }
        data = resp.json()
        model_ids = [
            str(item["id"])
            for item in (data.get("data") or [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        return {
            "ok": True,
            "models": sorted(model_ids),
            "latency_ms": latency_ms,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((_time.monotonic() - t0) * 1000)
        return {"ok": False, "models": [], "latency_ms": latency_ms, "error": str(exc)}


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
            for k in _KNOWN_KINDS
        ]
        return ListOut(providers=providers, kinds=kinds)

    @r.post("/admin/providers")
    async def upsert_provider(body: ProviderUpsert):
        if not body.name:
            return _bad("invalid_name", "provider name must be non-empty")
        if body.kind not in _KNOWN_KINDS:
            return _bad("invalid_kind", f"unknown provider kind: {body.kind}")
        state = get_admin_state()
        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = dict(providers.get(body.name) or {})
            existing["kind"] = body.kind
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
            err = await _persist(state, cfg)
            if err is not None:
                return err
        return {"status": "ok", "provider": _view_from_entry(body.name, existing).model_dump()}

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
                if body.kind not in _KNOWN_KINDS:
                    return _bad("invalid_kind", f"unknown provider kind: {body.kind}")
                entry["kind"] = body.kind
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
            err = await _persist(state, cfg)
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
            err = await _persist(state, cfg)
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

    @r.get("/admin/providers/kinds", response_model=CustomKindsOut)
    async def list_provider_kinds() -> CustomKindsOut:
        return CustomKindsOut(kinds=list_supported_kinds())

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
        if body.kind not in list_supported_kinds():
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
                "kind": body.kind,
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
            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err

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
                if body.kind not in list_supported_kinds():
                    return _bad("invalid_kind", f"unknown provider kind: {body.kind}")
                entry["kind"] = body.kind
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
            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err

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
            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err

        return Response(status_code=204)

    @r.post("/admin/providers/{name}/test")
    async def test_provider(name: str):
        cfg = dict(config_snapshot())
        result = await _query_provider_models(name, cfg)
        return result

    @r.get("/admin/providers/{name}/models")
    async def list_provider_models(name: str):
        cfg = dict(config_snapshot())
        result = await _query_provider_models(name, cfg)
        return {"models": result["models"], "error": result["error"]}

    return r
