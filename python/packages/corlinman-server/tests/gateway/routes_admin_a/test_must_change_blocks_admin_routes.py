"""SEC-007 — server-side enforcement of ``must_change_password``.

The first-boot admin credentials are ``admin``/``root`` with
``must_change_password=True`` on the seeded :class:`AdminState`. Until
the operator picks a real password, **every** ``/admin/*`` route that is
gated by :func:`authenticate_admin_request` (the central admin-auth
shim) must refuse to run — otherwise an attacker who reaches the
gateway during the first-boot window owns the box.

The only routes that may run while ``must_change_password`` is set are
the rotation paths themselves (``/admin/login``, ``/admin/logout``,
``/admin/me``, ``/admin/password``, ``/admin/username``,
``/admin/onboard``). Those routes mount **outside** the central shim by
design (they have to be reachable to issue the very first cookie) so
they're already unaffected by the gate — these tests just confirm that
the rotate-then-call path produces a clean recovery.

Acceptance: with seeded ``admin/root`` + ``must_change_password=True``,
hitting an admin-A protected route (``/admin/api_keys``) returns
**403 ``password_change_required``** instead of the underlying handler's
response. After ``POST /admin/password`` flips the flag, the same call
returns whatever the handler normally would (here: 503 since no
``admin_db`` is wired in the test fixture).
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from corlinman_server.gateway.lifecycle.admin_seed import ensure_admin_credentials
from corlinman_server.gateway.routes_admin_a.api_keys import router as api_keys_router
from corlinman_server.gateway.routes_admin_a.auth import router as auth_router
from corlinman_server.gateway.routes_admin_a._session_store import (
    SESSION_COOKIE_NAME,
    AdminSessionStore,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    set_admin_state,
)


def _basic_header(username: str, password: str) -> dict[str, str]:
    raw = f"{username}:{password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


@pytest.fixture
def seeded_state(tmp_path: Path) -> Iterator[AdminState]:
    """A first-boot ``AdminState`` — seeded ``admin/root`` + must_change=True."""
    cfg = tmp_path / "config.toml"
    seeded = asyncio.run(ensure_admin_credentials(config_path=cfg))
    state = AdminState(
        data_dir=tmp_path,
        admin_username=seeded.username,
        admin_password_hash=seeded.password_hash,
        config_path=seeded.config_path,
        must_change_password=seeded.must_change_password,
        session_store=AdminSessionStore(ttl_seconds=3600),
        admin_write_lock=asyncio.Lock(),
    )
    assert state.must_change_password is True, "seeded fixture must trip the gate"
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture
def client(seeded_state: AdminState) -> TestClient:
    """Mount the auth router (so rotation paths work) + a protected admin
    router (api_keys) so we can prove the gate fires on a real route."""
    app = FastAPI()
    app.include_router(auth_router())
    app.include_router(api_keys_router())
    return TestClient(app)


# ---------------------------------------------------------------------------
# Gate fires on protected routes (the bug)
# ---------------------------------------------------------------------------


def test_basic_auth_to_protected_route_is_blocked_until_password_changed(
    client: TestClient,
) -> None:
    """``admin/root`` Basic-auth + GET /admin/api_keys → 403 password_change_required.

    Pre-fix: this returns 503 ``tenants_disabled`` (the handler ran, then
    failed on the missing admin_db). Post-fix: the auth shim short-circuits
    with 403 before the handler is reached.
    """
    resp = client.get("/admin/api_keys", headers=_basic_header("admin", "root"))
    assert resp.status_code == 403, resp.text
    body = resp.json()
    # FastAPI wraps HTTPException.detail under the ``detail`` key.
    detail = body.get("detail", body)
    assert detail.get("error") == "password_change_required", body


def test_session_cookie_to_protected_route_is_blocked_until_password_changed(
    client: TestClient,
) -> None:
    """Even a freshly minted session cookie cannot reach a protected route
    while the seeded must_change flag is still set."""
    login = client.post(
        "/admin/login", json={"username": "admin", "password": "root"}
    )
    assert login.status_code == 200, login.text
    cookie = login.cookies.get(SESSION_COOKIE_NAME)
    assert cookie

    resp = client.get(
        "/admin/api_keys", cookies={SESSION_COOKIE_NAME: cookie}
    )
    assert resp.status_code == 403, resp.text
    detail = resp.json().get("detail", resp.json())
    assert detail.get("error") == "password_change_required", resp.text


def test_delete_api_key_is_blocked(client: TestClient) -> None:
    """Mutating routes are gated too — not just reads."""
    resp = client.delete(
        "/admin/api_keys/some-key-id",
        headers=_basic_header("admin", "root"),
    )
    assert resp.status_code == 403, resp.text


def test_mint_api_key_is_blocked(client: TestClient) -> None:
    """POST /admin/api_keys (write surface) is also gated."""
    resp = client.post(
        "/admin/api_keys",
        json={"scope": "test"},
        headers=_basic_header("admin", "root"),
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Allowlist — rotation + introspection routes still work
# ---------------------------------------------------------------------------


def test_login_still_works_under_must_change(client: TestClient) -> None:
    """``/admin/login`` mounts outside the central shim so it is reachable
    regardless of the flag — verify the contract didn't drift."""
    resp = client.post(
        "/admin/login", json={"username": "admin", "password": "root"}
    )
    assert resp.status_code == 200, resp.text


def test_me_still_works_under_must_change(client: TestClient) -> None:
    """The UI needs ``GET /admin/me`` to read the must_change flag in
    order to render the forced-rotate banner."""
    login = client.post(
        "/admin/login", json={"username": "admin", "password": "root"}
    )
    cookie = login.cookies.get(SESSION_COOKIE_NAME)
    me = client.get("/admin/me", cookies={SESSION_COOKIE_NAME: cookie})
    assert me.status_code == 200, me.text
    assert me.json()["must_change_password"] is True


def test_password_rotation_still_works_under_must_change(
    client: TestClient,
) -> None:
    """``POST /admin/password`` is the recovery path — it must remain
    reachable while ``must_change_password`` is set."""
    login = client.post(
        "/admin/login", json={"username": "admin", "password": "root"}
    )
    cookie = login.cookies.get(SESSION_COOKIE_NAME)
    rotated = client.post(
        "/admin/password",
        json={"old_password": "root", "new_password": "freshpass-1"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert rotated.status_code == 200, rotated.text


def test_logout_still_works_under_must_change(client: TestClient) -> None:
    """``/admin/logout`` must always work so a stuck operator can clear
    their cookie without first rotating."""
    login = client.post(
        "/admin/login", json={"username": "admin", "password": "root"}
    )
    cookie = login.cookies.get(SESSION_COOKIE_NAME)
    out = client.post("/admin/logout", cookies={SESSION_COOKIE_NAME: cookie})
    assert out.status_code == 204, out.text


# ---------------------------------------------------------------------------
# Gate clears after a successful rotation
# ---------------------------------------------------------------------------


def test_protected_route_unblocks_after_password_change(
    client: TestClient,
) -> None:
    """After a successful password rotation, the protected route is no
    longer 403-gated — the underlying handler runs (and in this test
    fixture 503s with ``tenants_disabled`` because we didn't wire an
    admin_db, which is the expected post-gate behavior)."""
    login = client.post(
        "/admin/login", json={"username": "admin", "password": "root"}
    )
    cookie = login.cookies.get(SESSION_COOKIE_NAME)
    rotated = client.post(
        "/admin/password",
        json={"old_password": "root", "new_password": "freshpass-1"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert rotated.status_code == 200

    # Re-authenticate with the new password — the old one must no
    # longer work.
    resp = client.get(
        "/admin/api_keys",
        headers=_basic_header("admin", "freshpass-1"),
    )
    # No more 403; the handler runs and surfaces its own 503 because
    # this minimal test fixture didn't wire an admin_db.
    assert resp.status_code != 403, resp.text
    assert resp.status_code == 503, resp.text


def test_password_change_required_response_carries_helpful_hint(
    client: TestClient,
) -> None:
    """The 403 envelope must include a human-readable message + the
    ``WWW-Authenticate`` header so curl users get a useful diagnostic."""
    resp = client.get(
        "/admin/api_keys", headers=_basic_header("admin", "root")
    )
    assert resp.status_code == 403
    detail = resp.json().get("detail", resp.json())
    assert detail.get("error") == "password_change_required"
    message = detail.get("message", "")
    assert isinstance(message, str) and message, "expected a hint message"
    assert resp.headers.get("WWW-Authenticate", "").startswith("Basic")
