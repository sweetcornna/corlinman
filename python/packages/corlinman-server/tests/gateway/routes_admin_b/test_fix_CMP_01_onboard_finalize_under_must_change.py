"""CMP-01 — the onboard wizard must complete while ``must_change_password``.

The admin-auth shim 403s every ``/admin/*`` path not in
``_PW_CHANGE_ALLOWED_PATHS``. That allowlist only carried the exact
``/admin/onboard`` and a stale ``/admin/onboard/finalize`` (which is a
real route but not where rotation happens). The actual first-run wizard
routes are ``/admin/onboard/finalize-skip``, ``-account``, ``-password``,
``-persona``, and ``-image-provider``. Because the onboard router mounts
``Depends(require_admin)`` — which runs through the central shim — a
fresh install with seeded ``admin/root`` + ``must_change_password=True``
cannot reach ``POST /admin/onboard/finalize-password`` (and friends): the
shim short-circuits with 403 ``password_change_required`` before the
handler runs, so the wizard can never rotate the seeded credentials.

Acceptance: with the seeded first-boot state, a valid session cookie can
drive ``POST /admin/onboard/finalize-password`` end-to-end (rotation
succeeds, flag clears) and the other ``finalize-*`` steps are reachable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_server.gateway.lifecycle.admin_seed import ensure_admin_credentials
from corlinman_server.gateway.routes_admin_a._session_store import (
    SESSION_COOKIE_NAME,
    AdminSessionStore,
)
from corlinman_server.gateway.routes_admin_a.auth import router as auth_router
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState as AdminStateA,
    set_admin_state as set_admin_state_a,
)
from corlinman_server.gateway.routes_admin_b.onboard import router as onboard_router
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState as AdminStateB,
    set_admin_state as set_admin_state_b,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def seeded(tmp_path: Path) -> Iterator[tuple[AdminStateA, AdminStateB]]:
    """First-boot states for both admin bundles, sharing one session store
    and the same seeded ``admin/root`` + ``must_change_password=True``."""
    cfg = tmp_path / "config.toml"
    seed = asyncio.run(ensure_admin_credentials(config_path=cfg))
    session_store = AdminSessionStore(ttl_seconds=3600)
    lock = asyncio.Lock()

    state_a = AdminStateA(
        data_dir=tmp_path,
        admin_username=seed.username,
        admin_password_hash=seed.password_hash,
        config_path=seed.config_path,
        must_change_password=seed.must_change_password,
        session_store=session_store,
        admin_write_lock=lock,
    )
    state_b = AdminStateB(
        data_dir=tmp_path,
        admin_username=seed.username,
        admin_password_hash=seed.password_hash,
        config_path=seed.config_path,
        must_change_password=seed.must_change_password,
        session_store=session_store,
        admin_write_lock=lock,
    )
    assert state_a.must_change_password is True
    assert state_b.must_change_password is True
    set_admin_state_a(state_a)
    set_admin_state_b(state_b)
    try:
        yield state_a, state_b
    finally:
        set_admin_state_a(None)
        set_admin_state_b(None)


@pytest.fixture
def client(seeded: tuple[AdminStateA, AdminStateB]) -> TestClient:
    app = FastAPI()
    app.include_router(auth_router())
    app.include_router(onboard_router())
    return TestClient(app)


def _login(client: TestClient) -> str:
    login = client.post(
        "/admin/login", json={"username": "admin", "password": "root"}
    )
    assert login.status_code == 200, login.text
    cookie = login.cookies.get(SESSION_COOKIE_NAME)
    assert cookie
    return cookie


def test_finalize_password_reachable_under_must_change(client: TestClient) -> None:
    """The core bug: rotating the seeded password through the wizard.

    Pre-fix: 403 ``password_change_required`` from the shim. Post-fix: the
    handler runs, rotation succeeds (200), flag cleared.
    """
    cookie = _login(client)
    resp = client.post(
        "/admin/onboard/finalize-password",
        json={"old_password": "root", "new_password": "freshpass-1"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["must_change_password"] is False


def test_finalize_account_reachable_under_must_change(client: TestClient) -> None:
    """``finalize-account`` (B1) must also be reachable during first-run."""
    cookie = _login(client)
    resp = client.post(
        "/admin/onboard/finalize-account",
        json={"new_username": "operator"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    # Not 403-gated; rename succeeds (200).
    assert resp.status_code != 403, resp.text
    assert resp.status_code == 200, resp.text


def test_finalize_persona_skip_reachable_under_must_change(
    client: TestClient,
) -> None:
    """``finalize-persona`` (B3) skip branch must be reachable."""
    cookie = _login(client)
    resp = client.post(
        "/admin/onboard/finalize-persona",
        json={"choice": "skip"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert resp.status_code != 403, resp.text
    assert resp.status_code == 200, resp.text


def test_finalize_skip_reachable_under_must_change(client: TestClient) -> None:
    """The zero-credential ``finalize-skip`` path must be reachable."""
    cookie = _login(client)
    resp = client.post(
        "/admin/onboard/finalize-skip",
        json={},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert resp.status_code != 403, resp.text
    assert resp.status_code == 200, resp.text


def test_finalize_image_provider_skip_reachable_under_must_change(
    client: TestClient,
) -> None:
    """``finalize-image-provider`` (B4) skip branch must be reachable."""
    cookie = _login(client)
    resp = client.post(
        "/admin/onboard/finalize-image-provider",
        json={"choice": "skip"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert resp.status_code != 403, resp.text
    assert resp.status_code == 200, resp.text
