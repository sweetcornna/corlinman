"""Anthropic OAuth PKCE driver.

Port of hermes ``agent/anthropic_adapter.py::_generate_pkce`` +
``run_hermes_oauth_login_pure`` + ``refresh_anthropic_oauth_pure``,
collapsed into three async functions that fit the corlinman HTTP-router
shape: :func:`generate_pkce_pair`, :func:`build_authorize_url`,
:func:`exchange_code`, :func:`refresh_token`.

URL / client-id constants are mirrored verbatim from
``/Users/cornna/project/hermes-agent/agent/anthropic_adapter.py`` lines
1041-1045 (and ``hermes_cli/web_server.py:1640``). Mirroring rather than
re-deriving is intentional: the Anthropic PKCE shape is undocumented and
the hermes constants are what works against today's production endpoints.

Networking is via ``httpx.AsyncClient`` so callers running inside FastAPI
get connection pooling + cancellation. We do not log access_token or
refresh_token anywhere — even at DEBUG.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
from typing import Any, Final

import httpx
import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants (mirrored from hermes — see module docstring)
# ---------------------------------------------------------------------------


# Anthropic-issued PKCE client id. Same one Claude Code uses; not secret.
ANTHROPIC_OAUTH_CLIENT_ID: Final[str] = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

# Authorize endpoint: claude.ai hosts the consent screen, redirects to
# console.anthropic.com which surfaces the code as `<code>#<state>`.
ANTHROPIC_OAUTH_AUTHORIZE_URL: Final[str] = "https://claude.ai/oauth/authorize"

# Token endpoint: hermes uses ``console.anthropic.com/v1/oauth/token``
# verbatim; its companion ``platform.claude.com/v1/oauth/token`` is the
# preferred refresh endpoint and we try that first on refresh because it
# is the newer canonical surface (see ``refresh_anthropic_oauth_pure``
# in hermes for the failover order).
ANTHROPIC_OAUTH_TOKEN_URL: Final[str] = "https://console.anthropic.com/v1/oauth/token"
ANTHROPIC_OAUTH_TOKEN_URL_FALLBACKS: Final[tuple[str, ...]] = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)

# Claude Code's loopback redirect URI — Anthropic's PKCE flow uses a
# fixed callback page that surfaces the code for the user to copy.
ANTHROPIC_OAUTH_REDIRECT_URI: Final[str] = "https://console.anthropic.com/oauth/code/callback"

# Scope set Anthropic grants to subscription clients. Includes
# ``user:inference`` (the actual chat permission), ``user:profile`` (so
# the gateway can show the logged-in username), and
# ``org:create_api_key`` (Anthropic's flow requires this even when we
# never call the create-key endpoint).
ANTHROPIC_OAUTH_SCOPES: Final[str] = "org:create_api_key user:profile user:inference"


# UA string Anthropic's edge accepts. We send a corlinman-flavoured UA
# but include the ``claude-cli`` token because the refresh endpoint
# 403s requests that omit it (verified against hermes which copies the
# exact pattern).
_USER_AGENT: Final[str] = "corlinman-gateway/1.0 (claude-cli compatible)"


# ---------------------------------------------------------------------------
# PKCE primitives
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str]:
    """Generate ``(code_verifier, code_challenge)``.

    Verifier is 43 chars (256 bits of entropy, URL-safe base64, no padding)
    — well inside the RFC 7636 43-128 length window. Challenge is the
    SHA256 of the verifier, URL-safe base64, no padding (the S256
    method).
    """
    verifier_bytes = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    challenge_bytes = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(challenge_bytes).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(*, code_challenge: str, state: str) -> str:
    """Build the URL the operator opens in their browser to consent."""
    params = {
        "code": "true",
        "client_id": ANTHROPIC_OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": ANTHROPIC_OAUTH_REDIRECT_URI,
        "scope": ANTHROPIC_OAUTH_SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{ANTHROPIC_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------
# Token endpoints
# ---------------------------------------------------------------------------


class OAuthExchangeError(Exception):
    """Raised when the Anthropic token endpoint rejects a request."""


def _parse_code_input(code_input: str) -> tuple[str, str | None]:
    """Anthropic's callback page formats the code as ``<code>#<state>``.

    Operators usually paste the whole string; we accept either form.
    """
    parts = code_input.strip().split("#", 1)
    code = parts[0].strip()
    state = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return code, state


async def exchange_code(
    *,
    code_input: str,
    code_verifier: str,
    expected_state: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Exchange the authorization code for ``access_token`` + ``refresh_token``.

    ``code_input`` may include the trailing ``#state`` suffix; we strip
    it before sending. Returns a dict with keys ``access_token``,
    ``refresh_token``, ``expires_at_ms``, ``scope``. Raises
    :class:`OAuthExchangeError` on every failure mode (network, HTTP
    non-2xx, malformed JSON, missing ``access_token``).

    A caller-supplied ``client`` is reused when present so tests can inject
    a ``MockTransport`` without us juggling a global.
    """
    code, callback_state = _parse_code_input(code_input)
    if not code:
        raise OAuthExchangeError("empty authorization code")
    state_to_send = callback_state or expected_state

    body = {
        "grant_type": "authorization_code",
        "client_id": ANTHROPIC_OAUTH_CLIENT_ID,
        "code": code,
        "state": state_to_send,
        "redirect_uri": ANTHROPIC_OAUTH_REDIRECT_URI,
        "code_verifier": code_verifier,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }

    own_client = client is None
    cli = client or httpx.AsyncClient(timeout=20.0)
    try:
        try:
            resp = await cli.post(ANTHROPIC_OAUTH_TOKEN_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise OAuthExchangeError(f"network error: {exc}") from exc
    finally:
        if own_client:
            await cli.aclose()

    if resp.status_code >= 400:
        # Surface the upstream status but do not echo the body — it may
        # contain the verifier or the code on some error paths.
        raise OAuthExchangeError(f"token exchange returned HTTP {resp.status_code}")

    try:
        result = resp.json()
    except ValueError as exc:
        raise OAuthExchangeError("token endpoint returned non-JSON body") from exc

    return _coerce_token_response(result)


async def refresh_token(
    *,
    refresh_token: str,  # noqa: A002 — argument name reads as a verb here
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Refresh an Anthropic OAuth access token.

    Mirrors hermes ``refresh_anthropic_oauth_pure``: tries the modern
    ``platform.claude.com`` endpoint first then falls back to
    ``console.anthropic.com``. Anthropic rotates the refresh token on
    each call; we surface the new one in the return value so the caller
    can persist it. Returns the same dict shape as :func:`exchange_code`.

    When the upstream omits a new ``refresh_token`` (some legacy paths
    do), we preserve the input ``refresh_token`` so the credential
    remains usable.
    """
    if not refresh_token:
        raise OAuthExchangeError("refresh_token is required")

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
                last_error = OAuthExchangeError(
                    f"refresh returned HTTP {resp.status_code} at {endpoint}"
                )
                continue
            try:
                result = resp.json()
            except ValueError as exc:
                last_error = exc
                continue
            coerced = _coerce_token_response(result, fallback_refresh_token=refresh_token)
            return coerced
    finally:
        if own_client:
            await cli.aclose()

    assert last_error is not None
    if isinstance(last_error, OAuthExchangeError):
        raise last_error
    raise OAuthExchangeError(f"refresh failed: {last_error}") from last_error


def _coerce_token_response(
    payload: Any,
    *,
    fallback_refresh_token: str | None = None,
) -> dict[str, Any]:
    """Validate the upstream JSON and stamp ``expires_at_ms``."""
    if not isinstance(payload, dict):
        raise OAuthExchangeError("token endpoint returned non-object body")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthExchangeError("token endpoint omitted access_token")
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
    "ANTHROPIC_OAUTH_AUTHORIZE_URL",
    "ANTHROPIC_OAUTH_CLIENT_ID",
    "ANTHROPIC_OAUTH_REDIRECT_URI",
    "ANTHROPIC_OAUTH_SCOPES",
    "ANTHROPIC_OAUTH_TOKEN_URL",
    "ANTHROPIC_OAUTH_TOKEN_URL_FALLBACKS",
    "OAuthExchangeError",
    "build_authorize_url",
    "exchange_code",
    "generate_pkce_pair",
    "refresh_token",
]
