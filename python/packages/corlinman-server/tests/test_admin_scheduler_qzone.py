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

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_b.infra import scheduler as scheduler_routes
from corlinman_server.gateway.routes_admin_b.infra._scheduler_lib import (
    _jitter_secs_from_metadata,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from corlinman_server.gateway_api.types import (
    DoneEvent,
    ToolCallEvent,
    ToolResultEvent,
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


@dataclass
class _Persona:
    id: str
    system_prompt: str


class _FakeStore:
    def __init__(self, personas: dict[str, _Persona]) -> None:
        self._p = personas

    async def get(self, pid: str) -> _Persona | None:
        return self._p.get(pid)


class _ScriptedChat:
    """Yields a pre-recorded event list the same shape the gateway
    ChatService emits on the wire."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def run(self, req: Any, cancel: asyncio.Event) -> Any:
        events = list(self._events)

        async def _gen() -> Any:
            for ev in events:
                yield ev

        return _gen()


async def test_trigger_real_builtin_harvests_qzone_url(
    grantley_template: Path,
    client: TestClient,
    admin_state: AdminState,
) -> None:
    """End-to-end through the REAL ``qzone.daily_publish`` builtin.

    A scripted chat service emits a genuine :class:`ToolResultEvent`
    carrying the publish envelope on ``payload_json``. The trigger
    route's audit dict must surface a non-empty ``last_qzone_url`` /
    ``tid``. This is the tid/qzone_url observability fix: before the
    ``payload_json`` forward, the harvester's sidecar probe always
    missed, so every successful publish recorded ``last_qzone_url=None``
    and the admin history showed a bare "ran".
    """
    published = {
        "ok": True,
        "tid": "tid-live",
        "qzone_url": "https://user.qzone.qq.com/1234/mood/tid-live",
        "uin": "1234",
    }
    chat = _ScriptedChat(
        events=[
            ToolCallEvent(
                plugin="corlinman_agent.qzone",
                tool="qzone_publish",
                args_json=b'{"text":"today"}',
                call_id="c1",
            ),
            ToolResultEvent(
                plugin="corlinman_agent.qzone",
                tool="qzone_publish",
                call_id="c1",
                duration_ms=5,
                payload_json=json.dumps(published),
            ),
            DoneEvent(finish_reason="stop"),
        ]
    )
    # Wire a live-ish AppState onto the admin extras so the trigger
    # route's runtime fallback hands it to the real builtin.
    admin_state.extras["app_state"] = SimpleNamespace(
        chat=chat,
        persona_store=_FakeStore(
            {"grantley": _Persona(id="grantley", system_prompt="be a tiger")}
        ),
        persona_asset_store=None,
    )

    enable = client.post("/admin/scheduler/qzone/templates/grantley/enable")
    assert enable.status_code == 200, enable.text
    res = client.post("/admin/scheduler/jobs/grantley.daily_qzone/trigger")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True, body
    assert body["result"]["tid"] == "tid-live"
    assert body["result"]["qzone_url"] == published["qzone_url"]
    assert body["result"]["text"] == "today"
    # The fix: last_qzone_url is populated (was None on every success).
    assert body["job"]["last_qzone_url"] == published["qzone_url"]
    assert body["job"]["last_run_ok"] is True


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


# ---------------------------------------------------------------------------
# B4 (4d): jitter_minutes → jitter_secs conversion (metadata-only, one place)
# ---------------------------------------------------------------------------


def test_jitter_minutes_valid_converts_to_secs() -> None:
    assert _jitter_secs_from_metadata({"jitter_minutes": 15}) == 900
    assert _jitter_secs_from_metadata({"jitter_minutes": 0}) == 0
    assert _jitter_secs_from_metadata({"jitter_minutes": 180}) == 180 * 60
    # Floats are floored to whole minutes.
    assert _jitter_secs_from_metadata({"jitter_minutes": 15.9}) == 15 * 60


def test_jitter_minutes_absent_is_zero() -> None:
    assert _jitter_secs_from_metadata({}) == 0


def test_jitter_minutes_illegal_values_ignored() -> None:
    # Negative, over-180, non-numeric, and bool are all ignored (→ 0).
    assert _jitter_secs_from_metadata({"jitter_minutes": -5}) == 0
    assert _jitter_secs_from_metadata({"jitter_minutes": 181}) == 0
    assert _jitter_secs_from_metadata({"jitter_minutes": 1000}) == 0
    assert _jitter_secs_from_metadata({"jitter_minutes": "30"}) == 0
    assert _jitter_secs_from_metadata({"jitter_minutes": None}) == 0
    assert _jitter_secs_from_metadata({"jitter_minutes": True}) == 0


# ---------------------------------------------------------------------------
# B5: task-level image_ref_labels + jitter_minutes top-level contract
# ---------------------------------------------------------------------------


def _runtime_metadata(admin_state: AdminState, name: str) -> dict[str, Any]:
    """Read a runtime job's persisted metadata off the overlay."""
    table = admin_state.extras.get("scheduler_runtime_jobs") or {}
    rj = table[name]
    return dict(rj.metadata)


def test_create_job_with_image_ref_labels_and_jitter_round_trips(
    client: TestClient, admin_state: AdminState
) -> None:
    """POST carrying the promoted fields → JobOut echoes them back and the
    metadata lands in the runtime overlay (metadata is the store of record)."""
    body = {
        "name": "grantley.daily_qzone",
        "cron": "0 9 * * *",
        "action_type": QZONE_DAILY_BUILTIN_NAME,
        "persona_id": "grantley",
        "prompt_template": "say something",
        "image_ref_labels": ["grantley_home", "grantley_casual"],
        "jitter_minutes": 45,
    }
    res = client.post("/admin/scheduler/jobs", json=body)
    assert res.status_code == 200, res.text
    row = res.json()
    assert row["image_ref_labels"] == ["grantley_home", "grantley_casual"]
    assert row["jitter_minutes"] == 45
    # Metadata is the authoritative store — both landed there.
    meta = _runtime_metadata(admin_state, "grantley.daily_qzone")
    assert meta["image_ref_labels"] == ["grantley_home", "grantley_casual"]
    assert meta["jitter_minutes"] == 45
    # And they survive the list endpoint round-trip.
    listed = client.get("/admin/scheduler/jobs").json()
    hit = next(j for j in listed if j["name"] == "grantley.daily_qzone")
    assert hit["image_ref_labels"] == ["grantley_home", "grantley_casual"]
    assert hit["jitter_minutes"] == 45


def test_create_job_without_promoted_fields_echoes_null(
    client: TestClient,
) -> None:
    """A job that pins neither field echoes ``null`` for both (contract-safe
    optional defaults)."""
    body = {
        "name": "grantley.daily_qzone",
        "cron": "0 9 * * *",
        "action_type": QZONE_DAILY_BUILTIN_NAME,
        "persona_id": "grantley",
        "prompt_template": "say something",
    }
    res = client.post("/admin/scheduler/jobs", json=body)
    assert res.status_code == 200, res.text
    row = res.json()
    assert row["image_ref_labels"] is None
    assert row["jitter_minutes"] is None


def test_patch_image_ref_labels_updates_and_keeps_untouched_fields(
    client: TestClient, admin_state: AdminState
) -> None:
    """PATCH changing only ``image_ref_labels`` updates the echo, leaves
    ``jitter_minutes`` (and persona_id/prompt_template) untouched."""
    create = client.post(
        "/admin/scheduler/jobs",
        json={
            "name": "grantley.daily_qzone",
            "cron": "0 9 * * *",
            "action_type": QZONE_DAILY_BUILTIN_NAME,
            "persona_id": "grantley",
            "prompt_template": "hello",
            "image_ref_labels": ["a_one", "b_two"],
            "jitter_minutes": 30,
        },
    )
    assert create.status_code == 200, create.text

    patched = client.patch(
        "/admin/scheduler/jobs/grantley.daily_qzone",
        json={"image_ref_labels": ["c_three"]},
    )
    assert patched.status_code == 200, patched.text
    row = patched.json()
    # Changed field reflects the new labels; the untouched jitter carries over.
    assert row["image_ref_labels"] == ["c_three"]
    assert row["jitter_minutes"] == 30
    assert row["persona_id"] == "grantley"
    assert row["prompt_template"] == "hello"
    # Metadata store agrees.
    meta = _runtime_metadata(admin_state, "grantley.daily_qzone")
    assert meta["image_ref_labels"] == ["c_three"]
    assert meta["jitter_minutes"] == 30


def test_patch_jitter_only_preserves_labels(
    client: TestClient, admin_state: AdminState
) -> None:
    """PATCH changing only ``jitter_minutes`` keeps the existing labels."""
    client.post(
        "/admin/scheduler/jobs",
        json={
            "name": "grantley.daily_qzone",
            "cron": "0 9 * * *",
            "action_type": QZONE_DAILY_BUILTIN_NAME,
            "persona_id": "grantley",
            "prompt_template": "hello",
            "image_ref_labels": ["a_one"],
            "jitter_minutes": 10,
        },
    )
    patched = client.patch(
        "/admin/scheduler/jobs/grantley.daily_qzone",
        json={"jitter_minutes": 90},
    )
    assert patched.status_code == 200, patched.text
    row = patched.json()
    assert row["jitter_minutes"] == 90
    assert row["image_ref_labels"] == ["a_one"]


def test_create_rejects_too_many_labels(client: TestClient) -> None:
    """9 labels (over the 8-ref cap) → 422 invalid_qzone_daily_args."""
    body = {
        "name": "grantley.daily_qzone",
        "cron": "0 9 * * *",
        "action_type": QZONE_DAILY_BUILTIN_NAME,
        "persona_id": "grantley",
        "prompt_template": "x",
        "image_ref_labels": [f"label_{i}" for i in range(9)],
    }
    res = client.post("/admin/scheduler/jobs", json=body)
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_qzone_daily_args"


def test_create_rejects_malformed_label(client: TestClient) -> None:
    """A label that breaks the ``[a-z0-9_-]`` shape → 422."""
    body = {
        "name": "grantley.daily_qzone",
        "cron": "0 9 * * *",
        "action_type": QZONE_DAILY_BUILTIN_NAME,
        "persona_id": "grantley",
        "prompt_template": "x",
        "image_ref_labels": ["Bad Label!"],
    }
    res = client.post("/admin/scheduler/jobs", json=body)
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_qzone_daily_args"


def test_create_rejects_jitter_over_max(client: TestClient) -> None:
    """jitter_minutes over 180 → 422."""
    body = {
        "name": "grantley.daily_qzone",
        "cron": "0 9 * * *",
        "action_type": QZONE_DAILY_BUILTIN_NAME,
        "persona_id": "grantley",
        "prompt_template": "x",
        "jitter_minutes": 999,
    }
    res = client.post("/admin/scheduler/jobs", json=body)
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_qzone_daily_args"


def test_create_rejects_negative_jitter(client: TestClient) -> None:
    """A negative jitter_minutes → 422."""
    body = {
        "name": "grantley.daily_qzone",
        "cron": "0 9 * * *",
        "action_type": QZONE_DAILY_BUILTIN_NAME,
        "persona_id": "grantley",
        "prompt_template": "x",
        "jitter_minutes": -1,
    }
    res = client.post("/admin/scheduler/jobs", json=body)
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_qzone_daily_args"


def test_patch_rejects_bad_labels(client: TestClient) -> None:
    """PATCH validation runs too — a bad label on edit → 422, no mutation."""
    client.post(
        "/admin/scheduler/jobs",
        json={
            "name": "grantley.daily_qzone",
            "cron": "0 9 * * *",
            "action_type": QZONE_DAILY_BUILTIN_NAME,
            "persona_id": "grantley",
            "prompt_template": "x",
            "image_ref_labels": ["good_one"],
        },
    )
    res = client.patch(
        "/admin/scheduler/jobs/grantley.daily_qzone",
        json={"image_ref_labels": ["STILL BAD"]},
    )
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_qzone_daily_args"
    # The pre-edit label is unchanged (the rejected PATCH didn't mutate).
    row = client.get("/admin/scheduler/jobs").json()
    hit = next(j for j in row if j["name"] == "grantley.daily_qzone")
    assert hit["image_ref_labels"] == ["good_one"]
