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


def test_put_qq_config_accepts_group_behavior_fields(tmp_path) -> None:
    """The W4 editor writes whitelist/policy/numbers — backend must accept
    them typed (numbers persist as TOML numbers, integral → int)."""
    writes: list[dict[str, Any]] = []

    async def writer(cfg: dict[str, Any]) -> None:
        writes.append(cfg)

    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config={
            "qq": {"enabled": True, "ws_url": "ws://napcat:3001"}
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
                    "flags": {
                        "group_replies_enabled": True,
                        "proactive_enabled": False,
                    },
                    "ids": {"group_whitelist": ["123456", "789"]},
                    "urls": {"group_reply_policy": "mention_or_keyword"},
                    "numbers": {
                        "group_reply_cooldown_secs": 20,
                        "proactive_daily_max": 4.0,
                    },
                },
            )
    finally:
        set_admin_state(None)

    assert resp.status_code == 200, resp.text
    qq = writes[-1]["qq"]
    assert qq["group_replies_enabled"] is True
    assert qq["group_whitelist"] == [123456, 789]
    assert qq["group_reply_policy"] == "mention_or_keyword"
    assert qq["group_reply_cooldown_secs"] == 20
    # Integral floats persist as ints.
    assert qq["proactive_daily_max"] == 4
    assert isinstance(qq["proactive_daily_max"], int)


def test_qq_status_echoes_behavior_fields_for_editor_seeding(tmp_path) -> None:
    """The editor pre-seeds from ``config_keys`` — whitelist, flags and
    tuning numbers must round-trip through the status route (they used to
    be silently dropped, so the form always rendered blank/off over a
    configured value)."""
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config={
            "qq": {
                "enabled": True,
                "self_ids": [10001],
                "group_whitelist": [123, 456],
                "proactive_groups": [123],
                "group_replies_enabled": True,
                "proactive_enabled": False,
                "group_reply_cooldown_secs": 45,
                "proactive_daily_max": 2,
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
    assert keys["group_whitelist"] == ["123", "456"]
    assert keys["proactive_groups"] == ["123"]
    assert keys["group_replies_enabled"] == "True"
    assert keys["proactive_enabled"] == "False"
    assert keys["group_reply_cooldown_secs"] == "45"
    assert keys["proactive_daily_max"] == "2"
