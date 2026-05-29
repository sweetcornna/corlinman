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

import hmac
import os
import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from corlinman_server.gateway.oauth import (
    OAuthCredential,
    delete_credential,
    load_credential,
    save_credential,
)
from corlinman_server.gateway.oauth import (
    anthropic_pkce,
    claude_code_import,
    claude_code_login,
    codex_external,
    codex_pkce,
    gemini_external,
    gemini_pkce,
    sessions,
    xai_pkce,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class ProviderStatus(BaseModel):
    """One row in the response of ``GET /admin/oauth/status``.

    ``source`` legend (string-typed, not enum — easier to extend):

    * Anthropic: ``"pkce" | "claude-code" | "env" | "api-key" | "none"``.
    * Codex / Gemini (external CLIs): ``"external-cli" | "none"``.
    * xAI: ``"pkce" | "none"``.
    """

    id: str
    source: str
    expires_in_seconds: int | None = None
    username: str | None = None


class StatusResponse(BaseModel):
    providers: list[ProviderStatus]


class ExternalCliStatus(BaseModel):
    """Per-provider response of ``GET /admin/oauth/{codex,gemini}/status``."""

    detected: bool
    account_id: str | None = None
    expires_at_ms: int | None = None


class StartPkceResponse(BaseModel):
    session_id: str
    auth_url: str
    expires_at_ms: int


class SubmitPkceBody(BaseModel):
    session_id: str
    code: str
    state: str | None = None


class SubmitPkceResponse(BaseModel):
    ok: bool = True
    expires_at_ms: int


class RefreshResponse(BaseModel):
    expires_at_ms: int


class ImportClaudeCodeResponse(BaseModel):
    imported: bool = True
    expires_at_ms: int | None = None


class ClaudeLoginLaunchResponse(BaseModel):
    """Returned by POST /admin/oauth/claude-code/launch."""

    session_id: str
    auth_url: str


class ClaudeLoginSubmitBody(BaseModel):
    session_id: str
    code: str


class ClaudeLoginCancelBody(BaseModel):
    session_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bad(code: str, status: int = 400, message: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": code}
    if message is not None:
        body["message"] = message
    return JSONResponse(status_code=status, content=body)


def _require_data_dir(state: AdminState) -> Path | JSONResponse:
    if state.data_dir is None:
        return _bad("data_dir_unset", status=503)
    return state.data_dir


def _check_state(received: str | None, expected: str) -> JSONResponse | None:
    """Validate the CSRF ``state`` echoed back in the OAuth callback.

    The legitimate UI (``ui/components/admin/oauth-login-modal.tsx``)
    always submits a non-empty ``state`` — it refuses to call ``/submit``
    otherwise and the wire type marks ``state`` non-optional. So we
    REQUIRE the callback state to match the per-session value minted at
    ``/start``: a missing/empty state is treated the same as a mismatch
    (R4-D1). ``state`` is a CSRF secret, so the comparison is
    constant-time via :func:`hmac.compare_digest`.

    Returns a 400 ``state_mismatch`` response on failure, or ``None`` when
    the state is valid.
    """
    if not received or not expected or not hmac.compare_digest(received, expected):
        return _bad("state_mismatch", status=400)
    return None


def _anthropic_status_row(state: AdminState) -> ProviderStatus:
    """Compute the highest-priority Anthropic credential source.

    Resolution order matches :mod:`corlinman_providers.anthropic_provider`:
    PKCE file > Claude Code CLI > ``ANTHROPIC_TOKEN`` env > config
    ``api_key`` > ``ANTHROPIC_API_KEY`` env > none.

    We never read the token value into the response — only its source and
    derived expiry. ``username`` is reserved for a future
    ``user:profile`` lookup; today we return ``None``.
    """
    # 1. PKCE file under data_dir/.oauth/anthropic.json
    if state.data_dir is not None:
        cred = load_credential(state.data_dir, "anthropic")
        if cred is not None:
            return ProviderStatus(
                id="anthropic",
                source="pkce",
                expires_in_seconds=cred.expires_in_seconds(),
                username=None,
            )

    # 2. Claude Code CLI ~/.claude/.credentials.json
    try:
        cc_cred = claude_code_import.read_claude_code_credentials()
    except claude_code_import.ClaudeCodeCredentialsMalformed:
        cc_cred = None
    if cc_cred is not None:
        return ProviderStatus(
            id="anthropic",
            source="claude-code",
            expires_in_seconds=cc_cred.expires_in_seconds(),
            username=None,
        )

    # 3. ANTHROPIC_TOKEN env (manual OAuth override)
    if os.environ.get("ANTHROPIC_TOKEN"):
        return ProviderStatus(id="anthropic", source="env", expires_in_seconds=None)

    # 4. Config-stored api_key
    cfg = dict(config_snapshot())
    providers_cfg = cfg.get("providers") or {}
    block = providers_cfg.get("anthropic") if isinstance(providers_cfg, dict) else None
    if isinstance(block, dict):
        api_key = block.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            return ProviderStatus(id="anthropic", source="api-key", expires_in_seconds=None)
        if isinstance(api_key, dict):
            env_ref = api_key.get("env")
            value = api_key.get("value")
            if isinstance(env_ref, str) and env_ref and os.environ.get(env_ref):
                return ProviderStatus(id="anthropic", source="api-key", expires_in_seconds=None)
            if isinstance(value, str) and value.strip():
                return ProviderStatus(id="anthropic", source="api-key", expires_in_seconds=None)

    # 5. ANTHROPIC_API_KEY env (legacy)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ProviderStatus(id="anthropic", source="api-key", expires_in_seconds=None)

    return ProviderStatus(id="anthropic", source="none", expires_in_seconds=None)


def _expires_in_from_ms(expires_at_ms: int | None) -> int | None:
    """Translate an absolute expiry (ms) to a relative ``expires_in`` (s)."""
    if not isinstance(expires_at_ms, int) or expires_at_ms <= 0:
        return None
    delta_ms = expires_at_ms - int(time.time() * 1000)
    return max(0, delta_ms // 1000)


def _codex_status_row() -> ProviderStatus:
    """Read-only Codex CLI detection (``~/.codex/auth.json``)."""
    status = codex_external.read_codex_status()
    if status is None or not status.detected:
        return ProviderStatus(id="codex", source="none", expires_in_seconds=None)
    return ProviderStatus(
        id="codex",
        source="external-cli",
        expires_in_seconds=_expires_in_from_ms(status.expires_at_ms),
        username=status.account_id,
    )


def _gemini_status_row() -> ProviderStatus:
    """Read-only Gemini CLI detection (``~/.gemini/oauth_creds.json``)."""
    status = gemini_external.read_gemini_status()
    if status is None or not status.detected:
        return ProviderStatus(id="gemini", source="none", expires_in_seconds=None)
    return ProviderStatus(
        id="gemini",
        source="external-cli",
        expires_in_seconds=_expires_in_from_ms(status.expires_at_ms),
        username=status.account_id,
    )


def _xai_status_row(state: AdminState) -> ProviderStatus:
    """Stored xAI PKCE token (``<data_dir>/.oauth/xai.json``).

    No env-var / api-key fallback this round — corlinman doesn't have a
    runtime xAI provider yet (see ``xai_pkce.py`` docstring TODO). The
    file presence is the sole signal.
    """
    if state.data_dir is None:
        return ProviderStatus(id="xai", source="none", expires_in_seconds=None)
    cred = load_credential(state.data_dir, "xai")
    if cred is None:
        return ProviderStatus(id="xai", source="none", expires_in_seconds=None)
    return ProviderStatus(
        id="xai",
        source="pkce",
        expires_in_seconds=cred.expires_in_seconds(),
        username=None,
    )


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
