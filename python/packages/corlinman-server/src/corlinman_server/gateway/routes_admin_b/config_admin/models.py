"""``/admin/models*`` — model routing / alias management.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/models.rs``.

Three routes:

* ``GET    /admin/models``                  — provider + alias snapshot.
* ``POST   /admin/models/aliases``          — single upsert, bulk
  replace, *or* default-only update (untagged union body).
* ``DELETE /admin/models/aliases/{name}``   — drop one alias.

Mutation routes atomic-write the active config TOML — requires
:attr:`AdminState.config_path`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from corlinman_providers.reasoning_tiers import reasoning_tiers_for_model
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.core.config_mutation import (
    publish_config_mutation as _publish_config_mutation_core,
)
from corlinman_server.gateway.routes_admin_b.config_admin._providers_lib import (
    ProviderView,
    _params_schema_for,
    _view_from_entry,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)


def _py_config_writer():
    from corlinman_server.gateway.lifecycle import write_py_config  # noqa: PLC0415

    return write_py_config


async def publish_config_mutation(state: Any, cfg: dict[str, Any]) -> None:
    await _publish_config_mutation_core(
        state,
        cfg,
        py_config_writer=_py_config_writer(),
    )


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class AliasRow(BaseModel):
    name: str
    model: str
    provider: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    effective_params_schema: dict[str, Any] = Field(default_factory=dict)
    # Reasoning-effort ladder for the *resolved* upstream model (family
    # registry in corlinman_providers.reasoning_tiers). ``None`` = unknown
    # family (client falls back to its own heuristics); ``[]`` = the model
    # is known to have no effort knob (hide the picker).
    reasoning_tiers: list[str] | None = None
    reasoning_default: str | None = None


class ModelsResponse(BaseModel):
    default: str
    aliases: list[AliasRow]
    providers: list[ProviderView]


class AliasUpsert(BaseModel):
    name: str
    model: str
    provider: str | None = None
    params: dict[str, Any] | None = None


class BulkAliases(BaseModel):
    aliases: dict[str, str]
    default: str | None = None


class DefaultOnly(BaseModel):
    """``{"default": "<alias>"}`` — update ``models.default`` WITHOUT
    touching the alias table.

    The bulk shape drops every alias name omitted from its payload, so a
    "set default only" client that posted ``{aliases: {}, default}`` would
    wipe the whole routing table. This shape is the non-destructive way to
    move the default (used by the guided provider-setup flow's last step).
    """

    default: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Backends ``corlinman_agent.web.search`` knows how to drive.
#:
#: Must stay in step with that module: a backend the agent accepts but this
#: whitelist rejects is selectable only by hand-editing config.toml, since
#: this endpoint 400s it and the UI dropdown is rendered from the same list.
#: That is precisely the "the setting exists but the UI is decorative" failure
#: #169 was written to eliminate.
_SEARCH_BACKENDS: frozenset[str] = frozenset({"ddg", "serpapi", "freesearch"})


def _secret_present(raw: Any) -> bool:
    """True when a config secret slot holds something usable.

    Accepts both a literal string and the ``SecretRef`` mapping shape
    (``{"env": "..."}`` / ``{"value": "..."}``) that ``py_config``
    resolves — an ``env`` ref counts as configured even though the literal
    is not in ``config.toml``.
    """
    if isinstance(raw, Mapping):
        return bool(str(raw.get("env") or raw.get("value") or "").strip())
    return bool(str(raw or "").strip())


def _bad(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": code, "message": message})


def _alias_entry_to_dict(alias: Any) -> tuple[str, str | None, dict[str, Any]]:
    """Coerce ``aliases[name]`` (which may be a string shorthand or a
    full dict) into ``(model, provider, params)``."""
    if isinstance(alias, str):
        return alias, None, {}
    if isinstance(alias, dict):
        return (
            str(alias.get("model", "")),
            alias.get("provider"),
            dict(alias.get("params") or {}),
        )
    return str(alias), None, {}


def _default_params_schema() -> dict[str, Any]:
    return _params_schema_for("openai_compatible")


def _alias_row(
    name: str,
    entry: Any,
    providers_cfg: dict[str, Any],
) -> AliasRow:
    model, provider, params = _alias_entry_to_dict(entry)
    schema = _default_params_schema()
    provider_name = provider if isinstance(provider, str) else ""
    provider_entry = providers_cfg.get(provider_name) if provider_name else None
    if isinstance(provider_entry, dict):
        schema = _view_from_entry(provider_name, provider_entry).params_schema
    tiers, tier_default = reasoning_tiers_for_model(model)
    return AliasRow(
        name=name,
        model=model,
        provider=provider_name,
        params=params,
        effective_params_schema=schema,
        reasoning_tiers=list(tiers) if tiers is not None else None,
        reasoning_default=tier_default,
    )


async def _persist_alias_swap(state: AdminState, new_models: dict[str, Any]) -> JSONResponse | None:
    """Atomic-write of just the ``[models]`` section. Returns ``None`` on
    success, a ``JSONResponse`` on failure."""
    return await _persist_section(state, "models", new_models)


async def _persist_section(
    state: AdminState, name: str, value: dict[str, Any]
) -> JSONResponse | None:
    """Atomic-write of a single top-level config section.

    An empty ``value`` removes the section entirely, so clearing a binding
    in the UI leaves no stub table behind. Returns ``None`` on success, a
    ``JSONResponse`` on failure."""
    if state.config_path is None:
        return JSONResponse(
            status_code=503, content={"error": "config_path_unset"}
        )
    try:
        try:
            import tomli_w  # noqa: PLC0415
        except ImportError:  # pragma: no cover
            import toml as tomli_w  # type: ignore  # noqa: PLC0415
    except ImportError:
        return JSONResponse(
            status_code=500,
            content={"error": "serialise_failed", "message": "no toml writer"},
        )

    cfg = dict(config_snapshot())
    if value:
        cfg[name] = value
    else:
        cfg.pop(name, None)
    try:
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
    await publish_config_mutation(state, cfg)
    return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "models"])

    @r.get("/admin/models", response_model=ModelsResponse)
    async def list_models():
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        providers: list[ProviderView] = []
        if isinstance(providers_cfg, dict):
            for name, entry in providers_cfg.items():
                if isinstance(entry, dict):
                    providers.append(_view_from_entry(str(name), entry))
        models_cfg = cfg.get("models") or {}
        aliases_map = models_cfg.get("aliases") or {}
        aliases: list[AliasRow] = []
        if isinstance(aliases_map, dict):
            for name, entry in aliases_map.items():
                aliases.append(_alias_row(str(name), entry, providers_cfg))
        aliases.sort(key=lambda a: a.name)
        providers.sort(key=lambda p: p.name)
        return ModelsResponse(
            default=str(models_cfg.get("default", "")),
            aliases=aliases,
            providers=providers,
        )

    @r.post("/admin/models/aliases")
    async def upsert_aliases(body: dict[str, Any]):
        # Untagged-union: try single shape first, then bulk.
        if "name" in body and "model" in body:
            try:
                single = AliasUpsert.model_validate(body)
            except Exception as exc:  # noqa: BLE001
                return _bad("invalid_body", str(exc))
            return await _apply_single(single)
        if "aliases" in body:
            try:
                bulk = BulkAliases.model_validate(body)
            except Exception as exc:  # noqa: BLE001
                return _bad("invalid_body", str(exc))
            return await _apply_bulk(bulk)
        if "default" in body:
            # Default-only update — the body carries NO ``aliases`` key, so
            # the alias table must be left untouched (see DefaultOnly).
            try:
                default_only = DefaultOnly.model_validate(body)
            except Exception as exc:  # noqa: BLE001
                return _bad("invalid_body", str(exc))
            return await _apply_default_only(default_only)
        return _bad(
            "invalid_body",
            "body must be one of {name, model}, {aliases}, or {default}",
        )

    @r.delete("/admin/models/aliases/{name}")
    async def delete_alias(name: str):
        state = get_admin_state()
        cfg = dict(config_snapshot())
        models_cfg = dict(cfg.get("models") or {})
        aliases = dict(models_cfg.get("aliases") or {})
        if name not in aliases:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "resource": "alias", "id": name},
            )
        aliases.pop(name)
        models_cfg["aliases"] = aliases
        err = await _persist_alias_swap(state, models_cfg)
        if err is not None:
            return err
        return {"status": "ok", "removed": name}

    async def _apply_single(up: AliasUpsert) -> Any:
        if not up.name or not up.model:
            return _bad("invalid_alias", "alias name and model must be non-empty")
        if up.provider is not None and not up.provider:
            return _bad("invalid_provider", "alias provider must be non-empty when supplied")
        state = get_admin_state()
        params = up.params or {}
        entry: Any
        if up.provider is not None or params:
            entry = {"model": up.model, "provider": up.provider, "params": params}
        else:
            entry = up.model
        cfg = dict(config_snapshot())
        models_cfg = dict(cfg.get("models") or {})
        aliases = dict(models_cfg.get("aliases") or {})
        aliases[up.name] = entry
        models_cfg["aliases"] = aliases
        err = await _persist_alias_swap(state, models_cfg)
        if err is not None:
            return err
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        if not isinstance(providers_cfg, dict):
            providers_cfg = {}
        return _alias_row(up.name, entry, providers_cfg).model_dump()

    async def _apply_bulk(bulk: BulkAliases) -> Any:
        for k, v in bulk.aliases.items():
            if not k or not v:
                return _bad(
                    "invalid_alias", "alias name and target must be non-empty"
                )
        if bulk.default is not None and not bulk.default:
            return _bad("invalid_default", "default model must be non-empty")
        state = get_admin_state()
        cfg = dict(config_snapshot())
        models_cfg = dict(cfg.get("models") or {})
        # Non-destructive merge. The bulk wire shape is a flat ``{name: target}``
        # string map (the Models page "Save all" button) which cannot carry an
        # alias's ``provider`` / ``params``. Replacing the table wholesale with
        # bare strings would strip the provider binding off every alias — e.g.
        # the ones OAuth login just provisioned — and the resolver then silently
        # drops provider-less aliases (``AliasEntry`` requires a provider), so
        # chat falls through to the wrong upstream (the reported 401 + "—"
        # provider column). So for each incoming entry we PRESERVE the existing
        # alias's provider + params when its name still maps to a dict alias,
        # updating only the target model. Names omitted from the payload are
        # dropped (honours row deletions); brand-new names become plain-string
        # shorthands exactly as before.
        existing_aliases = dict(models_cfg.get("aliases") or {})
        merged_aliases: dict[str, Any] = {}
        for name, target in bulk.aliases.items():
            prev = existing_aliases.get(name)
            if isinstance(prev, dict) and prev.get("provider"):
                next_entry = dict(prev)
                next_entry["model"] = target
                merged_aliases[name] = next_entry
            else:
                merged_aliases[name] = target
        models_cfg["aliases"] = merged_aliases
        if bulk.default is not None:
            models_cfg["default"] = bulk.default
        err = await _persist_alias_swap(state, models_cfg)
        if err is not None:
            return err
        return {
            "status": "ok",
            "default": models_cfg.get("default", ""),
            "aliases": dict(models_cfg.get("aliases") or {}),
        }

    async def _apply_default_only(body: DefaultOnly) -> Any:
        """Move ``models.default`` and NOTHING else.

        Unlike ``_apply_bulk`` the alias table is copied through verbatim
        (full dict entries included), so provider bindings — e.g. the ones
        OAuth login just provisioned — survive a default switch.
        """
        if not body.default:
            return _bad("invalid_default", "default model must be non-empty")
        state = get_admin_state()
        cfg = dict(config_snapshot())
        models_cfg = dict(cfg.get("models") or {})
        models_cfg["default"] = body.default
        err = await _persist_alias_swap(state, models_cfg)
        if err is not None:
            return err
        return {
            "status": "ok",
            "default": body.default,
            "aliases": dict(models_cfg.get("aliases") or {}),
        }

    @r.get("/admin/models/capabilities", response_model=None)
    async def get_capabilities():
        """Which model serves each capability: chat, image, speech, search.

        The model hub needs one place to answer "what actually runs when
        the agent generates a picture or speaks?" — previously only chat
        had a visible binding, image was implicit (first `image_capable`
        provider, or the chat provider), and speech lived only under
        /voice. Composed read-only from live config; each half is written
        back through its own PUT so nothing is coupled.
        """
        cfg = dict(config_snapshot())
        models_cfg = cfg.get("models") or {}
        voice_cfg = cfg.get("voice") or {}
        providers_cfg = cfg.get("providers") or {}
        search_cfg = cfg.get("web_search") or {}
        if not isinstance(search_cfg, dict):
            search_cfg = {}

        image_candidates: list[str] = []
        if isinstance(providers_cfg, dict):
            for name, entry in providers_cfg.items():
                if not isinstance(entry, dict) or entry.get("enabled") is False:
                    continue
                if entry.get("image_capable") is True:
                    image_candidates.append(str(name))

        alias_names: list[str] = []
        aliases_map = models_cfg.get("aliases") if isinstance(models_cfg, dict) else None
        if isinstance(aliases_map, dict):
            alias_names = sorted(str(k) for k in aliases_map)

        return {
            "text": {"model": str(models_cfg.get("default", "") or "")},
            "image": {
                "provider": str(models_cfg.get("image_provider", "") or ""),
                "model": str(models_cfg.get("image_model", "") or ""),
                # Slots that declared image_capable — a hint for the picker,
                # not a constraint: an explicit binding always wins.
                "capable_providers": image_candidates,
            },
            "voice": {
                "enabled": bool(voice_cfg.get("enabled", True))
                if isinstance(voice_cfg, dict)
                else True,
                "backend": str(voice_cfg.get("backend", "") or "")
                if isinstance(voice_cfg, dict)
                else "",
                "model": str(voice_cfg.get("model", "") or "")
                if isinstance(voice_cfg, dict)
                else "",
                "voice": str(voice_cfg.get("voice", "") or "")
                if isinstance(voice_cfg, dict)
                else "",
            },
            "search": {
                # "" = unset, which resolves to the keyless DDG scrape.
                "backend": str(search_cfg.get("backend", "") or ""),
                # Never echo the key itself — the UI only needs to know
                # whether one is on file so it can render "已配置". A dict
                # value is a SecretRef (``{env = "..."}``), which counts as
                # configured even though the literal lives elsewhere.
                "api_key_set": _secret_present(search_cfg.get("api_key")),
                # Rendered straight into the UI dropdown — derived from the
                # same whitelist the PUT validates against so the two can
                # never disagree about what is selectable.
                "backends": sorted(_SEARCH_BACKENDS),
            },
            "aliases": alias_names,
        }

    @r.put("/admin/models/capabilities/image", response_model=None)
    async def put_image_capability(body: dict[str, Any]):
        """Bind the global image-generation model.

        Writes ``[models].image_provider`` / ``image_model``, which the
        agent honours between a persona binding and the chat-provider
        fallback. Sending empty strings clears the binding.
        """
        provider = str(body.get("provider") or "").strip()
        model = str(body.get("model") or "").strip()
        state = get_admin_state()
        cfg = dict(config_snapshot())
        models_cfg = dict(cfg.get("models") or {})
        for key, value in (("image_provider", provider), ("image_model", model)):
            if value:
                models_cfg[key] = value
            else:
                models_cfg.pop(key, None)
        err = await _persist_alias_swap(state, models_cfg)
        if err is not None:
            return err
        return {"status": "ok", "provider": provider, "model": model}

    @r.put("/admin/models/capabilities/search", response_model=None)
    async def put_search_capability(body: dict[str, Any]):
        """Bind the web-search backend + key.

        Writes ``[web_search]``, which reaches the agent process through the
        ``py-config.json`` sidecar. Until this existed the backend was
        readable only from ``CORLINMAN_WEB_SEARCH_*`` — env vars the agent's
        systemd unit never receives — so every deployment silently used the
        keyless DuckDuckGo scrape.

        ``backend``: empty clears the binding (back to the keyless default).
        ``api_key``: omit the field to keep the stored key, send ``""`` to
        delete it. That asymmetry is deliberate — the GET never echoes the
        key, so a UI round-trip must not be able to wipe it by accident.
        """
        backend = str(body.get("backend") or "").strip().lower()
        if backend and backend not in _SEARCH_BACKENDS:
            return _bad(
                "unknown_backend",
                f"backend must be one of: {', '.join(sorted(_SEARCH_BACKENDS))}",
            )

        state = get_admin_state()
        cfg = dict(config_snapshot())
        search_cfg = dict(cfg.get("web_search") or {})

        if backend:
            search_cfg["backend"] = backend
        else:
            search_cfg.pop("backend", None)

        if "api_key" in body:
            api_key = str(body.get("api_key") or "").strip()
            if api_key:
                search_cfg["api_key"] = api_key
            else:
                search_cfg.pop("api_key", None)

        if backend == "serpapi" and not _secret_present(search_cfg.get("api_key")):
            return _bad(
                "api_key_required",
                "the serpapi backend needs an API key",
            )

        err = await _persist_section(state, "web_search", search_cfg)
        if err is not None:
            return err
        return {
            "status": "ok",
            "backend": backend,
            "api_key_set": _secret_present(search_cfg.get("api_key")),
        }

    return r
