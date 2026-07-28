"""Group-monitor digest admin routes (GET/PUT/trigger/status)."""

from __future__ import annotations

import base64
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_a import (
    AdminState,
    build_router,
    set_admin_state,
)
from corlinman_server.gateway.routes_admin_a import qq_instances as qq_instance_routes
from corlinman_server.gateway.routes_admin_a._session_store import AdminSessionStore
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _auth() -> str:
    encoded = base64.b64encode(b"admin:rootroot").decode("ascii")
    return f"Basic {encoded}"


class _Writer:
    def __init__(self, state: AdminState) -> None:
        self.state = state
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, channels: dict[str, Any]) -> None:
        candidate = deepcopy(channels)
        self.calls.append(candidate)
        self.state.channels_config = candidate

    async def mutate(self, mutator: Any) -> Any:
        candidate, result = mutator(deepcopy(self.state.channels_config or {}), {})
        await self(candidate)
        return result


_MONITOR = {
    "id": "daily-brief",
    "enabled": True,
    "sources": [
        {
            "group": "100200",
            "watch_user_ids": ["11111"],
            "focus_user_ids": ["33333"],
        }
    ],
    "schedule_type": "daily",
    "daily_time": "09:00",
    "interval_minutes": None,
    "timezone": "Asia/Shanghai",
    "window_minutes": 0,
    "target_type": "user",
    "target_id": "22222",
    "style_extra": "",
    "send_when_empty": False,
}


@pytest.fixture()
def setup(tmp_path: Path):
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config={
            "qq": {
                "default_instance": "bot-a",
                "instances": {
                    "bot-a": {
                        "display_name": "Bot A",
                        "enabled": True,
                        "connection_mode": "external",
                        "ws_url": "ws://bot-a:3001",
                    },
                },
            }
        },
    )
    state.channels_writer = _Writer(state)
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app, headers={"Authorization": _auth()}) as client:
        yield state, client
    set_admin_state(None)


def test_get_monitors_empty(setup) -> None:
    _state, client = setup
    response = client.get("/admin/channels/qq/instances/bot-a/monitors")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["monitors"] == []
    assert body["warnings"] == []
    assert body["revision"]


def test_put_and_get_roundtrip_persists_rows(setup) -> None:
    state, client = setup
    revision = client.get("/admin/channels/qq/instances/bot-a/monitors").json()[
        "revision"
    ]
    put = client.put(
        "/admin/channels/qq/instances/bot-a/monitors",
        headers={"If-Match": f'"{revision}"'},
        json={"monitors": [_MONITOR]},
    )
    assert put.status_code == 200, put.text
    echoed = put.json()["monitors"]
    assert echoed[0]["id"] == "daily-brief"
    assert echoed[0]["sources"][0]["watch_user_ids"] == ["11111"]
    assert echoed[0]["sources"][0]["focus_user_ids"] == ["33333"]
    # Persisted into the instance values (the runtime reads exactly this).
    rows = state.channels_config["qq"]["instances"]["bot-a"]["monitors"]
    assert rows[0]["sources"][0]["group"] == "100200"
    assert rows[0]["schedule_type"] == "daily"
    # GET echoes what PUT stored.
    got = client.get("/admin/channels/qq/instances/bot-a/monitors").json()
    assert [m["id"] for m in got["monitors"]] == ["daily-brief"]


def test_put_persists_toml_serialisable_rows(setup) -> None:
    """Regression: TOML has no null. A daily task carries
    interval_minutes=None (and an interval task daily_time=None) on the
    wire — persisting those Nones made tomli_w fail the whole config
    write, so EVERY monitor save 500'd `qq_config_write_failed`."""
    import tomli_w

    state, client = setup
    interval_task = {
        **_MONITOR,
        "id": "interval-task",
        "schedule_type": "interval",
        "daily_time": None,
        "interval_minutes": 30,
    }
    put = client.put(
        "/admin/channels/qq/instances/bot-a/monitors",
        json={"monitors": [_MONITOR, interval_task]},
    )
    assert put.status_code == 200, put.text
    rows = state.channels_config["qq"]["instances"]["bot-a"]["monitors"]

    def _assert_no_none(value: Any) -> None:
        if isinstance(value, dict):
            for v in value.values():
                _assert_no_none(v)
        elif isinstance(value, list):
            for v in value:
                _assert_no_none(v)
        else:
            assert value is not None

    _assert_no_none(rows)
    # The true contract: the whole channels tree must serialise to TOML.
    tomli_w.dumps({"channels": state.channels_config})
    # Absent keys still validate on read (defaults are None).
    got = client.get("/admin/channels/qq/instances/bot-a/monitors").json()
    assert got["warnings"] == []
    assert {m["id"] for m in got["monitors"]} == {"daily-brief", "interval-task"}


def test_humanlike_disable_persists_toml_serialisable(setup) -> None:
    """Same TOML-null class of bug on the adjacent surface: disabling
    humanlike without a persona must not write persona_id=None."""
    import tomli_w

    state, client = setup
    response = client.put(
        "/admin/channels/qq/instances/bot-a/humanlike",
        json={"enabled": False, "persona_id": None},
    )
    assert response.status_code == 200, response.text
    assert response.json()["persona_id"] is None
    block = state.channels_config["qq"]["instances"]["bot-a"]["humanlike"]
    assert block == {"enabled": False}
    tomli_w.dumps({"channels": state.channels_config})


def test_put_empty_list_removes_key(setup) -> None:
    state, client = setup
    revision = client.get("/admin/channels/qq/instances/bot-a/monitors").json()[
        "revision"
    ]
    put = client.put(
        "/admin/channels/qq/instances/bot-a/monitors",
        headers={"If-Match": f'"{revision}"'},
        json={"monitors": [_MONITOR]},
    )
    assert put.status_code == 200
    put2 = client.put(
        "/admin/channels/qq/instances/bot-a/monitors",
        headers={"If-Match": f'"{put.json()["revision"]}"'},
        json={"monitors": []},
    )
    assert put2.status_code == 200, put2.text
    assert "monitors" not in state.channels_config["qq"]["instances"]["bot-a"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"id": "BAD ID"},
        {"sources": [{"group": "not-a-number"}]},
        {"sources": []},
        {"sources": [{"group": "1"}, {"group": "1"}]},
        {"schedule_type": "daily", "daily_time": "25:00"},
        {"schedule_type": "interval", "daily_time": None, "interval_minutes": 3},
        {"target_id": "abc"},
        {"sources": [{"group": "1", "watch_user_ids": ["not-digits"]}]},
        {"sources": [{"group": "1", "focus_user_ids": ["not-digits"]}]},
        {"timezone": "Mars/Olympus"},
    ],
)
def test_put_rejects_invalid_specs(setup, mutation: dict[str, Any]) -> None:
    _state, client = setup
    body = {**_MONITOR, **mutation}
    response = client.put(
        "/admin/channels/qq/instances/bot-a/monitors",
        json={"monitors": [body]},
    )
    assert response.status_code == 422, response.text


def test_put_rejects_duplicate_ids(setup) -> None:
    _state, client = setup
    response = client.put(
        "/admin/channels/qq/instances/bot-a/monitors",
        json={"monitors": [_MONITOR, _MONITOR]},
    )
    assert response.status_code == 422


def test_put_stale_revision_conflicts(setup) -> None:
    _state, client = setup
    response = client.put(
        "/admin/channels/qq/instances/bot-a/monitors",
        headers={"If-Match": '"deadbeef"'},
        json={"monitors": [_MONITOR]},
    )
    assert response.status_code == 409, response.text


def test_get_lifts_legacy_flat_rows(setup) -> None:
    """#190 stored flat single-group rows; GET must lift them into the
    sources shape instead of reporting them as junk."""
    state, client = setup
    state.channels_config["qq"]["instances"]["bot-a"]["monitors"] = [
        {
            "id": "old-shape",
            "source_group": "100200",
            "watch_user_ids": ["11111"],
            "schedule_type": "interval",
            "interval_minutes": 30,
            "target_type": "user",
            "target_id": "22222",
        }
    ]
    body = client.get("/admin/channels/qq/instances/bot-a/monitors").json()
    assert body["warnings"] == []
    monitor = body["monitors"][0]
    assert monitor["id"] == "old-shape"
    assert monitor["sources"] == [
        {"group": "100200", "watch_user_ids": ["11111"], "focus_user_ids": []}
    ]


def test_get_reports_hand_edited_junk_as_warnings(setup) -> None:
    state, client = setup
    state.channels_config["qq"]["instances"]["bot-a"]["monitors"] = [
        dict(_MONITOR),
        {"id": "half-a-row"},
    ]
    body = client.get("/admin/channels/qq/instances/bot-a/monitors").json()
    assert [m["id"] for m in body["monitors"]] == ["daily-brief"]
    assert len(body["warnings"]) == 1
    assert "monitors[1]" in body["warnings"][0]


def test_unknown_instance_is_404(setup) -> None:
    _state, client = setup
    assert (
        client.get("/admin/channels/qq/instances/nope/monitors").status_code == 404
    )


def test_trigger_unknown_monitor_is_404(setup) -> None:
    state, client = setup
    state.channels_config["qq"]["instances"]["bot-a"]["monitors"] = [dict(_MONITOR)]
    response = client.post(
        "/admin/channels/qq/instances/bot-a/monitors/ghost/trigger"
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "monitor_not_found"


def test_trigger_disabled_monitor_is_404(setup) -> None:
    state, client = setup
    state.channels_config["qq"]["instances"]["bot-a"]["monitors"] = [
        {**_MONITOR, "enabled": False}
    ]
    response = client.post(
        "/admin/channels/qq/instances/bot-a/monitors/daily-brief/trigger"
    )
    assert response.status_code == 404


def test_trigger_without_live_loop_is_409(setup, monkeypatch) -> None:
    state, client = setup
    state.channels_config["qq"]["instances"]["bot-a"]["monitors"] = [dict(_MONITOR)]
    import corlinman_channels

    monkeypatch.setattr(
        corlinman_channels, "qq_monitor_trigger", lambda *_a: False
    )
    response = client.post(
        "/admin/channels/qq/instances/bot-a/monitors/daily-brief/trigger"
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "monitor_loop_not_running"


def test_trigger_with_live_loop_is_202(setup, monkeypatch) -> None:
    state, client = setup
    state.channels_config["qq"]["instances"]["bot-a"]["monitors"] = [dict(_MONITOR)]
    calls: list[tuple[str, str]] = []
    import corlinman_channels

    monkeypatch.setattr(
        corlinman_channels,
        "qq_monitor_trigger",
        lambda instance_id, monitor_id: calls.append((instance_id, monitor_id))
        or True,
    )
    response = client.post(
        "/admin/channels/qq/instances/bot-a/monitors/daily-brief/trigger"
    )
    assert response.status_code == 202, response.text
    assert calls == [("bot-a", "daily-brief")]


def test_status_merges_snapshot_and_counts(setup, monkeypatch) -> None:
    state, client = setup
    state.channels_config["qq"]["instances"]["bot-a"]["monitors"] = [dict(_MONITOR)]
    import corlinman_channels

    monkeypatch.setattr(
        corlinman_channels,
        "qq_monitor_status_snapshot",
        lambda instance_id: {
            "daily-brief": {"last_ok": True, "last_count": 7, "last_run_ms": 123}
        },
    )

    async def _fake_counts(instance_id: str, monitors: list[Any]) -> dict[str, int]:
        assert instance_id == "bot-a"
        return {m.id: 9 for m in monitors}

    monkeypatch.setattr(
        qq_instance_routes, "monitor_window_counts", _fake_counts
    )
    body = client.get(
        "/admin/channels/qq/instances/bot-a/monitors/status"
    ).json()
    assert body["statuses"]["daily-brief"]["last_count"] == 7
    assert body["counts"]["daily-brief"] == 9
