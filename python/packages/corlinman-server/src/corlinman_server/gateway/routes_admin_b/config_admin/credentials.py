"""``/admin/credentials*`` — provider-credential management surface.

Wave 2.3 of ``docs/PLAN_EASY_SETUP.md``. Borrows the hermes-agent EnvPage
mental model (provider-grouped rows + masked preview + paste-only edit)
but speaks the corlinman config TOML directly — there is no separate
``.env`` file. Every field is a string-shaped slot inside a
``[providers.<name>]`` block.

Routes:

* ``GET    /admin/credentials``                       — list every well-known
  provider with per-field set/preview/env_ref metadata.
* ``PUT    /admin/credentials/{provider}/{key}``      — write/update a single
  whitelisted field. Sets ``enabled = true`` on first write.
* ``DELETE /admin/credentials/{provider}/{key}``      — drop a field; flips
  ``enabled = false`` if the block ends up without any required fields
  (the block itself stays as a stub for UX continuity).
* ``POST   /admin/credentials/{provider}/enable``     — toggle the
  provider-wide ``enabled`` flag without touching field data.

The endpoint **never** returns plaintext values. ``preview`` is "last 4
chars" (``…xyz9``) when the stored value has 5+ characters, ``"***"`` for
shorter literals, and ``None`` when the slot is empty. When the operator
stored the credential as ``api_key = { env = "FOO" }`` the route surfaces
``env_ref="FOO"`` and ``set=true`` without ever resolving the env var.

The whitelist is intentionally small at launch — extending it later is
just adding entries to :data:`_ALLOWED_FIELDS`. Anything outside the
list gets a clean 400 ``unknown_field`` so the UI can show a precise
error without us needing to round-trip through pydantic.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse, Response

from corlinman_server.gateway.core.config_mutation import (
    publish_config_mutation as _publish_config_mutation_core,
)
from corlinman_server.gateway.core.config_mutation import (
    write_config_atomic as _write_config_atomic,
)
from corlinman_server.gateway.routes_admin_b.config_admin._credentials_lib import (
    _ALLOWED_FIELDS,
    _DEFAULT_KIND,
    _WELL_KNOWN_ORDER,
    CredentialProvider,
    CredentialsListResponse,
    EnableProviderBody,
    RevealResponse,
    SetCredentialBody,
    StatusOk,
    _bad,
    _has_primary_set,
    _resolve_field_view,
    _resolve_raw_literal,
    logger,
)
from corlinman_server.gateway.routes_admin_b.config_admin._providers_lib import (
    _autobind_default_alias,
    _can_autobind_default_alias,
    _remove_default_model_ref,
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


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(
        dependencies=[Depends(require_admin)], tags=["admin", "credentials"]
    )

    @r.get("/admin/credentials", response_model=CredentialsListResponse)
    async def list_credentials() -> CredentialsListResponse:
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        if not isinstance(providers_cfg, dict):
            providers_cfg = {}

        # Walk every well-known provider in the canonical order so the
        # UI always shows the same skeleton — even for providers the
        # operator hasn't touched yet. Operator-added providers go
        # through /admin/providers/custom and surface under the
        # "custom" whitelist so they remain manageable from the UI.
        seen: set[str] = set()
        out: list[CredentialProvider] = []
        for name in _WELL_KNOWN_ORDER:
            block = providers_cfg.get(name)
            block_dict = dict(block) if isinstance(block, dict) else {}
            kind = str(
                block_dict.get("kind") or _DEFAULT_KIND.get(name, "openai_compatible")
            )
            enabled = bool(block_dict.get("enabled", False))
            fields = [
                _resolve_field_view(name, k, block_dict.get(k))
                for k in _ALLOWED_FIELDS[name]
            ]
            out.append(
                CredentialProvider(
                    name=name, kind=kind, enabled=enabled, fields=fields
                )
            )
            seen.add(name)

        # Surface any operator-added providers via the "custom" whitelist
        # so they get visible rows + reveal/replace controls without
        # leaking unknown fields. We only expose api_key + base_url +
        # kind for them — anything richer should go through /admin/providers.
        for extra_name in sorted(providers_cfg):
            if extra_name in seen:
                continue
            extra_block = providers_cfg.get(extra_name)
            if not isinstance(extra_block, dict):
                continue
            kind = str(extra_block.get("kind") or "openai_compatible")
            enabled = bool(extra_block.get("enabled", False))
            fields = [
                _resolve_field_view(extra_name, k, extra_block.get(k))
                for k in _ALLOWED_FIELDS["custom"]
            ]
            out.append(
                CredentialProvider(
                    name=extra_name, kind=kind, enabled=enabled, fields=fields
                )
            )

        return CredentialsListResponse(providers=out)

    @r.put("/admin/credentials/{provider}/{key}", response_model=None)
    async def set_credential(
        body: SetCredentialBody,
        provider: str = Path(..., min_length=1),
        key: str = Path(..., min_length=1),
    ) -> JSONResponse | StatusOk:
        # Resolve the whitelist for this provider — fall back to the
        # ``custom`` set for unknown names so operator-added providers
        # remain editable through this surface.
        allowed = _ALLOWED_FIELDS.get(provider, _ALLOWED_FIELDS["custom"])
        if key not in allowed:
            return _bad("unknown_field")

        value = body.value
        if not isinstance(value, str):
            return _bad("invalid_value")

        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(
                status_code=503, content={"error": "config_path_unset"}
            )

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(provider)
            block = dict(existing) if isinstance(existing, dict) else {}

            # Ensure kind is set so downstream consumers can build a
            # provider spec without a follow-up write. Operators can
            # override via the ``kind`` field when allowed.
            if "kind" not in block:
                block["kind"] = _DEFAULT_KIND.get(provider, "openai_compatible")

            # Store as a plain string. The provider registry already
            # accepts both literal strings and the ``{value=...}`` shape,
            # so this keeps the on-disk TOML readable for humans grepping
            # the file.
            block[key] = value

            if _has_primary_set(provider, block):
                block["enabled"] = True
            elif "enabled" not in block:
                block["enabled"] = False

            providers[provider] = block
            cfg["providers"] = providers
            # Same single gate as the /admin/providers path: a provider is
            # autobindable when its adapter is usable — which includes a
            # built-in slot served by a vendor env-var key, even without a
            # config primary credential.
            if bool(block.get("enabled", False)) and _can_autobind_default_alias(
                block, provider
            ):
                cfg = await _autobind_default_alias(
                    cfg,
                    provider,
                    block,
                    data_dir=getattr(state, "data_dir", None),
                )

            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err
            await _publish_config_mutation(state, cfg)

        return StatusOk()

    @r.delete("/admin/credentials/{provider}/{key}", response_model=None)
    async def delete_credential(
        provider: str = Path(..., min_length=1),
        key: str = Path(..., min_length=1),
    ) -> Response | JSONResponse:
        allowed = _ALLOWED_FIELDS.get(provider, _ALLOWED_FIELDS["custom"])
        if key not in allowed:
            return _bad("unknown_field")

        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(
                status_code=503, content={"error": "config_path_unset"}
            )

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(provider)
            if not isinstance(existing, dict):
                # Nothing to delete — return 204 anyway so the UI's
                # optimistic update doesn't have to special-case the
                # "field never existed" race.
                return Response(status_code=204)
            block = dict(existing)
            had_primary = _has_primary_set(provider, block)
            block.pop(key, None)

            # If the primary field went away, flip enabled to false but
            # keep the block as a stub so the UI keeps showing it.
            if not _has_primary_set(provider, block):
                block["enabled"] = False

            providers[provider] = block
            cfg["providers"] = providers
            if had_primary and not _has_primary_set(provider, block):
                cfg = _remove_default_model_ref(cfg, provider)

            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err
            await _publish_config_mutation(state, cfg)

        return Response(status_code=204)

    @r.post("/admin/credentials/{provider}/enable", response_model=None)
    async def enable_provider(
        body: EnableProviderBody,
        provider: str = Path(..., min_length=1),
    ) -> JSONResponse | StatusOk:
        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(
                status_code=503, content={"error": "config_path_unset"}
            )

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(provider)
            block = dict(existing) if isinstance(existing, dict) else {}

            if "kind" not in block:
                block["kind"] = _DEFAULT_KIND.get(provider, "openai_compatible")
            block["enabled"] = bool(body.enabled)

            providers[provider] = block
            cfg["providers"] = providers
            # Mirror /admin/providers: usable-adapter check only (no extra
            # primary-credential gate), so enabling a built-in env-backed slot
            # also autobinds a default.
            if bool(body.enabled) and _can_autobind_default_alias(block, provider):
                cfg = await _autobind_default_alias(
                    cfg,
                    provider,
                    block,
                    data_dir=getattr(state, "data_dir", None),
                )
            elif not bool(body.enabled):
                cfg = _remove_default_model_ref(cfg, provider)

            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err
            await _publish_config_mutation(state, cfg)

        return StatusOk()

    @r.get(
        "/admin/credentials/{provider}/{key}/reveal",
        response_model=RevealResponse,
    )
    async def reveal_credential(
        provider: str = Path(..., min_length=1),
        key: str = Path(..., min_length=1),
    ) -> JSONResponse | RevealResponse:
        """Return the cleartext value of a stored credential.

        Auth-gated by the router-level ``require_admin`` dependency.
        Never logs the value — only ``provider`` and ``key`` make it into
        the audit record. Env-var-shaped credentials (``{env="FOO"}``)
        return 404 because the gateway intentionally never reads
        ``os.environ`` from this surface.
        """
        allowed = _ALLOWED_FIELDS.get(provider, _ALLOWED_FIELDS["custom"])
        if key not in allowed:
            return _bad("unknown_field")

        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        if not isinstance(providers_cfg, dict):
            providers_cfg = {}
        block = providers_cfg.get(provider)
        if not isinstance(block, dict):
            return JSONResponse(
                status_code=404, content={"error": "credential_not_found"}
            )

        literal = _resolve_raw_literal(block.get(key))
        if literal is None:
            return JSONResponse(
                status_code=404, content={"error": "credential_not_found"}
            )

        # Audit log without the value. The value never appears in any
        # log record on any path.
        logger.info("credential.revealed", provider=provider, key=key)
        return RevealResponse(value=literal)

    @r.get("/admin/credentials/codex/status")
    async def codex_credential_status():
        import time  # noqa: PLC0415

        from corlinman_server.gateway.oauth.codex_external import (  # noqa: PLC0415
            read_codex_status,
        )

        status = read_codex_status()
        if status is None:
            return {
                "detected": False,
                "account": None,
                "expires_at_ms": None,
                "expired": None,
            }
        expired: bool | None = False
        if status.expires_at_ms:
            expired = int(time.time() * 1000) >= status.expires_at_ms
        return {
            "detected": status.detected,
            "account": status.account_id,
            "expires_at_ms": status.expires_at_ms,
            "expired": expired,
        }

    return r
