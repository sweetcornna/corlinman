"""Inbound OIDC SSO login (G3) — ``/auth/oidc/*``.

Coverage matrix (per the task spec):

* discovery + status endpoint (mocked HTTP, success + failure);
* full happy path — /auth/oidc/login mints state+PKCE, the callback
  exchanges the code, validates a self-signed id_token against a test
  JWKS, and issues the SAME ``corlinman_session`` cookie as the
  password login;
* rejection paths: state mismatch, expired id_token, wrong ``aud``,
  nonce mismatch, email outside the whitelist, empty whitelist
  (fail-closed), missing id_token;
* password-login regression: ``POST /admin/login`` behaves identically
  with the OIDC router mounted.

All IdP traffic is intercepted with ``httpx.MockTransport`` via the
module's ``_TEST_TRANSPORT`` seam — nothing leaves the process. The
id_token is signed with a per-run RSA key (pyjwt[crypto]).
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
jwt = pytest.importorskip("jwt")

import httpx
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


# ---------------------------------------------------------------------------
# Test RSA key + JWKS (module-scoped: keygen is the slow part)
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
    nonce: str,
    email: str = "ops@example.com",
    aud: str = CLIENT_ID,
    iss: str = ISSUER,
    exp_delta: int = 600,
    kid: str = "test-key",
    extra: dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "sub": "user-1",
        "email": email,
        "nonce": nonce,
        "iat": now,
        "exp": now + exp_delta,
    }
    if extra:
        claims.update(extra)
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": kid})


# ---------------------------------------------------------------------------
# Mock IdP transport
# ---------------------------------------------------------------------------


def _make_transport(
    jwks: dict[str, Any],
    *,
    id_token_factory: Any = None,
    discovery_status: int = 200,
    token_status: int = 200,
) -> httpx.MockTransport:
    """A MockTransport playing the IdP: discovery + token + JWKS."""

    discovery_doc = {
        "issuer": ISSUER,
        "authorization_endpoint": AUTHORIZE_URL,
        "token_endpoint": TOKEN_URL,
        "jwks_uri": JWKS_URL,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == DISCOVERY_URL:
            return httpx.Response(discovery_status, json=discovery_doc)
        if url == JWKS_URL:
            return httpx.Response(200, json=jwks)
        if url == TOKEN_URL:
            if token_status != 200:
                return httpx.Response(token_status, json={"error": "invalid_grant"})
            body: dict[str, Any] = {
                "access_token": "at-test",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
            if id_token_factory is not None:
                # The factory gets the parsed form body so tests can
                # bind the token's nonce to the live transaction.
                form = dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))
                token = id_token_factory(form)
                if token is not None:
                    body["id_token"] = token
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


def _client(follow_redirects: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(oidc_mod.router())
    app.include_router(auth_mod.router())
    return TestClient(app, base_url="http://testserver", follow_redirects=follow_redirects)


def _install_transport(transport: httpx.MockTransport) -> None:
    oidc_mod._TEST_TRANSPORT = transport


def _start_login(client: TestClient) -> dict[str, str]:
    """Hit /auth/oidc/login and return the authorize-URL query params."""
    resp = client.get("/auth/oidc/login", params={"redirect": "/chat"})
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(AUTHORIZE_URL + "?")
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))


def _oidc_error_of(resp: httpx.Response) -> str:
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith("/login?"), location
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))[
        "oidc_error"
    ]


# ---------------------------------------------------------------------------
# Status + discovery
# ---------------------------------------------------------------------------


def test_status_reports_available_when_discovery_succeeds(
    state: AdminState, jwks_doc: dict[str, Any]
) -> None:
    _install_transport(_make_transport(jwks_doc))
    resp = _client().get("/auth/oidc/status")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "available": True}


def test_status_unavailable_when_discovery_fails(
    state: AdminState, jwks_doc: dict[str, Any]
) -> None:
    _install_transport(_make_transport(jwks_doc, discovery_status=503))
    resp = _client().get("/auth/oidc/status")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "available": False}


def test_status_disabled_when_not_configured(state: AdminState) -> None:
    state.oidc_settings = None
    resp = _client().get("/auth/oidc/status")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "available": False}


def test_login_redirects_to_login_page_when_discovery_fails(
    state: AdminState, jwks_doc: dict[str, Any]
) -> None:
    """Discovery failure must degrade to a visible login-page error, not a 500."""
    _install_transport(_make_transport(jwks_doc, discovery_status=500))
    resp = _client().get("/auth/oidc/login")
    assert _oidc_error_of(resp) == "discovery_failed"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_full_flow_issues_admin_session_cookie(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    _install_transport(
        _make_transport(
            jwks_doc,
            id_token_factory=lambda form: _mint_id_token(
                rsa_key, nonce=_nonce_holder["nonce"]
            ),
        )
    )
    client = _client()
    params = _start_login(client)
    # PKCE + state + nonce all ride the authorize URL.
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["code_challenge_method"] == "S256"
    assert params["scope"] == "openid email profile"
    assert len(params["state"]) >= 32
    _nonce_holder["nonce"] = params["nonce"]

    resp = client.get(
        "/auth/oidc/callback", params={"state": params["state"], "code": "authcode-1"}
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/chat"
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    assert "HttpOnly" in cookie

    # The cookie is honoured by the SAME session surface password logins
    # use — /admin/me answers with the OIDC identity.
    token = cookie.split(";", 1)[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE_NAME, token)
    me = client.get("/admin/me")
    assert me.status_code == 200
    assert me.json()["user"] == "ops@example.com"


_nonce_holder: dict[str, str] = {"nonce": ""}


def test_state_is_single_use(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    _install_transport(
        _make_transport(
            jwks_doc,
            id_token_factory=lambda form: _mint_id_token(
                rsa_key, nonce=_nonce_holder["nonce"]
            ),
        )
    )
    client = _client()
    params = _start_login(client)
    _nonce_holder["nonce"] = params["nonce"]
    first = client.get(
        "/auth/oidc/callback", params={"state": params["state"], "code": "authcode-1"}
    )
    assert first.status_code == 302 and first.headers["location"] == "/chat"
    # Replaying the exact same callback must fail: the state was popped.
    replay = client.get(
        "/auth/oidc/callback", params={"state": params["state"], "code": "authcode-1"}
    )
    assert _oidc_error_of(replay) == "state_mismatch"


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------


def test_callback_rejects_unknown_state(
    state: AdminState, jwks_doc: dict[str, Any]
) -> None:
    _install_transport(_make_transport(jwks_doc))
    resp = _client().get(
        "/auth/oidc/callback", params={"state": "forged-state", "code": "x"}
    )
    assert _oidc_error_of(resp) == "state_mismatch"


def test_callback_rejects_expired_id_token(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    _install_transport(
        _make_transport(
            jwks_doc,
            id_token_factory=lambda form: _mint_id_token(
                rsa_key, nonce=_nonce_holder["nonce"], exp_delta=-3600
            ),
        )
    )
    client = _client()
    params = _start_login(client)
    _nonce_holder["nonce"] = params["nonce"]
    resp = client.get(
        "/auth/oidc/callback", params={"state": params["state"], "code": "authcode-1"}
    )
    assert _oidc_error_of(resp) == "invalid_id_token"


def test_callback_rejects_wrong_audience(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    _install_transport(
        _make_transport(
            jwks_doc,
            id_token_factory=lambda form: _mint_id_token(
                rsa_key, nonce=_nonce_holder["nonce"], aud="some-other-client"
            ),
        )
    )
    client = _client()
    params = _start_login(client)
    _nonce_holder["nonce"] = params["nonce"]
    resp = client.get(
        "/auth/oidc/callback", params={"state": params["state"], "code": "authcode-1"}
    )
    assert _oidc_error_of(resp) == "invalid_id_token"


def test_callback_rejects_nonce_mismatch(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    _install_transport(
        _make_transport(
            jwks_doc,
            id_token_factory=lambda form: _mint_id_token(
                rsa_key, nonce="attacker-chosen-nonce"
            ),
        )
    )
    client = _client()
    params = _start_login(client)
    resp = client.get(
        "/auth/oidc/callback", params={"state": params["state"], "code": "authcode-1"}
    )
    assert _oidc_error_of(resp) == "invalid_id_token"


def test_callback_rejects_email_outside_whitelist(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    _install_transport(
        _make_transport(
            jwks_doc,
            id_token_factory=lambda form: _mint_id_token(
                rsa_key, nonce=_nonce_holder["nonce"], email="intruder@evil.example"
            ),
        )
    )
    client = _client()
    params = _start_login(client)
    _nonce_holder["nonce"] = params["nonce"]
    resp = client.get(
        "/auth/oidc/callback", params={"state": params["state"], "code": "authcode-1"}
    )
    assert _oidc_error_of(resp) == "email_not_allowed"


def test_empty_whitelist_fails_closed(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    """enabled + empty whitelist must reject EVERYONE, not admit everyone."""
    state.oidc_settings = _settings(allowed_emails=(), allowed_domains=())
    _install_transport(
        _make_transport(
            jwks_doc,
            id_token_factory=lambda form: _mint_id_token(
                rsa_key, nonce=_nonce_holder["nonce"]
            ),
        )
    )
    client = _client()
    params = _start_login(client)
    _nonce_holder["nonce"] = params["nonce"]
    resp = client.get(
        "/auth/oidc/callback", params={"state": params["state"], "code": "authcode-1"}
    )
    assert _oidc_error_of(resp) == "email_not_allowed"


def test_allowed_domain_admits_matching_email(
    state: AdminState, rsa_key: Any, jwks_doc: dict[str, Any]
) -> None:
    state.oidc_settings = _settings(allowed_emails=(), allowed_domains=("example.com",))
    _install_transport(
        _make_transport(
            jwks_doc,
            id_token_factory=lambda form: _mint_id_token(
                rsa_key, nonce=_nonce_holder["nonce"], email="Anyone@Example.com"
            ),
        )
    )
    client = _client()
    params = _start_login(client)
    _nonce_holder["nonce"] = params["nonce"]
    resp = client.get(
        "/auth/oidc/callback", params={"state": params["state"], "code": "authcode-1"}
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/chat"
    assert SESSION_COOKIE_NAME in resp.headers.get("set-cookie", "")


def test_callback_rejects_missing_id_token(
    state: AdminState, jwks_doc: dict[str, Any]
) -> None:
    _install_transport(_make_transport(jwks_doc, id_token_factory=lambda form: None))
    client = _client()
    params = _start_login(client)
    resp = client.get(
        "/auth/oidc/callback", params={"state": params["state"], "code": "authcode-1"}
    )
    assert _oidc_error_of(resp) == "invalid_id_token"


def test_callback_token_exchange_failure_is_visible_error(
    state: AdminState, jwks_doc: dict[str, Any]
) -> None:
    _install_transport(_make_transport(jwks_doc, token_status=400))
    client = _client()
    params = _start_login(client)
    resp = client.get(
        "/auth/oidc/callback", params={"state": params["state"], "code": "bad-code"}
    )
    assert _oidc_error_of(resp) == "token_exchange_failed"


def test_callback_disabled_returns_login_error(state: AdminState) -> None:
    state.oidc_settings = None
    resp = _client().get("/auth/oidc/callback", params={"state": "s", "code": "c"})
    assert _oidc_error_of(resp) == "oidc_disabled"


def test_login_404_when_disabled(state: AdminState) -> None:
    state.oidc_settings = None
    resp = _client().get("/auth/oidc/login")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "oidc_disabled"


def test_redirect_param_is_sanitized(
    state: AdminState, jwks_doc: dict[str, Any]
) -> None:
    """Absolute/protocol-relative redirect targets collapse to '/'."""
    _install_transport(_make_transport(jwks_doc))
    client = _client()
    for evil in ("https://evil.example/", "//evil.example", "/\\evil"):
        resp = client.get("/auth/oidc/login", params={"redirect": evil})
        assert resp.status_code == 302
        # The sanitized redirect lands in the txn; cheapest observable
        # assertion: the helper itself.
        assert oidc_mod._sanitize_redirect(evil) == "/"


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------


def test_resolve_settings_defaults_and_normalization() -> None:
    cfg = {
        "auth": {
            "oidc": {
                "enabled": True,
                "issuer": "https://idp.test/",
                "client_id": "cid",
                "allowed_emails": ["Ops@Example.COM"],
                "allowed_domains": ["@Corp.Example"],
            }
        }
    }
    settings = oidc_mod.resolve_oidc_settings(cfg)
    assert settings is not None
    assert settings.enabled is True
    assert settings.issuer == "https://idp.test"
    assert settings.scopes == ("openid", "email", "profile")
    assert settings.allowed_emails == ("ops@example.com",)
    assert settings.allowed_domains == ("corp.example",)


def test_resolve_settings_disables_on_missing_issuer() -> None:
    cfg = {"auth": {"oidc": {"enabled": True, "client_id": "cid"}}}
    settings = oidc_mod.resolve_oidc_settings(cfg)
    assert settings is not None
    assert settings.enabled is False


def test_resolve_settings_absent_section_returns_none() -> None:
    assert oidc_mod.resolve_oidc_settings({}) is None
    assert oidc_mod.resolve_oidc_settings(None) is None


# ---------------------------------------------------------------------------
# Password-login regression — OIDC must not perturb the existing path.
# ---------------------------------------------------------------------------


def test_password_login_still_works_with_oidc_mounted(state: AdminState) -> None:
    client = _client()
    resp = client.post(
        "/admin/login", json={"username": "admin", "password": "s3cret-pw"}
    )
    assert resp.status_code == 200
    assert resp.json()["expires_in"] > 0
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    token = cookie.split(";", 1)[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE_NAME, token)
    me = client.get("/admin/me")
    assert me.status_code == 200
    assert me.json()["user"] == "admin"


def test_password_login_wrong_password_still_401(state: AdminState) -> None:
    client = _client()
    resp = client.post(
        "/admin/login", json={"username": "admin", "password": "wrong"}
    )
    assert resp.status_code == 401
