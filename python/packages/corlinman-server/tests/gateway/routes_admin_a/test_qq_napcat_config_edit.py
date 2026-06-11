from __future__ import annotations

import base64
from typing import Any

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


def test_put_qq_config_accepts_napcat_url_and_webui_token(tmp_path) -> None:
    writes: list[dict[str, Any]] = []

    async def writer(cfg: dict[str, Any]) -> None:
        writes.append(cfg)

    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config={
            "qq": {
                "enabled": True,
                "ws_url": "ws://napcat:3001",
                "self_ids": [10001],
            }
        },
        channels_writer=writer,
    )
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(build_router())
        with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
            resp = c.put(
                "/admin/channels/qq/config",
                json={
                    "secrets": {"napcat_access_token": "webui-token"},
                    "urls": {"napcat_url": "http://user-napcat:6099"},
                },
            )
    finally:
        set_admin_state(None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["wrote"] == ["napcat_access_token", "napcat_url"]
    assert body["config_keys"]["napcat_url"] == "http://user-napcat:6099"
    assert "napcat_access_token" not in body["config_keys"]
    assert writes[-1]["qq"]["napcat_access_token"] == "webui-token"


def test_qq_status_returns_non_secret_napcat_config_keys(tmp_path) -> None:
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config={
            "qq": {
                "enabled": True,
                "ws_url": "ws://napcat:3001",
                "napcat_url": "http://user-napcat:6099",
                "napcat_access_token": "webui-token",
                "self_ids": [10001, 10002],
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
    keys = resp.json()["config_keys"]
    assert keys["ws_url"] == "ws://napcat:3001"
    assert keys["napcat_url"] == "http://user-napcat:6099"
    assert keys["self_ids"] == ["10001", "10002"]
    assert "napcat_access_token" not in keys
