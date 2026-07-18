from __future__ import annotations

import pytest
from corlinman_server.gateway.routes_admin_b import _napcat_lib as nc


class _NoopRefreshThenRestartClient(nc._NapcatClient):
    def __init__(self) -> None:
        super().__init__("http://napcat.test", "token")
        self.posts: list[str] = []
        self.restarted = False

    async def _login(self) -> str | None:
        return "credential-after-restart" if self.restarted else "credential-before-restart"

    async def aclose(self) -> None:
        return None

    async def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        del body
        self.posts.append(path)
        if path == "/api/QQLogin/GetQQLoginQrcode":
            return (
                {"qrcode": "https://qq.example/qr-after"}
                if self.restarted
                else {"qrcode": "https://qq.example/qr-before"}
            )
        if path == "/api/QQLogin/RefreshQRcode":
            return {}
        if path == "/api/QQLogin/RestartNapCat":
            self.restarted = True
            return {"message": "Restart initiated"}
        raise AssertionError(f"unexpected NapCat path {path}")


class _StaleLoginedClient(nc._NapcatClient):
    """NapCat wedged after a session drop: QR APIs answer ``QQ Is Logined``
    while CheckLoginStatus reports offline/not-logged-in. Cleared by restart."""

    def __init__(self, *, is_login: bool) -> None:
        super().__init__("http://napcat.test", "token")
        self.posts: list[str] = []
        self.restarted = False
        self.is_login = is_login

    async def _login(self) -> str | None:
        return "credential"

    async def aclose(self) -> None:
        return None

    async def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        del body
        self.posts.append(path)
        if path == "/api/QQLogin/CheckLoginStatus":
            return {"isLogin": self.is_login, "isOffline": not self.is_login}
        if path == "/api/QQLogin/RestartNapCat":
            self.restarted = True
            return {"message": "Restart initiated"}
        if path in ("/api/QQLogin/GetQQLoginQrcode", "/api/QQLogin/RefreshQRcode"):
            if not self.restarted:
                raise nc.NapcatError("napcat_app_error", "QQ Is Logined")
            if path == "/api/QQLogin/GetQQLoginQrcode":
                return {"qrcode": "https://qq.example/qr-after-restart"}
            return {}
        raise AssertionError(f"unexpected NapCat path {path}")


@pytest.mark.asyncio
async def test_request_qrcode_restarts_napcat_when_stuck_reporting_logged_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nc, "NAPCAT_QRCODE_RETRY_INTERVAL_S", 0.0, raising=False)
    monkeypatch.setattr(nc, "NAPCAT_QRCODE_RESTART_WAIT_S", 0.0, raising=False)
    client = _StaleLoginedClient(is_login=False)

    out = await client.request_qrcode()

    assert out.qrcode_url == "https://qq.example/qr-after-restart"
    assert "/api/QQLogin/RestartNapCat" in client.posts


@pytest.mark.asyncio
async def test_request_qrcode_rejects_when_genuinely_logged_in() -> None:
    client = _StaleLoginedClient(is_login=True)

    with pytest.raises(nc.NapcatError) as excinfo:
        await client.request_qrcode()

    assert excinfo.value.code == "napcat_already_logged_in"
    assert excinfo.value.upstream_status == 409
    assert "/api/QQLogin/RestartNapCat" not in client.posts


@pytest.mark.asyncio
async def test_request_qrcode_restarts_napcat_when_refresh_returns_same_qr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nc, "NAPCAT_QRCODE_RETRY_INTERVAL_S", 0.0, raising=False)
    monkeypatch.setattr(nc, "NAPCAT_QRCODE_RESTART_WAIT_S", 0.0, raising=False)
    client = _NoopRefreshThenRestartClient()

    out = await client.request_qrcode()

    assert out.qrcode_url == "https://qq.example/qr-after"
    assert "/api/QQLogin/RestartNapCat" in client.posts


def test_napcat_webui_refresh_route_uses_robust_qrcode_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from corlinman_server.gateway.routes_admin_b import napcat
    from corlinman_server.gateway.routes_admin_b.state import AdminState, set_admin_state
    from fastapi import FastAPI

    from ._admin_auth import authenticated_test_client, configure_admin_auth

    calls = {"request_qrcode": 0}

    class FakeClient:
        def __init__(self, *_args: object) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def request_qrcode(self) -> nc.QrcodeOut:
            calls["request_qrcode"] += 1
            return nc.QrcodeOut(
                token="qr-token",
                qrcode_url="https://qq.example/qr-after",
                expires_at=1,
            )

    monkeypatch.setattr(
        napcat,
        "config_snapshot",
        lambda: {
            "channels": {
                "qq": {
                    "napcat_url": "http://napcat:6099",
                    "napcat_access_token": "tok",
                }
            }
        },
    )
    monkeypatch.setattr(napcat, "_NapcatClient", FakeClient)

    state = configure_admin_auth(AdminState(data_dir=tmp_path))
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(napcat.router())
        client = authenticated_test_client(app)
        resp = client.post("/api/QQLogin/RefreshQRcode")
    finally:
        set_admin_state(None)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"code": 0, "message": "success", "data": None}
    assert calls == {"request_qrcode": 1}
