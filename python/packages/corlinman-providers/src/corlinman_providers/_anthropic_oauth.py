"""Provider-local Anthropic OAuth helpers.

The gateway owns the interactive OAuth routes, but the provider package
must be able to consume the credential file without importing
``corlinman_server``. This module mirrors only the small storage and refresh
surface the Anthropic adapter needs.
"""

from __future__ import annotations

import json
import os
import stat
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Final

import httpx

ANTHROPIC_OAUTH_CLIENT_ID: Final[str] = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_OAUTH_TOKEN_URL_FALLBACKS: Final[tuple[str, ...]] = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)

_USER_AGENT: Final[str] = "corlinman-gateway/1.0 (claude-cli compatible)"


@dataclass(frozen=True)
class AnthropicOAuthCredential:
    """Stored Anthropic OAuth credential bundle."""

    provider: str
    access_token: str
    refresh_token: str | None
    expires_at_ms: int | None
    scope: str | None
    obtained_at_ms: int

    def is_expired(self, *, skew_seconds: int = 0) -> bool:
        if self.expires_at_ms is None:
            return False
        return int(time.time() * 1000) >= (self.expires_at_ms - skew_seconds * 1000)

    def with_refreshed(
        self,
        *,
        access_token: str,
        refresh_token: str | None,
        expires_at_ms: int | None,
    ) -> AnthropicOAuthCredential:
        return replace(
            self,
            access_token=access_token,
            refresh_token=refresh_token if refresh_token else self.refresh_token,
            expires_at_ms=expires_at_ms,
            obtained_at_ms=int(time.time() * 1000),
        )


class AnthropicOAuthRefreshError(Exception):
    """Raised when the Anthropic token endpoint rejects a refresh."""


def _credential_path(data_dir: Path) -> Path:
    return Path(data_dir) / ".oauth" / "anthropic.json"


def load_anthropic_credential(data_dir: Path) -> AnthropicOAuthCredential | None:
    """Read the Anthropic OAuth credential file, returning ``None`` if absent."""
    path = _credential_path(data_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    access_token = raw.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    obtained_at = raw.get("obtained_at_ms")
    if not isinstance(obtained_at, int):
        obtained_at = int(time.time() * 1000)
    return AnthropicOAuthCredential(
        provider="anthropic",
        access_token=access_token,
        refresh_token=raw.get("refresh_token") if isinstance(raw.get("refresh_token"), str) else None,
        expires_at_ms=raw.get("expires_at_ms") if isinstance(raw.get("expires_at_ms"), int) else None,
        scope=raw.get("scope") if isinstance(raw.get("scope"), str) else None,
        obtained_at_ms=obtained_at,
    )


def save_anthropic_credential(
    data_dir: Path, credential: AnthropicOAuthCredential
) -> Path:
    """Persist the Anthropic credential file with owner-only permissions."""
    path = _credential_path(data_dir)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        os.chmod(parent, stat.S_IRWXU)

    payload: dict[str, Any] = asdict(credential)
    tmp = path.with_suffix(".tmp")
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
    except Exception:
        with suppress(OSError):
            tmp.unlink()
        raise
    with suppress(OSError):
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
    with suppress(OSError):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


async def refresh_anthropic_token(
    *,
    refresh_token: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Refresh an Anthropic OAuth access token."""
    if not refresh_token:
        raise AnthropicOAuthRefreshError("refresh_token is required")

    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": ANTHROPIC_OAUTH_CLIENT_ID,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }

    own_client = client is None
    cli = client or httpx.AsyncClient(timeout=15.0)
    last_error: Exception | None = None
    try:
        for endpoint in ANTHROPIC_OAUTH_TOKEN_URL_FALLBACKS:
            try:
                resp = await cli.post(endpoint, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if resp.status_code >= 400:
                last_error = AnthropicOAuthRefreshError(
                    f"refresh returned HTTP {resp.status_code} at {endpoint}"
                )
                continue
            try:
                result = resp.json()
            except ValueError as exc:
                last_error = exc
                continue
            return _coerce_token_response(result, fallback_refresh_token=refresh_token)
    finally:
        if own_client:
            await cli.aclose()

    assert last_error is not None
    if isinstance(last_error, AnthropicOAuthRefreshError):
        raise last_error
    raise AnthropicOAuthRefreshError(f"refresh failed: {last_error}") from last_error


def _coerce_token_response(
    payload: Any,
    *,
    fallback_refresh_token: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AnthropicOAuthRefreshError("token endpoint returned non-object body")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AnthropicOAuthRefreshError("token endpoint omitted access_token")
    new_refresh = payload.get("refresh_token")
    if not isinstance(new_refresh, str) or not new_refresh:
        new_refresh = fallback_refresh_token
    expires_in = payload.get("expires_in")
    if not isinstance(expires_in, int) or expires_in <= 0:
        expires_in = 3600
    scope = payload.get("scope")
    if not isinstance(scope, str):
        scope = None
    return {
        "access_token": access_token,
        "refresh_token": new_refresh,
        "expires_at_ms": int(time.time() * 1000) + (expires_in * 1000),
        "scope": scope,
    }


__all__ = [
    "AnthropicOAuthCredential",
    "AnthropicOAuthRefreshError",
    "load_anthropic_credential",
    "refresh_anthropic_token",
    "save_anthropic_credential",
]
