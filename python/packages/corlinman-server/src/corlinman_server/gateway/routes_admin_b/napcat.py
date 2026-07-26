"""``/admin/channels/qq/*`` — NapCat webui proxy + account history.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/napcat.rs``.

Routes:

* ``POST /admin/channels/qq/qrcode``        — fetch QR from NapCat.
* ``GET  /admin/channels/qq/qrcode/status`` — poll login status.
* ``GET  /admin/channels/qq/accounts``      — history from
  ``<data_dir>/qq-accounts.json``.
* ``POST /admin/channels/qq/quick-login``   — re-use a stored session.

NapCat URL resolution order matches Rust:

1. ``[channels.qq].napcat_url`` from config.
2. ``CORLINMAN_NAPCAT_URL`` env.
3. 503 ``napcat_not_configured``.

Authentication: NapCat 2.x exchanges
``POST /api/auth/login {"hash": sha256(token + ".napcat")}`` for a
short-lived ``Credential`` we then send as ``Bearer``.

Scope of the embedded WebUI proxy
----------------------------------
The same-origin ``/webui`` iframe + ``/api/*`` proxy exist to drive the
**QQ scan-login and OneBot configuration flow** — nothing more. We proxy
exactly the NapCat frontend API groups that flow touches
(``QQLogin`` / ``OB11Config`` / ``auth`` / ``base`` / ``Log`` /
``Process`` / ``WebUIConfig``) as *named prefixes*, NOT a blanket
``/api/{path}`` catch-all: corlinman serves its own first-party routes
under ``/api/channels/corlinman/*`` (see ``corlinman_channel.py``) and a
catch-all here would shadow them. Full NapCat WebUI administration —
plugin management, the file browser, the debug terminal
(``/api/ws/terminal`` WebSocket), and live log *tailing*
(``/api/Log/GetLogRealTime``, a long-lived ``text/event-stream`` the
buffering proxy below cannot stream) — is intentionally **out of scope**;
operators manage those on the NapCat instance directly.

Trust model: the iframe runs NapCat's HTML/JS at corlinman's own origin,
so it must be a **trusted, operator-managed NapCat** (the bundled
default — local docker/native). Pointing ``[channels.qq].napcat_url`` at
an untrusted external host is not a supported security configuration: a
same-origin iframe that authenticates to the proxied ``/api/*`` cannot be
sandboxed without breaking that auth. As defense-in-depth the proxy still
never forwards the browser's ``Cookie`` / ``Authorization`` upstream (see
``_FORWARD_REQUEST_HEADERS``).
"""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from corlinman_server.gateway.qq_instances import QqAdminError, QqInstanceAdminService
from corlinman_server.gateway.routes_admin_b._napcat_lib import (
    _NAPCAT_CRED_CACHE,
    NAPCAT_TIMEOUT,
    AccountsOut,
    NapcatDiagnosticsOut,
    NapcatError,
    QrcodeOut,
    QuickLoginBody,
    StatusOut,
    _accounts_path,
    _cached_napcat_credential,
    _cached_napcat_credential_for_endpoint,
    _ensure_onebot_websocket_server,
    _load_accounts,
    _napcat_http_error,
    _NapcatClient,
    _onebot_websocket_server_from_config,
    _probe_napcat_diagnostics,
    _resolve_napcat_endpoint_for_instance,
    _resolve_napcat_url,
    _upsert_account,
)
from corlinman_server.gateway.routes_admin_b.napcat_instances import (
    accounts_for_instance,
    create_login_attempt,
    poll_login_attempt,
    quick_login_instance,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)


def _raise_qq_admin(error: QqAdminError) -> NoReturn:
    raise HTTPException(
        status_code=error.status_code,
        detail=error.public_detail(),
    ) from error


def _canonical_default_instance(state: AdminState | None = None) -> str | None:
    """Return the explicit canonical default, or ``None`` for legacy config only."""
    state = state or get_admin_state()
    service = getattr(state, "qq_instance_admin", None)
    if not isinstance(service, QqInstanceAdminService):
        return None
    channels = getattr(service.state, "channels_config", None)
    qq = channels.get("qq") if isinstance(channels, dict) else None
    if not isinstance(qq, dict) or "instances" not in qq:
        return None
    return service.resolve_instance_id(None)


async def _canonical_proxy_endpoint(state: AdminState):
    instance_id = _canonical_default_instance(state)
    if instance_id is None:
        return None
    service = state.qq_instance_admin
    assert isinstance(service, QqInstanceAdminService)
    return await _resolve_napcat_endpoint_for_instance(service.state, instance_id)

_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_RESPONSE_DROP_HEADERS = _HOP_BY_HOP_HEADERS | {"content-encoding", "content-type"}

# Allowlist of request headers forwarded upstream to NapCat. We
# deliberately do NOT mirror the browser's full header set: when
# ``[channels.qq].napcat_url`` points at an external/user-configured host,
# blindly copying ``Cookie`` would leak the admin session
# (``corlinman_session=...``) and copying ``Authorization`` would leak the
# browser's own creds. The proxy supplies its own NapCat Bearer below, so
# only transport/content negotiation + range/caching headers pass through.
_FORWARD_REQUEST_HEADERS = {
    "accept",
    "accept-encoding",
    "accept-language",
    "content-type",
    "user-agent",
    "range",
    "if-none-match",
    "if-modified-since",
    "cache-control",
}

# Re-exported from ``_napcat_lib`` for external callers (e.g. tests that patch
# the credential cache / client). Listed here so the re-imports are treated as
# public API rather than unused.
__all__ = [
    "_NAPCAT_CRED_CACHE",
    "_NapcatClient",
    "_cached_napcat_credential",
    "_probe_napcat_diagnostics",
    "router",
]


async def _proxy_napcat_request(request: Request, upstream_path: str) -> Response:
    state = get_admin_state()
    try:
        canonical = await _canonical_proxy_endpoint(state)
    except QqAdminError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.public_detail(),
        )
    if canonical is None:
        cfg = dict(config_snapshot())
        url, token = _resolve_napcat_url(cfg)
    else:
        url, token = canonical.url, canonical.access_token
    if url is None:
        return JSONResponse(
            status_code=503,
            content={
                "error": "napcat_not_configured",
                "message": "NapCat URL is not configured",
            },
        )

    target = f"{url}{upstream_path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    # Allowlist, not "copy-all-minus-hop-by-hop": never forward Cookie /
    # Authorization / Host etc. to a (possibly external) NapCat upstream.
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in _FORWARD_REQUEST_HEADERS
    }
    credential = (
        await _cached_napcat_credential()
        if canonical is None
        else await _cached_napcat_credential_for_endpoint(url, token)
    )
    if credential:
        headers["authorization"] = f"Bearer {credential}"

    try:
        async with httpx.AsyncClient(timeout=NAPCAT_TIMEOUT) as client:
            upstream = await client.request(
                request.method,
                target,
                content=await request.body(),
                headers=headers,
                follow_redirects=False,
            )
    except httpx.HTTPError:
        return JSONResponse(
            status_code=503,
            content={
                "error": "napcat_unreachable",
                "message": "NapCat is unreachable",
            },
        )

    if not 200 <= upstream.status_code < 300:
        return JSONResponse(
            status_code=502,
            content={
                "error": "napcat_upstream_error",
                "message": "NapCat returned an error",
            },
        )

    out_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _RESPONSE_DROP_HEADERS
        and key.lower() != "location"
    }
    return Response(
        content=b"" if request.method == "HEAD" else upstream.content,
        status_code=upstream.status_code,
        headers=out_headers,
        media_type=upstream.headers.get("content-type"),
    )

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "napcat"])

    def _build_client(state: AdminState) -> tuple[_NapcatClient | None, JSONResponse | None, Path]:
        cfg = dict(config_snapshot())
        url, token = _resolve_napcat_url(cfg)
        path = _accounts_path(state, cfg)
        # Defensive: _resolve_napcat_url now falls back to DEFAULT_NAPCAT_URL so
        # this arm is unreachable in the normal flow (a missing/down NapCat
        # surfaces as a typed napcat_unreachable 503 on first call instead).
        # Kept as a guard so an explicit None override still yields a clean 503.
        if url is None:
            return None, JSONResponse(
                status_code=503,
                content={
                    "error": "napcat_not_configured",
                    "message": (
                        "[channels.qq].napcat_url is empty;"
                        " set it in config.toml or export CORLINMAN_NAPCAT_URL"
                    ),
                },
            ), path
        return _NapcatClient(url, token), None, path

    @r.get("/internal/napcat-credential")
    async def napcat_credential() -> Response:
        """Return a fresh NapCat WebUI Bearer credential in a header.

        Consumed by the reverse proxy's ``auth_request`` on every NapCat
        ``/api/*`` request: nginx copies ``X-Napcat-Credential`` into the
        upstream ``Authorization`` header so the embedded WebUI is
        authenticated server-side regardless of the browser's stored
        credential. Always 200 (so ``auth_request`` never blocks ``/api``);
        the header is omitted when no credential is available, letting the
        proxy fall back to the request's own ``Authorization``. Gated by the
        router's ``require_admin`` dependency.
        """
        state = get_admin_state()
        try:
            endpoint = await _canonical_proxy_endpoint(state)
        except QqAdminError:
            endpoint = None
            cred = ""
        else:
            cred = (
                await _cached_napcat_credential()
                if endpoint is None
                else await _cached_napcat_credential_for_endpoint(
                    endpoint.url,
                    endpoint.access_token,
                )
            )
        headers = {"X-Napcat-Credential": cred} if cred else {}
        return Response(status_code=200, headers=headers)

    @r.get(
        "/admin/channels/qq/napcat/diagnostics",
        response_model=NapcatDiagnosticsOut,
    )
    async def napcat_diagnostics() -> NapcatDiagnosticsOut:
        state = get_admin_state()
        try:
            instance_id = _canonical_default_instance(state)
            if instance_id is not None:
                from corlinman_server.gateway.routes_admin_b._napcat_lib import (
                    _probe_napcat_diagnostics_for_instance,
                )

                service = state.qq_instance_admin
                assert isinstance(service, QqInstanceAdminService)
                return await _probe_napcat_diagnostics_for_instance(
                    service.state,
                    instance_id,
                )
        except QqAdminError as exc:
            _raise_qq_admin(exc)
        return await _probe_napcat_diagnostics(dict(config_snapshot()))

    # NapCat serves its WebUI at ``<napcat_url>/webui`` (per its startup
    # URL / docs), so the same-origin ``/webui`` iframe must keep the
    # ``/webui`` prefix upstream — stripping it to ``/`` hits NapCat's
    # root and renders a blank/404 frame.
    @r.api_route("/webui", methods=_PROXY_METHODS, include_in_schema=False)
    async def napcat_webui_root(request: Request) -> Response:
        return await _proxy_napcat_request(request, "/webui")

    @r.api_route("/webui/{path:path}", methods=_PROXY_METHODS, include_in_schema=False)
    async def napcat_webui_path(request: Request, path: str) -> Response:
        return await _proxy_napcat_request(request, f"/webui/{path}")

    @r.post("/admin/channels/qq/qrcode", response_model=QrcodeOut)
    async def qrcode(request: Request):
        state = get_admin_state()
        try:
            instance_id = _canonical_default_instance(state)
            if instance_id is not None:
                attempt = await create_login_attempt(state, instance_id, request)
                return QrcodeOut(
                    token=attempt.attempt_id,
                    image_base64=attempt.image_base64,
                    qrcode_url=attempt.qrcode_url,
                    expires_at=attempt.expires_at,
                )
        except QqAdminError as exc:
            _raise_qq_admin(exc)
        client, err, _path = _build_client(state)
        if err is not None or client is None:
            return err
        try:
            async with client:
                return await client.request_qrcode()
        except NapcatError as exc:
            return exc.response()

    @r.post("/api/QQLogin/RefreshQRcode", include_in_schema=False)
    async def napcat_webui_refresh_qrcode():
        """Refresh the explicit default instance used by the compatibility WebUI."""
        state = get_admin_state()
        try:
            endpoint = await _canonical_proxy_endpoint(state)
        except QqAdminError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"code": -1, "message": exc.message, "data": None},
            )
        if endpoint is None:
            client, err, _path = _build_client(state)
            if err is not None or client is None:
                return err
        else:
            if endpoint.url is None:  # pragma: no cover - resolver fails closed
                return JSONResponse(
                    status_code=503,
                    content={"code": -1, "message": "NapCat URL unavailable", "data": None},
                )
            client = _NapcatClient(endpoint.url, endpoint.access_token)
        try:
            async with client:
                await client.request_qrcode()
        except NapcatError as exc:
            error_status, detail = _napcat_http_error(exc)
            return JSONResponse(
                status_code=error_status,
                content={
                    "code": -1,
                    "message": detail["message"],
                    "data": None,
                },
            )
        return {"code": 0, "message": "success", "data": None}

    @r.api_route(
        "/api/QQLogin/{path:path}",
        methods=_PROXY_METHODS,
        include_in_schema=False,
    )
    async def napcat_qqlogin_api_proxy(request: Request, path: str) -> Response:
        return await _proxy_napcat_request(request, f"/api/QQLogin/{path}")

    @r.api_route(
        "/api/OB11Config/{path:path}",
        methods=_PROXY_METHODS,
        include_in_schema=False,
    )
    async def napcat_ob11_api_proxy(request: Request, path: str) -> Response:
        return await _proxy_napcat_request(request, f"/api/OB11Config/{path}")

    @r.api_route(
        "/api/auth/{path:path}",
        methods=_PROXY_METHODS,
        include_in_schema=False,
    )
    async def napcat_auth_api_proxy(request: Request, path: str) -> Response:
        return await _proxy_napcat_request(request, f"/api/auth/{path}")

    # The embedded WebUI also calls these NapCat frontend API groups
    # (base info / log tail / process control / WebUI config). Without
    # them the iframe's status + config pages fall through to corlinman
    # and fail. Named prefixes (not a blanket /api catch-all) so the
    # first-party /api/channels/corlinman/* routes are never shadowed.
    for _grp in ("base", "Log", "Process", "WebUIConfig"):

        def _make_proxy(group: str):
            async def _proxy(request: Request, path: str) -> Response:
                return await _proxy_napcat_request(request, f"/api/{group}/{path}")

            return _proxy

        r.add_api_route(
            f"/api/{_grp}/{{path:path}}",
            _make_proxy(_grp),
            methods=_PROXY_METHODS,
            include_in_schema=False,
        )

    @r.get("/admin/channels/qq/qrcode/status", response_model=StatusOut)
    async def qrcode_status(request: Request, token: str = Query("")):
        state = get_admin_state()
        try:
            instance_id = _canonical_default_instance(state)
            if instance_id is not None:
                if not token:
                    raise HTTPException(
                        status_code=422,
                        detail={"error": "login_attempt_token_required"},
                    )
                attempt = await poll_login_attempt(
                    state,
                    instance_id,
                    token,
                    request,
                )
                return StatusOut(
                    status=attempt.status,
                    account=attempt.account,
                    message=attempt.message,
                )
        except QqAdminError as exc:
            _raise_qq_admin(exc)
        client, err, path = _build_client(state)
        if err is not None or client is None:
            return err
        try:
            async with client:
                out = await client.check_status()
                if out.status == "confirmed":
                    await _ensure_onebot_websocket_server(
                        client,
                        _onebot_websocket_server_from_config(dict(config_snapshot())),
                    )
        except NapcatError as exc:
            return exc.response()
        if out.account is not None:
            try:
                await _upsert_account(path, out.account)
            except OSError:
                pass
        return out

    @r.get("/admin/channels/qq/accounts", response_model=AccountsOut)
    async def accounts():
        state = get_admin_state()
        try:
            instance_id = _canonical_default_instance(state)
            if instance_id is not None:
                return await accounts_for_instance(state, instance_id)
        except QqAdminError as exc:
            _raise_qq_admin(exc)
        cfg = dict(config_snapshot())
        path = _accounts_path(state, cfg)
        try:
            accts = await _load_accounts(path)
        except OSError:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "accounts_read_failed",
                    "message": "failed to read QQ login history",
                },
            )
        return AccountsOut(accounts=accts)

    @r.post("/admin/channels/qq/quick-login", response_model=StatusOut)
    async def quick_login(body: QuickLoginBody):
        state = get_admin_state()
        try:
            instance_id = _canonical_default_instance(state)
            if instance_id is not None:
                return await quick_login_instance(state, instance_id, body)
        except QqAdminError as exc:
            _raise_qq_admin(exc)
        if not body.uin.strip():
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_uin", "message": "uin is required"},
            )
        client, err, path = _build_client(state)
        if err is not None or client is None:
            return err
        try:
            async with client:
                out = await client.quick_login(body.uin)
                if out.status == "confirmed":
                    await _ensure_onebot_websocket_server(
                        client,
                        _onebot_websocket_server_from_config(dict(config_snapshot())),
                    )
        except NapcatError as exc:
            return exc.response()
        if out.account is not None:
            try:
                await _upsert_account(path, out.account)
            except OSError:
                pass
        return out

    return r
