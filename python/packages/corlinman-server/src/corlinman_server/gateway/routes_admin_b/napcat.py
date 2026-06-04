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
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response

from corlinman_server.gateway.routes_admin_b._napcat_lib import (
    _NAPCAT_CRED_CACHE,
    AccountsOut,
    NapcatError,
    QrcodeOut,
    QuickLoginBody,
    StatusOut,
    _accounts_path,
    _cached_napcat_credential,
    _ensure_onebot_websocket_server,
    _load_accounts,
    _NapcatClient,
    _onebot_websocket_server_from_config,
    _resolve_napcat_url,
    _upsert_account,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)

# Re-exported from ``_napcat_lib`` for external callers (e.g. tests that patch
# the credential cache / client). Listed here so the re-imports are treated as
# public API rather than unused.
__all__ = [
    "_NAPCAT_CRED_CACHE",
    "_NapcatClient",
    "_cached_napcat_credential",
    "router",
]

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
        cred = await _cached_napcat_credential()
        headers = {"X-Napcat-Credential": cred} if cred else {}
        return Response(status_code=200, headers=headers)

    @r.post("/admin/channels/qq/qrcode", response_model=QrcodeOut)
    async def qrcode():
        state = get_admin_state()
        client, err, _path = _build_client(state)
        if err is not None or client is None:
            return err
        try:
            async with client:
                return await client.request_qrcode()
        except NapcatError as exc:
            return exc.response()

    @r.get("/admin/channels/qq/qrcode/status", response_model=StatusOut)
    async def qrcode_status(token: str = Query("")):
        state = get_admin_state()
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
        cfg = dict(config_snapshot())
        path = _accounts_path(state, cfg)
        try:
            accts = await _load_accounts(path)
        except OSError as exc:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "accounts_read_failed",
                    "message": f"failed to read {path}: {exc}",
                },
            )
        return AccountsOut(accounts=accts)

    @r.post("/admin/channels/qq/quick-login", response_model=StatusOut)
    async def quick_login(body: QuickLoginBody):
        if not body.uin.strip():
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_uin", "message": "uin is required"},
            )
        state = get_admin_state()
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
