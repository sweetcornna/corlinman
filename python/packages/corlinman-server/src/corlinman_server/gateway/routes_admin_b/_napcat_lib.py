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
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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
ONEBOT_WS_SERVER_NAME = "corlinman"
ONEBOT_ENSURE_MIN_INTERVAL_S = 10.0
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


# ---------------------------------------------------------------------------
# NapCat client
# ---------------------------------------------------------------------------


class NapcatError(Exception):
    """Generic NapCat call failure with optional upstream metadata."""

    def __init__(self, code: str, message: str = "", status: int | None = None):
        super().__init__(message or code)
        self.code = code
        self.upstream_status = status

    def response(self) -> JSONResponse:
        status = self.upstream_status if self.upstream_status else 502
        return JSONResponse(
            status_code=status,
            content={
                "error": self.code,
                "message": str(self),
            },
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


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
    qq = ((cfg.get("channels") or {}).get("qq")) or {}
    url = qq.get("napcat_url")
    if not url or not str(url).strip():
        url = os.environ.get("CORLINMAN_NAPCAT_URL")
    if not url or not str(url).strip():
        url = DEFAULT_NAPCAT_URL
    url = str(url).rstrip("/")
    access_token: str | None = None
    sec = qq.get("napcat_access_token")
    if isinstance(sec, dict):
        if "value" in sec:
            access_token = str(sec["value"])
        elif "env" in sec:
            access_token = os.environ.get(str(sec["env"]))
    elif isinstance(sec, str) and sec:
        access_token = sec
    if not access_token:
        access_token = os.environ.get("NAPCAT_WEBUI_TOKEN") or os.environ.get(
            "NAPCAT_WEBUI_SECRET_KEY"
        )
    return url, access_token


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
    return {
        "enable": True,
        "name": ONEBOT_WS_SERVER_NAME,
        "host": "0.0.0.0",
        "port": _onebot_ws_port_from_config(cfg),
        "messagePostFormat": "array",
        "reportSelfMessage": False,
        "enableForcePushEvent": True,
        "token": "",
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
        raise NapcatError(
            "napcat_app_error",
            str(body.get("message") or "napcat returned a non-zero code"),
        )
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

    async def request_qrcode(self) -> QrcodeOut:
        # Force a refresh — older builds will 404; we swallow that.
        try:
            await self.post("/api/QQLogin/RefreshQRcode", {})
        except NapcatError:
            pass
        data = await self.post("/api/QQLogin/GetQQLoginQrcode", {})
        qr = data.get("qrcode")
        if not isinstance(qr, str):
            raise NapcatError("napcat_bad_response", "missing data.qrcode")
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


def _matches_onebot_server(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    return all(current.get(key) == value for key, value in desired.items())


def _same_onebot_server(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    if current.get("name") == desired["name"]:
        return True
    try:
        return int(current.get("port")) == int(desired["port"])
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
#: ``{"value": <cred str>, "exp": <unix ts>}`` — process-global cache so the
#: per-request auth_request hop doesn't re-exchange against NapCat every time.
_NAPCAT_CRED_CACHE: dict[str, Any] = {"value": "", "exp": 0.0}


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
    cred = ""
    if url and token:
        try:
            async with _NapcatClient(url, token) as client:
                cred = await client.get_credential() or ""
        except Exception:  # noqa: BLE001 — never fail the proxy over a credential
            cred = ""
    cached["value"] = cred
    cached["exp"] = now + _NAPCAT_CRED_TTL_S
    return cred


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
