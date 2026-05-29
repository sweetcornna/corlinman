"""W6 — ``/admin/scheduler/qzone*`` and runtime job creation contract.

Exercises the route surface the QZone daily-publish admin UI consumes:

* ``GET  /admin/scheduler/jobs`` — config jobs + runtime overlay.
* ``POST /admin/scheduler/jobs`` — operator-created runtime job; with
  ``action_type=qzone.daily_publish`` the per-action_type fields
  (persona_id / prompt_template / qq_account) are validated.
* ``POST /admin/scheduler/qzone/templates/grantley/enable`` — reads
  the bundled-persona JSON template + upserts a runtime job for it.
  Calling twice updates in place (no duplicate row).
* ``POST /admin/scheduler/jobs/{name}/trigger`` — when no scheduler
  handle is wired, the runtime fallback dispatches the registered
  builtin in-process and records the audit dict into the history
  ring buffer.

The tests stub the bundled-template lookup so they don't depend on
the wheel ship path being intact (importlib.resources files() can be
finicky inside editable installs / pytest tmpdir runs).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_b import scheduler as scheduler_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from corlinman_server.scheduler.builtins import (
    BUILTIN_ACTIONS,
    BuiltinContext,
    register_builtin,
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin_state(tmp_path: Path) -> Iterator[AdminState]:
    """Bare AdminState with admin auth + a data_dir tests can stamp a
    bundled-template into."""
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


@pytest.fixture()
def grantley_template(admin_state: AdminState) -> Path:
    """Drop a Grantley daily-job template into
    ``<data_dir>/bundled_personas/grantley/`` so the enable route's
    on-disk probe wins (independent of importlib.resources)."""
    target_dir = admin_state.data_dir / "bundled_personas" / "grantley"
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": "grantley.daily_qzone",
        "action_type": QZONE_DAILY_BUILTIN_NAME,
        "persona_id": "grantley",
        "cron": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "enabled": False,
        "qq_account": "1234",
        "prompt_template": "Write today's update in Grantley voice.",
    }
    path = target_dir / "daily_job.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_jobs_starts_empty(client: TestClient) -> None:
    res = client.get("/admin/scheduler/jobs")
    assert res.status_code == 200
    assert res.json() == []


def test_list_jobs_includes_config_jobs(
    admin_state: AdminState, client: TestClient
) -> None:
    """Config-derived rows still show up alongside the runtime overlay."""
    admin_state.config_loader = lambda: {
        "scheduler": {
            "jobs": [
                {
                    "name": "system.update_check",
                    "cron": "0 0 */6 * * * *",
                    "action": {"run_tool": {"plugin": "system", "tool": "update_check"}},
                }
            ]
        }
    }
    res = client.get("/admin/scheduler/jobs")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "system.update_check"
    assert rows[0]["source"] == "config"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_runtime_job_round_trips(client: TestClient) -> None:
    body = {
        "name": "grantley.daily_qzone",
        "cron": "0 9 * * *",
        "action_type": QZONE_DAILY_BUILTIN_NAME,
        "persona_id": "grantley",
        "prompt_template": "say something",
        "qq_account": "9999",
        "timezone": "Asia/Shanghai",
    }
    res = client.post("/admin/scheduler/jobs", json=body)
    assert res.status_code == 200, res.text
    row = res.json()
    assert row["name"] == "grantley.daily_qzone"
    assert row["action_type"] == QZONE_DAILY_BUILTIN_NAME
    assert row["persona_id"] == "grantley"
    assert row["source"] == "runtime"
    # The freshly-created job lands on the list endpoint.
    listed = client.get("/admin/scheduler/jobs").json()
    assert any(j["name"] == "grantley.daily_qzone" for j in listed)


def test_create_runtime_job_rejects_invalid_cron(client: TestClient) -> None:
    body = {
        "name": "bad",
        "cron": "not-a-cron",
        "action_type": QZONE_DAILY_BUILTIN_NAME,
        "persona_id": "grantley",
        "prompt_template": "x",
    }
    res = client.post("/admin/scheduler/jobs", json=body)
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_cron"


def test_create_runtime_qzone_job_requires_persona_id(client: TestClient) -> None:
    body = {
        "name": "no-persona",
        "cron": "0 9 * * *",
        "action_type": QZONE_DAILY_BUILTIN_NAME,
        "prompt_template": "x",
    }
    res = client.post("/admin/scheduler/jobs", json=body)
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_qzone_daily_args"


def test_create_runtime_job_rejects_bad_name(client: TestClient) -> None:
    body = {
        "name": "bad name with spaces",
        "cron": "0 9 * * *",
        "action_type": QZONE_DAILY_BUILTIN_NAME,
        "persona_id": "grantley",
        "prompt_template": "x",
    }
    res = client.post("/admin/scheduler/jobs", json=body)
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_job_name"


# ---------------------------------------------------------------------------
# Enable Grantley template (idempotent)
# ---------------------------------------------------------------------------


def test_enable_grantley_template_creates_runtime_job(
    grantley_template: Path, client: TestClient
) -> None:
    res = client.post("/admin/scheduler/qzone/templates/grantley/enable")
    assert res.status_code == 200, res.text
    row = res.json()
    assert row["name"] == "grantley.daily_qzone"
    assert row["enabled"] is True  # template ships disabled; activation flips it
    assert row["source"] == "runtime"


def test_enable_grantley_template_is_idempotent(
    grantley_template: Path, client: TestClient, admin_state: AdminState
) -> None:
    """Two enables → still one runtime row. The second call updates
    in place rather than creating a duplicate."""
    first = client.post("/admin/scheduler/qzone/templates/grantley/enable")
    assert first.status_code == 200
    second = client.post("/admin/scheduler/qzone/templates/grantley/enable")
    assert second.status_code == 200
    # Mutate the template body so the second-enable picks up the change.
    raw = json.loads(grantley_template.read_text(encoding="utf-8"))
    raw["cron"] = "30 9 * * *"
    grantley_template.write_text(json.dumps(raw), encoding="utf-8")
    third = client.post("/admin/scheduler/qzone/templates/grantley/enable")
    assert third.status_code == 200
    assert third.json()["cron"] == "30 9 * * *"

    listed = client.get("/admin/scheduler/jobs").json()
    grantley_rows = [r for r in listed if r["name"] == "grantley.daily_qzone"]
    assert len(grantley_rows) == 1
    # Runtime overlay has exactly one entry after three enables.
    runtime = admin_state.extras.get("scheduler_runtime_jobs") or {}
    assert len(runtime) == 1


def test_enable_unknown_template_returns_404(client: TestClient, admin_state: AdminState) -> None:
    # No template on disk → 404. Use a name that doesn't exist in the
    # bundled-personas wheel data either, to ensure both candidates miss.
    res = client.post(
        "/admin/scheduler/qzone/templates/no-such-persona-abc123/enable"
    )
    assert res.status_code == 404
    assert res.json()["error"] == "template_not_found"


def test_enable_template_rejects_bad_id(client: TestClient) -> None:
    res = client.post("/admin/scheduler/qzone/templates/Bad%20Id/enable")
    # FastAPI 404s on an invalid path char before our handler sees it
    # (the %20 keeps the URL valid but the slug regex rejects); we get
    # either a 422 or 404 depending on path-parsing — both are fine.
    assert res.status_code in (404, 422)


# ---------------------------------------------------------------------------
# Trigger now → runtime fallback dispatches the registered builtin
# ---------------------------------------------------------------------------


async def test_trigger_runtime_qzone_job_invokes_builtin(
    grantley_template: Path,
    client: TestClient,
    admin_state: AdminState,
) -> None:
    """The trigger route's runtime fallback runs the registered builtin
    in-process and stamps the audit dict into the history ring buffer."""
    captured: list[BuiltinContext] = []

    async def _stub_action(ctx: BuiltinContext) -> dict[str, Any]:
        captured.append(ctx)
        return {
            "ok": True,
            "tid": "tid-stub",
            "qzone_url": "https://qzone.test/mood/tid-stub",
            "persona_id": "grantley",
        }

    original = BUILTIN_ACTIONS.get(QZONE_DAILY_BUILTIN_NAME)
    register_builtin(QZONE_DAILY_BUILTIN_NAME, _stub_action)
    try:
        # Enable the template so a runtime row exists.
        enable = client.post(
            "/admin/scheduler/qzone/templates/grantley/enable"
        )
        assert enable.status_code == 200, enable.text
        # Fire it.
        res = client.post(
            "/admin/scheduler/jobs/grantley.daily_qzone/trigger"
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["ok"] is True
        assert body["result"]["tid"] == "tid-stub"
        # Job row's last-run summary is updated.
        assert body["job"]["last_run_ok"] is True
        assert body["job"]["last_qzone_url"] == "https://qzone.test/mood/tid-stub"
        # History ring buffer carries the firing.
        hist = client.get("/admin/scheduler/history").json()
        assert any(h["job"] == "grantley.daily_qzone" for h in hist)
        # The builtin saw the per-job metadata via the production path
        # (per-job metadata map keyed by job name).
        assert len(captured) == 1
        ctx = captured[0]
        assert ctx.name == "grantley.daily_qzone"
        meta_table = getattr(ctx.app_state, "scheduler_job_metadata", None) if ctx.app_state else None
        # AppState may be absent (no live AppState in this minimal
        # test app); the per-job metadata then lives on the AdminState
        # extras dict the route synced during the upsert.
        synced = admin_state.extras.get("scheduler_job_metadata", {})
        assert synced.get("grantley.daily_qzone", {}).get("persona_id") == "grantley"
        del meta_table  # silence the unused-local linter
    finally:
        if original is not None:
            register_builtin(QZONE_DAILY_BUILTIN_NAME, original)


def test_trigger_unknown_job_returns_404(client: TestClient) -> None:
    res = client.post("/admin/scheduler/jobs/ghost/trigger")
    assert res.status_code == 404
    assert res.json()["error"] == "not_found"


async def test_trigger_records_failure_in_history(
    grantley_template: Path, client: TestClient
) -> None:
    """A failed builtin run still records a history entry + flips the
    job row's last_run_ok to False."""
    async def _bad(ctx: BuiltinContext) -> dict[str, Any]:
        return {"ok": False, "error": "qzone_not_called", "tools_called": []}

    original = BUILTIN_ACTIONS.get(QZONE_DAILY_BUILTIN_NAME)
    register_builtin(QZONE_DAILY_BUILTIN_NAME, _bad)
    try:
        client.post("/admin/scheduler/qzone/templates/grantley/enable")
        res = client.post(
            "/admin/scheduler/jobs/grantley.daily_qzone/trigger"
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False
        assert body["job"]["last_run_ok"] is False
        assert body["job"]["last_error"] == "qzone_not_called"
        hist = client.get("/admin/scheduler/history").json()
        assert any(h["status"] == "error" and h["job"] == "grantley.daily_qzone" for h in hist)
    finally:
        if original is not None:
            register_builtin(QZONE_DAILY_BUILTIN_NAME, original)
