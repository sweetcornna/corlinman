"""``/admin/channels/qq/status`` — runtime badge derivation.

Regression for the badge that was permanently "unknown": the route never
set ``runtime`` even though the health watcher populated
``health_online``, so a fully-connected bot still rendered as not-live
in every UI surface driven by the field.
"""

from __future__ import annotations

import base64

import pytest
from corlinman_server.gateway.routes_admin_a import (
    AdminState,
    build_router,
    set_admin_state,
)
from corlinman_server.gateway.routes_admin_a._session_store import AdminSessionStore
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _basic_auth_header() -> str:
    token = base64.b64encode(b"admin:rootroot").decode("ascii")
    return f"Basic {token}"


def _status_with_health(
    tmp_path, monkeypatch: pytest.MonkeyPatch, health: dict, *, enabled: bool = True
) -> dict:
    import corlinman_channels.service as svc

    monkeypatch.setattr(svc, "QQ_HEALTH", health)
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config={
            "qq": {
                "enabled": enabled,
                "ws_url": "ws://napcat:3001",
                "self_ids": [10001],
            }
        },
    )
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(build_router())
        with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
            resp = c.get("/admin/channels/qq/status")
    finally:
        set_admin_state(None)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_qq_runtime_connected_when_enabled_and_health_online(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _status_with_health(tmp_path, monkeypatch, {"online": True})
    assert body["runtime"] == "connected"


def test_qq_runtime_disconnected_when_health_offline(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _status_with_health(tmp_path, monkeypatch, {"online": False})
    assert body["runtime"] == "disconnected"


def test_qq_runtime_unknown_when_no_health_snapshot(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _status_with_health(tmp_path, monkeypatch, {})
    assert body["runtime"] == "unknown"


def test_qq_runtime_account_id_wins_over_configured_fallback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _status_with_health(
        tmp_path,
        monkeypatch,
        {"online": True, "account_qq": 20002, "account_online": True},
    )
    assert body["account_qq"] == 20002
    assert body["self_ids"] == [20002]
    assert body["config_keys"]["self_ids"] == ["10001"]


def test_qq_runtime_self_ids_fall_back_before_detection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _status_with_health(tmp_path, monkeypatch, {"online": True})
    assert body["account_qq"] is None
    assert body["self_ids"] == [10001]


def test_qq_runtime_offline_account_does_not_surface_stale_detection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _status_with_health(
        tmp_path,
        monkeypatch,
        {"online": True, "account_qq": 20002, "account_online": False},
    )
    assert body["account_online"] is False
    assert body["self_ids"] == [10001]
