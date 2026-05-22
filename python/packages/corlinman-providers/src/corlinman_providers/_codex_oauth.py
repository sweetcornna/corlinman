"""Provider-local Codex OAuth helpers.

Reads ``~/.codex/auth.json`` (written by the official Codex CLI) and
refreshes the access token when close to expiry.  The interactive OAuth
flow lives in ``corlinman_server.gateway.oauth.codex_pkce``; this module
only does the read + refresh the provider adapter needs at request time.

Never writes to ``~/.codex/auth.json`` directly — that file is owned by
the Codex CLI.  Refreshed tokens are cached in-memory on the credential
object; the Codex CLI will refresh its own copy on next run.

Never logs ``access_token`` / ``refresh_token``.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

CODEX_OAUTH_CLIENT_ID: str = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL: str = "https://auth.openai.com/oauth/token"
CODEX_OAUTH_SCOPE: str = (
    "openid profile email offline_access "
    "api.connectors.read api.connectors.invoke"
)
_REFRESH_SKEW_SECONDS: int = 300  # refresh 5 min before JWT expiry
_USER_AGENT: str = "corlinman-gateway/1.0 (codex-oauth)"


class CodexOAuthRefreshError(Exception):
    """Raised when the token refresh request fails."""


@dataclass(frozen=True)
class CodexOAuthCredential:
    """In-memory credential bundle read from ``~/.codex/auth.json``."""

    access_token: str
    refresh_token: str | None
    expires_at_ms: int | None  # derived from JWT ``exp`` claim

    def is_expired(self) -> bool:
        """True when the access token needs refreshing."""
        if self.expires_at_ms is None:
            return False
        now_ms = int(time.time() * 1000)
        return now_ms >= (self.expires_at_ms - _REFRESH_SKEW_SECONDS * 1000)

    def with_refreshed(
        self,
        *,
        access_token: str,
        refresh_token: str | None,
        expires_at_ms: int | None,
    ) -> CodexOAuthCredential:
        return replace(
            self,
            access_token=access_token,
            refresh_token=refresh_token if refresh_token else self.refresh_token,
            expires_at_ms=expires_at_ms,
        )


# ---------------------------------------------------------------------------
# Path resolution — mirrors corlinman_server.gateway.oauth.codex_external
# ---------------------------------------------------------------------------


def _codex_auth_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if not codex_home:
        codex_home = str(Path.home() / ".codex")
    return Path(codex_home).expanduser() / "auth.json"


def _decode_jwt_exp(token: str) -> int | None:
    """Best-effort decode of the ``exp`` claim from a JWT without verifying signature."""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload_b64 = token.split(".", 2)[1]
    pad = "=" * ((4 - len(payload_b64) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + pad)
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    if isinstance(exp, (int, float)) and exp > 0:
        return int(exp * 1000)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_codex_credential(path: Path | None = None) -> CodexOAuthCredential | None:
    """Read ``~/.codex/auth.json`` and return a credential bundle.

    Returns ``None`` when the file is absent, unreadable, or has no valid
    ``access_token``.  Malformed JSON / unexpected shapes silently return
    ``None`` (matches the approach in ``codex_external.read_codex_status``).
    """
    target = path or _codex_auth_path()
    if not target.is_file():
        return None
    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    refresh_token: str | None = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = None
    expires_at_ms = _decode_jwt_exp(access_token)
    return CodexOAuthCredential(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_ms=expires_at_ms,
    )


async def refresh_codex_token(
    *,
    refresh_token: str,
    client: httpx.AsyncClient | None = None,
) -> CodexOAuthCredential:
    """Refresh the Codex access token using ``grant_type=refresh_token``.

    Returns a new :class:`CodexOAuthCredential` on success.
    Raises :class:`CodexOAuthRefreshError` on any failure.
    """
    if not refresh_token:
        raise CodexOAuthRefreshError("refresh_token is required")

    body = {
        "grant_type": "refresh_token",
        "client_id": CODEX_OAUTH_CLIENT_ID,
        "refresh_token": refresh_token,
        "scope": CODEX_OAUTH_SCOPE,
    }
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
            raise CodexOAuthRefreshError(f"network error: {exc}") from exc
    finally:
        if own:
            await cli.aclose()

    if resp.status_code >= 400:
        detail = resp.text[:400] if resp.text else "<empty>"
        raise CodexOAuthRefreshError(
            f"token endpoint returned HTTP {resp.status_code}: {detail}"
        )
    try:
        result = resp.json()
    except ValueError as exc:
        raise CodexOAuthRefreshError("token endpoint returned non-JSON body") from exc

    new_access = result.get("access_token")
    if not isinstance(new_access, str) or not new_access:
        raise CodexOAuthRefreshError("token endpoint omitted access_token")

    new_refresh: str | None = result.get("refresh_token")
    if not isinstance(new_refresh, str) or not new_refresh:
        new_refresh = refresh_token  # keep old one if not rotated

    expires_in = result.get("expires_in")
    if isinstance(expires_in, int) and expires_in > 0:
        expires_at_ms: int | None = int(time.time() * 1000) + expires_in * 1000
    else:
        expires_at_ms = _decode_jwt_exp(new_access)

    return CodexOAuthCredential(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_at_ms=expires_at_ms,
    )


def _extract_chatgpt_account_id(access_token: str) -> str | None:
    """Extract chatgpt_account_id from the Codex OAuth JWT claims."""
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad))
        return claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
    except Exception:  # noqa: BLE001
        return None


def codex_cloudflare_headers(access_token: str) -> dict[str, str]:
    """Headers required to bypass Cloudflare on chatgpt.com/backend-api/codex."""
    headers: dict[str, str] = {
        "User-Agent": "codex_cli_rs/0.0.0",
        "originator": "codex_cli_rs",
    }
    acct_id = _extract_chatgpt_account_id(access_token)
    if acct_id:
        headers["ChatGPT-Account-ID"] = acct_id
    return headers


__all__ = [
    "CodexOAuthCredential",
    "CodexOAuthRefreshError",
    "codex_cloudflare_headers",
    "load_codex_credential",
    "refresh_codex_token",
]
