from __future__ import annotations

from corlinman_server.gateway.routes_admin_b import napcat
from corlinman_server.gateway.routes_admin_b.state import AdminState, set_admin_state
from fastapi import FastAPI, Request
from fastapi.responses import Response

from ._admin_auth import authenticated_test_client, configure_admin_auth


def test_webui_route_proxies_to_napcat_root(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[str] = []

    async def fake_proxy(request: Request, upstream_path: str) -> Response:
        del request
        calls.append(upstream_path)
        return Response("<html>NapCat</html>", media_type="text/html")

    monkeypatch.setattr(napcat, "_proxy_napcat_request", fake_proxy)
    state = configure_admin_auth(AdminState(data_dir=tmp_path))
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(napcat.router())
        client = authenticated_test_client(app)
        resp = client.get("/webui")
    finally:
        set_admin_state(None)

    assert resp.status_code == 200
    assert "NapCat" in resp.text
    assert calls == ["/"]


def test_webui_api_route_proxies_qqlogin_without_catching_corlinman_api(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_proxy(request: Request, upstream_path: str) -> Response:
        calls.append((request.method, upstream_path))
        return Response('{"code":0,"data":{}}', media_type="application/json")

    monkeypatch.setattr(napcat, "_proxy_napcat_request", fake_proxy)
    state = configure_admin_auth(AdminState(data_dir=tmp_path))
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(napcat.router())
        client = authenticated_test_client(app)
        resp = client.post("/api/QQLogin/GetQQLoginQrcode", json={})
    finally:
        set_admin_state(None)

    assert resp.status_code == 200
    assert calls == [("POST", "/api/QQLogin/GetQQLoginQrcode")]
