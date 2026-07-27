"""Inbound OIDC SSO login — ``/auth/oidc/*`` (G3).

Authorization Code + PKCE flow against a single configured IdP
(``[auth.oidc]`` in ``config.toml``). Mirrors the state/verifier
handling of the outbound :mod:`corlinman_server.gateway.oauth.xai_pkce`
driver, but the *gateway* is the relying party here: the operator's
browser is bounced to the IdP and comes back to
``GET /auth/oidc/callback``, which — after full id_token validation
(JWKS signature, iss/aud/exp/nonce) and an email whitelist check —
reuses the **same** admin-session issuance path as ``POST /admin/login``
(:func:`auth._ensure_session_store` + :func:`auth._set_cookie_header`).
No second session system is introduced.

Security posture
----------------
* ``state`` + ``nonce`` are single-use: the transaction row is popped
  before any other validation and never reusable.
* The PKCE ``code_verifier`` and the configured ``client_secret`` are
  never logged — not even at DEBUG.
* An **empty** whitelist with OIDC enabled fails closed: every callback
  is rejected with a WARN, nobody is let in.
* Every callback failure redirects to ``/login?oidc_error=<code>`` so
  the operator sees a friendly error instead of a bare 500.
* id_token signature verification accepts asymmetric algorithms only
  (RS*/ES*/PS*) — HS* would let anyone who knows the (widely shared)
  client_secret forge tokens.

Phase 2 (RP-initiated logout + token refresh)
---------------------------------------------
* On a successful OIDC login the id_token (and, when the IdP returned
  one, the refresh_token) is parked in an in-process side-map keyed by
  the opaque admin session token (:class:`OidcSsoTokenStore`) — the
  session store itself stays untouched. Entries share the session TTL
  and are dropped on logout / expiry. Neither token is ever logged.
* ``GET /auth/oidc/logout`` revokes the local admin session first and
  then — only when discovery advertises ``end_session_endpoint`` and an
  id_token was captured at login — bounces the browser to the IdP with
  ``id_token_hint`` + ``post_logout_redirect_uri``. Every degradation
  path falls back to a plain local logout.
* ``POST /auth/oidc/refresh`` (authenticated) redeems the stored
  refresh_token and re-validates the NEW id_token end to end
  (signature/iss/aud/email_verified/whitelist) — a refresh re-proves
  identity, it is not a rubber-stamp renewal. Failure returns 401 and
  clears the side-map entry; the session itself is not revoked so
  password-session semantics are unaffected. Per-entry locking makes
  concurrent refreshes single-flight (no refresh_token replay).
* refresh_tokens only exist when ``offline_access`` is requested —
  gated behind ``[auth.oidc].request_offline_access`` (default false;
  the default scope set is never widened silently).

Configuration is gateway-side only (no agent sidecar involvement): the
resolved :class:`OidcSettings` ride on
:class:`~corlinman_server.gateway.routes_admin_a.state.AdminState`
(``oidc_settings``), wired at boot by ``app_factory``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import threading
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from corlinman_server.gateway.routes_admin_a import auth as _auth_mod
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)
from corlinman_server.tenancy import default_tenant

logger = structlog.get_logger(__name__)

# Test seam: pytest installs an ``httpx.MockTransport`` here so the
# discovery / token / JWKS fetches never leave the process. ``None`` in
# production → httpx builds its default transport. (``MockTransport``
# implements the async transport interface, hence the async bound.)
_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None

#: Asymmetric-only signature allowlist for id_token verification.
#: HS* is intentionally absent — symmetric verification keyed on the
#: client_secret would let any party that knows the secret mint tokens.
_ALLOWED_ID_TOKEN_ALGS = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
)

_DEFAULT_SCOPES: tuple[str, ...] = ("openid", "email", "profile")

#: How long a successful discovery / JWKS document is trusted (seconds).
_DISCOVERY_TTL_SECS = 300.0
#: Back-off after a failed discovery so a dead IdP doesn't get hammered
#: by every login-page render.
_DISCOVERY_FAILURE_BACKOFF_SECS = 30.0

#: Pending-login transaction TTL. Operators have 10 minutes to finish
#: the IdP round-trip before the state value expires.
_TXN_TTL_SECS = 600.0
#: Hard cap on parked transactions — prevents an unauthenticated
#: attacker from ballooning memory by spamming ``/auth/oidc/login``.
_TXN_MAX = 512

#: Browser-binding cookie for the pending login's ``state`` (review fix:
#: login-CSRF — the callback requires cookie == query state, so a forced
#: cross-site callback cannot complete in a victim's browser).
_STATE_COOKIE = "corlinman_oidc_state"

#: Unauthenticated /auth/oidc/login window: max starts per IP per minute.
_LOGIN_RATE_MAX = 10
_LOGIN_RATE_WINDOW_S = 60.0
_LOGIN_RATE: dict[str, list[float]] = {}
_LOGIN_RATE_LOCK = threading.Lock()


def _login_rate_limited(client_ip: str) -> bool:
    """Sliding-window per-IP limiter for the unauthenticated login start.

    In-memory (same lifetime as the txn table). Bounded: stale IPs are
    pruned on every call, so the map cannot grow past active-window IPs.
    """
    now = time.monotonic()
    with _LOGIN_RATE_LOCK:
        stale = [
            ip
            for ip, ts in _LOGIN_RATE.items()
            if not ts or now - ts[-1] > _LOGIN_RATE_WINDOW_S
        ]
        for ip in stale:
            del _LOGIN_RATE[ip]
        window = [
            t
            for t in _LOGIN_RATE.get(client_ip, [])
            if now - t <= _LOGIN_RATE_WINDOW_S
        ]
        if len(window) >= _LOGIN_RATE_MAX:
            _LOGIN_RATE[client_ip] = window
            return True
        window.append(now)
        _LOGIN_RATE[client_ip] = window
        return False

#: Clock-skew allowance for exp/iat validation (seconds).
_JWT_LEEWAY_SECS = 60


class OidcError(Exception):
    """OIDC flow failure with a machine-readable ``code``.

    ``code`` is what lands in ``/login?oidc_error=<code>`` — keep it
    free of user input and secrets.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


# ---------------------------------------------------------------------------
# Settings — resolved once at boot from ``[auth.oidc]``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OidcSettings:
    """Parsed ``[auth.oidc]`` block. Gateway-side only."""

    enabled: bool
    issuer: str
    client_id: str
    client_secret: str = ""
    scopes: tuple[str, ...] = _DEFAULT_SCOPES
    allowed_emails: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    # Optional full callback-URL override for deploys where the
    # gateway-visible request host differs from the public one (reverse
    # proxy without forwarded headers). Must exactly match the redirect
    # URI registered at the IdP.
    redirect_url: str = ""
    #: Review fix (major): the email claim is only trustworthy when the
    #: IdP asserts ``email_verified: true`` — on IdPs that let users
    #: self-assert an address, an unverified email walks straight through
    #: the whitelist into a full admin session. Default STRICT; the
    #: opt-out exists for IdPs that verify out-of-band but omit the claim.
    require_verified_email: bool = True
    #: Phase 2: request ``offline_access`` on top of the configured
    #: scopes so the IdP returns a refresh_token. OFF by default — the
    #: default scope set is never widened silently.
    request_offline_access: bool = False


def _extract_section(obj: Any, key: str) -> Any:
    """Read ``obj[key]`` / ``obj.key`` tolerantly (dict, dataclass, None)."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def resolve_oidc_settings(config: Any) -> OidcSettings | None:
    """Resolve ``[auth.oidc]`` from the loaded gateway config.

    Returns ``None`` when the section is absent — the ``/auth/oidc/*``
    routes then answer with their "disabled" envelopes. A present but
    ``enabled = false`` section also resolves (so ``/auth/oidc/status``
    can report ``enabled: false`` distinctly from "not configured").
    """
    auth_section = _extract_section(config, "auth")
    oidc = _extract_section(auth_section, "oidc")
    if not isinstance(oidc, Mapping):
        return None

    raw_enabled = oidc.get("enabled")
    enabled = raw_enabled if isinstance(raw_enabled, bool) else False

    issuer = str(oidc.get("issuer") or "").strip().rstrip("/")
    client_id = str(oidc.get("client_id") or "").strip()
    client_secret = str(oidc.get("client_secret") or "")
    scopes = _coerce_str_tuple(oidc.get("scopes")) or _DEFAULT_SCOPES
    allowed_emails = tuple(e.lower() for e in _coerce_str_tuple(oidc.get("allowed_emails")))
    allowed_domains = tuple(
        d.lower().lstrip("@") for d in _coerce_str_tuple(oidc.get("allowed_domains"))
    )
    redirect_url = str(oidc.get("redirect_url") or "").strip()
    raw_require_verified = oidc.get("require_verified_email")
    require_verified_email = (
        raw_require_verified if isinstance(raw_require_verified, bool) else True
    )
    raw_offline = oidc.get("request_offline_access")
    request_offline_access = raw_offline if isinstance(raw_offline, bool) else False

    if enabled and (not issuer or not client_id):
        logger.warning(
            "auth.oidc.misconfigured",
            reason="issuer and client_id are required when [auth.oidc] enabled = true",
        )
        enabled = False
    if enabled and not allowed_emails and not allowed_domains:
        # Fail-closed is enforced again at callback time; warn early so
        # the operator learns about it at boot rather than at first login.
        logger.warning(
            "auth.oidc.whitelist_empty",
            reason=(
                "[auth.oidc] is enabled but allowed_emails/allowed_domains are both "
                "empty; every OIDC login will be rejected (fail-closed)"
            ),
        )

    return OidcSettings(
        enabled=enabled,
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        allowed_emails=allowed_emails,
        allowed_domains=allowed_domains,
        redirect_url=redirect_url,
        require_verified_email=require_verified_email,
        request_offline_access=request_offline_access,
    )


# ---------------------------------------------------------------------------
# Discovery + JWKS cache
# ---------------------------------------------------------------------------


class _DocumentCache:
    """TTL cache for one JSON document (discovery doc or JWKS) keyed by
    URL, with a failure back-off so a dead IdP isn't hammered."""

    def __init__(self, *, ttl: float, failure_backoff: float) -> None:
        self._ttl = ttl
        self._failure_backoff = failure_backoff
        self._lock = threading.Lock()
        self._url: str | None = None
        self._doc: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self._failed_at = 0.0

    def cached(self, url: str) -> dict[str, Any] | None:
        with self._lock:
            if self._url == url and self._doc is not None:
                if time.monotonic() - self._fetched_at < self._ttl:
                    return self._doc
        return None

    def in_failure_backoff(self, url: str) -> bool:
        with self._lock:
            return (
                self._url == url
                and self._doc is None
                and self._failed_at > 0.0
                and time.monotonic() - self._failed_at < self._failure_backoff
            )

    def store(self, url: str, doc: dict[str, Any] | None) -> None:
        with self._lock:
            self._url = url
            if doc is None:
                self._doc = None
                self._failed_at = time.monotonic()
            else:
                self._doc = doc
                self._fetched_at = time.monotonic()
                self._failed_at = 0.0

    def reset(self) -> None:
        with self._lock:
            self._url = None
            self._doc = None
            self._fetched_at = 0.0
            self._failed_at = 0.0


_DISCOVERY_CACHE = _DocumentCache(
    ttl=_DISCOVERY_TTL_SECS, failure_backoff=_DISCOVERY_FAILURE_BACKOFF_SECS
)
_JWKS_CACHE = _DocumentCache(
    ttl=_DISCOVERY_TTL_SECS, failure_backoff=_DISCOVERY_FAILURE_BACKOFF_SECS
)


async def _fetch_json(url: str, *, timeout: float = 8.0) -> dict[str, Any]:
    """GET ``url`` and parse a JSON object; raises :class:`OidcError`."""
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=_TEST_TRANSPORT) as cli:
            resp = await cli.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OidcError("network_error", f"GET failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise OidcError("http_error", f"GET returned HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise OidcError("invalid_json", "non-JSON body") from exc
    if not isinstance(payload, dict):
        raise OidcError("invalid_json", "body is not an object")
    return payload


def _require_https(url: str, *, fld: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise OidcError("discovery_invalid", f"{fld} is not a valid URL") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise OidcError("discovery_invalid", f"{fld} must be an absolute https URL")
    return url


async def _get_discovery(settings: OidcSettings) -> dict[str, Any] | None:
    """Fetch (or return cached) OIDC discovery for the configured issuer.

    Returns ``None`` on failure — callers surface "SSO unavailable"
    instead of a 500.
    """
    url = f"{settings.issuer}/.well-known/openid-configuration"
    doc = _DISCOVERY_CACHE.cached(url)
    if doc is not None:
        return doc
    if _DISCOVERY_CACHE.in_failure_backoff(url):
        return None
    try:
        payload = await _fetch_json(url)
        issuer = str(payload.get("issuer") or "").strip().rstrip("/")
        if issuer != settings.issuer:
            raise OidcError("discovery_invalid", "discovery issuer mismatch")
        for fld in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = str(payload.get(fld) or "").strip()
            if not value:
                raise OidcError("discovery_invalid", f"discovery missing {fld}")
            _require_https(value, fld=fld)
    except OidcError as exc:
        logger.warning("auth.oidc.discovery_failed", issuer=settings.issuer, error=str(exc))
        _DISCOVERY_CACHE.store(url, None)
        return None
    _DISCOVERY_CACHE.store(url, payload)
    return payload


async def _get_jwks(jwks_uri: str, *, force: bool = False) -> dict[str, Any] | None:
    """``force=True`` bypasses the TTL cache — used exactly once when the
    id_token names a ``kid`` the cached JWKS lacks (key rotation): without
    it every SSO login fails for up to the full cache TTL."""
    if not force:
        doc = _JWKS_CACHE.cached(jwks_uri)
        if doc is not None:
            return doc
    if _JWKS_CACHE.in_failure_backoff(jwks_uri):
        return None
    try:
        payload = await _fetch_json(jwks_uri)
        if not isinstance(payload.get("keys"), list):
            raise OidcError("jwks_invalid", "JWKS body missing keys[]")
    except OidcError as exc:
        logger.warning("auth.oidc.jwks_fetch_failed", error=str(exc))
        _JWKS_CACHE.store(jwks_uri, None)
        return None
    _JWKS_CACHE.store(jwks_uri, payload)
    return payload


# ---------------------------------------------------------------------------
# Pending-login transaction store (state → verifier/nonce), single-use.
# ---------------------------------------------------------------------------


@dataclass
class _Txn:
    code_verifier: str
    nonce: str
    redirect: str
    created_at: float = field(default_factory=time.monotonic)


class OidcTxnStore:
    """In-memory single-use ``state`` → transaction map.

    ``pop`` removes the row — a state value can never be replayed. Rows
    older than :data:`_TXN_TTL_SECS` are treated as absent. Insertion
    evicts expired rows first and refuses (oldest-first eviction) past
    :data:`_TXN_MAX` so unauthenticated spam can't balloon memory.
    """

    def __init__(self, *, ttl: float = _TXN_TTL_SECS, max_rows: int = _TXN_MAX) -> None:
        self._ttl = ttl
        self._max = max_rows
        self._lock = threading.Lock()
        self._rows: dict[str, _Txn] = {}

    def put(self, state_value: str, txn: _Txn) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [k for k, v in self._rows.items() if now - v.created_at > self._ttl]
            for k in expired:
                del self._rows[k]
            while len(self._rows) >= self._max:
                oldest = min(self._rows, key=lambda k: self._rows[k].created_at)
                del self._rows[oldest]
            self._rows[state_value] = txn

    def pop(self, state_value: str) -> _Txn | None:
        with self._lock:
            row = self._rows.pop(state_value, None)
        if row is None:
            return None
        if time.monotonic() - row.created_at > self._ttl:
            return None
        return row

    def reset(self) -> None:
        with self._lock:
            self._rows.clear()


_TXNS = OidcTxnStore()


# ---------------------------------------------------------------------------
# SSO token side-store (session token → id_token / refresh_token).
# ---------------------------------------------------------------------------

#: Hard cap on SSO token rows — one row per live OIDC admin session, so
#: this is generous; oldest-expiry eviction past the cap.
_SSO_TOKENS_MAX = 256


@dataclass
class _SsoTokenRow:
    """IdP tokens captured at login for one admin session.

    ``lock`` single-flights refreshes for this session: a refresh_token
    must never be redeemed twice concurrently (many IdPs rotate it and
    revoke the grant on replay). It is a ``threading.Lock`` acquired via
    ``asyncio.to_thread`` so it works across event loops and never
    blocks the loop itself.
    """

    id_token: str
    refresh_token: str | None
    expires_at: float
    lock: threading.Lock = field(default_factory=threading.Lock)


class OidcSsoTokenStore:
    """In-process ``session_token → _SsoTokenRow`` map.

    The admin session store stays untouched (password-session semantics
    are sacred); this sidecar only remembers what the IdP handed us at
    login so logout can send ``id_token_hint`` and refresh can redeem
    the refresh_token. Entries share the session TTL, are refreshed on
    successful token refresh, and are dropped on logout. Token values
    never appear in logs.
    """

    def __init__(self, *, max_rows: int = _SSO_TOKENS_MAX) -> None:
        self._max = max_rows
        self._lock = threading.Lock()
        self._rows: dict[str, _SsoTokenRow] = {}

    def put(
        self,
        session_token: str,
        *,
        id_token: str,
        refresh_token: str | None,
        ttl: float,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [k for k, v in self._rows.items() if v.expires_at <= now]
            for k in expired:
                del self._rows[k]
            while len(self._rows) >= self._max:
                oldest = min(self._rows, key=lambda k: self._rows[k].expires_at)
                del self._rows[oldest]
            self._rows[session_token] = _SsoTokenRow(
                id_token=id_token,
                refresh_token=refresh_token,
                expires_at=now + ttl,
            )

    def get(self, session_token: str) -> _SsoTokenRow | None:
        """Live row for ``session_token`` — expired rows are evicted."""
        now = time.monotonic()
        with self._lock:
            row = self._rows.get(session_token)
            if row is None:
                return None
            if row.expires_at <= now:
                del self._rows[session_token]
                return None
            return row

    def update(
        self,
        session_token: str,
        *,
        id_token: str,
        refresh_token: str | None,
        ttl: float,
    ) -> None:
        """Swap tokens on the existing row (post-refresh) and slide its
        expiry. No-op when the row is gone (logout raced the refresh)."""
        now = time.monotonic()
        with self._lock:
            row = self._rows.get(session_token)
            if row is None:
                return
            row.id_token = id_token
            row.refresh_token = refresh_token
            row.expires_at = now + ttl

    def pop(self, session_token: str) -> _SsoTokenRow | None:
        with self._lock:
            return self._rows.pop(session_token, None)

    def reset(self) -> None:
        with self._lock:
            self._rows.clear()


_SSO_TOKENS = OidcSsoTokenStore()


def _reset_caches_for_tests() -> None:
    """Test helper: wipe module-level caches between test cases."""
    _DISCOVERY_CACHE.reset()
    _JWKS_CACHE.reset()
    _TXNS.reset()
    _SSO_TOKENS.reset()
    with _LOGIN_RATE_LOCK:
        _LOGIN_RATE.clear()


# ---------------------------------------------------------------------------
# Flow helpers
# ---------------------------------------------------------------------------


def _generate_pkce_pair() -> tuple[str, str]:
    """RFC 7636 S256 pair — same algorithm as ``oauth.xai_pkce``."""
    verifier_bytes = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    challenge_bytes = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(challenge_bytes).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _sanitize_redirect(raw: str | None) -> str:
    """Only allow same-origin absolute paths — blocks open redirects."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or raw.startswith("/\\"):
        return "/"
    return raw


def _redirect_uri(
    request: Request, settings: OidcSettings, admin_state: AdminState
) -> str:
    if settings.redirect_url:
        return settings.redirect_url
    base = str(request.base_url).rstrip("/")
    # Review fix: behind the documented TLS-terminating proxy the gateway
    # sees plain http — reuse the repo's trusted X-Forwarded-Proto
    # resolution (same helper the session cookie's Secure flag uses) so
    # the registered https redirect URI matches.
    if base.startswith("http://") and _auth_mod._request_is_https(
        request, admin_state
    ):
        base = "https://" + base[len("http://"):]
    return base + "/auth/oidc/callback"


def _public_base_url(
    request: Request, settings: OidcSettings, admin_state: AdminState
) -> str:
    """Public origin used for absolute URLs sent to the IdP (e.g.
    ``post_logout_redirect_uri``). Prefers the origin of the configured
    ``redirect_url`` override, else the request base with the same
    trusted X-Forwarded-Proto upgrade as :func:`_redirect_uri`."""
    if settings.redirect_url:
        parsed = urllib.parse.urlsplit(settings.redirect_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    base = str(request.base_url).rstrip("/")
    if base.startswith("http://") and _auth_mod._request_is_https(request, admin_state):
        base = "https://" + base[len("http://"):]
    return base


def _merge_query(url: str, params: Mapping[str, str]) -> str:
    """Append ``params`` to ``url``, merging with any query string the
    endpoint already carries — a second bare ``?`` would produce a
    malformed URL (review fix, shared by authorize + end_session)."""
    split = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(split.query, keep_blank_values=True)
    query.extend(params.items())
    return urllib.parse.urlunsplit(
        (split.scheme, split.netloc, split.path, urllib.parse.urlencode(query), "")
    )


def _login_error_redirect(code: str) -> RedirectResponse:
    """Bounce to the login page with a UI-visible error code (never a 500)."""
    return RedirectResponse(
        url=f"/login?oidc_error={urllib.parse.quote(code, safe='')}",
        status_code=status.HTTP_302_FOUND,
    )


async def _exchange_code(
    *,
    token_endpoint: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    settings: OidcSettings,
) -> dict[str, Any]:
    """Swap the authorization code for tokens.

    ``client_secret`` (when configured) rides the form body
    (``client_secret_post``); neither it nor the verifier appear in any
    log or raised error message.
    """
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": settings.client_id,
        "code_verifier": code_verifier,
    }
    return await _post_token_endpoint(token_endpoint, body, settings=settings)


async def _exchange_refresh(
    *,
    token_endpoint: str,
    refresh_token: str,
    settings: OidcSettings,
) -> dict[str, Any]:
    """Redeem a refresh_token for fresh tokens (RFC 6749 §6).

    The refresh_token itself never reaches a log line or an error
    message — failures are reported status-code-only, same as the
    authorization-code exchange.
    """
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.client_id,
    }
    return await _post_token_endpoint(token_endpoint, body, settings=settings)


async def _post_token_endpoint(
    token_endpoint: str, body: dict[str, str], *, settings: OidcSettings
) -> dict[str, Any]:
    """Shared token-endpoint POST for both grant types. Secrets ride
    the form body only; error reporting is status-code-only because the
    IdP's error body may echo request parameters we keep out of logs."""
    if settings.client_secret:
        body["client_secret"] = settings.client_secret
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, transport=_TEST_TRANSPORT) as cli:
            resp = await cli.post(token_endpoint, data=body, headers=headers)
    except httpx.HTTPError as exc:
        raise OidcError(
            "token_exchange_failed", f"network error: {type(exc).__name__}"
        ) from exc
    if resp.status_code >= 400:
        # Deliberately status-code-only: the error body may echo request
        # parameters we must keep out of logs.
        raise OidcError("token_exchange_failed", f"token endpoint HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise OidcError("token_exchange_failed", "token endpoint returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise OidcError("token_exchange_failed", "token endpoint body is not an object")
    return payload


def _select_jwk(jwks: dict[str, Any], *, kid: str | None, alg: str) -> Any:
    """Pick the signing key for ``kid`` out of the JWKS document."""
    from jwt import PyJWK  # lazy: keep module importable without pyjwt

    keys = [k for k in jwks.get("keys", []) if isinstance(k, dict)]
    if kid is not None:
        keys = [k for k in keys if k.get("kid") == kid]
    for candidate in keys:
        try:
            return PyJWK(candidate, algorithm=alg).key
        except Exception:  # noqa: BLE001 — try the next candidate key
            continue
    raise OidcError("invalid_id_token", "no JWKS key matches the id_token header")


def _verify_id_token(
    id_token: str,
    *,
    jwks: dict[str, Any],
    settings: OidcSettings,
    expected_issuer: str,
    nonce: str | None,
) -> dict[str, Any]:
    """Full id_token validation: signature (JWKS), iss, aud, exp, nonce.

    ``nonce=None`` skips the nonce binding — used ONLY for id_tokens
    minted by a refresh-grant response, which per OIDC Core 12.2 need
    not (and should not) carry the original login nonce. Everything
    else (signature/iss/aud/exp/azp) is still enforced.

    Raises :class:`OidcError` (code ``invalid_id_token``) on every
    failure mode; the specific reason goes to the server log only.
    """
    import jwt  # lazy: keep module importable without pyjwt

    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OidcError("invalid_id_token", "unparsable JWT header") from exc
    alg = str(header.get("alg") or "")
    if alg not in _ALLOWED_ID_TOKEN_ALGS:
        raise OidcError("invalid_id_token", f"disallowed alg {alg!r}")
    kid = header.get("kid")
    key = _select_jwk(jwks, kid=kid if isinstance(kid, str) else None, alg=alg)

    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=[alg],
            audience=settings.client_id,
            issuer=expected_issuer,
            leeway=_JWT_LEEWAY_SECS,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        # PyJWT error strings name the failing claim but never echo the
        # token payload — safe to log.
        raise OidcError("invalid_id_token", f"id_token rejected: {exc}") from exc

    if nonce is not None:
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
            raise OidcError("invalid_id_token", "nonce mismatch")
    # OIDC Core 3.1.3.7 #4/#5: with multiple audiences the token must
    # name US as the authorized party — otherwise an id_token minted for
    # a sibling client that merely lists our client_id in aud is accepted.
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1:
        azp = claims.get("azp")
        if not isinstance(azp, str) or azp != settings.client_id:
            raise OidcError("invalid_id_token", "azp missing/mismatched for multi-aud token")
    return claims


def _email_allowed(email: str, settings: OidcSettings) -> bool:
    """Whitelist check. Empty whitelist + enabled OIDC ⇒ fail closed."""
    if not settings.allowed_emails and not settings.allowed_domains:
        logger.warning(
            "auth.oidc.whitelist_empty",
            reason=(
                "[auth.oidc] allowed_emails/allowed_domains are both empty; "
                "rejecting all OIDC logins (fail-closed)"
            ),
        )
        return False
    normalized = email.strip().lower()
    if normalized in settings.allowed_emails:
        return True
    _, _, domain = normalized.rpartition("@")
    return bool(domain) and domain in settings.allowed_domains


async def _verify_id_token_against_jwks(
    id_token: str,
    *,
    doc: dict[str, Any],
    settings: OidcSettings,
    nonce: str | None,
) -> dict[str, Any]:
    """JWKS fetch + full id_token validation, with exactly ONE forced
    JWKS refetch when the token names a ``kid`` the cached document
    lacks (key rotation) — shared by the login callback and the
    refresh path so a refresh is validated to the same bar as a login.

    Raises :class:`OidcError` — code ``jwks_failed`` when the JWKS
    document is unavailable, ``invalid_id_token`` otherwise.
    """
    jwks_uri = str(doc["jwks_uri"])
    jwks = await _get_jwks(jwks_uri)
    if jwks is None:
        raise OidcError("jwks_failed", "JWKS unavailable")
    expected_issuer = str(doc.get("issuer") or settings.issuer)
    try:
        return _verify_id_token(
            id_token,
            jwks=jwks,
            settings=settings,
            expected_issuer=expected_issuer,
            nonce=nonce,
        )
    except OidcError as exc:
        if "no JWKS key matches" not in str(exc):
            raise
        fresh = await _get_jwks(jwks_uri, force=True)
        if fresh is None:
            raise
        return _verify_id_token(
            id_token,
            jwks=fresh,
            settings=settings,
            expected_issuer=expected_issuer,
            nonce=nonce,
        )


def _authorize_claims(claims: dict[str, Any], settings: OidcSettings) -> str:
    """Identity gate shared by login + refresh: extract the email and
    enforce ``email_verified`` + the whitelist. Returns the normalized
    email; raises :class:`OidcError` with the UI-visible code
    (``missing_email`` / ``email_unverified`` / ``email_not_allowed``).
    """
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        raise OidcError("missing_email", "id_token has no usable email claim")
    email = email.strip().lower()
    if settings.require_verified_email and claims.get("email_verified") is not True:
        raise OidcError("email_unverified", f"email {email} not asserted verified")
    if not _email_allowed(email, settings):
        raise OidcError("email_not_allowed", f"email {email} outside the whitelist")
    return email


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """``/auth/oidc/*`` — mounted OUTSIDE the ``/admin/`` auth gate:
    every endpoint here is reachable pre-login by design."""
    r = APIRouter()

    @r.get("/auth/oidc/status", summary="OIDC availability for the login page")
    async def oidc_status(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> JSONResponse:
        settings = _settings_of(admin_state)
        if settings is None or not settings.enabled:
            return JSONResponse(
                {
                    "enabled": False,
                    "available": False,
                    "end_session": False,
                    "refresh": False,
                }
            )
        doc = await _get_discovery(settings)
        # Capability report (explicit degradation): the front-end shows
        # "SSO logout" / refresh affordances only when the IdP actually
        # supports them — end_session comes from discovery, refresh from
        # the operator's offline_access opt-in.
        end_session = bool(str((doc or {}).get("end_session_endpoint") or "").strip())
        return JSONResponse(
            {
                "enabled": True,
                "available": doc is not None,
                "end_session": end_session,
                "refresh": bool(settings.request_offline_access),
            }
        )

    @r.get("/auth/oidc/login", summary="Start the OIDC Authorization Code + PKCE flow")
    async def oidc_login(
        request: Request,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> RedirectResponse:
        settings = _settings_of(admin_state)
        if settings is None or not settings.enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "oidc_disabled", "message": "[auth.oidc] is not enabled"},
            )
        doc = await _get_discovery(settings)
        if doc is None:
            return _login_error_redirect("discovery_failed")

        # Review fix: /auth/oidc/login is unauthenticated — a cheap
        # per-IP window stops one caller from churning the txn table's
        # oldest-first eviction to wash out legitimate pending states.
        client_ip = request.client.host if request.client else "unknown"
        if _login_rate_limited(client_ip):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error": "rate_limited", "message": "retry later"},
            )

        verifier, challenge = _generate_pkce_pair()
        state_value = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        redirect = _sanitize_redirect(request.query_params.get("redirect"))
        _TXNS.put(state_value, _Txn(code_verifier=verifier, nonce=nonce, redirect=redirect))

        # Phase 2: ``offline_access`` (→ refresh_token) is requested only
        # behind the explicit opt-in — default scopes are never widened.
        scopes = list(settings.scopes)
        if settings.request_offline_access and "offline_access" not in scopes:
            scopes.append("offline_access")

        params = {
            "response_type": "code",
            "client_id": settings.client_id,
            "redirect_uri": _redirect_uri(request, settings, admin_state),
            "scope": " ".join(scopes),
            "state": state_value,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        del challenge  # verifier/challenge never reach logs
        authorize_url = _merge_query(str(doc["authorization_endpoint"]), params)
        resp = RedirectResponse(url=authorize_url, status_code=status.HTTP_302_FOUND)
        # Review fix (login-CSRF / session fixation): park the state in an
        # HttpOnly cookie too — the callback requires cookie == query
        # state, so a forced cross-site callback with an attacker's state
        # cannot complete in a victim's browser.
        resp.set_cookie(
            _STATE_COOKIE,
            state_value,
            max_age=int(_TXN_TTL_SECS),
            httponly=True,
            samesite="lax",
            secure=_auth_mod._session_cookie_secure(request, admin_state),
            path="/auth/oidc",
        )
        return resp

    @r.get("/auth/oidc/callback", summary="OIDC redirect target — issues the admin session")
    async def oidc_callback(
        request: Request,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> RedirectResponse:
        settings = _settings_of(admin_state)
        if settings is None or not settings.enabled:
            return _login_error_redirect("oidc_disabled")

        qp = request.query_params
        state_value = qp.get("state") or ""
        # Pop FIRST — the state (and its nonce/verifier) is single-use
        # no matter how the rest of the callback goes.
        txn = _TXNS.pop(state_value) if state_value else None

        idp_error = qp.get("error")
        if idp_error:
            # The IdP's error code is attacker-controllable query input;
            # log a trimmed slug, show the UI a fixed code.
            logger.warning("auth.oidc.idp_error", error=str(idp_error)[:64])
            return _login_error_redirect("idp_error")

        if txn is None:
            logger.warning("auth.oidc.state_mismatch")
            return _login_error_redirect("state_mismatch")

        cookie_state = request.cookies.get(_STATE_COOKIE) or ""
        if not hmac.compare_digest(cookie_state, state_value):
            # The completing browser is not the one that started the
            # flow — reject (login-CSRF binding, review fix).
            logger.warning("auth.oidc.state_cookie_mismatch")
            return _login_error_redirect("state_mismatch")

        code = qp.get("code") or ""
        if not code:
            return _login_error_redirect("missing_code")

        doc = await _get_discovery(settings)
        if doc is None:
            return _login_error_redirect("discovery_failed")

        try:
            tokens = await _exchange_code(
                token_endpoint=str(doc["token_endpoint"]),
                code=code,
                code_verifier=txn.code_verifier,
                redirect_uri=_redirect_uri(request, settings, admin_state),
                settings=settings,
            )
        except OidcError as exc:
            logger.warning("auth.oidc.token_exchange_failed", error=str(exc))
            return _login_error_redirect("token_exchange_failed")

        id_token = tokens.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            logger.warning("auth.oidc.id_token_missing")
            return _login_error_redirect("invalid_id_token")

        # Key rotation is handled inside the shared helper: a kid the
        # cached JWKS lacks gets exactly ONE forced refetch — else every
        # SSO login breaks for the full cache TTL (review fix).
        try:
            claims = await _verify_id_token_against_jwks(
                id_token, doc=doc, settings=settings, nonce=txn.nonce
            )
        except OidcError as exc:
            if exc.code == "jwks_failed":
                return _login_error_redirect("jwks_failed")
            logger.warning("auth.oidc.id_token_rejected", error=str(exc))
            return _login_error_redirect("invalid_id_token")

        # Identity gate (review fix, MAJOR): the whitelist is only as
        # strong as the email's provenance — ``email_verified: true`` is
        # required by default (opt out via require_verified_email=false
        # for IdPs that verify out-of-band but omit the claim).
        try:
            email = _authorize_claims(claims, settings)
        except OidcError as exc:
            logger.warning("auth.oidc.identity_rejected", code=exc.code, error=str(exc))
            return _login_error_redirect(exc.code)

        # -- Session issuance — SAME path as POST /admin/login ----------
        store = _auth_mod._ensure_session_store(admin_state)
        login_tenant = admin_state.default_tenant or default_tenant()
        token = store.create(email, tenant=login_tenant.as_str())
        max_age = (
            store.ttl_seconds()
            if hasattr(store, "ttl_seconds")
            else admin_state.session_ttl_seconds
        )
        logger.info("admin.login.oidc_succeeded", email=email)

        # Phase 2: park the IdP tokens next to the session so logout can
        # send id_token_hint and refresh can redeem the refresh_token.
        # Token VALUES never reach logs; presence flags are safe.
        refresh_token = tokens.get("refresh_token")
        _SSO_TOKENS.put(
            token,
            id_token=id_token,
            refresh_token=(
                refresh_token
                if isinstance(refresh_token, str) and refresh_token
                else None
            ),
            ttl=float(max_age),
        )

        resp = RedirectResponse(url=txn.redirect or "/", status_code=status.HTTP_302_FOUND)
        resp.headers["set-cookie"] = _auth_mod._set_cookie_header(
            token,
            max_age,
            secure=_auth_mod._session_cookie_secure(request, admin_state),
        )
        return resp

    @r.get(
        "/auth/oidc/logout",
        summary="RP-initiated logout — local session first, IdP end_session when available",
    )
    async def oidc_logout(
        request: Request,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> RedirectResponse:
        """Local logout ALWAYS happens (same semantics as
        ``POST /admin/logout``: invalidate + clear cookie); the IdP
        bounce is strictly additive and every degradation path — OIDC
        disabled, discovery down, no ``end_session_endpoint``, session
        not born from OIDC — falls back to a plain ``/login`` redirect.
        """
        token = _auth_mod._read_session_cookie(request)
        row = _SSO_TOKENS.pop(token) if token else None
        if token and admin_state.session_store is not None:
            try:
                admin_state.session_store.invalidate(token)
            except Exception:  # noqa: BLE001 — best-effort, cookie clear below still happens
                pass

        target = "/login"
        settings = _settings_of(admin_state)
        if settings is not None and settings.enabled and row is not None and row.id_token:
            doc = await _get_discovery(settings)
            end_session = str((doc or {}).get("end_session_endpoint") or "").strip()
            if end_session:
                try:
                    _require_https(end_session, fld="end_session_endpoint")
                except OidcError:
                    # Discovery only vets authorize/token/jwks URLs; an
                    # http end_session would leak the id_token_hint in
                    # cleartext — degrade to local-only logout instead.
                    logger.warning("auth.oidc.end_session_endpoint_invalid")
                    end_session = ""
            if end_session:
                target = _merge_query(
                    end_session,
                    {
                        # id_token value rides the redirect only — never a log.
                        "id_token_hint": row.id_token,
                        "post_logout_redirect_uri": (
                            _public_base_url(request, settings, admin_state) + "/login"
                        ),
                        "client_id": settings.client_id,
                    },
                )
                logger.info("admin.logout.oidc_end_session")

        resp = RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
        resp.headers["set-cookie"] = _auth_mod._clear_cookie_header(
            secure=_auth_mod._session_cookie_secure(request, admin_state)
        )
        return resp

    @r.post(
        "/auth/oidc/refresh",
        summary="Redeem the stored refresh_token and re-prove the identity",
    )
    async def oidc_refresh(
        request: Request,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> JSONResponse:
        """A refresh is a fresh identity proof, not a rubber-stamp
        renewal: the NEW id_token goes through the exact same
        signature/iss/aud/email_verified/whitelist gate as a login.
        Failure → 401 + the side-map entry is cleared; the admin session
        itself is NOT revoked (password-session semantics untouched).
        """
        settings = _settings_of(admin_state)
        if settings is None or not settings.enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "oidc_disabled", "message": "[auth.oidc] is not enabled"},
            )
        token = _auth_mod._read_session_cookie(request)
        store = admin_state.session_store
        session = store.validate(token) if (token and store is not None) else None
        if session is None or token is None or store is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthenticated"},
            )
        row = _SSO_TOKENS.get(token)
        if row is None or not row.refresh_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "no_refresh_token",
                    "message": (
                        "no refresh_token for this session — enable "
                        "[auth.oidc].request_offline_access and log in via SSO"
                    ),
                },
            )
        doc = await _get_discovery(settings)
        if doc is None:
            # IdP unreachable is an infra failure, not an identity
            # rejection — keep the entry, report explicitly.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "sso_unavailable", "message": "IdP discovery failed"},
            )

        # Single-flight (review requirement): a refresh_token must never
        # be redeemed twice concurrently — IdPs that rotate tokens revoke
        # the whole grant on replay. threading.Lock via a worker thread
        # keeps the event loop free and works across loops.
        await asyncio.to_thread(row.lock.acquire)
        try:
            # Re-read after acquiring: a concurrent refresh may have
            # rotated the token, a concurrent failure may have cleared it.
            live = _SSO_TOKENS.get(token)
            if live is None or not live.refresh_token:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"error": "no_refresh_token"},
                )
            current_refresh = live.refresh_token

            def _reject(exc: OidcError, event: str) -> HTTPException:
                # Every identity failure clears the entry (the refresh
                # grant is burned) but leaves the session untouched.
                _SSO_TOKENS.pop(token)
                logger.warning(event, error=str(exc))
                return HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"error": "refresh_failed", "code": exc.code},
                )

            try:
                tokens = await _exchange_refresh(
                    token_endpoint=str(doc["token_endpoint"]),
                    refresh_token=current_refresh,
                    settings=settings,
                )
            except OidcError as exc:
                raise _reject(exc, "auth.oidc.refresh_exchange_failed") from exc

            new_id_token = tokens.get("id_token")
            if not isinstance(new_id_token, str) or not new_id_token:
                raise _reject(
                    OidcError("invalid_id_token", "refresh response lacks id_token"),
                    "auth.oidc.refresh_id_token_missing",
                )
            try:
                claims = await _verify_id_token_against_jwks(
                    new_id_token, doc=doc, settings=settings, nonce=None
                )
                email = _authorize_claims(claims, settings)
            except OidcError as exc:
                raise _reject(exc, "auth.oidc.refresh_identity_rejected") from exc

            max_age = (
                store.ttl_seconds()
                if hasattr(store, "ttl_seconds")
                else admin_state.session_ttl_seconds
            )
            rotated = tokens.get("refresh_token")
            _SSO_TOKENS.update(
                token,
                id_token=new_id_token,
                refresh_token=(
                    rotated
                    if isinstance(rotated, str) and rotated
                    else current_refresh
                ),
                ttl=float(max_age),
            )
        finally:
            row.lock.release()

        # Sliding renewal: ``store.validate`` above already refreshed the
        # row's ``last_used``; re-emitting the cookie refreshes the
        # browser-side Max-Age so both halves agree.
        logger.info("admin.session.oidc_refreshed", email=email)
        resp = JSONResponse({"status": "ok", "expires_in": max_age})
        resp.headers["set-cookie"] = _auth_mod._set_cookie_header(
            token,
            max_age,
            secure=_auth_mod._session_cookie_secure(request, admin_state),
        )
        return resp

    return r


def _settings_of(admin_state: AdminState) -> OidcSettings | None:
    """Read ``oidc_settings`` off the state, tolerating foreign shapes."""
    settings = getattr(admin_state, "oidc_settings", None)
    return settings if isinstance(settings, OidcSettings) else None


__all__ = [
    "OidcError",
    "OidcSettings",
    "OidcSsoTokenStore",
    "OidcTxnStore",
    "resolve_oidc_settings",
    "router",
]
