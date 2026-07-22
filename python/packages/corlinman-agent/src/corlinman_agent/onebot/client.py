"""Async OneBot v11 HTTP client.

Talks to a running NapCat / Lagrange.Core instance via its HTTP action
endpoint (``POST {base}/{action}`` with a JSON body). Used by the
``qzone_publish`` tool to borrow the QQ login state — cookies + uin —
without re-implementing the QR-login dance.

Configuration precedence
------------------------
1. **Explicit constructor args** — ``OneBotClient(base_url=..., access_token=...)``.
2. **Env vars** — ``CORLINMAN_NAPCAT_HTTP_URL`` + ``CORLINMAN_NAPCAT_ACCESS_TOKEN``.
3. **Derived from ``channels.qq.ws_url``** — when neither of the above
   resolves, callers can pass ``ws_url=`` and the constructor flips
   ``ws://`` → ``http://`` (``wss://`` → ``https://``) and drops a
   trailing ``/onebot`` path segment if present. This is the docker-
   compose default: NapCat exposes the same host:port for both the WS
   client and the HTTP action API.

Env vars
--------
* ``CORLINMAN_NAPCAT_HTTP_URL`` — base URL of the OneBot HTTP API
  (e.g. ``http://127.0.0.1:3000``).
* ``CORLINMAN_NAPCAT_ACCESS_TOKEN`` — optional bearer token if NapCat's
  HTTP server has ``access-token`` configured.
* ``CORLINMAN_NAPCAT_HTTP_TIMEOUT_SECS`` — per-request timeout in
  seconds; defaults to ``10``.

Failures
--------
* Transport errors (ConnectError, TimeoutException, …) surface as
  :class:`OneBotError` with a clear message.
* Non-2xx responses surface as :class:`OneBotError` with status + body.
* OneBot ``status:"failed"`` envelopes surface as :class:`OneBotError`
  with ``retcode`` + ``message``.
"""

from __future__ import annotations

import asyncio
import json
import os
from itertools import count
from pathlib import Path
from typing import Any

import httpx
import structlog
from websockets.asyncio.client import connect as ws_connect

logger = structlog.get_logger(__name__)


__all__ = [
    "OneBotClient",
    "OneBotError",
]


#: Default per-request HTTP timeout in seconds. Tuned for the small
#: request/reply pattern the QZone tool uses — anything longer than ~10s
#: is almost certainly NapCat itself being unreachable rather than a
#: slow QQ login lookup.
_DEFAULT_TIMEOUT_SECS: float = 10.0
_ECHO_COUNTER = count()


class OneBotError(RuntimeError):
    """Raised on any failure path of an :class:`OneBotClient` call.

    Covers three distinct failure modes — all surface as one type so
    callers (tool dispatchers) can fold them into a single error
    envelope without caring which layer broke:

    1. Transport failure (NapCat unreachable, timed out).
    2. Non-2xx HTTP response.
    3. OneBot envelope reports ``status: "failed"``.
    """


def _env_timeout() -> float:
    """Read ``CORLINMAN_NAPCAT_HTTP_TIMEOUT_SECS`` as a float, falling
    back to :data:`_DEFAULT_TIMEOUT_SECS` on missing / unparseable
    values. A non-positive value also falls back so a misconfigured 0
    doesn't silently turn every call into an immediate timeout."""
    raw = os.environ.get("CORLINMAN_NAPCAT_HTTP_TIMEOUT_SECS")
    if raw is None or not raw.strip():
        return _DEFAULT_TIMEOUT_SECS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SECS
    return value if value > 0 else _DEFAULT_TIMEOUT_SECS


def _derive_http_from_ws(ws_url: str) -> str:
    """Turn a NapCat WebSocket URL into the matching HTTP base URL.

    Rules (mirrored from the docker-compose / install.sh defaults):

    * ``ws://`` → ``http://``; ``wss://`` → ``https://``. Anything else
      is returned unchanged so an operator who already configured an
      HTTP URL inline isn't silently mangled.
    * A trailing ``/onebot`` path segment (the default NapCat WS path
      when an HTTP server runs alongside) is dropped — NapCat serves the
      action API at the root.
    * Trailing ``/`` stripped so ``f"{base}/{action}"`` builds cleanly.
    """
    url = (ws_url or "").strip()
    if not url:
        return ""
    lower = url.lower()
    if lower.startswith("wss://"):
        url = "https://" + url[len("wss://") :]
    elif lower.startswith("ws://"):
        url = "http://" + url[len("ws://") :]
    # Drop common path suffix(es). Walk in order so /onebot/v11 / /onebot
    # both collapse to the bare host:port.
    for suffix in ("/onebot/v11", "/onebot"):
        if url.lower().endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


def _resolve_base_url(
    *, explicit: str | None, ws_url: str | None
) -> str:
    """Apply the documented precedence to pick a base URL.

    Precedence: explicit > ``CORLINMAN_NAPCAT_HTTP_URL`` env > derived
    from ``ws_url``. Returns ``""`` when nothing resolves so the
    constructor can raise a clear error.
    """
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")
    env_url = os.environ.get("CORLINMAN_NAPCAT_HTTP_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    if ws_url and ws_url.strip():
        return _derive_http_from_ws(ws_url)
    return ""


def _resolve_token(*, explicit: str | None) -> str:
    """Apply the documented precedence to pick an access token.

    Precedence: explicit > ``CORLINMAN_NAPCAT_ACCESS_TOKEN`` env > "".
    """
    if explicit and explicit.strip():
        return explicit.strip()
    return os.environ.get("CORLINMAN_NAPCAT_ACCESS_TOKEN", "").strip()


def _sidecar_onebot_config() -> tuple[str, str]:
    """Read the gateway-emitted QQ action transport from py-config.json."""
    raw_path = os.environ.get("CORLINMAN_PY_CONFIG", "").strip()
    if not raw_path:
        return "", ""
    try:
        raw = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return "", ""
    section = raw.get("qq_onebot") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return "", ""
    ws_url = section.get("ws_url")
    access_token = section.get("access_token")
    return (
        ws_url.strip() if isinstance(ws_url, str) else "",
        access_token.strip() if isinstance(access_token, str) else "",
    )


class OneBotClient:
    """Async HTTP client for the OneBot v11 action API.

    Each method posts to ``{base}/{action}`` with a JSON body and
    unwraps the standard OneBot envelope::

        {"status": "ok", "retcode": 0, "data": {...}}

    Re-uses one :class:`httpx.AsyncClient` instance across calls so a
    long-lived dispatcher (one client per servicer) avoids per-call
    TCP setup. Closing is opt-in via :meth:`aclose` — the tool
    dispatcher constructs the client lazily and lets the process
    lifecycle clean up the underlying transport.

    Parameters
    ----------
    base_url
        Explicit base URL override. When ``None`` the constructor falls
        back to ``CORLINMAN_NAPCAT_HTTP_URL``, then to ``ws_url``
        derivation.
    access_token
        Explicit bearer token override. When ``None`` the constructor
        falls back to ``CORLINMAN_NAPCAT_ACCESS_TOKEN``.
    ws_url
        Optional ``channels.qq.ws_url`` — used as the last-ditch
        fallback when no HTTP URL is configured. The docker-compose
        default sets only the WS URL; this lets ``qzone_publish`` work
        out-of-the-box on that deployment.
    timeout
        Per-request timeout in seconds. ``None`` reads from
        ``CORLINMAN_NAPCAT_HTTP_TIMEOUT_SECS`` / falls back to 10s.
    transport
        Optional :mod:`httpx` test seam — production callers leave
        ``None``.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        access_token: str | None = None,
        ws_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        sidecar_ws_url, sidecar_token = _sidecar_onebot_config()
        resolved_ws_url = (ws_url or "").strip() or sidecar_ws_url
        self._ws_url = resolved_ws_url
        resolved_base = _resolve_base_url(
            explicit=base_url, ws_url=resolved_ws_url
        )
        if not resolved_base:
            raise OneBotError(
                "OneBot transport not configured — set "
                "CORLINMAN_NAPCAT_HTTP_URL, pass base_url/ws_url explicitly, "
                "or emit qq_onebot.ws_url in CORLINMAN_PY_CONFIG"
            )
        self._base_url: str = resolved_base
        self._token: str = _resolve_token(
            explicit=access_token or sidecar_token
        )
        self._timeout: float = (
            timeout if timeout is not None and timeout > 0 else _env_timeout()
        )
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        client_kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "headers": headers,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    @property
    def base_url(self) -> str:
        """The resolved OneBot HTTP base URL (no trailing slash)."""
        return self._base_url

    async def aclose(self) -> None:
        """Close the underlying :class:`httpx.AsyncClient`."""
        await self._client.aclose()

    async def __aenter__(self) -> OneBotClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Low-level action call
    # ------------------------------------------------------------------

    async def _call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        """Call one OneBot action and return the ``data`` field.

        Prefer HTTP when configured. If a deployment exposes only NapCat's
        forward WebSocket action API, an HTTP 404/426 or transport failure
        falls back to a short-lived WS request carrying a unique ``echo``.
        """
        body = params or {}
        payload: Any
        url = f"{self._base_url}/{action}"
        try:
            response = await self._client.post(url, json=body)
            if response.status_code >= 400:
                raise OneBotError(
                    f"OneBot action {action!r} returned HTTP "
                    f"{response.status_code}"
                )
            payload = response.json()
        except (httpx.HTTPError, ValueError, OneBotError) as exc:
            if not self._ws_url:
                raise OneBotError(
                    f"OneBot action {action!r} HTTP transport failed"
                ) from exc
            payload = await self._call_ws(action, body, http_error=exc)

        if not isinstance(payload, dict):
            raise OneBotError(
                f"OneBot action {action!r} returned a non-object response."
            )
        status = payload.get("status")
        if status != "ok":
            # NapCat surfaces failures with either ``message`` or
            # ``wording``; ``retcode`` is the OneBot v11 numeric code.
            msg = (
                payload.get("message")
                or payload.get("wording")
                or "unknown error"
            )
            raise OneBotError(
                f"OneBot action {action!r} failed: {msg} "
                f"(retcode={payload.get('retcode')})"
            )
        data = payload.get("data")
        if data is None:
            raise OneBotError(
                f"OneBot action {action!r} returned no data field."
            )
        return data

    async def _call_ws(
        self,
        action: str,
        params: dict[str, Any],
        *,
        http_error: Exception,
    ) -> Any:
        echo = f"corlinman-agent:{id(self)}:{next(_ECHO_COUNTER)}:{action}"
        headers = (
            [("Authorization", f"Bearer {self._token}")]
            if self._token
            else None
        )
        frame = json.dumps(
            {"action": action, "params": params, "echo": echo},
            ensure_ascii=False,
        )
        try:
            async with ws_connect(
                self._ws_url,
                additional_headers=headers,
                open_timeout=self._timeout,
                close_timeout=2,
                ping_interval=None,
            ) as websocket:
                await websocket.send(frame)
                async with asyncio.timeout(self._timeout):
                    async for raw in websocket:
                        if not isinstance(raw, str):
                            continue
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(payload, dict) and payload.get("echo") == echo:
                            return payload
        except Exception as exc:  # noqa: BLE001 — transport boundary
            # Preserve no upstream body/snippet; callers log only the stable
            # action-level error while retaining the HTTP cause for debugging.
            raise OneBotError(
                f"OneBot action {action!r} WebSocket transport failed"
            ) from exc
        raise OneBotError(
            f"OneBot action {action!r} returned no matching WebSocket response"
        ) from http_error

    # ------------------------------------------------------------------
    # High-level QZone helpers
    # ------------------------------------------------------------------

    async def fetch_login_info(self) -> dict[str, Any]:
        """Return ``{user_id, nickname, ...}`` for the logged-in QQ.

        Maps to the OneBot v11 ``get_login_info`` action. Raises
        :class:`OneBotError` when the QQ login state is missing
        (``user_id`` absent or 0).
        """
        data = await self._call("get_login_info")
        if not isinstance(data, dict):
            raise OneBotError(
                "OneBot get_login_info returned a non-object payload."
            )
        user_id = data.get("user_id")
        if not user_id:
            raise OneBotError(
                "OneBot get_login_info returned no user_id — the QQ "
                "client may not be logged in."
            )
        # Normalize shape: callers want both a string ``qq`` and the
        # raw numeric ``user_id`` since the OneBot envelope ships the
        # latter and QZone wants the former in URLs.
        return {
            "qq": str(user_id),
            "user_id": user_id,
            "nickname": data.get("nickname") or "",
            "raw": data,
        }

    async def fetch_cookies(self, domain: str = "user.qzone.qq.com") -> str:
        """Return the raw ``k=v; k2=v2`` cookie string for ``domain``.

        Maps to the OneBot v11 ``get_cookies`` action. The default
        domain is QZone's so callers that want QZone cookies can call
        the method with no args. Raises :class:`OneBotError` when the
        cookie string is empty (login is stale or NapCat hasn't been
        granted QZone access).
        """
        data = await self._call("get_cookies", {"domain": domain})
        cookies = data.get("cookies") if isinstance(data, dict) else data
        if not isinstance(cookies, str) or not cookies.strip():
            raise OneBotError(
                f"OneBot get_cookies for domain={domain!r} returned "
                "an empty cookie string — the QQ login state may be "
                "stale; re-login the NapCat client."
            )
        return cookies.strip()

    async def fetch_csrf_token(self) -> int:
        """Return the QQ web CSRF token (``g_tk`` / ``bkn``) as an int.

        Maps to the OneBot v11 ``get_csrf_token`` action. The QZone
        publish flow uses the value as a query-string parameter; we
        return ``int`` to match the OneBot wire shape (NapCat returns
        a JSON number). Falls back to parsing a numeric string when
        a less-spec-compliant backend ships ``"123"``.
        """
        data = await self._call("get_csrf_token")
        token = data.get("token") if isinstance(data, dict) else data
        if isinstance(token, int):
            return token
        if isinstance(token, str) and token.strip().isdigit():
            return int(token.strip())
        raise OneBotError(
            f"OneBot get_csrf_token returned unexpected token shape: "
            f"{token!r}"
        )

    async def fetch_friend_list(self) -> list[dict[str, Any]]:
        """Return the QQ friend list as a list of raw friend dicts.

        Maps to the OneBot v11 ``get_friend_list`` action. NapCat /
        Lagrange return a JSON array of ``{user_id, nickname, remark, …}``
        objects directly under ``data``; some forks wrap it one level
        deeper under ``data.friends``. Both shapes are unwrapped here so
        the QZone comment tool gets a plain list. Raises
        :class:`OneBotError` (via :meth:`_call`) on transport / envelope
        failures; a logged-in account with zero friends returns ``[]``.
        """
        data = await self._call("get_friend_list")
        if isinstance(data, dict):
            friends = data.get("friends") or data.get("data") or []
        else:
            friends = data
        if not isinstance(friends, list):
            raise OneBotError(
                "OneBot get_friend_list returned an unexpected shape: "
                f"{type(friends).__name__}"
            )
        return [f for f in friends if isinstance(f, dict)]
