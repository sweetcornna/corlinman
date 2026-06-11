from __future__ import annotations

from corlinman_server.gateway.routes_admin_b import napcat
from corlinman_server.gateway.routes_admin_b.state import AdminState, set_admin_state
from fastapi import FastAPI, Request
from fastapi.responses import Response

from ._admin_auth import authenticated_test_client, configure_admin_auth


def test_webui_route_preserves_prefix_upstream(
    monkeypatch,
    tmp_path,
) -> None:
    # NapCat serves its WebUI at <napcat_url>/webui, so the same-origin
    # /webui[/...] iframe must keep the /webui prefix upstream — stripping
    # it to "/" hits NapCat's root and renders a blank frame.
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
        resp_root = client.get("/webui")
        resp_asset = client.get("/webui/assets/app.js")
    finally:
        set_admin_state(None)

    assert resp_root.status_code == 200
    assert "NapCat" in resp_root.text
    assert resp_asset.status_code == 200
    assert calls == ["/webui", "/webui/assets/app.js"]


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


def test_webui_api_route_proxies_frontend_groups(
    monkeypatch,
    tmp_path,
) -> None:
    # The embedded WebUI calls base/Log/Process/WebUIConfig groups too;
    # without these the iframe's status/config pages fall through.
    calls: list[str] = []

    async def fake_proxy(request: Request, upstream_path: str) -> Response:
        del request
        calls.append(upstream_path)
        return Response('{"code":0}', media_type="application/json")

    monkeypatch.setattr(napcat, "_proxy_napcat_request", fake_proxy)
    state = configure_admin_auth(AdminState(data_dir=tmp_path))
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(napcat.router())
        client = authenticated_test_client(app)
        for grp in ("base", "Log", "Process", "WebUIConfig"):
            client.get(f"/api/{grp}/Version")
    finally:
        set_admin_state(None)

    assert calls == [
        "/api/base/Version",
        "/api/Log/Version",
        "/api/Process/Version",
        "/api/WebUIConfig/Version",
    ]


def test_proxy_strips_cookie_and_admin_auth_from_upstream(monkeypatch) -> None:
    # Regression: a same-origin /webui request carries the admin session
    # cookie; the proxy must NOT forward it (or the browser's own auth) to
    # a possibly-external NapCat upstream. Exercised against
    # _proxy_napcat_request directly with a hand-built ASGI Request so the
    # admin-auth dependency (which would reject our injected headers) is
    # out of the picture — this is a pure header-allowlist unit test.
    import asyncio

    import httpx

    captured: dict[str, httpx.Headers] = {}

    class _FakeResp:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/html"})
        content = b"ok"

    class _FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def request(self, method, target, content, headers, **kw):
            captured["headers"] = httpx.Headers(headers)
            return _FakeResp()

    monkeypatch.setattr(napcat.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(napcat, "config_snapshot", lambda: {})
    monkeypatch.setattr(
        napcat, "_resolve_napcat_url", lambda cfg: ("http://napcat.example", None)
    )

    async def _no_cred() -> str:
        return ""

    monkeypatch.setattr(napcat, "_cached_napcat_credential", _no_cred)

    raw_headers = [
        (b"cookie", b"corlinman_session=secret"),
        (b"authorization", b"Bearer browser-creds"),
        (b"accept", b"text/html"),
        (b"host", b"corlinman.local"),
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/webui",
        "query_string": b"",
        "headers": raw_headers,
    }

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(scope, _receive)
    resp = asyncio.run(napcat._proxy_napcat_request(request, "/webui"))

    assert resp.status_code == 200
    fwd = captured["headers"]
    # Admin session cookie + browser creds + host must NOT reach upstream.
    assert "cookie" not in fwd
    assert "authorization" not in fwd  # no NapCat cred → none injected, none leaked
    assert "host" not in fwd
    # Allowlisted negotiation header passes through.
    assert fwd.get("accept") == "text/html"
