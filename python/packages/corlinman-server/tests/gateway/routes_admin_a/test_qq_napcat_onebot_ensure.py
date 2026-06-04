from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

import pytest
from corlinman_channels.service import QQ_HEALTH
from corlinman_server.gateway.routes_admin_a import (
    AdminState,
    build_router,
    set_admin_state,
)
from corlinman_server.gateway.routes_admin_a import channels as channels_routes
from corlinman_server.gateway.routes_admin_a._session_store import AdminSessionStore
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from corlinman_server.gateway.routes_admin_b._napcat_lib import NapcatError
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _basic_auth_header() -> str:
    token = base64.b64encode(b"admin:rootroot").decode("ascii")
    return f"Basic {token}"


@pytest.fixture(autouse=True)
def _reset_qq_health() -> Iterator[None]:
    before = dict(QQ_HEALTH)
    QQ_HEALTH.update(
        {
            "online": False,
            "last_event_at_ms": None,
            "seconds_since_event": None,
            "checked_at_ms": None,
            "account_online": None,
            "account_qq": None,
            "account_nickname": None,
            "account_checked_at_ms": None,
            "account_last_error": None,
        }
    )
    yield
    QQ_HEALTH.clear()
    QQ_HEALTH.update(before)


def test_qq_status_schedules_napcat_onebot_ensure_when_ws_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_schedule(cfg: dict[str, Any]) -> bool:
        calls.append(cfg)
        return True

    monkeypatch.setattr(
        channels_routes,
        "_schedule_onebot_websocket_server_ensure",
        fake_schedule,
    )
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config={
            "qq": {
                "enabled": True,
                "ws_url": "ws://napcat:3001",
                "napcat_url": "http://napcat:6099",
                "napcat_access_token": "napcat-webui-token",
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
    assert calls == [
        {
            "channels": {
                "qq": {
                    "enabled": True,
                    "ws_url": "ws://napcat:3001",
                    "napcat_url": "http://napcat:6099",
                    "napcat_access_token": "napcat-webui-token",
                }
            }
        }
    ]


def test_qq_reconnect_reports_napcat_not_logged_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    async def fake_ensure(_cfg: dict[str, Any]) -> bool:
        raise NapcatError("napcat_app_error", "Not Login")

    monkeypatch.setattr(
        channels_routes,
        "_ensure_onebot_websocket_server_for_config",
        fake_ensure,
    )
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config={"qq": {"enabled": True}},
    )
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(build_router())
        with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
            resp = c.post("/admin/channels/qq/reconnect")
    finally:
        set_admin_state(None)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "napcat_not_logged_in"


def test_qq_status_does_not_schedule_napcat_onebot_ensure_when_ws_online(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    QQ_HEALTH["online"] = True
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        channels_routes,
        "_schedule_onebot_websocket_server_ensure",
        lambda cfg: calls.append(cfg) or True,
    )
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config={
            "qq": {
                "enabled": True,
                "ws_url": "ws://napcat:3001",
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
    assert calls == []


def test_qq_reconnect_ensures_napcat_onebot_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_ensure(cfg: dict[str, Any]) -> bool:
        calls.append(cfg)
        return True

    monkeypatch.setattr(
        channels_routes,
        "_ensure_onebot_websocket_server_for_config",
        fake_ensure,
    )
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config={
            "qq": {
                "enabled": True,
                "ws_url": "ws://napcat:3001",
                "napcat_url": "http://napcat:6099",
                "napcat_access_token": "napcat-webui-token",
            }
        },
    )
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(build_router())
        with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
            resp = c.post("/admin/channels/qq/reconnect")
    finally:
        set_admin_state(None)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "changed": True}
    assert calls == [
        {
            "channels": {
                "qq": {
                    "enabled": True,
                    "ws_url": "ws://napcat:3001",
                    "napcat_url": "http://napcat:6099",
                    "napcat_access_token": "napcat-webui-token",
                }
            }
        }
    ]
