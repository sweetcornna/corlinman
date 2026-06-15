"""Codex (OpenAI ChatGPT subscription) OAuth PKCE driver.

Port of the codex CLI's own login flow: same client_id, scopes, and
loopback redirect_uri that the upstream ``codex login`` uses. Constants
mirror hermes ``hermes_cli/auth.py`` (``CODEX_OAUTH_CLIENT_ID``,
``CODEX_OAUTH_TOKEN_URL``) verbatim — OpenAI's consent screen will
reject any other client_id.

Why a paste-code flow:

The registered redirect_uri is ``http://localhost:1455/auth/callback``
(the codex CLI's loopback listener). corlinman is remote (VPS); we
can't bind 1455 on the user's browser-side machine. Instead the operator
opens the auth URL, OpenAI redirects to ``localhost:1455/...?code=...``
which fails with "connection refused" — but the **URL bar shows the
code**. Operator copies that URL (or just the ``code`` query param)
back into the UI, the gateway exchanges it for tokens, and writes the
result to ``~/.codex/auth.json`` in the same shape codex CLI would.

Refresh works because OpenAI honours ``grant_type=refresh_token`` on the
same client_id without re-presenting the redirect_uri.

We do not log ``access_token`` / ``refresh_token`` / ``id_token``
anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants — mirrored from hermes_cli/auth.py + codex CLI runtime output.
# Do NOT change client_id / scope / redirect_uri without re-checking
# what auth.openai.com's consent screen accepts; the only registered
# tuple is the one the upstream CLI uses.
# ---------------------------------------------------------------------------


CODEX_OAUTH_CLIENT_ID: Final[str] = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_AUTHORIZE_URL: Final[str] = "https://auth.openai.com/oauth/authorize"
CODEX_OAUTH_TOKEN_URL: Final[str] = "https://auth.openai.com/oauth/token"
CODEX_OAUTH_SCOPE: Final[str] = (
    "openid profile email offline_access "
    "api.connectors.read api.connectors.invoke"
)
CODEX_OAUTH_REDIRECT_URI: Final[str] = "http://localhost:1455/auth/callback"

_USER_AGENT: Final[str] = "corlinman-gateway/1.0 (codex-oauth)"


class CodexOAuthError(Exception):
    """Raised by any predictable failure in the codex OAuth pipeline."""


# ---------------------------------------------------------------------------
# PKCE primitives (RFC 7636 S256 — same algorithm as xai_pkce)
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str]:
    verifier_bytes = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    challenge_bytes = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(challenge_bytes).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# Authorize URL — matches the codex CLI's emitted URL field-for-field.
# Two CLI-specific tags must be carried verbatim:
#   id_token_add_organizations=true     — codex CLI sends this
#   codex_cli_simplified_flow=true      — gates the consent screen format
#   originator=codex_cli_rs             — best-effort attribution tag
# Removing any of them makes auth.openai.com flag the request as
# "unsupported flow" and 4xx the exchange.
# ---------------------------------------------------------------------------


def build_authorize_url(*, code_challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CODEX_OAUTH_CLIENT_ID,
        "redirect_uri": CODEX_OAUTH_REDIRECT_URI,
        "scope": CODEX_OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "codex_cli_rs",
    }
    return f"{CODEX_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------
# Token exchange + refresh
# ---------------------------------------------------------------------------


def _coerce_token_response(
    payload: Any,
    *,
    fallback_refresh_token: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CodexOAuthError("token endpoint returned non-object body")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise CodexOAuthError("token endpoint omitted access_token")
    new_refresh = payload.get("refresh_token")
    if not isinstance(new_refresh, str) or not new_refresh:
        new_refresh = fallback_refresh_token
    expires_in = payload.get("expires_in")
    if not isinstance(expires_in, int) or expires_in <= 0:
        expires_in = 3600
    out: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": new_refresh,
        "expires_at_ms": int(time.time() * 1000) + (expires_in * 1000),
    }
    id_token = payload.get("id_token")
    if isinstance(id_token, str) and id_token:
        out["id_token"] = id_token
    scope = payload.get("scope")
    if isinstance(scope, str):
        out["scope"] = scope
    return out


async def exchange_code(
    *,
    code: str,
    code_verifier: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    code = (code or "").strip()
    if not code:
        raise CodexOAuthError("empty authorization code")

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CODEX_OAUTH_REDIRECT_URI,
        "client_id": CODEX_OAUTH_CLIENT_ID,
        "code_verifier": code_verifier,
    }
    return await _post_token(body, client=client)


async def refresh_token(
    *,
    refresh_token: str,  # noqa: A002
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if not refresh_token:
        raise CodexOAuthError("refresh_token is required")
    body = {
        "grant_type": "refresh_token",
        "client_id": CODEX_OAUTH_CLIENT_ID,
        "refresh_token": refresh_token,
        "scope": CODEX_OAUTH_SCOPE,
    }
    return await _post_token(body, client=client, fallback_refresh=refresh_token)


async def _post_token(
    body: dict[str, str],
    *,
    client: httpx.AsyncClient | None,
    fallback_refresh: str | None = None,
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    own = client is None
    cli = client or httpx.AsyncClient(timeout=20.0)
    try:
        try:
            resp = await cli.post(CODEX_OAUTH_TOKEN_URL, data=body, headers=headers)
        except httpx.HTTPError as exc:
            raise CodexOAuthError(f"network error: {exc}") from exc
    finally:
        if own:
            await cli.aclose()
    if resp.status_code >= 400:
        # Surface the upstream error body so the operator can see what
        # OpenAI didn't like (consent revoked? expired refresh?). We
        # truncate to keep response size sane.
        detail = resp.text[:400] if resp.text else "<empty>"
        raise CodexOAuthError(
            f"token endpoint returned HTTP {resp.status_code}: {detail}"
        )
    try:
        result = resp.json()
    except ValueError as exc:
        raise CodexOAuthError("token endpoint returned non-JSON body") from exc
    return _coerce_token_response(result, fallback_refresh_token=fallback_refresh)


# ---------------------------------------------------------------------------
# ~/.codex/auth.json persistence — write side
#
# Shape mirrors what the official codex CLI writes (so any tool that
# reads the file, including our own codex_external.read_codex_status,
# keeps working). `OPENAI_API_KEY` is left null — the codex CLI fills it
# only when the operator opts into "use my ChatGPT subscription's API
# key" which isn't part of the PKCE flow.
# ---------------------------------------------------------------------------


def _codex_auth_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if not codex_home:
        codex_home = str(Path.home() / ".codex")
    return Path(codex_home).expanduser() / "auth.json"


def write_auth_json(tokens: dict[str, Any], *, path: Path | None = None) -> Path:
    """Persist tokens to ``~/.codex/auth.json`` in the CLI's canonical shape."""
    target = path or _codex_auth_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload: dict[str, Any] = {
        "tokens": {
            "access_token": tokens["access_token"],
        },
        "OPENAI_API_KEY": None,
        "last_refresh": datetime.now(UTC).isoformat(),
    }
    if tokens.get("refresh_token"):
        payload["tokens"]["refresh_token"] = tokens["refresh_token"]
    if tokens.get("id_token"):
        payload["tokens"]["id_token"] = tokens["id_token"]
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(target)
    return target


def delete_auth_json(*, path: Path | None = None) -> bool:
    """Remove ``~/.codex/auth.json``. Returns True iff a file was deleted."""
    target = path or _codex_auth_path()
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False


__all__ = [
    "CODEX_OAUTH_AUTHORIZE_URL",
    "CODEX_OAUTH_CLIENT_ID",
    "CODEX_OAUTH_REDIRECT_URI",
    "CODEX_OAUTH_SCOPE",
    "CODEX_OAUTH_TOKEN_URL",
    "CodexOAuthError",
    "build_authorize_url",
    "delete_auth_json",
    "exchange_code",
    "generate_pkce_pair",
    "generate_state",
    "refresh_token",
    "write_auth_json",
]
