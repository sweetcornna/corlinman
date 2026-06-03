"""Tests for the W1.3 one-click upgrade routes (``POST /admin/system/upgrade``
and friends).

Eight focused cases covering the validation matrix in
``docs/PLAN_ONE_CLICK_UPGRADE.md`` §1 W1.3:

1. typed-confirmation mismatch → 400
2. no upgrader wired → 503
3. upgrader self-check fails → 503
4. tag not in observed releases → 400
5. downgrade refused (current >= target) → 400
6. ``allow_downgrade=true`` overrides
7. single-flight 409 with in-flight request_id
8. happy path → 202 + audit log row recorded

Plus tests for the polling + audit-tail surfaces.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from corlinman_server.gateway.routes_admin_b.infra import system as system_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from corlinman_server.system.audit import SystemAuditLog
from corlinman_server.system.update_checker import UpdateStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeUpgradeRequest:
    request_id: str
    tag: str
    mode: str = "docker"


@dataclass
class _FakeUpgradeStatus:
    request_id: str
    tag: str
    state: str
    phase: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    log_excerpt: str | None = None
    error: str | None = None


class _StubUpgrader:
    """Configurable mock implementing the W1.1 UpgraderProtocol surface."""

    def __init__(
        self,
        *,
        available: bool = True,
        mode: str = "docker",
        already_running: str | None = None,
    ) -> None:
        self._available = available
        self.mode = mode
        self._already_running = already_running
        self.started: list[tuple[str, str]] = []
        self._status_by_id: dict[str, _FakeUpgradeStatus] = {}

    async def is_available(self) -> bool:
        return self._available

    async def start(self, target_tag: str, actor: str) -> _FakeUpgradeRequest:
        if self._already_running is not None:
            # Import lazily so we exercise the actual symbol the route
            # imported at module-load time. The real W1.1
            # ``UpgradeAlreadyRunning`` wraps an :class:`UpgradeStatus`
            # carrying request_id + tag + state; we synthesise a
            # matching one for the mock.
            from corlinman_server.gateway.routes_admin_b.infra.system import (
                UpgradeAlreadyRunning,
            )

            in_flight = _FakeUpgradeStatus(
                request_id=self._already_running,
                tag=target_tag,
                state="running",
                phase="pulling",
            )
            raise UpgradeAlreadyRunning(in_flight)
        self.started.append((target_tag, actor))
        request_id = f"req-{len(self.started)}"
        self._status_by_id[request_id] = _FakeUpgradeStatus(
            request_id=request_id, tag=target_tag, state="queued"
        )
        return _FakeUpgradeRequest(
            request_id=request_id, tag=target_tag, mode=self.mode
        )

    async def status(self, request_id: str) -> _FakeUpgradeStatus | None:
        return self._status_by_id.get(request_id)

    async def progress(
        self, request_id: str
    ) -> AsyncIterator[_FakeUpgradeStatus]:
        status = self._status_by_id.get(request_id)
        if status is None:
            return
        # Two frames: running → succeeded.
        yield _FakeUpgradeStatus(
            request_id=request_id,
            tag=status.tag,
            state="running",
            phase="pulling",
        )
        yield _FakeUpgradeStatus(
            request_id=request_id,
            tag=status.tag,
            state="succeeded",
            phase="done",
            finished_at="2026-05-25T10:00:00Z",
        )


class _StubChecker:
    """Test double for UpdateChecker — duck-typed surface only."""

    def __init__(
        self,
        *,
        current: str = "1.2.0",
        latest: str | None = "1.2.1",
        prerelease_seen: list[str] | None = None,
    ) -> None:
        self._current = current
        self._status = UpdateStatus(
            current=current,
            latest=latest,
            available=bool(latest),
            release_url=None,
            release_notes_md=None,
            published_at=None,
            last_checked_at=1716000000000,
            prerelease_seen=prerelease_seen or [],
        )

    def current_version(self) -> str:
        return self._current

    async def poll(self, *, force: bool = False) -> UpdateStatus:
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
def audit_log(admin_state: AdminState, tmp_path: Path) -> SystemAuditLog:
    log = SystemAuditLog(tmp_path / "audit.log")
    admin_state.audit_log = log
    return log


def _wire_default(admin_state: AdminState) -> _StubUpgrader:
    admin_state.update_checker = _StubChecker(
        current="1.2.0", latest="1.2.1"
    )
    upgrader = _StubUpgrader(available=True)
    admin_state.upgrader = upgrader
    return upgrader


# ---------------------------------------------------------------------------
# Tests — POST /admin/system/upgrade
# ---------------------------------------------------------------------------


def test_typed_confirmation_mismatch_returns_400(
    client: TestClient, admin_state: AdminState
) -> None:
    _wire_default(admin_state)
    resp = client.post(
        "/admin/system/upgrade",
        json={"tag": "v1.2.1", "typed_confirmation": "v1.2.0"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "typed_confirmation_mismatch"


def test_no_upgrader_returns_503(
    client: TestClient, admin_state: AdminState
) -> None:
    admin_state.update_checker = _StubChecker()
    admin_state.upgrader = None
    resp = client.post(
        "/admin/system/upgrade",
        json={"tag": "v1.2.1", "typed_confirmation": "v1.2.1"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "upgrader_unavailable"


def test_upgrader_not_available_returns_503(
    client: TestClient, admin_state: AdminState
) -> None:
    admin_state.update_checker = _StubChecker()
    admin_state.upgrader = _StubUpgrader(available=False)
    resp = client.post(
        "/admin/system/upgrade",
        json={"tag": "v1.2.1", "typed_confirmation": "v1.2.1"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "upgrader_unavailable"


def test_tag_not_in_releases_returns_400(
    client: TestClient, admin_state: AdminState
) -> None:
    # Checker only knows about v1.2.1; "v9.9.9z" is neither in the
    # observed releases nor a valid semver string.
    admin_state.update_checker = _StubChecker(latest="1.2.1")
    admin_state.upgrader = _StubUpgrader(available=True)
    resp = client.post(
        "/admin/system/upgrade",
        json={
            "tag": "not-a-tag-at-all",
            "typed_confirmation": "not-a-tag-at-all",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "tag_not_whitelisted"


def test_downgrade_blocked_by_default(
    client: TestClient, admin_state: AdminState
) -> None:
    # Current is 1.2.0, target is 1.1.0 (older). Should refuse.
    admin_state.update_checker = _StubChecker(
        current="1.2.0", latest="1.2.0", prerelease_seen=["v1.1.0"]
    )
    admin_state.upgrader = _StubUpgrader(available=True)
    resp = client.post(
        "/admin/system/upgrade",
        json={"tag": "v1.1.0", "typed_confirmation": "v1.1.0"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "downgrade_blocked"
    assert body["current"] == "1.2.0"
    assert body["target"] == "v1.1.0"


def test_allow_downgrade_overrides_block(
    client: TestClient, admin_state: AdminState, audit_log: SystemAuditLog
) -> None:
    admin_state.update_checker = _StubChecker(
        current="1.2.0", latest="1.2.0", prerelease_seen=["v1.1.0"]
    )
    upgrader = _StubUpgrader(available=True)
    admin_state.upgrader = upgrader
    resp = client.post(
        "/admin/system/upgrade",
        json={
            "tag": "v1.1.0",
            "typed_confirmation": "v1.1.0",
            "allow_downgrade": True,
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["state"] == "queued"
    assert body["tag"] == "v1.1.0"
    assert upgrader.started == [("v1.1.0", "admin")]


def test_in_flight_returns_409_with_inflight_id(
    client: TestClient, admin_state: AdminState
) -> None:
    admin_state.update_checker = _StubChecker(
        current="1.2.0", latest="1.2.1"
    )
    admin_state.upgrader = _StubUpgrader(
        available=True, already_running="req-existing"
    )
    resp = client.post(
        "/admin/system/upgrade",
        json={"tag": "v1.2.1", "typed_confirmation": "v1.2.1"},
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "upgrade_already_running"
    assert body["request_id"] == "req-existing"


@pytest.mark.asyncio
async def test_happy_path_records_audit_and_returns_202(
    client: TestClient, admin_state: AdminState, audit_log: SystemAuditLog
) -> None:
    upgrader = _wire_default(admin_state)
    resp = client.post(
        "/admin/system/upgrade",
        json={"tag": "v1.2.1", "typed_confirmation": "v1.2.1"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["request_id"] == "req-1"
    assert body["state"] == "queued"
    assert body["mode"] == "docker"
    assert body["tag"] == "v1.2.1"
    # The upgrader recorded the start
    assert upgrader.started == [("v1.2.1", "admin")]
    # And the audit log got a "requested" row
    entries = await audit_log.tail(limit=10)
    assert len(entries) == 1
    assert entries[0].event == "system.upgrade.requested"
    assert entries[0].request_id == "req-1"
    assert entries[0].tag == "v1.2.1"
    assert entries[0].actor == "admin"
    assert entries[0].details["mode"] == "docker"
    assert entries[0].details["allow_downgrade"] is False


# ---------------------------------------------------------------------------
# Tests — GET /admin/system/upgrade/{id}/status + /audit
# ---------------------------------------------------------------------------


def test_status_returns_known_request(
    client: TestClient, admin_state: AdminState
) -> None:
    _wire_default(admin_state)
    client.post(
        "/admin/system/upgrade",
        json={"tag": "v1.2.1", "typed_confirmation": "v1.2.1"},
    )
    resp = client.get("/admin/system/upgrade/req-1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"] == "req-1"
    assert body["tag"] == "v1.2.1"
    assert body["state"] == "queued"


def test_status_404_on_unknown_request(
    client: TestClient, admin_state: AdminState
) -> None:
    _wire_default(admin_state)
    resp = client.get("/admin/system/upgrade/nope/status")
    assert resp.status_code == 404
    assert resp.json()["error"] == "upgrade_request_not_found"


@pytest.mark.asyncio
async def test_audit_endpoint_returns_recorded_entries(
    client: TestClient, admin_state: AdminState, audit_log: SystemAuditLog
) -> None:
    _wire_default(admin_state)
    client.post(
        "/admin/system/upgrade",
        json={"tag": "v1.2.1", "typed_confirmation": "v1.2.1"},
    )
    resp = client.get("/admin/system/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert entry["event"] == "system.upgrade.requested"
    assert entry["request_id"] == "req-1"
    assert entry["actor"] == "admin"


def test_audit_endpoint_returns_empty_when_log_unwired(
    client: TestClient, admin_state: AdminState
) -> None:
    # No audit log wired; route should return [] not 503.
    admin_state.audit_log = None
    resp = client.get("/admin/system/audit")
    assert resp.status_code == 200
    assert resp.json() == {"entries": [], "next_before_ts": None}


# ---------------------------------------------------------------------------
# Tests — SSE stream
# ---------------------------------------------------------------------------


def test_sse_stream_emits_status_frames_until_terminal(
    client: TestClient, admin_state: AdminState
) -> None:
    """The SSE generator should emit one frame per progress tick and
    close after a terminal state."""
    _wire_default(admin_state)
    client.post(
        "/admin/system/upgrade",
        json={"tag": "v1.2.1", "typed_confirmation": "v1.2.1"},
    )
    with client.stream("GET", "/admin/system/upgrade/req-1/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
            # Once we see the terminal frame, stop reading.
            if b'"state":"succeeded"' in body:
                break
    # We should see at least two ``event: status`` frames.
    text = body.decode("utf-8")
    assert text.count("event: status") >= 2
    assert "running" in text
    assert "succeeded" in text


def test_sse_stream_503_when_upgrader_unwired(
    client: TestClient, admin_state: AdminState
) -> None:
    admin_state.upgrader = None
    resp = client.get("/admin/system/upgrade/req-1/events")
    # When upgrader is unavailable we surface a JSON 503, not an SSE
    # stream.
    assert resp.status_code == 503
    assert resp.json()["error"] == "upgrader_unavailable"
