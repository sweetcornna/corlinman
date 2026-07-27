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

Configuration is gateway-side only (no agent sidecar involvement): the
resolved :class:`OidcSettings` ride on
:class:`~corlinman_server.gateway.routes_admin_a.state.AdminState`
(``oidc_settings``), wired at boot by ``app_factory``.
"""

from __future__ import annotations

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


async def _get_jwks(jwks_uri: str) -> dict[str, Any] | None:
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


def _reset_caches_for_tests() -> None:
    """Test helper: wipe module-level caches between test cases."""
    _DISCOVERY_CACHE.reset()
    _JWKS_CACHE.reset()
    _TXNS.reset()


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


def _redirect_uri(request: Request, settings: OidcSettings) -> str:
    if settings.redirect_url:
        return settings.redirect_url
    return str(request.base_url).rstrip("/") + "/auth/oidc/callback"


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
    nonce: str,
) -> dict[str, Any]:
    """Full id_token validation: signature (JWKS), iss, aud, exp, nonce.

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

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
        raise OidcError("invalid_id_token", "nonce mismatch")
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
            return JSONResponse({"enabled": False, "available": False})
        doc = await _get_discovery(settings)
        return JSONResponse({"enabled": True, "available": doc is not None})

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

        verifier, challenge = _generate_pkce_pair()
        state_value = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        redirect = _sanitize_redirect(request.query_params.get("redirect"))
        _TXNS.put(state_value, _Txn(code_verifier=verifier, nonce=nonce, redirect=redirect))

        params = {
            "response_type": "code",
            "client_id": settings.client_id,
            "redirect_uri": _redirect_uri(request, settings),
            "scope": " ".join(settings.scopes),
            "state": state_value,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        authorize_url = f"{doc['authorization_endpoint']}?{urllib.parse.urlencode(params)}"
        return RedirectResponse(url=authorize_url, status_code=status.HTTP_302_FOUND)

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
                redirect_uri=_redirect_uri(request, settings),
                settings=settings,
            )
        except OidcError as exc:
            logger.warning("auth.oidc.token_exchange_failed", error=str(exc))
            return _login_error_redirect("token_exchange_failed")

        id_token = tokens.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            logger.warning("auth.oidc.id_token_missing")
            return _login_error_redirect("invalid_id_token")

        jwks = await _get_jwks(str(doc["jwks_uri"]))
        if jwks is None:
            return _login_error_redirect("jwks_failed")

        try:
            claims = _verify_id_token(
                id_token,
                jwks=jwks,
                settings=settings,
                expected_issuer=str(doc.get("issuer") or settings.issuer),
                nonce=txn.nonce,
            )
        except OidcError as exc:
            logger.warning("auth.oidc.id_token_rejected", error=str(exc))
            return _login_error_redirect("invalid_id_token")

        email = claims.get("email")
        if not isinstance(email, str) or not email.strip():
            logger.warning("auth.oidc.email_missing")
            return _login_error_redirect("missing_email")
        email = email.strip().lower()

        if not _email_allowed(email, settings):
            logger.warning("auth.oidc.email_not_allowed", email=email)
            return _login_error_redirect("email_not_allowed")

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

        resp = RedirectResponse(url=txn.redirect or "/", status_code=status.HTTP_302_FOUND)
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
    "OidcTxnStore",
    "resolve_oidc_settings",
    "router",
]
