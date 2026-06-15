"""Module-level support extracted from ``routes_admin_b/oauth.py``.

Holds the wire models and pure helper functions for the
``/admin/oauth/*`` routes. This is a behavior-preserving split: the code
below was moved verbatim out of the route module. MUST NOT import the
route module (``corlinman_server.gateway.routes_admin_b.oauth``) — that
would create an import cycle.
"""

from __future__ import annotations

import hmac
import os
import time
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from corlinman_server.gateway.oauth import (
    claude_code_import,
    codex_external,
    gemini_external,
    load_credential,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
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


def _codex_status_row(state: AdminState | None = None) -> ProviderStatus:
    """Read-only Codex CLI detection (``~/.codex/auth.json``)."""
    path = None
    data_dir = getattr(state, "data_dir", None) if state is not None else None
    if data_dir is not None:
        path = Path(data_dir) / ".codex" / "auth.json"
    status = codex_external.read_codex_status(path)
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
