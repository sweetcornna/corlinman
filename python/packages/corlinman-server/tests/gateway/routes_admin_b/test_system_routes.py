"""Tests for ``/admin/system/*`` (W1.1).

Three endpoints + auth + rate-limit + the upgrade-commands fallback when
no release has been observed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_b import system as system_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from corlinman_server.system import (
    SystemUpdateCheckConfig,
    UpdateChecker,
    UpdateStatus,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth


# ---------------------------------------------------------------------------
# Stub checker that records calls
# ---------------------------------------------------------------------------


class _StubChecker:
    """Test double whose ``poll`` returns a programmable UpdateStatus."""

    def __init__(
        self,
        *,
        current: str = "1.1.1",
        latest: str | None = "1.1.2",
        available: bool = True,
        release_url: str | None = "https://github.com/ymylive/corlinman/releases/tag/v1.1.2",
        release_notes_md: str | None = "## Notes\n\n- thing",
        published_at: int | None = 1716000000000,
    ) -> None:
        self.calls: list[bool] = []
        self._status = UpdateStatus(
            current=current,
            latest=latest,
            available=available,
            release_url=release_url,
            release_notes_md=release_notes_md,
            published_at=published_at,
            last_checked_at=1716540000000,
            prerelease_seen=[],
        )

    async def poll(self, *, force: bool = False) -> UpdateStatus:
        self.calls.append(force)
        return self._status


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin_state() -> Iterator[AdminState]:
    state = AdminState()
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(admin_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(system_routes.router())
    return authenticated_test_client(app)


@pytest.fixture()
def unauth_client(admin_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(system_routes.router())
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests — GET /admin/system/info
# ---------------------------------------------------------------------------


def test_get_info_returns_update_status_shape(
    client: TestClient, admin_state: AdminState
) -> None:
    stub = _StubChecker()
    admin_state.update_checker = stub
    resp = client.get("/admin/system/info")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Wire-shape assertions
    for key in (
        "current",
        "latest",
        "available",
        "release_url",
        "release_notes_md",
        "published_at",
        "last_checked_at",
        "prerelease_seen",
    ):
        assert key in body, f"missing key {key!r}"
    assert body["current"] == "1.1.1"
    assert body["latest"] == "1.1.2"
    assert body["available"] is True
    # GET /info uses the cached path → ``force=False``
    assert stub.calls == [False]


def test_get_info_503_when_no_checker_wired(
    client: TestClient, admin_state: AdminState
) -> None:
    admin_state.update_checker = None
    resp = client.get("/admin/system/info")
    assert resp.status_code == 503
    assert resp.json()["error"] == "update_checker_disabled"


# ---------------------------------------------------------------------------
# Tests — POST /admin/system/check-updates
# ---------------------------------------------------------------------------


def test_check_updates_forces_poll(
    client: TestClient, admin_state: AdminState
) -> None:
    stub = _StubChecker()
    admin_state.update_checker = stub
    resp = client.post("/admin/system/check-updates")
    assert resp.status_code == 200
    assert stub.calls == [True]


def test_check_updates_rate_limited_once_per_minute(
    client: TestClient, admin_state: AdminState
) -> None:
    stub = _StubChecker()
    admin_state.update_checker = stub
    first = client.post("/admin/system/check-updates")
    assert first.status_code == 200
    second = client.post("/admin/system/check-updates")
    assert second.status_code == 429
    body = second.json()
    assert body["error"] == "rate_limited"
    assert body["retry_after"] >= 1
    assert second.headers.get("Retry-After") is not None
    # Only the first call reached the checker.
    assert stub.calls == [True]


# ---------------------------------------------------------------------------
# Tests — GET /admin/system/upgrade-commands
# ---------------------------------------------------------------------------


def test_upgrade_commands_inject_version(
    client: TestClient, admin_state: AdminState
) -> None:
    stub = _StubChecker(latest="1.1.2")
    admin_state.update_checker = stub
    resp = client.get("/admin/system/upgrade-commands")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"native", "docker", "docker_with_qq"}
    assert "--version v1.1.2" in body["native"]
    assert "--version v1.1.2" in body["docker"]
    assert "--mode docker" in body["docker"]
    assert "--with-qq" in body["docker_with_qq"]
    assert "--with-qq" not in body["docker"]


def test_upgrade_commands_fall_back_to_main_when_no_release(
    client: TestClient, admin_state: AdminState
) -> None:
    stub = _StubChecker(latest=None, available=False)
    admin_state.update_checker = stub
    resp = client.get("/admin/system/upgrade-commands")
    assert resp.status_code == 200
    body = resp.json()
    assert "--version main" in body["native"]
    assert "--version main" in body["docker"]
    assert "--version main" in body["docker_with_qq"]


# ---------------------------------------------------------------------------
# Tests — auth gating
# ---------------------------------------------------------------------------


def test_no_admin_auth_rejected(
    unauth_client: TestClient, admin_state: AdminState
) -> None:
    admin_state.update_checker = _StubChecker()
    resp = unauth_client.get("/admin/system/info")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Sanity: the real UpdateChecker is also wire-compatible
# ---------------------------------------------------------------------------


def test_resolve_checker_accepts_real_update_checker(tmp_path: Path) -> None:
    """The handler's duck-typed extractor must accept the real class."""
    state = AdminState()
    checker = UpdateChecker(
        config=SystemUpdateCheckConfig(),
        cache_path=tmp_path / ".update_check.json",
    )
    state.update_checker = checker
    assert system_routes._resolve_checker(state) is checker
