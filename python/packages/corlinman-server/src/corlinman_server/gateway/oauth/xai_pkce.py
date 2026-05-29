"""xAI Grok OAuth PKCE driver.

Port of hermes ``hermes_cli/auth.py::_xai_oauth_*`` helpers (lines
2062-3192 and 5286-5454), collapsed into the same three-async-functions
shape that :mod:`anthropic_pkce` exposes: :func:`generate_pkce_pair`,
:func:`build_authorize_url`, :func:`exchange_code`,
:func:`refresh_token`.

URL / client-id / scope constants are mirrored **verbatim** from
``/Users/cornna/project/hermes-agent/hermes_cli/auth.py`` (see
:data:`XAI_OAUTH_CLIENT_ID` / :data:`XAI_OAUTH_SCOPE` / etc. below for
the exact line cites). Mirroring rather than re-deriving is intentional:
the xAI OAuth client_id is the upstream Grok-CLI client that xAI's
consent screen accepts; using anything else gets a 403 from
``accounts.x.ai`` for loopback OAuth.

The token endpoint is discovered at runtime via OIDC discovery
(``${XAI_OAUTH_ISSUER}/.well-known/openid-configuration``), the same way
hermes does it — the issuer publishes the live token / authorize
endpoints and we re-validate them at every refresh to avoid pinning a
stale endpoint that may have been MITM'd at first contact.

----
TODO (corlinman): the persisted credential lands in
``<data_dir>/.oauth/xai.json`` but **no provider class consumes it yet**.
Corlinman's ``corlinman-providers`` package today has no xAI provider
(only ``anthropic``, ``openai``, ``openai_compatible``, ``google``,
``mock``). When a runtime xAI provider lands, extend its
``_resolve_credential()`` chain to read the OAuth file first (mirror
:mod:`corlinman_providers.anthropic_provider`'s W-A1 pattern). Until
then this module stores credentials but the runtime request path
won't reach for them.
----

Networking is via ``httpx.AsyncClient`` so callers running inside
FastAPI get connection pooling + cancellation. We do not log
``access_token`` or ``refresh_token`` anywhere — even at DEBUG.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
import uuid
from typing import Any, Final

import httpx
import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants — mirrored verbatim from hermes_cli/auth.py
# ---------------------------------------------------------------------------


# Hermes ``hermes_cli/auth.py:93-99``
XAI_OAUTH_ISSUER: Final[str] = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL: Final[str] = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID: Final[str] = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE: Final[str] = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_REDIRECT_HOST: Final[str] = "127.0.0.1"
XAI_OAUTH_REDIRECT_PORT: Final[int] = 56121
XAI_OAUTH_REDIRECT_PATH: Final[str] = "/callback"


def _default_redirect_uri() -> str:
    """Build the loopback redirect URI hermes uses by default."""
    return f"http://{XAI_OAUTH_REDIRECT_HOST}:{XAI_OAUTH_REDIRECT_PORT}{XAI_OAUTH_REDIRECT_PATH}"


# UA tag — hermes uses the bare ``httpx`` UA; we add a corlinman flavour
# so xAI's edge logs can attribute traffic. Not security-sensitive.
_USER_AGENT: Final[str] = "corlinman-gateway/1.0 (xai-oauth)"


# ---------------------------------------------------------------------------
# PKCE primitives (same algorithm as anthropic_pkce — RFC 7636 S256)
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str]:
    """Generate ``(code_verifier, code_challenge)``.

    Verifier is 43 chars (256 bits of entropy, URL-safe base64, no
    padding) — well inside the RFC 7636 43-128 length window. Challenge
    is SHA256(verifier), URL-safe base64, no padding (S256 method).
    Matches the algorithm hermes uses at
    ``hermes_cli/auth.py::_oauth_pkce_code_verifier`` (line 1930).
    """
    verifier_bytes = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    challenge_bytes = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(challenge_bytes).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    """Mint a CSRF state value (hex uuid4 — matches hermes line 5329)."""
    return uuid.uuid4().hex


def generate_nonce() -> str:
    """Mint an OIDC nonce value (hex uuid4 — matches hermes line 5330)."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# OIDC discovery
# ---------------------------------------------------------------------------


class OAuthExchangeError(Exception):
    """Raised when an xAI OAuth endpoint rejects a request."""


def _validate_xai_endpoint(url: str, *, field: str) -> str:
    """Sanity-check that a discovered endpoint really points at xAI.

    Mirrors hermes ``_xai_validate_oauth_endpoint`` (line 2997). An
    auth.json written by an older hermes / corlinman (or hand-edited)
    may carry a non-xAI ``token_endpoint`` that would receive every
    future ``refresh_token`` in plaintext if we trusted it blindly. We
    do a cheap suffix check; non-conforming URLs raise so the caller
    can re-discover.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise OAuthExchangeError(f"{field} is not a valid URL: {exc}") from exc
    if parsed.scheme != "https":
        raise OAuthExchangeError(f"{field} must use https; got {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not (host == "x.ai" or host.endswith(".x.ai")):
        raise OAuthExchangeError(
            f"{field} host {host!r} is not under x.ai — refusing to trust"
        )
    return url


async def discover_endpoints(
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 15.0,
) -> dict[str, str]:
    """Hit the issuer's OIDC discovery and return validated endpoints.

    Returns ``{"authorization_endpoint": str, "token_endpoint": str}``.
    Raises :class:`OAuthExchangeError` for anything that doesn't look
    like a healthy discovery payload.
    """
    own = client is None
    cli = client or httpx.AsyncClient(timeout=timeout)
    try:
        try:
            resp = await cli.get(
                XAI_OAUTH_DISCOVERY_URL,
                headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            )
        except httpx.HTTPError as exc:
            raise OAuthExchangeError(f"discovery network error: {exc}") from exc
    finally:
        if own:
            await cli.aclose()

    if resp.status_code != 200:
        raise OAuthExchangeError(
            f"discovery returned HTTP {resp.status_code}"
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise OAuthExchangeError("discovery returned non-JSON body") from exc
    if not isinstance(payload, dict):
        raise OAuthExchangeError("discovery body is not an object")

    auth_ep = str(payload.get("authorization_endpoint") or "").strip()
    token_ep = str(payload.get("token_endpoint") or "").strip()
    if not auth_ep or not token_ep:
        raise OAuthExchangeError("discovery body missing required endpoints")
    _validate_xai_endpoint(auth_ep, field="authorization_endpoint")
    _validate_xai_endpoint(token_ep, field="token_endpoint")
    return {"authorization_endpoint": auth_ep, "token_endpoint": token_ep}


# ---------------------------------------------------------------------------
# Authorize URL — mirrors hermes _xai_oauth_build_authorize_url (line 5286)
# ---------------------------------------------------------------------------


def build_authorize_url(
    *,
    authorization_endpoint: str,
    code_challenge: str,
    state: str,
    nonce: str,
    redirect_uri: str | None = None,
) -> str:
    """Build the URL the operator opens in their browser to consent.

    The ``plan=generic`` and ``referrer=hermes-agent`` query params are
    copied verbatim from hermes
    (``hermes_cli/auth.py:5294-5311``). hermes's comment is worth
    keeping: ``plan=generic`` opts the consent screen into xAI's
    generic OAuth plan tier instead of falling back to the per-account
    default; without it ``accounts.x.ai`` rejects loopback OAuth from
    non-allowlisted clients. ``referrer=hermes-agent`` is a best-effort
    attribution tag — we keep it as-is so we inherit hermes's allowlist
    rather than getting rejected for advertising a fresh client.
    """
    params = {
        "response_type": "code",
        "client_id": XAI_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri or _default_redirect_uri(),
        "scope": XAI_OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
        "referrer": "hermes-agent",
    }
    return f"{authorization_endpoint}?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------


def _coerce_token_response(
    payload: Any,
    *,
    fallback_refresh_token: str | None = None,
) -> dict[str, Any]:
    """Validate the upstream JSON and stamp ``expires_at_ms``.

    Same shape as :mod:`anthropic_pkce._coerce_token_response`; the xAI
    payload has the standard OAuth2 fields plus an ``id_token`` we keep
    for forward use.
    """
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
    out: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": new_refresh,
        "expires_at_ms": int(time.time() * 1000) + (expires_in * 1000),
        "scope": scope,
    }
    id_token = payload.get("id_token")
    if isinstance(id_token, str) and id_token:
        out["id_token"] = id_token
    return out


async def exchange_code(
    *,
    token_endpoint: str,
    code: str,
    code_verifier: str,
    redirect_uri: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Exchange the authorization code for tokens.

    Mirrors hermes ``_xai_oauth_loopback_login`` lines 5395-5449:
    form-urlencoded body, ``grant_type=authorization_code``, the four
    canonical fields (code, redirect_uri, client_id, code_verifier).

    Returns the same coerced dict shape as the Anthropic driver.
    Raises :class:`OAuthExchangeError` on every failure mode.
    """
    code = (code or "").strip()
    if not code:
        raise OAuthExchangeError("empty authorization code")

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri or _default_redirect_uri(),
        "client_id": XAI_OAUTH_CLIENT_ID,
        "code_verifier": code_verifier,
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
            resp = await cli.post(token_endpoint, data=body, headers=headers)
        except httpx.HTTPError as exc:
            raise OAuthExchangeError(f"network error: {exc}") from exc
    finally:
        if own:
            await cli.aclose()

    if resp.status_code >= 400:
        raise OAuthExchangeError(f"token exchange returned HTTP {resp.status_code}")

    try:
        result = resp.json()
    except ValueError as exc:
        raise OAuthExchangeError("token endpoint returned non-JSON body") from exc

    return _coerce_token_response(result)


async def refresh_token(
    *,
    refresh_token: str,  # noqa: A002 — argument name reads as a verb here
    token_endpoint: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Refresh an xAI OAuth access token.

    Mirrors hermes ``refresh_xai_oauth_pure`` (line 3087-3160): form-
    urlencoded body, ``grant_type=refresh_token``, ``client_id``,
    ``refresh_token``. xAI may or may not rotate the refresh token; we
    fall back to the input when it omits a new one (mirrors hermes line
    3154).
    """
    if not refresh_token:
        raise OAuthExchangeError("refresh_token is required")

    body = {
        "grant_type": "refresh_token",
        "client_id": XAI_OAUTH_CLIENT_ID,
        "refresh_token": refresh_token,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }

    own = client is None
    cli = client or httpx.AsyncClient(timeout=15.0)
    try:
        try:
            resp = await cli.post(token_endpoint, data=body, headers=headers)
        except httpx.HTTPError as exc:
            raise OAuthExchangeError(f"network error: {exc}") from exc
    finally:
        if own:
            await cli.aclose()

    if resp.status_code >= 400:
        raise OAuthExchangeError(f"refresh returned HTTP {resp.status_code}")

    try:
        result = resp.json()
    except ValueError as exc:
        raise OAuthExchangeError("refresh endpoint returned non-JSON body") from exc

    return _coerce_token_response(result, fallback_refresh_token=refresh_token)


__all__ = [
    "XAI_OAUTH_CLIENT_ID",
    "XAI_OAUTH_DISCOVERY_URL",
    "XAI_OAUTH_ISSUER",
    "XAI_OAUTH_REDIRECT_HOST",
    "XAI_OAUTH_REDIRECT_PATH",
    "XAI_OAUTH_REDIRECT_PORT",
    "XAI_OAUTH_SCOPE",
    "OAuthExchangeError",
    "build_authorize_url",
    "discover_endpoints",
    "exchange_code",
    "generate_nonce",
    "generate_pkce_pair",
    "generate_state",
    "refresh_token",
]
