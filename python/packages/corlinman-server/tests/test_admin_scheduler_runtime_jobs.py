"""Scheduler runtime-job persistence + edit/pause/resume/delete contract.

Covers the gap-fill that turned the in-memory runtime overlay into a
durable, tick-loop-backed surface:

* runtime jobs persist to ``<data_dir>/scheduler_runtime_jobs.json`` and
  rehydrate into the overlay on the next process (fresh AdminState);
* ``enabled`` actually gates the live tick loop — create/resume register
  a loop on the attached :class:`SchedulerHandle`, pause/disable cancel
  it;
* ``PATCH /admin/scheduler/jobs/{name}`` partial-edits a runtime job;
* ``POST .../pause`` + ``POST .../resume`` flip ``enabled`` and
  reconcile the loop;
* ``DELETE .../{name}`` removes the job + cancels its loop;
* config-derived rows are not editable via these routes (404).

The tick-loop registration is exercised against a *fake* handle that
records register/unregister calls — the real :class:`SchedulerHandle`
register/unregister are covered by ``tests/scheduler/test_runtime_register.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_b.infra import scheduler as scheduler_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from corlinman_server.scheduler.builtins.qzone_daily import (
    QZONE_DAILY_BUILTIN_NAME,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .gateway.routes_admin_b._admin_auth import (
    authenticated_test_client,
    configure_admin_auth,
)

# ---------------------------------------------------------------------------
# Fake scheduler handle — records register/unregister so a test can assert
# that ``enabled`` actually drives loop registration.
# ---------------------------------------------------------------------------


class _FakeHandle:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def register(self, spec: Any) -> bool:
        self.registered.append(spec.name)
        return True

    def unregister(self, name: str) -> None:
        self.unregistered.append(name)


@pytest.fixture()
def admin_state(tmp_path: Path) -> Iterator[AdminState]:
    state = AdminState(data_dir=tmp_path)
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(admin_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(scheduler_routes.router())
    return authenticated_test_client(app)


def _make_qzone_body(name: str = "rt.daily", **over: Any) -> dict[str, Any]:
    body = {
        "name": name,
        "cron": "0 9 * * *",
        "action_type": QZONE_DAILY_BUILTIN_NAME,
        "persona_id": "grantley",
        "prompt_template": "say something",
        "qq_account": "9999",
        "timezone": "Asia/Shanghai",
    }
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# Persistence + rehydrate
# ---------------------------------------------------------------------------


def test_create_persists_to_sidecar(
    admin_state: AdminState, client: TestClient, tmp_path: Path
) -> None:
    res = client.post("/admin/scheduler/jobs", json=_make_qzone_body())
    assert res.status_code == 200, res.text
    sidecar = tmp_path / "scheduler_runtime_jobs.json"
    assert sidecar.is_file()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    rows = payload["jobs"]
    assert len(rows) == 1
    assert rows[0]["name"] == "rt.daily"
    assert rows[0]["persona_id"] == "grantley"
    assert rows[0]["enabled"] is True


def test_persisted_job_rehydrates_into_fresh_state(tmp_path: Path) -> None:
    """A second AdminState pointed at the same data_dir picks the job back
    up — this is the across-restart durability contract."""
    sidecar = tmp_path / "scheduler_runtime_jobs.json"
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "name": "rt.daily",
                        "cron": "0 9 * * *",
                        "action_type": QZONE_DAILY_BUILTIN_NAME,
                        "persona_id": "grantley",
                        "prompt_template": "x",
                        "enabled": True,
                        "metadata": {"persona_id": "grantley", "prompt_template": "x"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fresh = AdminState(data_dir=tmp_path)
    configure_admin_auth(fresh)
    set_admin_state(fresh)
    try:
        app = FastAPI()
        app.include_router(scheduler_routes.router())
        c = authenticated_test_client(app)
        rows = c.get("/admin/scheduler/jobs").json()
        assert any(r["name"] == "rt.daily" and r["source"] == "runtime" for r in rows)
        # Metadata table was repopulated so the qzone builtin can read it.
        meta = fresh.extras.get("scheduler_job_metadata", {})
        assert meta.get("rt.daily", {}).get("persona_id") == "grantley"
    finally:
        set_admin_state(None)


def test_malformed_sidecar_does_not_crash_listing(tmp_path: Path) -> None:
    (tmp_path / "scheduler_runtime_jobs.json").write_text(
        "{ not valid json", encoding="utf-8"
    )
    state = AdminState(data_dir=tmp_path)
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(scheduler_routes.router())
        c = authenticated_test_client(app)
        res = c.get("/admin/scheduler/jobs")
        assert res.status_code == 200
        assert res.json() == []
    finally:
        set_admin_state(None)


# ---------------------------------------------------------------------------
# enabled gating drives loop registration
# ---------------------------------------------------------------------------


def test_create_enabled_registers_loop(
    admin_state: AdminState, client: TestClient
) -> None:
    handle = _FakeHandle()
    admin_state.scheduler = handle
    res = client.post("/admin/scheduler/jobs", json=_make_qzone_body(enabled=True))
    assert res.status_code == 200, res.text
    assert handle.registered == ["rt.daily"]


def test_create_disabled_does_not_register_loop(
    admin_state: AdminState, client: TestClient
) -> None:
    handle = _FakeHandle()
    admin_state.scheduler = handle
    res = client.post("/admin/scheduler/jobs", json=_make_qzone_body(enabled=False))
    assert res.status_code == 200, res.text
    assert handle.registered == []
    # A disabled job still calls unregister (reconcile to the off state).
    assert handle.unregistered == ["rt.daily"]


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


def test_pause_then_resume_cycle(
    admin_state: AdminState, client: TestClient
) -> None:
    handle = _FakeHandle()
    admin_state.scheduler = handle
    client.post("/admin/scheduler/jobs", json=_make_qzone_body(enabled=True))
    handle.registered.clear()

    paused = client.post("/admin/scheduler/jobs/rt.daily/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["enabled"] is False
    assert handle.unregistered[-1] == "rt.daily"

    resumed = client.post("/admin/scheduler/jobs/rt.daily/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["enabled"] is True
    assert handle.registered[-1] == "rt.daily"


def test_pause_persists_disabled_state(
    admin_state: AdminState, client: TestClient, tmp_path: Path
) -> None:
    client.post("/admin/scheduler/jobs", json=_make_qzone_body(enabled=True))
    client.post("/admin/scheduler/jobs/rt.daily/pause")
    payload = json.loads(
        (tmp_path / "scheduler_runtime_jobs.json").read_text(encoding="utf-8")
    )
    assert payload["jobs"][0]["enabled"] is False


def test_resume_revalidates_qzone_args(
    admin_state: AdminState, client: TestClient
) -> None:
    """A runtime qzone job that lost its persona_id can't silently resume."""
    client.post("/admin/scheduler/jobs", json=_make_qzone_body(enabled=False))
    # Corrupt the stored job's args directly to simulate a bad edit.
    rj = admin_state.extras["scheduler_runtime_jobs"]["rt.daily"]
    rj.persona_id = None
    res = client.post("/admin/scheduler/jobs/rt.daily/resume")
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_qzone_daily_args"


def test_pause_unknown_job_404(client: TestClient) -> None:
    res = client.post("/admin/scheduler/jobs/ghost/pause")
    assert res.status_code == 404
    assert res.json()["error"] == "not_found"


# ---------------------------------------------------------------------------
# edit (PATCH)
# ---------------------------------------------------------------------------


def test_patch_updates_cron(admin_state: AdminState, client: TestClient) -> None:
    handle = _FakeHandle()
    admin_state.scheduler = handle
    client.post("/admin/scheduler/jobs", json=_make_qzone_body(enabled=True))
    res = client.patch(
        "/admin/scheduler/jobs/rt.daily", json={"cron": "30 8 * * *"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["cron"] == "30 8 * * *"
    # Re-registered so the new cron takes effect.
    assert handle.registered[-1] == "rt.daily"


def test_patch_rejects_invalid_cron(client: TestClient) -> None:
    client.post("/admin/scheduler/jobs", json=_make_qzone_body())
    res = client.patch(
        "/admin/scheduler/jobs/rt.daily", json={"cron": "not-a-cron"}
    )
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_cron"


def test_patch_unknown_job_404(client: TestClient) -> None:
    res = client.patch("/admin/scheduler/jobs/ghost", json={"cron": "0 9 * * *"})
    assert res.status_code == 404


def test_patch_disable_unregisters_and_persists(
    admin_state: AdminState, client: TestClient, tmp_path: Path
) -> None:
    handle = _FakeHandle()
    admin_state.scheduler = handle
    client.post("/admin/scheduler/jobs", json=_make_qzone_body(enabled=True))
    res = client.patch("/admin/scheduler/jobs/rt.daily", json={"enabled": False})
    assert res.status_code == 200
    assert res.json()["enabled"] is False
    assert handle.unregistered[-1] == "rt.daily"
    payload = json.loads(
        (tmp_path / "scheduler_runtime_jobs.json").read_text(encoding="utf-8")
    )
    assert payload["jobs"][0]["enabled"] is False


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_job_and_cancels_loop(
    admin_state: AdminState, client: TestClient, tmp_path: Path
) -> None:
    handle = _FakeHandle()
    admin_state.scheduler = handle
    client.post("/admin/scheduler/jobs", json=_make_qzone_body(enabled=True))
    res = client.delete("/admin/scheduler/jobs/rt.daily")
    assert res.status_code == 200
    assert res.json()["deleted"] == "rt.daily"
    assert handle.unregistered[-1] == "rt.daily"
    # Gone from the list + the sidecar.
    assert client.get("/admin/scheduler/jobs").json() == []
    payload = json.loads(
        (tmp_path / "scheduler_runtime_jobs.json").read_text(encoding="utf-8")
    )
    assert payload["jobs"] == []


def test_delete_unknown_job_404(client: TestClient) -> None:
    res = client.delete("/admin/scheduler/jobs/ghost")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# config jobs are not editable via the runtime routes
# ---------------------------------------------------------------------------


def test_patch_config_job_404(admin_state: AdminState, client: TestClient) -> None:
    admin_state.config_loader = lambda: {
        "scheduler": {
            "jobs": [
                {
                    "name": "system.update_check",
                    "cron": "0 0 */6 * * * *",
                    "action": {
                        "run_tool": {"plugin": "system", "tool": "update_check"}
                    },
                }
            ]
        }
    }
    res = client.patch(
        "/admin/scheduler/jobs/system.update_check", json={"cron": "0 9 * * *"}
    )
    assert res.status_code == 404
    assert res.json()["resource"] == "runtime_scheduler_job"
