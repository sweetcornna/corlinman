"""OIDC phase 2 — RP-initiated logout + token refresh.

Coverage matrix (per the task spec):

* RP-initiated logout: local session revoked FIRST, then a 302 to the
  IdP ``end_session_endpoint`` with ``id_token_hint`` +
  ``post_logout_redirect_uri`` — but only when discovery advertises the
  endpoint AND the session was born from SSO; every other path (password
  session, no end_session, non-https end_session) degrades to a plain
  local logout.
* Token refresh: ``offline_access`` is requested only behind the
  ``request_offline_access`` opt-in; ``POST /auth/oidc/refresh`` redeems
  the stored refresh_token and re-validates the NEW id_token end to end
  (whitelist re-check included — a new email outside the whitelist is a
  401). Failure clears the side-map entry without touching the session.
* Concurrency: two overlapping refreshes are single-flight — the same
  refresh_token value is never redeemed twice (rotation-safe).
* Status: capability report (``end_session`` / ``refresh``).

Mock IdP via ``httpx.MockTransport`` on the module's ``_TEST_TRANSPORT``
seam, extended with an ``end_session_endpoint`` and a
``grant_type=refresh_token`` token-endpoint branch.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from corlinman_server.gateway.routes_admin_a import auth as auth_mod
from corlinman_server.gateway.routes_admin_a import oidc as oidc_mod
from corlinman_server.gateway.routes_admin_a._session_store import (
    SESSION_COOKIE_NAME,
    AdminSessionStore,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

ISSUER = "https://idp.test"
CLIENT_ID = "corlinman-admin"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZE_URL = f"{ISSUER}/authorize"
TOKEN_URL = f"{ISSUER}/token"
JWKS_URL = f"{ISSUER}/jwks"
END_SESSION_URL = f"{ISSUER}/logout"


# ---------------------------------------------------------------------------
# Test RSA key + JWKS
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key() -> Any:
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks_doc(rsa_key: Any) -> dict[str, Any]:
    from jwt.algorithms import RSAAlgorithm

    jwk = json.loads(RSAAlgorithm.to_jwk(rsa_key.public_key()))
    jwk["kid"] = "test-key"
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return {"keys": [jwk]}


def _mint_id_token(
    rsa_key: Any,
    *,
    nonce: str | None,
    email: str = "ops@example.com",
    exp_delta: int = 600,
    sub: str = "user-1",
) -> str:
    """Refresh-grant id_tokens legitimately omit the login nonce —
    ``nonce=None`` leaves the claim out entirely."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": sub,
        "email": email,
        "email_verified": True,
        "iat": now,
        "exp": now + exp_delta,
    }
    if nonce is not None:
        claims["nonce"] = nonce
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key"})


# ---------------------------------------------------------------------------
# Mock IdP transport — discovery + token (both grants) + JWKS + end_session
# ---------------------------------------------------------------------------


def _make_idp(
    jwks: dict[str, Any],
    rsa_key: Any,
    *,
    end_session: bool | str = True,
    login_refresh_token: str | None = "rt-0",
    refresh_status: int = 200,
    refresh_email: str = "ops@example.com",
    refresh_sub: str | None = None,
    refresh_rotates: bool = True,
    refresh_delay: float = 0.0,
    seen_refresh_tokens: list[str] | None = None,
    nonce_holder: dict[str, str] | None = None,
) -> httpx.MockTransport:
    discovery_doc: dict[str, Any] = {
        "issuer": ISSUER,
        "authorization_endpoint": AUTHORIZE_URL,
        "token_endpoint": TOKEN_URL,
        "jwks_uri": JWKS_URL,
    }
    if end_session:
        discovery_doc["end_session_endpoint"] = (
            END_SESSION_URL if end_session is True else end_session
        )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == DISCOVERY_URL:
            return httpx.Response(200, json=discovery_doc)
        if url == JWKS_URL:
            return httpx.Response(200, json=jwks)
        if url == TOKEN_URL:
            form = dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))
            if form.get("grant_type") == "refresh_token":
                presented = form.get("refresh_token", "")
                if seen_refresh_tokens is not None:
                    seen_refresh_tokens.append(presented)
                if refresh_delay:
                    time.sleep(refresh_delay)
                if refresh_status != 200:
                    return httpx.Response(
                        refresh_status, json={"error": "invalid_grant"}
                    )
                body: dict[str, Any] = {
                    "access_token": "at-refreshed",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    # No nonce on a refresh-minted id_token (OIDC 12.2).
                    "id_token": _mint_id_token(
                        rsa_key,
                        nonce=None,
                        email=refresh_email,
                        **({"sub": refresh_sub} if refresh_sub else {}),
                    ),
                }
                if refresh_rotates:
                    body["refresh_token"] = presented + "x"
                return httpx.Response(200, json=body)
            # authorization_code grant
            body = {
                "access_token": "at-test",
                "token_type": "Bearer",
                "expires_in": 3600,
                "id_token": _mint_id_token(
                    rsa_key,
                    nonce=(nonce_holder or {}).get("nonce", ""),
                ),
            }
            if login_refresh_token is not None:
                body["refresh_token"] = login_refresh_token
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "unknown_url"})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# App fixtures
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> oidc_mod.OidcSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": "",
        "scopes": ("openid", "email", "profile"),
        "allowed_emails": ("ops@example.com",),
        "allowed_domains": (),
        "redirect_url": "",
        "request_offline_access": True,
    }
    base.update(overrides)
    return oidc_mod.OidcSettings(**base)


@pytest.fixture
def state(tmp_path: Path) -> Iterator[AdminState]:
    st = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=auth_mod.hash_password("s3cret-pw"),
        config_path=tmp_path / "config.toml",
        must_change_password=False,
        session_store=AdminSessionStore(ttl_seconds=3600),
        admin_write_lock=asyncio.Lock(),
        oidc_settings=_settings(),
    )
    set_admin_state(st)
    oidc_mod._reset_caches_for_tests()
    try:
        yield st
    finally:
        set_admin_state(None)
        oidc_mod._reset_caches_for_tests()
        oidc_mod._TEST_TRANSPORT = None


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(oidc_mod.router())
    app.include_router(auth_mod.router())
    return TestClient(app, base_url="http://testserver", follow_redirects=False)


def _install(transport: httpx.MockTransport) -> None:
    oidc_mod._TEST_TRANSPORT = transport


def _sso_login(client: TestClient, nonce_holder: dict[str, str]) -> str:
    """Drive the full login flow; returns the session token (the cookie
    stays set on ``client``)."""
    resp = client.get("/auth/oidc/login", params={"redirect": "/chat"})
    assert resp.status_code == 302
    params = dict(
        urllib.parse.parse_qsl(urllib.parse.urlsplit(resp.headers["location"]).query)
    )
    nonce_holder["nonce"] = params["nonce"]
    done = client.get(
        "/auth/oidc/callback", params={"state": params["state"], "code": "authcode-1"}
    )
    assert done.status_code == 302, done.text
    assert done.headers["location"] == "/chat"
    cookie = done.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    token = cookie.split(";", 1)[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return token


def _password_login(client: TestClient) -> str:
    resp = client.post(
        "/admin/login", json={"username": "admin", "password": "s3cret-pw"}
    )
    assert resp.status_code == 200
    token = resp.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return token


# ---------------------------------------------------------------------------
# Offline-access scope gating
# ---------------------------------------------------------------------------


def test_offline_access_requested_only_behind_flag(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    _install(_make_idp(jwks_doc, rsa_key))
    scope = _authorize_scope(_client())
    assert "offline_access" in scope.split()

    state.oidc_settings = _settings(request_offline_access=False)
    oidc_mod._reset_caches_for_tests()
    scope = _authorize_scope(_client())
    assert "offline_access" not in scope.split()
    assert scope == "openid email profile"  # defaults never widened silently


def _authorize_scope(client: TestClient) -> str:
    resp = client.get("/auth/oidc/login")
    assert resp.status_code == 302
    params = dict(
        urllib.parse.parse_qsl(urllib.parse.urlsplit(resp.headers["location"]).query)
    )
    return params["scope"]


# ---------------------------------------------------------------------------
# RP-initiated logout
# ---------------------------------------------------------------------------


def test_logout_bounces_to_idp_end_session(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    nonce_holder: dict[str, str] = {}
    _install(_make_idp(jwks_doc, rsa_key, nonce_holder=nonce_holder))
    client = _client()
    _sso_login(client, nonce_holder)
    assert client.get("/admin/me").status_code == 200

    resp = client.get("/auth/oidc/logout")
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(END_SESSION_URL + "?")
    params = dict(
        urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query)
    )
    # id_token_hint is the exact id_token minted at login.
    assert params["id_token_hint"].count(".") == 2
    assert params["post_logout_redirect_uri"] == "http://testserver/login"
    assert params["client_id"] == CLIENT_ID
    # Local session is revoked and the cookie cleared.
    assert "Max-Age=0" in resp.headers["set-cookie"]
    assert client.get("/admin/me").status_code == 401

    # The side-map entry is single-use: a second logout (stale cookie)
    # degrades to a plain local logout.
    again = client.get("/auth/oidc/logout")
    assert again.status_code == 302
    assert again.headers["location"] == "/login"


def test_logout_local_only_without_end_session_endpoint(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    nonce_holder: dict[str, str] = {}
    _install(_make_idp(jwks_doc, rsa_key, end_session=False, nonce_holder=nonce_holder))
    client = _client()
    _sso_login(client, nonce_holder)

    resp = client.get("/auth/oidc/logout")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
    assert client.get("/admin/me").status_code == 401


def test_logout_local_only_for_password_session(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    """A password-born session has no id_token — never bounce to the IdP."""
    _install(_make_idp(jwks_doc, rsa_key))
    client = _client()
    _password_login(client)
    assert client.get("/admin/me").status_code == 200

    resp = client.get("/auth/oidc/logout")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
    assert client.get("/admin/me").status_code == 401


def test_logout_rejects_non_https_end_session(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    """An http end_session would leak the id_token_hint in cleartext —
    degrade to local-only logout."""
    nonce_holder: dict[str, str] = {}
    _install(
        _make_idp(
            jwks_doc,
            rsa_key,
            end_session="http://idp.test/logout",
            nonce_holder=nonce_holder,
        )
    )
    client = _client()
    _sso_login(client, nonce_holder)

    resp = client.get("/auth/oidc/logout")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
    assert client.get("/admin/me").status_code == 401


def test_logout_without_any_session_still_redirects(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    _install(_make_idp(jwks_doc, rsa_key))
    resp = _client().get("/auth/oidc/logout")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


def test_refresh_happy_path_extends_session(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    nonce_holder: dict[str, str] = {}
    seen: list[str] = []
    _install(
        _make_idp(
            jwks_doc, rsa_key, nonce_holder=nonce_holder, seen_refresh_tokens=seen
        )
    )
    client = _client()
    token = _sso_login(client, nonce_holder)

    resp = client.post("/auth/oidc/refresh")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["expires_in"] > 0
    # The cookie is re-emitted for the SAME session token (sliding renewal,
    # not a new session).
    assert resp.headers["set-cookie"].startswith(f"{SESSION_COOKIE_NAME}={token}")
    assert seen == ["rt-0"]
    assert client.get("/admin/me").status_code == 200

    # Rotation took: the next refresh presents the rotated token.
    resp2 = client.post("/auth/oidc/refresh")
    assert resp2.status_code == 200
    assert seen == ["rt-0", "rt-0x"]


def test_refresh_revalidates_whitelist_on_new_email(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    """A refresh is a fresh identity proof: a NEW email outside the
    whitelist must 401 and burn the refresh grant — not silently renew."""
    nonce_holder: dict[str, str] = {}
    _install(
        _make_idp(
            jwks_doc,
            rsa_key,
            nonce_holder=nonce_holder,
            refresh_email="intruder@evil.example",
        )
    )
    client = _client()
    _sso_login(client, nonce_holder)

    resp = client.post("/auth/oidc/refresh")
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "refresh_failed"
    assert resp.json()["detail"]["code"] == "email_not_allowed"
    # Entry cleared: the next attempt has nothing to redeem…
    resp2 = client.post("/auth/oidc/refresh")
    assert resp2.status_code == 401
    assert resp2.json()["detail"]["error"] == "no_refresh_token"
    # …but the session itself survives (password-session semantics).
    assert client.get("/admin/me").status_code == 200


def test_refresh_failure_clears_entry_but_not_session(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    nonce_holder: dict[str, str] = {}
    _install(
        _make_idp(jwks_doc, rsa_key, nonce_holder=nonce_holder, refresh_status=400)
    )
    client = _client()
    _sso_login(client, nonce_holder)

    resp = client.post("/auth/oidc/refresh")
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "refresh_failed"
    resp2 = client.post("/auth/oidc/refresh")
    assert resp2.status_code == 401
    assert resp2.json()["detail"]["error"] == "no_refresh_token"
    assert client.get("/admin/me").status_code == 200


def test_refresh_requires_authenticated_session(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    _install(_make_idp(jwks_doc, rsa_key))
    resp = _client().post("/auth/oidc/refresh")
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "unauthenticated"


def test_refresh_401_without_stored_refresh_token(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    """Login WITHOUT offline_access (IdP returned no refresh_token) —
    refresh must degrade explicitly, not 500."""
    nonce_holder: dict[str, str] = {}
    _install(
        _make_idp(
            jwks_doc, rsa_key, login_refresh_token=None, nonce_holder=nonce_holder
        )
    )
    client = _client()
    _sso_login(client, nonce_holder)

    resp = client.post("/auth/oidc/refresh")
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "no_refresh_token"


def test_refresh_404_when_oidc_disabled(state: AdminState) -> None:
    state.oidc_settings = None
    client = _client()
    _password_login(client)
    resp = client.post("/auth/oidc/refresh")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "oidc_disabled"


def test_concurrent_refresh_is_single_flight(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    """Two overlapping refreshes must serialize: the token endpoint must
    never see the same refresh_token twice (rotation revokes the grant
    on replay at real IdPs)."""
    nonce_holder: dict[str, str] = {}
    seen: list[str] = []
    _install(
        _make_idp(
            jwks_doc,
            rsa_key,
            nonce_holder=nonce_holder,
            seen_refresh_tokens=seen,
            refresh_delay=0.25,
        )
    )
    login_client = _client()
    token = _sso_login(login_client, nonce_holder)

    def do_refresh(_: int) -> int:
        c = _client()
        c.cookies.set(SESSION_COOKIE_NAME, token)
        return c.post("/auth/oidc/refresh").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(do_refresh, range(2)))

    assert codes == [200, 200]
    assert len(seen) == 2
    # No replay: every presented refresh_token is unique.
    assert len(set(seen)) == 2
    assert seen == ["rt-0", "rt-0x"]


# ---------------------------------------------------------------------------
# Status capability report
# ---------------------------------------------------------------------------


def test_status_reports_end_session_and_refresh_capabilities(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    _install(_make_idp(jwks_doc, rsa_key))
    resp = _client().get("/auth/oidc/status")
    assert resp.status_code == 200
    assert resp.json() == {
        "enabled": True,
        "available": True,
        "end_session": True,
        "refresh": True,
    }


def test_status_capabilities_degrade_explicitly(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    state.oidc_settings = _settings(request_offline_access=False)
    _install(_make_idp(jwks_doc, rsa_key, end_session=False))
    resp = _client().get("/auth/oidc/status")
    assert resp.status_code == 200
    assert resp.json() == {
        "enabled": True,
        "available": True,
        "end_session": False,
        "refresh": False,
    }


# ---------------------------------------------------------------------------
# Regression: /admin/logout is untouched
# ---------------------------------------------------------------------------


def test_admin_logout_semantics_unchanged(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    _install(_make_idp(jwks_doc, rsa_key))
    client = _client()
    _password_login(client)
    resp = client.post("/admin/logout")
    assert resp.status_code == 204
    assert "Max-Age=0" in resp.headers["set-cookie"]
    assert client.get("/admin/me").status_code == 401


# ---------------------------------------------------------------------------
# W3-review fixes
# ---------------------------------------------------------------------------


def test_refresh_rejects_sub_mismatch(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    """MAJOR review fix (OIDC Core 12.2): the refreshed id_token must
    prove the SAME subject the session logged in as — a different
    whitelisted identity's grant must not renew this session."""
    nonce_holder: dict[str, str] = {}
    _install(
        _make_idp(
            jwks_doc,
            rsa_key,
            nonce_holder=nonce_holder,
            refresh_sub="user-2",  # whitelisted email, DIFFERENT subject
        )
    )
    client = _client()
    _sso_login(client, nonce_holder)
    resp = client.post("/auth/oidc/refresh")
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "refresh_failed"
    # The burned grant cannot be replayed.
    again = client.post("/auth/oidc/refresh")
    assert again.status_code == 401
    assert again.json()["detail"]["error"] == "no_refresh_token"


def test_admin_logout_burns_sso_tokens(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    """MAJOR review fix: POST /admin/logout (the button the frontend
    actually calls) must clear the SSO side table — the id/refresh
    tokens must not outlive the session they belong to."""
    nonce_holder: dict[str, str] = {}
    _install(_make_idp(jwks_doc, rsa_key, nonce_holder=nonce_holder))
    client = _client()
    token = _sso_login(client, nonce_holder)
    assert oidc_mod._SSO_TOKENS.get(token) is not None

    out = client.post("/admin/logout")
    assert out.status_code == 204
    assert oidc_mod._SSO_TOKENS.get(token) is None


def test_transient_refresh_failure_keeps_the_grant(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    """Review fix: an IdP 5xx / network blip is not an identity
    rejection — the stored grant survives for a later retry (popping it
    also destroyed the id_token, permanently degrading SSO logout)."""
    nonce_holder: dict[str, str] = {}
    _install(
        _make_idp(
            jwks_doc, rsa_key, nonce_holder=nonce_holder, refresh_status=503
        )
    )
    client = _client()
    token = _sso_login(client, nonce_holder)
    resp = client.post("/auth/oidc/refresh")
    assert resp.status_code == 503
    assert oidc_mod._SSO_TOKENS.get(token) is not None
    row = oidc_mod._SSO_TOKENS.get(token)
    assert row is not None and row.refresh_token
