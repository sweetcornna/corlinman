"""Wire models, NapCat client, and helpers for :mod:`.napcat`.

Extracted verbatim from ``napcat.py`` (the ``/admin/channels/qq/*`` route
module) to keep that file focused on ``router()`` + handlers. This module owns
the module-level shapes/constants/helpers; ``napcat.py`` re-imports them. It
must NOT import ``napcat`` (no cycle).
"""

from __future__ import annotations

import asyncio
import hashlib
import json as json_lib
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
)

ACCOUNTS_FILE = "qq-accounts.json"
NAPCAT_TIMEOUT = 6.0

# Loopback default for the NapCat webui + scan-login HTTP API. Both supported
# deploy modes land NapCat here from the gateway's point of view:
#   * docker — docker-compose.qq.yml sets CORLINMAN_NAPCAT_URL=http://napcat:6099
#     (in-network DNS), so the env wins and this default is never used.
#   * native — install.sh provisions a NapCat AppImage + corlinman-napcat.service
#     listening on 127.0.0.1:6099 and exports CORLINMAN_NAPCAT_URL to match; this
#     default keeps the scan-login UI working even if that export is missing
#     (e.g. a hand-rolled native install) instead of a confusing immediate 503.
DEFAULT_NAPCAT_URL = "http://127.0.0.1:6099"
DEFAULT_ONEBOT_WS_PORT = 3001
OB11_CONFIG_GET_PATH = "/api/OB11Config/GetConfig"
OB11_CONFIG_SET_PATH = "/api/OB11Config/SetConfig"
QQ_QRCODE_GET_PATH = "/api/QQLogin/GetQQLoginQrcode"
QQ_QRCODE_REFRESH_PATH = "/api/QQLogin/RefreshQRcode"
QQ_NAPCAT_RESTART_PATH = "/api/QQLogin/RestartNapCat"
ONEBOT_WS_SERVER_NAME = "corlinman"
ONEBOT_ENSURE_MIN_INTERVAL_S = 10.0
NAPCAT_QRCODE_RETRY_COUNT = 4
NAPCAT_QRCODE_RETRY_INTERVAL_S = 0.4
NAPCAT_QRCODE_RESTART_RETRY_COUNT = 18
NAPCAT_QRCODE_RESTART_WAIT_S = 1.0
_ONEBOT_ENSURE_LAST_ATTEMPT: dict[str, float] = {}
_ONEBOT_ENSURE_TASKS: set[asyncio.Task[None]] = set()


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class QqAccount(BaseModel):
    uin: str
    nickname: str | None = None
    avatar_url: str | None = None
    last_login_at: int


class QrcodeOut(BaseModel):
    token: str
    image_base64: str | None = None
    qrcode_url: str | None = None
    expires_at: int


class StatusOut(BaseModel):
    status: str
    account: QqAccount | None = None
    message: str | None = None


class AccountsOut(BaseModel):
    accounts: list[QqAccount]


class QuickLoginBody(BaseModel):
    uin: str


class NapcatDiagnosticsOut(BaseModel):
    mode: str
    url: str | None
    url_source: str
    managed: bool
    auth_configured: bool
    credential: str
    qrcode_api: str
    onebot_config_api: str
    issues: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# NapCat client
# ---------------------------------------------------------------------------


_NAPCAT_PUBLIC_ERRORS: dict[str, tuple[int, str]] = {
    "napcat_already_logged_in": (
        409,
        "QQ is already logged in; a new login QR code is unavailable",
    ),
    "napcat_app_error": (502, "NapCat rejected the request"),
    "napcat_bad_response": (502, "NapCat returned an invalid response"),
    "napcat_not_logged_in": (409, "NapCat is not logged in"),
    "napcat_qrcode_refresh_noop": (
        502,
        "NapCat could not refresh the login QR code",
    ),
    "napcat_unreachable": (503, "NapCat is unreachable"),
    "napcat_upstream_error": (502, "NapCat request failed"),
}
_MANAGER_PUBLIC_STATUSES: dict[str, int] = {
    "forbidden": 403,
    "generation_conflict": 409,
    "instance_conflict": 409,
    "manager_unavailable": 503,
    "resource_not_owned": 409,
    "unsupported_operation": 409,
}


class NapcatError(Exception):
    """NapCat failure whose internal detail is never used as a wire message."""

    def __init__(self, code: str, message: str = "", status: int | None = None):
        super().__init__(message or code)
        self.code = code
        self.upstream_status = status

    @property
    def public_status(self) -> int:
        return _NAPCAT_PUBLIC_ERRORS.get(self.code, (502, ""))[0]

    @property
    def public_message(self) -> str:
        return _NAPCAT_PUBLIC_ERRORS.get(
            self.code,
            (502, "NapCat request failed"),
        )[1]

    def response(
        self,
        *,
        code: str | None = None,
        message: str | None = None,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=self.public_status,
            content={
                "error": code or self.code,
                "message": message or self.public_message,
            },
        )


def _napcat_http_error(
    error: NapcatError,
    *,
    code: str | None = None,
    message: str | None = None,
) -> tuple[int, dict[str, str]]:
    """Return a stable public envelope without upstream bodies or locations."""
    return error.public_status, {
        "error": code or error.code,
        "message": message or error.public_message,
    }


def _manager_http_error(
    response: Any,
    *,
    fallback_code: str,
    fallback_message: str,
    fallback_status: int,
) -> tuple[int, str, str]:
    """Map manager failures without reflecting privileged helper messages."""
    code = getattr(response, "error_code", None)
    if code not in _MANAGER_PUBLIC_STATUSES:
        return fallback_status, fallback_code, fallback_message
    return _MANAGER_PUBLIC_STATUSES[code], code, fallback_message


def _now_ms() -> int:
    return int(time.time() * 1000)


def _resolve_secret(value: Any) -> str | None:
    if isinstance(value, dict):
        if "value" in value and str(value["value"]).strip():
            return str(value["value"])
        if "env" in value:
            env_value = os.environ.get(str(value["env"]))
            return env_value if env_value else None
        return None
    if isinstance(value, str) and value.strip():
        return value
    return None


@dataclass(frozen=True)
class _NapcatEndpoint:
    url: str | None
    access_token: str | None
    url_source: str
    managed: bool
    generation: int | None = None

    @property
    def mode(self) -> str:
        return "managed" if self.managed else "external"


def _resolve_napcat_endpoint(cfg: dict[str, Any]) -> _NapcatEndpoint:
    qq = ((cfg.get("channels") or {}).get("qq")) or {}
    url = qq.get("napcat_url")
    url_source = "config"
    managed = False
    if not url or not str(url).strip():
        url = os.environ.get("CORLINMAN_NAPCAT_URL")
        url_source = "env"
        managed = True
    if not url or not str(url).strip():
        url = DEFAULT_NAPCAT_URL
        url_source = "default"
        managed = True
    url = str(url).rstrip("/") if url is not None else None
    access_token = _resolve_secret(qq.get("napcat_access_token"))
    if not access_token:
        access_token = (
            os.environ.get("NAPCAT_WEBUI_TOKEN")
            or os.environ.get("NAPCAT_WEBUI_SECRET_KEY")
            or os.environ.get("WEBUI_TOKEN")
        )
    return _NapcatEndpoint(
        url=url,
        access_token=access_token,
        url_source=url_source,
        managed=managed,
    )


async def _resolve_napcat_endpoint_for_instance(
    state: Any,
    instance_id: str,
) -> _NapcatEndpoint:
    """Resolve one exact instance without accepting a browser-supplied URL."""
    from corlinman_server.gateway.qq_instances import QqAdminError, QqInstanceAdminService

    service = QqInstanceAdminService(state)
    snapshot = service.get_instance(instance_id)
    if snapshot.connection_mode == "managed":
        manager = getattr(state, "napcat_manager", None)
        if manager is None:
            raise QqAdminError(
                503,
                "manager_unavailable",
                "managed NapCat manager is unavailable",
            )
        response = await manager.request("inspect", instance_id)
        if not response.ok and response.error_code == "instance_not_found":
            operation = "adopt" if snapshot.is_default else "provision"
            response = await manager.request(operation, instance_id)
            if (
                operation == "adopt"
                and not response.ok
                and response.error_code == "instance_not_found"
            ):
                response = await manager.request("provision", instance_id)
        if not response.ok or response.descriptor is None:
            error_status, error_code, error_message = _manager_http_error(
                response,
                fallback_code="manager_unavailable",
                fallback_message="managed NapCat instance is unavailable",
                fallback_status=503,
            )
            raise QqAdminError(error_status, error_code, error_message)
        return _NapcatEndpoint(
            url=response.descriptor.http_url.rstrip("/"),
            access_token=response.descriptor.napcat_access_token,
            url_source="manager",
            managed=True,
            generation=response.descriptor.generation,
        )
    raw_values = service.resolved_instance_config(instance_id)
    url = raw_values.get("napcat_url")
    if not isinstance(url, str) or not url.strip():
        raise QqAdminError(
            503,
            "napcat_not_configured",
            f"QQ instance {instance_id!r} has no NapCat URL",
        )
    return _NapcatEndpoint(
        url=url.rstrip("/"),
        access_token=_resolve_secret(raw_values.get("napcat_access_token")),
        url_source="instance_config",
        managed=False,
    )


def _safe_diagnostic_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        if not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))
    except ValueError:
        return None


def _onebot_websocket_server_from_instance_config(
    instance_id: str,
    config: Mapping[str, Any],
    *,
    endpoint: _NapcatEndpoint,
) -> dict[str, Any]:
    desired = _onebot_websocket_server_from_config(
        {"channels": {"qq": dict(config)}}
    )
    desired["name"] = f"corlinman-{instance_id}"
    if endpoint.managed:
        # The manager descriptor owns the actual token. Config snapshots never
        # carry it, so reconnect leaves the existing managed OneBot token alone
        # unless the instance runtime supplies one through its private config.
        desired["token"] = str(config.get("access_token") or "")
    return desired


async def _probe_napcat_diagnostics_for_instance(
    state: Any,
    instance_id: str,
    *,
    client_factory: Any = None,
) -> NapcatDiagnosticsOut:
    endpoint = await _resolve_napcat_endpoint_for_instance(state, instance_id)
    client_factory = client_factory or _NapcatClient
    issues: list[str] = []
    actions: list[str] = []
    credential = "missing_token" if not endpoint.access_token else "unknown"
    qrcode_api = "unknown"
    onebot_config_api = "unknown"
    if not endpoint.access_token:
        issues.append("napcat_webui_token_missing")
        actions.append("set_napcat_webui_token")
    assert endpoint.url is not None
    async with client_factory(endpoint.url, endpoint.access_token) as client:
        if endpoint.access_token:
            try:
                credential = "ok" if await client.get_credential() else "missing_token"
            except Exception:
                credential = "failed"
                issues.append("napcat_credential_failed")
        try:
            await client._fetch_qrcode()
        except NapcatError as exc:
            qrcode_api = "unreachable" if exc.code == "napcat_unreachable" else "failed"
            _append_unique(issues, exc.code)
        else:
            qrcode_api = "ok"
        try:
            await client.post(OB11_CONFIG_GET_PATH, {})
        except NapcatError as exc:
            onebot_config_api = (
                "unreachable" if exc.code == "napcat_unreachable" else "failed"
            )
            _append_unique(issues, exc.code)
        else:
            onebot_config_api = "ok"
    if "napcat_unreachable" in issues:
        actions.append(
            "restart_managed_napcat"
            if endpoint.managed
            else "check_external_napcat_url"
        )
    return NapcatDiagnosticsOut(
        mode=endpoint.mode,
        url=_safe_diagnostic_url(endpoint.url),
        url_source=endpoint.url_source,
        managed=endpoint.managed,
        auth_configured=bool(endpoint.access_token),
        credential=credential,
        qrcode_api=qrcode_api,
        onebot_config_api=onebot_config_api,
        issues=issues,
        actions=actions,
    )


def _resolve_napcat_url(cfg: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(url, access_token)``.

    Resolution order:

    1. ``[channels.qq].napcat_url`` from config.
    2. ``CORLINMAN_NAPCAT_URL`` env (set by docker-compose.qq.yml /
       the native systemd unit).
    3. ``DEFAULT_NAPCAT_URL`` (``http://127.0.0.1:6099``) — the loopback NapCat
       both deploy modes provision, so a native install with QQ on resolves
       without any manual config. ``url`` is therefore never ``None`` now; the
       ``None`` arm is kept for callers/tests that pass an explicit override.

    If NapCat is genuinely unreachable at the resolved URL, the
    ``_NapcatClient`` raises a typed ``napcat_unreachable`` (503) on first call
    — distinct from the old "not configured" 503, and the correct signal.
    """
    endpoint = _resolve_napcat_endpoint(cfg)
    return endpoint.url, endpoint.access_token


def _onebot_ws_port_from_config(cfg: dict[str, Any]) -> int:
    qq = ((cfg.get("channels") or {}).get("qq")) or {}
    ws_url = qq.get("ws_url") or os.environ.get("QQ_WS_URL") or ""
    if not isinstance(ws_url, str) or not ws_url.strip():
        return DEFAULT_ONEBOT_WS_PORT
    try:
        parsed = urlparse(ws_url)
        if parsed.port is not None:
            return parsed.port
    except ValueError:
        return DEFAULT_ONEBOT_WS_PORT
    return DEFAULT_ONEBOT_WS_PORT


def _onebot_websocket_server_from_config(cfg: dict[str, Any]) -> dict[str, Any]:
    qq = ((cfg.get("channels") or {}).get("qq")) or {}
    return {
        "enable": True,
        "name": ONEBOT_WS_SERVER_NAME,
        "host": "0.0.0.0",
        "port": _onebot_ws_port_from_config(cfg),
        "messagePostFormat": "array",
        "reportSelfMessage": False,
        "enableForcePushEvent": True,
        "token": _resolve_secret(qq.get("access_token")) or "",
        "debug": False,
        "heartInterval": 30000,
    }


def _resolve_data_dir(state: AdminState, cfg: dict[str, Any]) -> Path:
    if state.data_dir is not None:
        return state.data_dir
    server = cfg.get("server") or {}
    if isinstance(server.get("data_dir"), str):
        return Path(server["data_dir"])
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".corlinman"


def _accounts_path(state: AdminState, cfg: dict[str, Any]) -> Path:
    return _resolve_data_dir(state, cfg) / ACCOUNTS_FILE


def _accounts_path_for_instance(
    state: Any,
    instance_id: str,
    *,
    default_instance: bool,
) -> Path:
    """Keep legacy singleton history on ``default``; isolate every other ID."""
    del default_instance
    data_dir = getattr(state, "data_dir", None)
    if data_dir is None:
        data_dir = Path(os.environ.get("CORLINMAN_DATA_DIR") or Path.home() / ".corlinman")
    root = Path(data_dir)
    if instance_id == "default":
        return root / ACCOUNTS_FILE
    return root / "qq-accounts" / f"{instance_id}.json"


def _classify_qr(qr: str) -> tuple[str | None, str | None]:
    trimmed = qr.strip()
    if trimmed.startswith("http://") or trimmed.startswith("https://"):
        return None, trimmed
    for prefix in ("data:image/png;base64,", "data:image/jpeg;base64,"):
        if trimmed.startswith(prefix):
            return trimmed[len(prefix):], None
    return trimmed, None


def _extract_ok_data(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise NapcatError("napcat_bad_response", "non-object envelope")
    code = body.get("code", -1)
    if code != 0:
        message = str(body.get("message") or "napcat returned a non-zero code")
        error_code = (
            "napcat_not_logged_in"
            if "not login" in message.lower()
            else "napcat_app_error"
        )
        raise NapcatError(error_code, message)
    if "data" not in body:
        raise NapcatError("napcat_bad_response", "missing data field")
    data = body.get("data")
    if data is None:
        return {}
    return data if isinstance(data, dict) else {"value": data}


def _parse_account(data: dict[str, Any]) -> QqAccount | None:
    uin = data.get("uin")
    if uin is None:
        return None
    uin = str(uin)
    nickname = data.get("nick") or data.get("nickName")
    avatar = data.get("avatarUrl") or data.get("avatar")
    return QqAccount(
        uin=uin,
        nickname=nickname if isinstance(nickname, str) else None,
        avatar_url=avatar if isinstance(avatar, str) else None,
        last_login_at=_now_ms(),
    )


class _NapcatClient:
    def __init__(self, base_url: str, access_token: str | None):
        self.base_url = base_url
        self.access_token = access_token
        self._client = httpx.AsyncClient(timeout=NAPCAT_TIMEOUT)
        self._credential: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> _NapcatClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _login(self) -> str | None:
        if not self.access_token:
            return None
        if self._credential is not None:
            return self._credential
        h = hashlib.sha256()
        h.update(self.access_token.encode("utf-8"))
        h.update(b".napcat")
        hash_hex = h.hexdigest()
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/auth/login",
                json={"hash": hash_hex},
            )
        except httpx.HTTPError as exc:
            raise NapcatError("napcat_unreachable", str(exc), status=503) from exc
        if resp.status_code >= 400:
            raise NapcatError(
                "napcat_unreachable", resp.text, status=503
            )
        try:
            data = _extract_ok_data(resp.json())
        except json_lib.JSONDecodeError as exc:
            raise NapcatError("napcat_bad_response", str(exc)) from exc
        credential = data.get("Credential")
        if not credential:
            raise NapcatError("napcat_bad_response", "missing data.Credential")
        self._credential = str(credential)
        return self._credential

    async def get_credential(self) -> str | None:
        """Public accessor for the exchanged WebUI Bearer credential.

        Returns ``None`` when no ``access_token`` is configured (NapCat
        WebUI then runs unauthenticated). Performs the token -> Credential
        exchange (cached for the client's lifetime) via :meth:`_login`.
        """
        return await self._login()

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        credential = await self._login()
        headers = {"Authorization": f"Bearer {credential}"} if credential else {}
        try:
            resp = await self._client.post(
                f"{self.base_url}{path}", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise NapcatError("napcat_unreachable", str(exc), status=503) from exc
        if resp.status_code >= 400:
            raise NapcatError(
                "napcat_upstream_error",
                resp.text,
                status=502,
            )
        try:
            payload = resp.json()
        except json_lib.JSONDecodeError as exc:
            raise NapcatError("napcat_bad_response", str(exc)) from exc
        return _extract_ok_data(payload)

    async def _fetch_qrcode(self) -> str:
        data = await self.post(QQ_QRCODE_GET_PATH, {})
        qr = data.get("qrcode")
        if not isinstance(qr, str):
            raise NapcatError("napcat_bad_response", "missing data.qrcode")
        return qr

    async def _wait_for_qrcode_change(
        self,
        previous_qr: str | None,
        *,
        attempts: int,
        interval_s: float,
    ) -> str | None:
        last_error: NapcatError | None = None
        for attempt in range(max(attempts, 1)):
            try:
                qr = await self._fetch_qrcode()
            except NapcatError as exc:
                last_error = exc
            else:
                if previous_qr is None or qr != previous_qr:
                    return qr
            if attempt < attempts - 1 and interval_s > 0:
                await asyncio.sleep(interval_s)
        if previous_qr is None and last_error is not None:
            raise last_error
        return None

    async def _restart_napcat_for_qrcode_refresh(self) -> None:
        try:
            await self.post(QQ_NAPCAT_RESTART_PATH, {})
        except NapcatError as exc:
            if exc.code not in {"napcat_unreachable", "napcat_upstream_error"}:
                raise NapcatError(
                    "napcat_qrcode_refresh_noop",
                    f"NapCat restart fallback for QR refresh failed: {exc}",
                    status=exc.upstream_status,
                ) from exc
        finally:
            self._credential = None

    async def _recover_qrcode_without_previous(self, original: NapcatError) -> str:
        """Recover when NapCat refuses to hand out any login QR at all.

        NapCat can wedge itself after a session drop: the login manager keeps
        answering ``GetQQLoginQrcode`` / ``RefreshQRcode`` with
        ``{"code":-1,"message":"QQ Is Logined"}`` while ``CheckLoginStatus``
        simultaneously reports ``isLogin: false, isOffline: true``. Only a
        NapCat restart clears that state. Distinguish it from a *genuine*
        logged-in session (where minting a login QR is meaningless) via
        ``CheckLoginStatus`` before restarting.
        """
        try:
            status = await self.check_status()
        except NapcatError:
            raise original from None
        if status.status == "confirmed":
            raise NapcatError(
                "napcat_already_logged_in",
                "QQ is already logged in; there is no login QR code to refresh",
                status=409,
            ) from original
        await self._restart_napcat_for_qrcode_refresh()
        qr = await self._wait_for_qrcode_change(
            None,
            attempts=NAPCAT_QRCODE_RESTART_RETRY_COUNT,
            interval_s=NAPCAT_QRCODE_RESTART_WAIT_S,
        )
        if qr is None:
            raise NapcatError(
                "napcat_qrcode_refresh_noop",
                (
                    "NapCat reported a login state that blocks QR refresh "
                    f"({original}); restart fallback did not produce a login QR"
                ),
            )
        return qr

    async def request_qrcode(self) -> QrcodeOut:
        previous_qr: str | None = None
        try:
            previous_qr = await self._fetch_qrcode()
        except NapcatError:
            # NapCat can briefly have no QR during boot/login recovery. The
            # refresh call below is the path that asks it to mint one.
            previous_qr = None

        qr: str | None = None
        try:
            await self.post(QQ_QRCODE_REFRESH_PATH, {})
        except NapcatError as exc:
            if previous_qr is None:
                qr = await self._recover_qrcode_without_previous(exc)

        if qr is None:
            try:
                qr = await self._wait_for_qrcode_change(
                    previous_qr,
                    attempts=NAPCAT_QRCODE_RETRY_COUNT,
                    interval_s=NAPCAT_QRCODE_RETRY_INTERVAL_S,
                )
            except NapcatError as exc:
                # Only raises when previous_qr is None and every fetch failed
                # (refresh itself succeeded) — same wedge, same recovery.
                qr = await self._recover_qrcode_without_previous(exc)
            if qr is None and previous_qr is not None:
                await self._restart_napcat_for_qrcode_refresh()
                qr = await self._wait_for_qrcode_change(
                    previous_qr,
                    attempts=NAPCAT_QRCODE_RESTART_RETRY_COUNT,
                    interval_s=NAPCAT_QRCODE_RESTART_WAIT_S,
                )
        if qr is None:
            raise NapcatError(
                "napcat_qrcode_refresh_noop",
                (
                    "NapCat accepted QR refresh but kept returning the same "
                    "login QR code after restart fallback"
                ),
            )

        image, url = _classify_qr(qr)
        return QrcodeOut(
            token=str(uuid.uuid4()),
            image_base64=image,
            qrcode_url=url,
            expires_at=_now_ms() + 120_000,
        )

    async def check_status(self) -> StatusOut:
        data = await self.post("/api/QQLogin/CheckLoginStatus", {})
        if data.get("isLogin"):
            return StatusOut(status="confirmed", account=_parse_account(data))
        qr_url = data.get("qrcodeurl") or ""
        return StatusOut(status="expired" if not qr_url else "waiting")

    async def quick_login(self, uin: str) -> StatusOut:
        data = await self.post("/api/QQLogin/SetQuickLogin", {"uin": uin})
        is_login = data.get("isLogin", True)
        account = _parse_account(data) or QqAccount(
            uin=uin, last_login_at=_now_ms()
        )
        return StatusOut(
            status="confirmed" if is_login else "error",
            account=account,
        )


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


async def _probe_napcat_diagnostics(
    cfg: dict[str, Any],
    *,
    client_factory: Any = _NapcatClient,
) -> NapcatDiagnosticsOut:
    endpoint = _resolve_napcat_endpoint(cfg)
    issues: list[str] = []
    actions: list[str] = []
    credential = "missing_token" if not endpoint.access_token else "unknown"
    qrcode_api = "unknown"
    onebot_config_api = "unknown"

    if endpoint.url is None:
        return NapcatDiagnosticsOut(
            mode=endpoint.mode,
            url=None,
            url_source=endpoint.url_source,
            managed=endpoint.managed,
            auth_configured=False,
            credential="missing_token",
            qrcode_api="unreachable",
            onebot_config_api="unreachable",
            issues=["napcat_url_missing"],
            actions=["set_napcat_url"],
        )

    if not endpoint.access_token:
        _append_unique(issues, "napcat_webui_token_missing")
        _append_unique(actions, "set_napcat_webui_token")

    async with client_factory(endpoint.url, endpoint.access_token) as client:
        if endpoint.access_token:
            try:
                cred = await client.get_credential()
            except NapcatError as exc:
                credential = "failed"
                _append_unique(issues, exc.code)
            except Exception:
                credential = "failed"
                _append_unique(issues, "napcat_credential_failed")
            else:
                credential = "ok" if cred else "missing_token"
                if not cred:
                    _append_unique(issues, "napcat_credential_missing")

        try:
            await client._fetch_qrcode()
        except NapcatError as exc:
            qrcode_api = (
                "unreachable" if exc.code == "napcat_unreachable" else "failed"
            )
            _append_unique(issues, exc.code)
        except Exception:
            qrcode_api = "failed"
            _append_unique(issues, "napcat_qrcode_probe_failed")
        else:
            qrcode_api = "ok"

        try:
            await client.post(OB11_CONFIG_GET_PATH, {})
        except NapcatError as exc:
            onebot_config_api = (
                "unreachable" if exc.code == "napcat_unreachable" else "failed"
            )
            _append_unique(issues, exc.code)
        except Exception:
            onebot_config_api = "failed"
            _append_unique(issues, "napcat_onebot_config_probe_failed")
        else:
            onebot_config_api = "ok"

    if "napcat_unreachable" in issues:
        _append_unique(
            actions,
            "restart_managed_napcat"
            if endpoint.managed
            else "check_external_napcat_url",
        )
    if qrcode_api == "failed":
        _append_unique(actions, "check_napcat_qrcode_api")
    if onebot_config_api == "failed":
        _append_unique(actions, "check_napcat_onebot_api")

    return NapcatDiagnosticsOut(
        mode=endpoint.mode,
        url=endpoint.url,
        url_source=endpoint.url_source,
        managed=endpoint.managed,
        auth_configured=bool(endpoint.access_token),
        credential=credential,
        qrcode_api=qrcode_api,
        onebot_config_api=onebot_config_api,
        issues=issues,
        actions=actions,
    )


def _matches_onebot_server(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    return all(current.get(key) == value for key, value in desired.items())


def _same_onebot_server(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    if current.get("name") == desired["name"]:
        return True
    current_port = current.get("port")
    desired_port = desired.get("port")
    if current_port is None or desired_port is None:
        return False
    try:
        return int(current_port) == int(desired_port)
    except (TypeError, ValueError):
        return False


async def _ensure_onebot_websocket_server(
    client: Any, desired_server: dict[str, Any]
) -> bool:
    config = await client.post(OB11_CONFIG_GET_PATH, {})
    network = config.get("network")
    if not isinstance(network, dict):
        network = {}
        config["network"] = network
    servers = network.get("websocketServers")
    if not isinstance(servers, list):
        servers = []
        network["websocketServers"] = servers

    changed = False
    for idx, item in enumerate(servers):
        if not isinstance(item, dict):
            continue
        if not _same_onebot_server(item, desired_server):
            continue
        if _matches_onebot_server(item, desired_server):
            return False
        servers[idx] = {**item, **desired_server}
        changed = True
        break
    if not changed:
        servers.append(dict(desired_server))
        changed = True

    await client.post(
        OB11_CONFIG_SET_PATH,
        {"config": json_lib.dumps(config, ensure_ascii=False)},
    )
    return True


async def _ensure_onebot_websocket_server_for_config(cfg: dict[str, Any]) -> bool:
    url, token = _resolve_napcat_url(cfg)
    if url is None:
        return False
    async with _NapcatClient(url, token) as client:
        return await _ensure_onebot_websocket_server(
            client,
            _onebot_websocket_server_from_config(cfg),
        )


async def _ensure_onebot_websocket_server_silent(cfg: dict[str, Any]) -> None:
    try:
        await _ensure_onebot_websocket_server_for_config(cfg)
    except Exception:
        pass


def _schedule_onebot_websocket_server_ensure(cfg: dict[str, Any]) -> bool:
    desired = _onebot_websocket_server_from_config(cfg)
    url, _token = _resolve_napcat_url(cfg)
    key = f"{url or ''}:{desired['port']}"
    now = time.monotonic()
    last = _ONEBOT_ENSURE_LAST_ATTEMPT.get(key, 0.0)
    if now - last < ONEBOT_ENSURE_MIN_INTERVAL_S:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _ONEBOT_ENSURE_LAST_ATTEMPT[key] = now
    task = loop.create_task(
        _ensure_onebot_websocket_server_silent(cfg),
        name="napcat-onebot-ensure",
    )
    _ONEBOT_ENSURE_TASKS.add(task)
    task.add_done_callback(_ONEBOT_ENSURE_TASKS.discard)
    return True


# ---------------------------------------------------------------------------
# Server-side WebUI credential injection (nginx ``auth_request`` seam)
# ---------------------------------------------------------------------------
#
# The admin UI embeds NapCat's first-party WebUI (``<iframe src="/webui">``).
# NapCat's WebUI authenticates client-side: it exchanges a URL ``?token=`` for
# a short Credential it stashes in ``localStorage`` and sends as a Bearer on
# its ``/api/*`` calls. That breaks intermittently — a stale/expired Credential
# left in the browser (e.g. after NapCat rotated its signing secret) makes the
# WebUI land unauthenticated and every ``获取QQ列表`` call returns
# ``{"code":-1,"message":"Unauthorized"}``.
#
# To make it robust we let the gateway mint the Credential server-side and the
# reverse proxy inject it as the ``Authorization`` header on every NapCat
# ``/api/*`` request (via nginx ``auth_request`` -> ``/internal/napcat-credential``).
# The browser's stored Credential becomes irrelevant. The endpoint is gated by
# ``require_admin`` (the napcat router dependency) so the Credential never leaks
# to a non-admin, even though the gateway also listens on a public port.

_NAPCAT_CRED_TTL_S = 60.0
#: Endpoint/token-fingerprint keyed cache. The ``value``/``exp`` compatibility
#: keys remain for tests and the singleton default auth_request seam.
_NAPCAT_CRED_CACHE: dict[str, Any] = {"value": "", "exp": 0.0, "entries": {}}


def _credential_cache_key(url: str, token: str) -> str:
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return f"{url.rstrip('/')}:{fingerprint}"


async def _cached_napcat_credential_for_endpoint(
    url: str | None,
    token: str | None,
) -> str:
    if not url or not token:
        return ""
    now = time.time()
    entries = _NAPCAT_CRED_CACHE.setdefault("entries", {})
    if not isinstance(entries, dict):
        entries = {}
        _NAPCAT_CRED_CACHE["entries"] = entries
    key = _credential_cache_key(url, token)
    cached = entries.get(key)
    if isinstance(cached, dict) and cached.get("value") and now < cached.get("exp", 0.0):
        return str(cached["value"])
    try:
        async with _NapcatClient(url, token) as client:
            credential = await client.get_credential() or ""
    except Exception:  # noqa: BLE001 — proxy auth degrades to the legacy path
        credential = ""
    entries[key] = {"value": credential, "exp": now + _NAPCAT_CRED_TTL_S}
    return credential


async def _cached_napcat_credential() -> str:
    """Return a (cached) NapCat WebUI Bearer credential, or ``""`` if none.

    Cached for :data:`_NAPCAT_CRED_TTL_S` seconds (the credential is stable);
    refreshed lazily on expiry. Never raises — a failure to reach NapCat or a
    missing ``access_token`` yields ``""`` so the proxy degrades to the
    WebUI's own (legacy) auth path rather than erroring.
    """
    now = time.time()
    cached = _NAPCAT_CRED_CACHE
    if cached["value"] and now < cached["exp"]:
        return str(cached["value"])
    cfg = dict(config_snapshot())
    url, token = _resolve_napcat_url(cfg)
    # The singleton compatibility seam keeps its historical top-level cache
    # contract. Canonical instance callers use the endpoint-keyed helper above.
    credential = ""
    if url and token:
        try:
            async with _NapcatClient(url, token) as client:
                credential = await client.get_credential() or ""
        except Exception:  # noqa: BLE001 — never fail auth_request over a credential
            credential = ""
    cached["value"] = credential
    cached["exp"] = now + _NAPCAT_CRED_TTL_S
    return credential


# ---------------------------------------------------------------------------
# Accounts file helpers
# ---------------------------------------------------------------------------


_ACCOUNTS_LOCK = asyncio.Lock()


async def _load_accounts(path: Path) -> list[QqAccount]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
        raw = json_lib.loads(text)
    except (OSError, json_lib.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[QqAccount] = []
    for item in raw:
        if isinstance(item, dict) and "uin" in item:
            out.append(
                QqAccount(
                    uin=str(item["uin"]),
                    nickname=item.get("nickname"),
                    avatar_url=item.get("avatar_url"),
                    last_login_at=int(item.get("last_login_at", 0) or 0),
                )
            )
    return out


async def _upsert_account(path: Path, acct: QqAccount) -> None:
    async with _ACCOUNTS_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = await _load_accounts(path)
        out: list[QqAccount] = []
        updated = False
        for a in existing:
            if a.uin == acct.uin:
                out.append(
                    QqAccount(
                        uin=a.uin,
                        nickname=acct.nickname or a.nickname,
                        avatar_url=acct.avatar_url or a.avatar_url,
                        last_login_at=acct.last_login_at,
                    )
                )
                updated = True
            else:
                out.append(a)
        if not updated:
            out.append(acct)
        out.sort(key=lambda a: a.last_login_at, reverse=True)
        tmp = path.with_suffix(path.suffix + ".new")
        tmp.write_text(
            json_lib.dumps([a.model_dump() for a in out], indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
