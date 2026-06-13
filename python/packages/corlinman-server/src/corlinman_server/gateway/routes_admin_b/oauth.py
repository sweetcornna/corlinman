"""``/admin/oauth/*`` — subscription-based credential management.

Waves W-A1 + W-A3 of ``docs/PLAN_PROVIDER_AUTH.md``. Exposes:

* the **Anthropic** PKCE flow (start / submit / refresh / disconnect)
* the **Claude Code** CLI read-only import
* the **OpenAI Codex** CLI read-only status detection (file lives at
  ``~/.codex/auth.json``; the Codex CLI owns the login dance)
* the **Gemini** CLI read-only status detection (file lives at
  ``~/.gemini/oauth_creds.json``; the Gemini CLI owns the login dance)
* the **xAI** Grok OAuth PKCE flow (start / submit / refresh / disconnect)

Routes (per provider; see each handler for exact request/response shape):

* ``GET    /admin/oauth/status``                 — combined status across
  every known provider (Anthropic + Codex external + Gemini external +
  xAI). Each row carries an ``id`` + ``source`` so the dashboard can
  render a single OAuth tile per provider with the right badge.
* ``POST   /admin/oauth/anthropic/{start,submit,refresh}``
* ``DELETE /admin/oauth/anthropic``
* ``POST   /admin/oauth/claude-code/import``
* ``GET    /admin/oauth/codex/status``           — read-only detect of
  ``~/.codex/auth.json``.
* ``GET    /admin/oauth/gemini/status``          — read-only detect of
  ``~/.gemini/oauth_creds.json``.
* ``POST   /admin/oauth/xai/{start,submit,refresh}``
* ``DELETE /admin/oauth/xai``

All endpoints require :func:`require_admin` from
:mod:`corlinman_server.gateway.routes_admin_b.state`.

Tokens are never echoed back to the client and never logged.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response

from corlinman_server.gateway.oauth import (
    OAuthCredential,
    anthropic_pkce,
    claude_code_import,
    claude_code_login,
    codex_external,
    codex_pkce,
    delete_credential,
    gemini_external,
    gemini_pkce,
    load_credential,
    save_credential,
    sessions,
    xai_pkce,
)
from corlinman_server.gateway.routes_admin_b._oauth_lib import (
    ClaudeLoginCancelBody,
    ClaudeLoginLaunchResponse,
    ClaudeLoginSubmitBody,
    ExternalCliStatus,
    ImportClaudeCodeResponse,
    RefreshResponse,
    StartPkceResponse,
    StatusResponse,
    SubmitPkceBody,
    SubmitPkceResponse,
    _anthropic_status_row,
    _bad,
    _check_state,
    _codex_status_row,
    _gemini_status_row,
    _require_data_dir,
    _xai_status_row,
)
from corlinman_server.gateway.core.config_mutation import (
    publish_config_mutation as _publish_config_mutation_core,
    write_config_atomic as _write_config_atomic,
)
from corlinman_server.gateway.routes_admin_b.state import (
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
# OAuth model provisioning helpers
# ---------------------------------------------------------------------------

_ANTHROPIC_MODEL_PREFERENCE: tuple[str, ...] = (
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-latest",
)
_CODEX_MODEL_PREFERENCE: tuple[str, ...] = (
    "gpt-5.5",
    "gpt-5",
    "gpt-4.5-turbo",
    "gpt-4.5",
    "chatgpt-4o-latest",
    "gpt-4o",
    "gpt-4o-mini",
    "o4-mini",
)
_FALLBACK_OAUTH_MODELS: dict[str, list[str]] = {
    "anthropic": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "codex": ["gpt-5.5", "gpt-4o"],
}


def _ordered_unique_model_ids(models: list[str], preference: tuple[str, ...]) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    for model in models:
        mid = str(model).strip()
        if not mid or mid in seen:
            continue
        clean.append(mid)
        seen.add(mid)
    preferred = [mid for mid in preference if mid in seen]
    rest = [mid for mid in clean if mid not in set(preferred)]
    return preferred + rest


async def _query_anthropic_oauth_models(access_token: str) -> list[str]:
    """Best-effort live model discovery for Anthropic OAuth tokens."""
    import httpx

    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": "oauth-2025-04-20",
        "x-app": "cli",
        "user-agent": "claude-cli/2.1.88 (claude-code)",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get("https://api.anthropic.com/v1/models", headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    data = resp.json()
    out: list[str] = []
    for item in data.get("data") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            out.append(item["id"])
    return out


async def _query_codex_oauth_models(access_token: str) -> list[str]:
    """Best-effort live model discovery for ChatGPT Codex OAuth tokens."""
    import httpx
    from corlinman_providers._codex_oauth import codex_cloudflare_headers

    headers = {
        "Authorization": f"Bearer {access_token}",
        **codex_cloudflare_headers(access_token),
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0",
            headers=headers,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    data = resp.json()
    out: list[str] = []
    for item in data.get("models") or []:
        slug = item.get("slug") if isinstance(item, dict) else None
        if isinstance(slug, str) and slug:
            out.append(slug)
    return out


def _upsert_oauth_provider_and_aliases(
    cfg: dict[str, Any],
    *,
    provider: str,
    kind: str,
    models: list[str],
    preference: tuple[str, ...],
) -> dict[str, Any]:
    providers_cfg = dict(cfg.get("providers") or {})
    existing_provider = dict(providers_cfg.get(provider) or {})
    existing_provider["kind"] = kind
    existing_provider["enabled"] = True
    providers_cfg[provider] = existing_provider
    cfg["providers"] = providers_cfg

    models_cfg = dict(cfg.get("models") or {})
    aliases = dict(models_cfg.get("aliases") or {})
    selected = _ordered_unique_model_ids(models, preference)
    if not selected:
        selected = list(_FALLBACK_OAUTH_MODELS.get(kind, []))
    selected = _ordered_unique_model_ids(selected, preference)

    bindable_aliases: list[str] = []
    for model_id in selected:
        existing = aliases.get(model_id)
        if isinstance(existing, dict) and existing.get("provider"):
            if existing.get("provider") == provider:
                bindable_aliases.append(model_id)
            continue
        aliases[model_id] = {"provider": provider, "model": model_id, "params": {}}
        bindable_aliases.append(model_id)

    if bindable_aliases and not str(models_cfg.get("default") or "").strip():
        models_cfg["default"] = bindable_aliases[0]
    models_cfg["aliases"] = aliases
    cfg["models"] = models_cfg
    return cfg


async def _provision_oauth_models(
    state: Any,
    *,
    provider: str,
    kind: str,
    access_token: str,
) -> JSONResponse | None:
    if state.config_path is None:
        return JSONResponse(status_code=503, content={"error": "config_path_unset"})

    if kind == "codex":
        preference = _CODEX_MODEL_PREFERENCE
        query = _query_codex_oauth_models
    else:
        preference = _ANTHROPIC_MODEL_PREFERENCE
        query = _query_anthropic_oauth_models

    try:
        discovered = await query(access_token)
    except Exception:
        discovered = []

    async with state.admin_write_lock:
        cfg = dict(getattr(state, "config_loader", lambda: {})() or {})
        cfg = _upsert_oauth_provider_and_aliases(
            cfg,
            provider=provider,
            kind=kind,
            models=discovered,
            preference=preference,
        )
        err = _write_config_atomic(state.config_path, cfg)
        if err is not None:
            return err
        await _publish_config_mutation(state, cfg)
    return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "oauth"])

    @r.get("/admin/oauth/status", response_model=StatusResponse)
    async def oauth_status() -> StatusResponse:
        state = get_admin_state()
        return StatusResponse(
            providers=[
                _anthropic_status_row(state),
                _codex_status_row(),
                _gemini_status_row(),
                _xai_status_row(state),
            ]
        )

    # -- Codex / Gemini: read-only external CLI status --------------------

    @r.get("/admin/oauth/codex/status", response_model=ExternalCliStatus)
    async def codex_status() -> ExternalCliStatus:
        status = codex_external.read_codex_status()
        if status is None:
            return ExternalCliStatus(detected=False)
        return ExternalCliStatus(
            detected=status.detected,
            account_id=status.account_id,
            expires_at_ms=status.expires_at_ms,
        )

    @r.get("/admin/oauth/gemini/status", response_model=ExternalCliStatus)
    async def gemini_status() -> ExternalCliStatus:
        status = gemini_external.read_gemini_status()
        if status is None:
            return ExternalCliStatus(detected=False)
        return ExternalCliStatus(
            detected=status.detected,
            account_id=status.account_id,
            expires_at_ms=status.expires_at_ms,
        )

    # -- xAI PKCE: full start / submit / refresh / disconnect -------------

    @r.post("/admin/oauth/xai/start", response_model=StartPkceResponse)
    async def xai_start() -> StartPkceResponse | JSONResponse:
        verifier, challenge = xai_pkce.generate_pkce_pair()
        state_value = xai_pkce.generate_state()
        nonce_value = xai_pkce.generate_nonce()
        try:
            discovery = await xai_pkce.discover_endpoints()
        except xai_pkce.OAuthExchangeError as exc:
            return _bad("discovery_failed", status=502, message=str(exc))
        sid, record = sessions.create_session(
            "xai",
            flow="pkce",
            code_verifier=verifier,
            state=state_value,
            nonce=nonce_value,
            authorization_endpoint=discovery["authorization_endpoint"],
            token_endpoint=discovery["token_endpoint"],
        )
        auth_url = xai_pkce.build_authorize_url(
            authorization_endpoint=discovery["authorization_endpoint"],
            code_challenge=challenge,
            state=state_value,
            nonce=nonce_value,
        )
        return StartPkceResponse(
            session_id=sid,
            auth_url=auth_url,
            expires_at_ms=record["expires_at_ms"],
        )

    @r.post("/admin/oauth/xai/submit", response_model=SubmitPkceResponse)
    async def xai_submit(body: SubmitPkceBody) -> SubmitPkceResponse | JSONResponse:
        state = get_admin_state()
        data_dir = _require_data_dir(state)
        if isinstance(data_dir, JSONResponse):
            return data_dir

        record = sessions.get_session(body.session_id)
        if record is None or record.get("provider") != "xai" or record.get("flow") != "pkce":
            return _bad("unknown_session", status=404)

        # CSRF state guard: the callback state MUST equal the per-session
        # value minted at /start. Absent-or-mismatched is rejected (R4-D1).
        bad_state = _check_state(body.state, record.get("state", ""))
        if bad_state is not None:
            return bad_state

        token_endpoint = record.get("token_endpoint", "")
        verifier = record.get("code_verifier", "")
        try:
            tokens = await xai_pkce.exchange_code(
                token_endpoint=token_endpoint,
                code=body.code,
                code_verifier=verifier,
            )
        except xai_pkce.OAuthExchangeError as exc:
            sessions.update_session(body.session_id, status="error", error_message=str(exc))
            return _bad("exchange_failed", status=400, message=str(exc))

        cred = OAuthCredential.new(
            provider="xai",
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            expires_at_ms=tokens.get("expires_at_ms"),
            scope=tokens.get("scope") or xai_pkce.XAI_OAUTH_SCOPE,
        )
        try:
            save_credential(data_dir, cred)
        except OSError as exc:
            return _bad("save_failed", status=500, message=str(exc))

        sessions.update_session(body.session_id, status="approved")
        return SubmitPkceResponse(
            ok=True,
            expires_at_ms=cred.expires_at_ms or int(time.time() * 1000),
        )

    @r.post("/admin/oauth/xai/refresh", response_model=RefreshResponse)
    async def xai_refresh() -> RefreshResponse | JSONResponse:
        state = get_admin_state()
        data_dir = _require_data_dir(state)
        if isinstance(data_dir, JSONResponse):
            return data_dir

        cred = load_credential(data_dir, "xai")
        if cred is None:
            return _bad("no_credential", status=404)
        if not cred.refresh_token:
            return _bad("no_refresh_token", status=400)

        # Re-discover the token endpoint each refresh; the stored file
        # doesn't carry it (only Hermes's flavour does) and revalidating
        # avoids trusting a pinned endpoint that could have rotated.
        try:
            discovery = await xai_pkce.discover_endpoints()
        except xai_pkce.OAuthExchangeError as exc:
            return _bad("discovery_failed", status=502, message=str(exc))

        try:
            refreshed = await xai_pkce.refresh_token(
                refresh_token=cred.refresh_token,
                token_endpoint=discovery["token_endpoint"],
            )
        except xai_pkce.OAuthExchangeError as exc:
            return _bad("refresh_failed", status=502, message=str(exc))

        new_cred = cred.with_refreshed(
            access_token=refreshed["access_token"],
            refresh_token=refreshed.get("refresh_token"),
            expires_at_ms=refreshed.get("expires_at_ms"),
        )
        try:
            save_credential(data_dir, new_cred)
        except OSError as exc:
            return _bad("save_failed", status=500, message=str(exc))

        return RefreshResponse(expires_at_ms=new_cred.expires_at_ms or int(time.time() * 1000))

    @r.delete("/admin/oauth/xai", response_model=None)
    async def xai_disconnect() -> Response | JSONResponse:
        state = get_admin_state()
        data_dir = _require_data_dir(state)
        if isinstance(data_dir, JSONResponse):
            return data_dir
        delete_credential(data_dir, "xai")
        return Response(status_code=204)

    # -- Codex PKCE: start / submit / refresh / disconnect ----------------
    # Mirrors hermes hermes_cli/auth.py codex flow. Tokens persist to
    # ~/.codex/auth.json (the codex CLI's canonical path) so the existing
    # read-only detector immediately surfaces "connected".

    @r.post("/admin/oauth/codex/start", response_model=StartPkceResponse)
    async def codex_start() -> StartPkceResponse | JSONResponse:
        verifier, challenge = codex_pkce.generate_pkce_pair()
        state_value = codex_pkce.generate_state()
        sid, record = sessions.create_session(
            "codex",
            flow="pkce",
            code_verifier=verifier,
            state=state_value,
        )
        auth_url = codex_pkce.build_authorize_url(
            code_challenge=challenge, state=state_value
        )
        return StartPkceResponse(
            session_id=sid,
            auth_url=auth_url,
            expires_at_ms=record["expires_at_ms"],
        )

    @r.post("/admin/oauth/codex/submit", response_model=SubmitPkceResponse)
    async def codex_submit(
        body: SubmitPkceBody,
    ) -> SubmitPkceResponse | JSONResponse:
        record = sessions.get_session(body.session_id)
        if (
            record is None
            or record.get("provider") != "codex"
            or record.get("flow") != "pkce"
        ):
            return _bad("unknown_session", status=404)
        # CSRF state guard: reject absent-or-mismatched (R4-D1).
        bad_state = _check_state(body.state, record.get("state", ""))
        if bad_state is not None:
            return bad_state
        try:
            tokens = await codex_pkce.exchange_code(
                code=body.code,
                code_verifier=record.get("code_verifier", ""),
            )
        except codex_pkce.CodexOAuthError as exc:
            sessions.update_session(
                body.session_id, status="error", error_message=str(exc)
            )
            return _bad("exchange_failed", status=400, message=str(exc))

        try:
            codex_pkce.write_auth_json(tokens)
        except OSError as exc:
            return _bad("save_failed", status=500, message=str(exc))

        state = get_admin_state()
        provision_err = await _provision_oauth_models(
            state,
            provider="codex",
            kind="codex",
            access_token=str(tokens.get("access_token") or ""),
        )
        if provision_err is not None:
            return provision_err

        sessions.update_session(body.session_id, status="approved")
        return SubmitPkceResponse(
            ok=True,
            expires_at_ms=tokens.get("expires_at_ms")
            or int(time.time() * 1000),
        )

    @r.post("/admin/oauth/codex/refresh", response_model=RefreshResponse)
    async def codex_refresh() -> RefreshResponse | JSONResponse:
        # Reach into the on-disk auth.json directly — the codex format
        # is what we just wrote in codex_pkce.write_auth_json().
        path = codex_pkce._codex_auth_path()  # noqa: SLF001
        if not path.is_file():
            return _bad("no_credential", status=404)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _bad("malformed", status=500, message=str(exc))
        rt = ((raw or {}).get("tokens") or {}).get("refresh_token")
        if not isinstance(rt, str) or not rt:
            return _bad("no_refresh_token", status=400)
        try:
            refreshed = await codex_pkce.refresh_token(refresh_token=rt)
        except codex_pkce.CodexOAuthError as exc:
            return _bad("refresh_failed", status=502, message=str(exc))
        try:
            codex_pkce.write_auth_json(refreshed)
        except OSError as exc:
            return _bad("save_failed", status=500, message=str(exc))
        return RefreshResponse(
            expires_at_ms=refreshed.get("expires_at_ms")
            or int(time.time() * 1000)
        )

    @r.delete("/admin/oauth/codex", response_model=None)
    async def codex_disconnect() -> Response:
        codex_pkce.delete_auth_json()
        return Response(status_code=204)

    # -- Gemini PKCE: start / submit / refresh / disconnect ----------------
    # Tokens persist to ~/.gemini/oauth_creds.json (canonical Google CLI
    # path) so gemini_external.read_gemini_status surfaces the result.

    @r.post("/admin/oauth/gemini/start", response_model=StartPkceResponse)
    async def gemini_start() -> StartPkceResponse | JSONResponse:
        verifier, challenge = gemini_pkce.generate_pkce_pair()
        state_value = gemini_pkce.generate_state()
        sid, record = sessions.create_session(
            "gemini",
            flow="pkce",
            code_verifier=verifier,
            state=state_value,
        )
        auth_url = gemini_pkce.build_authorize_url(
            code_challenge=challenge, state=state_value
        )
        return StartPkceResponse(
            session_id=sid,
            auth_url=auth_url,
            expires_at_ms=record["expires_at_ms"],
        )

    @r.post("/admin/oauth/gemini/submit", response_model=SubmitPkceResponse)
    async def gemini_submit(
        body: SubmitPkceBody,
    ) -> SubmitPkceResponse | JSONResponse:
        record = sessions.get_session(body.session_id)
        if (
            record is None
            or record.get("provider") != "gemini"
            or record.get("flow") != "pkce"
        ):
            return _bad("unknown_session", status=404)
        # CSRF state guard: reject absent-or-mismatched (R4-D1).
        bad_state = _check_state(body.state, record.get("state", ""))
        if bad_state is not None:
            return bad_state
        try:
            tokens = await gemini_pkce.exchange_code(
                code=body.code,
                code_verifier=record.get("code_verifier", ""),
            )
        except gemini_pkce.GeminiOAuthError as exc:
            sessions.update_session(
                body.session_id, status="error", error_message=str(exc)
            )
            return _bad("exchange_failed", status=400, message=str(exc))

        try:
            gemini_pkce.write_creds_json(tokens)
        except OSError as exc:
            return _bad("save_failed", status=500, message=str(exc))

        sessions.update_session(body.session_id, status="approved")
        return SubmitPkceResponse(
            ok=True,
            expires_at_ms=tokens.get("expires_at_ms")
            or int(time.time() * 1000),
        )

    @r.post("/admin/oauth/gemini/refresh", response_model=RefreshResponse)
    async def gemini_refresh() -> RefreshResponse | JSONResponse:
        path = gemini_pkce._gemini_creds_path()  # noqa: SLF001
        if not path.is_file():
            return _bad("no_credential", status=404)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _bad("malformed", status=500, message=str(exc))
        rt = (raw or {}).get("refresh_token")
        if not isinstance(rt, str) or not rt:
            return _bad("no_refresh_token", status=400)
        try:
            refreshed = await gemini_pkce.refresh_token(refresh_token=rt)
        except gemini_pkce.GeminiOAuthError as exc:
            return _bad("refresh_failed", status=502, message=str(exc))
        try:
            gemini_pkce.write_creds_json(refreshed)
        except OSError as exc:
            return _bad("save_failed", status=500, message=str(exc))
        return RefreshResponse(
            expires_at_ms=refreshed.get("expires_at_ms")
            or int(time.time() * 1000)
        )

    @r.delete("/admin/oauth/gemini", response_model=None)
    async def gemini_disconnect() -> Response:
        gemini_pkce.delete_creds_json()
        return Response(status_code=204)

    @r.post("/admin/oauth/anthropic/start", response_model=StartPkceResponse)
    async def anthropic_start() -> StartPkceResponse | JSONResponse:
        verifier, challenge = anthropic_pkce.generate_pkce_pair()
        # Anthropic's callback page echoes verifier-as-state, so we use
        # the verifier itself as the state parameter (matches hermes).
        sid, record = sessions.create_session(
            "anthropic",
            flow="pkce",
            code_verifier=verifier,
            state=verifier,
        )
        auth_url = anthropic_pkce.build_authorize_url(
            code_challenge=challenge, state=verifier
        )
        return StartPkceResponse(
            session_id=sid,
            auth_url=auth_url,
            expires_at_ms=record["expires_at_ms"],
        )

    @r.post("/admin/oauth/anthropic/submit", response_model=SubmitPkceResponse)
    async def anthropic_submit(body: SubmitPkceBody) -> SubmitPkceResponse | JSONResponse:
        state = get_admin_state()
        data_dir = _require_data_dir(state)
        if isinstance(data_dir, JSONResponse):
            return data_dir

        record = sessions.get_session(body.session_id)
        if record is None or record.get("provider") != "anthropic" or record.get("flow") != "pkce":
            return _bad("unknown_session", status=404)

        verifier = record.get("code_verifier", "")
        expected_state = record.get("state", "")
        try:
            tokens = await anthropic_pkce.exchange_code(
                code_input=body.code,
                code_verifier=verifier,
                expected_state=expected_state,
            )
        except anthropic_pkce.OAuthExchangeError as exc:
            sessions.update_session(body.session_id, status="error", error_message=str(exc))
            return _bad("exchange_failed", status=400, message=str(exc))

        cred = OAuthCredential.new(
            provider="anthropic",
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            expires_at_ms=tokens.get("expires_at_ms"),
            scope=tokens.get("scope") or anthropic_pkce.ANTHROPIC_OAUTH_SCOPES,
        )
        try:
            save_credential(data_dir, cred)
        except OSError as exc:
            return _bad("save_failed", status=500, message=str(exc))

        provision_err = await _provision_oauth_models(
            state,
            provider="anthropic",
            kind="anthropic",
            access_token=cred.access_token,
        )
        if provision_err is not None:
            return provision_err

        sessions.update_session(body.session_id, status="approved")
        return SubmitPkceResponse(
            ok=True,
            expires_at_ms=cred.expires_at_ms or int(time.time() * 1000),
        )

    @r.post("/admin/oauth/anthropic/refresh", response_model=RefreshResponse)
    async def anthropic_refresh() -> RefreshResponse | JSONResponse:
        state = get_admin_state()
        data_dir = _require_data_dir(state)
        if isinstance(data_dir, JSONResponse):
            return data_dir

        cred = load_credential(data_dir, "anthropic")
        if cred is None:
            return _bad("no_credential", status=404)
        if not cred.refresh_token:
            return _bad("no_refresh_token", status=400)

        try:
            refreshed = await anthropic_pkce.refresh_token(
                refresh_token=cred.refresh_token,
            )
        except anthropic_pkce.OAuthExchangeError as exc:
            return _bad("refresh_failed", status=502, message=str(exc))

        new_cred = cred.with_refreshed(
            access_token=refreshed["access_token"],
            refresh_token=refreshed.get("refresh_token"),
            expires_at_ms=refreshed.get("expires_at_ms"),
        )
        try:
            save_credential(data_dir, new_cred)
        except OSError as exc:
            return _bad("save_failed", status=500, message=str(exc))

        return RefreshResponse(expires_at_ms=new_cred.expires_at_ms or int(time.time() * 1000))

    @r.delete("/admin/oauth/anthropic", response_model=None)
    async def anthropic_disconnect() -> Response | JSONResponse:
        state = get_admin_state()
        data_dir = _require_data_dir(state)
        if isinstance(data_dir, JSONResponse):
            return data_dir
        delete_credential(data_dir, "anthropic")
        return Response(status_code=204)

    @r.post(
        "/admin/oauth/claude-code/import",
        response_model=ImportClaudeCodeResponse,
    )
    async def import_claude_code() -> ImportClaudeCodeResponse | JSONResponse:
        state = get_admin_state()
        data_dir = _require_data_dir(state)
        if isinstance(data_dir, JSONResponse):
            return data_dir

        try:
            cred = claude_code_import.read_claude_code_credentials()
        except claude_code_import.ClaudeCodeCredentialsMalformed as exc:
            return _bad("malformed", status=400, message=str(exc))
        if cred is None:
            return _bad("not_found", status=404)

        # Re-stamp via the constructor so we record a fresh obtained_at_ms.
        persisted = OAuthCredential.new(
            provider="anthropic",
            access_token=cred.access_token,
            refresh_token=cred.refresh_token,
            expires_at_ms=cred.expires_at_ms,
            scope=cred.scope,
        )
        try:
            save_credential(data_dir, persisted)
        except OSError as exc:
            return _bad("save_failed", status=500, message=str(exc))

        provision_err = await _provision_oauth_models(
            state,
            provider="anthropic",
            kind="anthropic",
            access_token=persisted.access_token,
        )
        if provision_err is not None:
            return provision_err

        return ImportClaudeCodeResponse(
            imported=True,
            expires_at_ms=persisted.expires_at_ms,
        )

    # -- Claude Code: drive `claude auth login` from the UI --------------
    #
    # Three endpoints, mirroring the xAI PKCE pattern but going through
    # an external CLI subprocess:
    #
    #   launch  — spawn `claude auth login`, return its OAuth URL + a
    #             session_id (subprocess remains parked on stdin).
    #   submit  — paste the code back to subprocess stdin, wait for
    #             clean exit, then re-import ~/.claude/.credentials.json
    #             into the gateway's anthropic credential slot.
    #   cancel  — kill an abandoned subprocess.

    @r.post(
        "/admin/oauth/claude-code/launch",
        response_model=ClaudeLoginLaunchResponse,
    )
    async def claude_login_launch() -> (
        ClaudeLoginLaunchResponse | JSONResponse
    ):
        try:
            result = await claude_code_login.launch_claude_login()
        except claude_code_login.ClaudeLoginError as exc:
            status = 503 if exc.code == "claude_cli_not_installed" else 502
            return _bad(exc.code, status=status, message=exc.message)
        return ClaudeLoginLaunchResponse(
            session_id=result.session_id, auth_url=result.url
        )

    @r.post(
        "/admin/oauth/claude-code/submit",
        response_model=ImportClaudeCodeResponse,
    )
    async def claude_login_submit(
        body: ClaudeLoginSubmitBody,
    ) -> ImportClaudeCodeResponse | JSONResponse:
        state = get_admin_state()
        data_dir = _require_data_dir(state)
        if isinstance(data_dir, JSONResponse):
            return data_dir

        try:
            await claude_code_login.submit_code(body.session_id, body.code)
        except claude_code_login.ClaudeLoginError as exc:
            status_map = {
                "unknown_session": 404,
                "empty_code": 400,
                "subprocess_exited": 410,
                "write_failed": 502,
                "submit_timeout": 504,
                "subprocess_nonzero": 502,
            }
            return _bad(
                exc.code,
                status=status_map.get(exc.code, 500),
                message=exc.message,
            )

        # CLI exited cleanly → ~/.claude/.credentials.json should now
        # exist. Re-use the import path so we persist into the same slot
        # the existing "Import" button uses.
        try:
            cred = claude_code_import.read_claude_code_credentials()
        except claude_code_import.ClaudeCodeCredentialsMalformed as exc:
            return _bad("malformed", status=400, message=str(exc))
        if cred is None:
            return _bad(
                "not_found",
                status=404,
                message=(
                    "Login finished but ~/.claude/.credentials.json was "
                    "not written — check the gateway HOME env var."
                ),
            )
        persisted = OAuthCredential.new(
            provider="anthropic",
            access_token=cred.access_token,
            refresh_token=cred.refresh_token,
            expires_at_ms=cred.expires_at_ms,
            scope=cred.scope,
        )
        try:
            save_credential(data_dir, persisted)
        except OSError as exc:
            return _bad("save_failed", status=500, message=str(exc))
        provision_err = await _provision_oauth_models(
            state,
            provider="anthropic",
            kind="anthropic",
            access_token=persisted.access_token,
        )
        if provision_err is not None:
            return provision_err
        return ImportClaudeCodeResponse(
            imported=True, expires_at_ms=persisted.expires_at_ms
        )

    @r.post("/admin/oauth/claude-code/cancel")
    async def claude_login_cancel(
        body: ClaudeLoginCancelBody,
    ) -> Response:
        claude_code_login.cancel(body.session_id)
        return Response(status_code=204)

    return r


__all__ = ["router"]
