from __future__ import annotations

import json
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_b import _napcat_lib as nc

from ._admin_auth import authenticated_test_client, configure_admin_auth


def _empty_ob11_config() -> dict[str, Any]:
    return {
        "network": {
            "httpServers": [],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [],
            "websocketClients": [],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
        "imageDownloadProxy": "",
        "timeout": {
            "baseTimeout": 10000,
            "uploadSpeedKBps": 256,
            "downloadSpeedKBps": 256,
            "maxTimeout": 1800000,
        },
    }


class _RecordingNapcatClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, body))
        if path == "/api/OB11Config/GetConfig":
            return self.config
        if path == "/api/OB11Config/SetConfig":
            self.config = json.loads(str(body["config"]))
            return {}
        raise AssertionError(f"unexpected NapCat path {path}")


@pytest.mark.asyncio
async def test_ensure_onebot_websocket_server_adds_corlinman_server() -> None:
    client = _RecordingNapcatClient(_empty_ob11_config())

    changed = await nc._ensure_onebot_websocket_server(
        client,
        nc._onebot_websocket_server_from_config(
            {"channels": {"qq": {"ws_url": "ws://napcat:3001"}}}
        ),
    )

    assert changed is True
    assert [path for path, _body in client.posts] == [
        "/api/OB11Config/GetConfig",
        "/api/OB11Config/SetConfig",
    ]
    server = client.config["network"]["websocketServers"][0]
    assert server == {
        "enable": True,
        "name": "corlinman",
        "host": "0.0.0.0",
        "port": 3001,
        "messagePostFormat": "array",
        "reportSelfMessage": False,
        "enableForcePushEvent": True,
        "token": "",
        "debug": False,
        "heartInterval": 30000,
    }


@pytest.mark.asyncio
async def test_ensure_onebot_websocket_server_is_idempotent() -> None:
    config = _empty_ob11_config()
    config["network"]["websocketServers"].append(
        {
            "enable": True,
            "name": "corlinman",
            "host": "0.0.0.0",
            "port": 3001,
            "messagePostFormat": "array",
            "reportSelfMessage": False,
            "enableForcePushEvent": True,
            "token": "",
            "debug": False,
            "heartInterval": 30000,
        }
    )
    client = _RecordingNapcatClient(config)

    changed = await nc._ensure_onebot_websocket_server(
        client,
        nc._onebot_websocket_server_from_config(
            {"channels": {"qq": {"ws_url": "ws://napcat:3001"}}}
        ),
    )

    assert changed is False
    assert [path for path, _body in client.posts] == [
        "/api/OB11Config/GetConfig"
    ]


@pytest.mark.asyncio
async def test_ensure_onebot_websocket_server_uses_configured_ws_port() -> None:
    client = _RecordingNapcatClient(_empty_ob11_config())

    await nc._ensure_onebot_websocket_server(
        client,
        nc._onebot_websocket_server_from_config(
            {"channels": {"qq": {"ws_url": "ws://10.0.0.5:4567/ws"}}}
        ),
    )

    server = client.config["network"]["websocketServers"][0]
    assert server["port"] == 4567


@pytest.mark.asyncio
async def test_confirmed_qrcode_status_ensures_onebot_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from corlinman_server.gateway.routes_admin_b import napcat
    from corlinman_server.gateway.routes_admin_b.state import AdminState, set_admin_state
    from fastapi import FastAPI

    calls: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, *_args: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def check_status(self) -> nc.StatusOut:
            return nc.StatusOut(
                status="confirmed",
                account=nc.QqAccount(uin="1670942141", last_login_at=1),
            )

    async def fake_ensure(client: Any, server: dict[str, Any]) -> bool:
        calls.append(server)
        return True

    monkeypatch.setattr(
        napcat, "config_snapshot", lambda: {"channels": {"qq": {"ws_url": "ws://napcat:3001"}}}
    )
    monkeypatch.setattr(napcat, "_NapcatClient", FakeClient)
    monkeypatch.setattr(napcat, "_ensure_onebot_websocket_server", fake_ensure)

    state = configure_admin_auth(AdminState(data_dir=tmp_path))
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(napcat.router())
        client = authenticated_test_client(app)
        resp = client.get("/admin/channels/qq/qrcode/status")
    finally:
        set_admin_state(None)

    assert resp.status_code == 200, resp.text
    assert calls == [
        {
            "enable": True,
            "name": "corlinman",
            "host": "0.0.0.0",
            "port": 3001,
            "messagePostFormat": "array",
            "reportSelfMessage": False,
            "enableForcePushEvent": True,
            "token": "",
            "debug": False,
            "heartInterval": 30000,
        }
    ]


@pytest.mark.asyncio
async def test_confirmed_quick_login_ensures_onebot_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from corlinman_server.gateway.routes_admin_b import napcat
    from corlinman_server.gateway.routes_admin_b.state import AdminState, set_admin_state
    from fastapi import FastAPI

    calls: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, *_args: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def quick_login(self, uin: str) -> nc.StatusOut:
            return nc.StatusOut(
                status="confirmed",
                account=nc.QqAccount(uin=uin, last_login_at=1),
            )

    async def fake_ensure(client: Any, server: dict[str, Any]) -> bool:
        calls.append(server)
        return True

    monkeypatch.setattr(
        napcat,
        "config_snapshot",
        lambda: {"channels": {"qq": {"ws_url": "ws://napcat:3001"}}},
    )
    monkeypatch.setattr(napcat, "_NapcatClient", FakeClient)
    monkeypatch.setattr(napcat, "_ensure_onebot_websocket_server", fake_ensure)

    state = configure_admin_auth(AdminState(data_dir=tmp_path))
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(napcat.router())
        client = authenticated_test_client(app)
        resp = client.post(
            "/admin/channels/qq/quick-login", json={"uin": "1670942141"}
        )
    finally:
        set_admin_state(None)

    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    assert calls[0]["name"] == "corlinman"
    assert calls[0]["port"] == 3001
