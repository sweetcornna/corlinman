"""Tests for the admin-session -> /v1/chat bridge.

Bug: the in-app chat UI authenticates to the gateway with the HttpOnly
``corlinman_session`` cookie (same as the rest of the admin dashboard), but
``/v1/chat/completions`` is gated by the bearer-only
:class:`ApiKeyAuthMiddleware`. After the v1.15 authz hardening installed that
gate, the cookie-only chat request started returning ``401
missing_authorization`` ("missing auth") because no API key is minted at boot
and the UI attaches no bearer.

Fix: when no bearer/X-API-Key is present AND the path is a chat endpoint, the
middleware consults an injected ``admin_session_resolver`` that returns the
operator's tenant for a valid admin browser session. It is strictly additive
and fails closed:

* a cookieless request still 401s (the regression contract);
* the bridge is scoped to :data:`ADMIN_SESSION_BRIDGE_PREFIXES` (``/v1/chat``)
  — other protected ``/v1`` surfaces still demand a real key;
* it never fires when a bearer is present (SDK/curl unchanged);
* it works even before any API key is minted (``admin_db`` may be ``None``).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from corlinman_server.gateway.middleware.auth import (
    ADMIN_SESSION_BRIDGE_PREFIXES,
    install_api_key_middleware,
)
from corlinman_server.gateway.routes_admin_a._auth_shim import admin_session_tenant
from corlinman_server.tenancy import ApiKeyRow, default_tenant
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

_COOKIE = "corlinman_session"
_VALID = "valid-session-token"


@dataclass
class _FakeAdminDb:
    valid_token: str = "secret-token"

    async def verify_api_key(self, token: str) -> ApiKeyRow | None:
        if token != self.valid_token:
            return None
        return ApiKeyRow(
            key_id="key_test",
            tenant_id=default_tenant(),
            username="alice",
            scope="chat",
            label=None,
            token_hash="dummy",
            created_at_ms=0,
            last_used_at_ms=None,
            revoked_at_ms=None,
        )


def _resolver_factory() -> tuple[list[int], object]:
    """A fake admin_session_resolver + a call-counter list."""
    calls = [0]

    def resolver(request: Request):
        calls[0] += 1
        if request.cookies.get(_COOKIE) == _VALID:
            return default_tenant()
        return None

    return calls, resolver


def _app(
    *,
    admin_db: _FakeAdminDb | None,
    with_resolver: bool = True,
) -> tuple[FastAPI, list[int]]:
    app = FastAPI()
    calls, resolver = _resolver_factory()
    install_api_key_middleware(
        app,
        admin_db=admin_db,  # type: ignore[arg-type]
        admin_session_resolver=resolver if with_resolver else None,
    )

    @app.post("/v1/chat/completions")
    def chat(request: Request) -> dict[str, object]:
        tenant = getattr(request.state, "tenant", None)
        return {
            "tenant": tenant.as_str() if tenant is not None else None,
            "bridged": getattr(request.state, "admin_session_bridged", False),
        }

    @app.post("/v1/memory/upsert")
    def memory(request: Request) -> dict[str, str]:
        return {"ok": "true"}

    return app, calls


# ---------------------------------------------------------------------------
# Middleware-level behaviour.
# ---------------------------------------------------------------------------


def test_chat_bridged_with_valid_session_cookie() -> None:
    app, calls = _app(admin_db=_FakeAdminDb())
    client = TestClient(app)
    client.cookies.set(_COOKIE, _VALID)
    resp = client.post("/v1/chat/completions")
    assert resp.status_code == 200
    assert resp.json() == {"tenant": "default", "bridged": True}
    assert calls[0] == 1  # resolver consulted exactly once


def test_chat_bridge_works_before_any_key_minted() -> None:
    # The fresh-deploy case: tenants.sqlite exists but holds zero keys, so
    # admin_db verifies nothing — the bridge must STILL let the admin chat.
    app, _ = _app(admin_db=None)
    client = TestClient(app)
    client.cookies.set(_COOKIE, _VALID)
    resp = client.post("/v1/chat/completions")
    assert resp.status_code == 200
    assert resp.json()["bridged"] is True


def test_cookieless_chat_still_401() -> None:
    # The fail-closed contract: no cookie, no bearer → 401 (unchanged).
    app, _ = _app(admin_db=_FakeAdminDb())
    client = TestClient(app)
    resp = client.post("/v1/chat/completions")
    assert resp.status_code == 401
    assert resp.json()["reason"] == "missing_authorization"


def test_invalid_session_cookie_falls_through_to_401() -> None:
    app, calls = _app(admin_db=_FakeAdminDb())
    client = TestClient(app)
    client.cookies.set(_COOKIE, "garbage")
    resp = client.post("/v1/chat/completions")
    assert resp.status_code == 401
    assert resp.json()["reason"] == "missing_authorization"
    assert calls[0] == 1  # resolver was consulted but returned None


def test_bridge_is_scoped_to_chat_only() -> None:
    # A valid admin cookie does NOT unlock other protected /v1 surfaces;
    # the resolver isn't even consulted off the chat prefix.
    app, calls = _app(admin_db=_FakeAdminDb())
    client = TestClient(app)
    client.cookies.set(_COOKIE, _VALID)
    resp = client.post("/v1/memory/upsert")
    assert resp.status_code == 401
    assert resp.json()["reason"] == "missing_authorization"
    assert calls[0] == 0  # bridge never fires off the chat prefix


def test_bearer_wins_resolver_not_consulted() -> None:
    # When a real bearer is present the bridge is skipped entirely.
    app, calls = _app(admin_db=_FakeAdminDb())
    client = TestClient(app)
    client.cookies.set(_COOKIE, _VALID)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["bridged"] is False
    assert calls[0] == 0  # resolver never consulted when a bearer is present


def test_no_resolver_wired_keeps_bearer_only() -> None:
    # Default (no bridge wired) — a cookie must NOT authenticate /v1/chat.
    app, _ = _app(admin_db=_FakeAdminDb(), with_resolver=False)
    client = TestClient(app)
    client.cookies.set(_COOKIE, _VALID)
    resp = client.post("/v1/chat/completions")
    assert resp.status_code == 401


def test_bridge_prefixes_are_chat_only() -> None:
    assert ADMIN_SESSION_BRIDGE_PREFIXES == ("/v1/chat",)


# ---------------------------------------------------------------------------
# admin_session_tenant resolver (the cookie validator the bridge injects).
# ---------------------------------------------------------------------------


class _FakeSessionStore:
    def __init__(self, valid_token: str) -> None:
        self._valid = valid_token

    def validate(self, token: str):
        return SimpleNamespace(user="admin") if token == self._valid else None


def _fake_request(cookie_value: str | None):
    cookies = {_COOKIE: cookie_value} if cookie_value is not None else {}
    return SimpleNamespace(cookies=cookies, headers={}, state=SimpleNamespace())


def _admin_state(**overrides):
    base = {
        "session_store": _FakeSessionStore(_VALID),
        "default_tenant": None,
        "must_change_password": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_resolver_returns_tenant_for_valid_cookie() -> None:
    req = _fake_request(_VALID)
    tenant = admin_session_tenant(req, _admin_state())
    assert tenant is not None
    assert tenant.as_str() == "default"
    # Side-effects mirror authenticate_admin_request's cookie branch.
    assert req.state.admin_user == "admin"
    assert req.state.admin_session is not None


def test_resolver_none_without_cookie() -> None:
    assert admin_session_tenant(_fake_request(None), _admin_state()) is None


def test_resolver_none_for_invalid_token() -> None:
    assert admin_session_tenant(_fake_request("nope"), _admin_state()) is None


def test_resolver_none_when_store_unconfigured() -> None:
    state = _admin_state(session_store=None)
    assert admin_session_tenant(_fake_request(_VALID), state) is None


def test_resolver_closed_while_must_change_password() -> None:
    # SEC-007: the seeded admin/root creds must be rotated before the bridge
    # opens — same gate the /admin/* surface enforces.
    state = _admin_state(must_change_password=True)
    assert admin_session_tenant(_fake_request(_VALID), state) is None
