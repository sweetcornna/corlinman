"""微信公众号 outbound — access-token cache + customer-service send.

Companion to :mod:`corlinman_channels.wechat_official`. The inbound
adapter is webhook-driven and doesn't need network credentials beyond
the signature ``token``; this module owns the side that talks back to
``api.weixin.qq.com``.

## Access-token lifecycle

WeChat's REST API requires an ``access_token`` derived from
``appid + appsecret``:

::

    GET https://api.weixin.qq.com/cgi-bin/token
        ?grant_type=client_credential
        &appid=APPID
        &secret=APPSECRET

The response carries an ``access_token`` (2-hour TTL) and an
``expires_in`` countdown. WeChat's quota is **2000 fetches per day per
app**, so the sender single-flights refresh through an ``asyncio.Lock``
and caches the token until ~5 minutes before expiry. Matches the Feishu
``tenant_access_token`` pattern.

## Customer-service push

Within 48 hours of any inbound user message the bot can push arbitrary
messages back to the user via ``/cgi-bin/message/custom/send``. Outside
that window the API rejects the call — application logic must respect
the rule (see :ref:`wechat-passive-vs-customer-service`).

* text (``msgtype=text``) — :meth:`WeChatOfficialSender.send_text_customer`
  with 4096-char split.
* image (``msgtype=image``) — needs an uploaded ``media_id``;
  :meth:`WeChatOfficialSender.upload_temp_media` does the upload
  (3-day temp store) and :meth:`send_image_customer` pushes it.
* voice (``msgtype=voice``) — same upload→push split.
* news / video / template — out of scope for v1.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from corlinman_channels.common import ConfigError, TransportError

__all__ = [
    "DEFAULT_API_BASE",
    "MAX_TEXT_CHUNK",
    "WeChatOfficialSender",
    "split_for_send",
]

_log = logging.getLogger(__name__)

#: WeChat OAPI base. Tencent runs only one region — there's no
#: international fork to switch.
DEFAULT_API_BASE: str = "https://api.weixin.qq.com"

#: WeChat's text-payload cap for ``custom/send`` is 8192 bytes; we use
#: 4096 chars as the conservative break point (each Chinese char is 3
#: bytes UTF-8, so 4096 chars * 3 = 12288 bytes worst-case > 8192). We
#: split on this and let the receiver concat — same approach Telegram
#: uses for its 4096-char limit.
MAX_TEXT_CHUNK: int = 2048


def split_for_send(text: str, chunk: int = MAX_TEXT_CHUNK) -> list[str]:
    """Split ``text`` into ``<= chunk``-length pieces.

    Tries to break at the nearest newline / sentence boundary inside the
    last 200 chars before the hard limit so each chunk reads naturally.
    Empty / falsy input → empty list (caller should skip the send).
    """
    if not text:
        return []
    if len(text) <= chunk:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > chunk:
        # Prefer to break at a paragraph then sentence boundary inside
        # the last 200 chars of the window — feels less abrupt to the
        # user than a blind mid-word split.
        slice_end = chunk
        window = remaining[chunk - 200 : chunk] if chunk > 200 else remaining[:chunk]
        for marker in ("\n\n", "\n", "。", ". ", "! ", "? ", "！", "？"):
            idx = window.rfind(marker)
            if idx >= 0:
                slice_end = (chunk - 200 if chunk > 200 else 0) + idx + len(marker)
                break
        parts.append(remaining[:slice_end].rstrip())
        remaining = remaining[slice_end:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


class WeChatOfficialSender:
    """Outbound client for WeChat Official Account customer-service push.

    Owns:

    * the cached ``access_token`` + its expiry timestamp;
    * a single-flight refresh lock so concurrent sends don't burn the
      2000/day fetch quota;
    * the four customer-service endpoints v1 cares about: text, image,
      voice, and a ``upload_temp_media`` builder for the latter two.

    Construct with an :class:`httpx.AsyncClient` so tests can plug in an
    :class:`httpx.MockTransport`.
    """

    __slots__ = (
        "_api_base",
        "_app_id",
        "_app_secret",
        "_client",
        "_owns_client",
        "_refresh_lock",
        "_token",
        "_token_expiry",
    )

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        client: httpx.AsyncClient | None = None,
        *,
        api_base: str = DEFAULT_API_BASE,
    ) -> None:
        if not app_id:
            raise ConfigError("WeChatOfficialSender.app_id is empty")
        if not app_secret:
            raise ConfigError("WeChatOfficialSender.app_secret is empty")
        self._app_id = app_id
        self._app_secret = app_secret
        self._api_base = api_base.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._token: str = ""
        self._token_expiry: float = 0.0
        self._refresh_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the owned HTTP client (no-op when one was supplied)."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> WeChatOfficialSender:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Access-token lifecycle (single-flight)
    # ------------------------------------------------------------------

    @property
    def cached_token(self) -> str:
        """The currently-cached token. Empty string before the first fetch."""
        return self._token

    async def access_token(self) -> str:
        """Return a fresh access token, refreshing if necessary.

        Concurrent callers funnel through ``_refresh_lock`` so only ONE
        HTTP fetch ever happens per expiry window. Mirrors the Feishu
        adapter's ``_refresh_token`` cache contract — except Feishu's
        adapter owns the loop and the sender has no lock; here the
        sender owns both so we lock the refresh explicitly.
        """
        now = time.time()
        if self._token and now < self._token_expiry:
            return self._token
        async with self._refresh_lock:
            # Re-check after the lock: another waiter may have refreshed
            # while we were blocked. Single-flight contract.
            if self._token and time.time() < self._token_expiry:
                return self._token
            await self._fetch_token_locked()
            return self._token

    async def _fetch_token_locked(self) -> None:
        """Inner refresh — must be called with ``_refresh_lock`` held."""
        url = f"{self._api_base}/cgi-bin/token"
        params = {
            "grant_type": "client_credential",
            "appid": self._app_id,
            "secret": self._app_secret,
        }
        try:
            resp = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise TransportError(f"wechat token fetch failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(f"wechat token HTTP {resp.status_code}")
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(f"wechat token invalid JSON: {exc}") from exc
        if not isinstance(env, dict) or "access_token" not in env:
            code = env.get("errcode") if isinstance(env, dict) else "?"
            msg = env.get("errmsg") if isinstance(env, dict) else "?"
            raise TransportError(f"wechat token error code={code} msg={msg}")
        self._token = str(env["access_token"])
        expires = int(env.get("expires_in", 7200))
        # Refresh 5 minutes before real expiry so a slow request can't
        # race into a stale-token 40001 response.
        self._token_expiry = time.time() + max(expires - 300, 60)

    # ------------------------------------------------------------------
    # Customer-service send — text
    # ------------------------------------------------------------------

    async def send_text_customer(self, openid: str, content: str) -> None:
        """Push a text customer-service message to ``openid``.

        Splits ``content`` at :data:`MAX_TEXT_CHUNK` so one logical reply
        becomes N customer-service messages — the WeChat client renders
        them as consecutive bubbles, same as the QQ adapter's group
        ``send_group_msg`` chunking semantics.

        Returns nothing — the underlying API has no "message id" to give
        back (the customer-service envelope ack is just ``errcode=0``).
        On a non-zero ``errcode`` raises :class:`TransportError` so the
        caller can log + decide whether to retry.
        """
        if not openid:
            raise ValueError("wechat send_text_customer: openid is empty")
        if not content or not content.strip():
            return
        for chunk in split_for_send(content):
            payload: dict[str, Any] = {
                "touser": openid,
                "msgtype": "text",
                "text": {"content": chunk},
            }
            await self._customer_post(payload)

    async def send_image_customer(self, openid: str, media_id: str) -> None:
        """Push a customer-service image referencing an uploaded ``media_id``.

        ``media_id`` must come from :meth:`upload_temp_media` (or the
        permanent-media upload — not implemented here). Temp media
        expires in 3 days; the caller is responsible for re-uploading
        for delayed sends.
        """
        if not openid:
            raise ValueError("wechat send_image_customer: openid is empty")
        if not media_id:
            raise ValueError("wechat send_image_customer: media_id is empty")
        payload = {
            "touser": openid,
            "msgtype": "image",
            "image": {"media_id": media_id},
        }
        await self._customer_post(payload)

    async def send_voice_customer(self, openid: str, media_id: str) -> None:
        """Push a customer-service voice message referencing ``media_id``.

        WeChat requires the voice asset to be AMR / MP3 ≤ 60 s and
        ≤ 2 MB — :meth:`upload_temp_media` enforces neither limit; the
        upstream service does and surfaces an ``errcode`` we re-raise as
        :class:`TransportError`.
        """
        if not openid:
            raise ValueError("wechat send_voice_customer: openid is empty")
        if not media_id:
            raise ValueError("wechat send_voice_customer: media_id is empty")
        payload = {
            "touser": openid,
            "msgtype": "voice",
            "voice": {"media_id": media_id},
        }
        await self._customer_post(payload)

    # ------------------------------------------------------------------
    # Temp media upload
    # ------------------------------------------------------------------

    async def upload_temp_media(
        self,
        media_type: str,
        file_path: str | Path,
    ) -> str:
        """Upload a file to WeChat's temp-media store; return the ``media_id``.

        ``media_type`` is one of ``image`` / ``voice`` / ``video`` /
        ``thumb`` per WeChat docs. The stored asset expires in 3 days —
        suitable for one-off replies, not for content that needs to
        persist across sessions.

        Raises :class:`TransportError` on any HTTP / WeChat error.
        """
        if media_type not in ("image", "voice", "video", "thumb"):
            raise ValueError(
                f"wechat upload_temp_media: unsupported media_type={media_type!r}"
            )
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"wechat upload_temp_media: {path} missing")

        token = await self.access_token()
        url = f"{self._api_base}/cgi-bin/media/upload"
        params = {"access_token": token, "type": media_type}
        try:
            with path.open("rb") as fh:
                files = {"media": (path.name, fh, "application/octet-stream")}
                resp = await self._client.post(url, params=params, files=files)
        except httpx.HTTPError as exc:
            raise TransportError(f"wechat media upload failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(f"wechat media upload HTTP {resp.status_code}")
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(f"wechat media upload invalid JSON: {exc}") from exc
        if not isinstance(env, dict) or "media_id" not in env:
            code = env.get("errcode") if isinstance(env, dict) else "?"
            msg = env.get("errmsg") if isinstance(env, dict) else "?"
            raise TransportError(f"wechat upload errcode={code} errmsg={msg}")
        return str(env["media_id"])

    # ------------------------------------------------------------------
    # Internal POST helper for /cgi-bin/message/custom/send
    # ------------------------------------------------------------------

    async def _customer_post(self, payload: dict[str, Any]) -> None:
        """POST ``payload`` to ``/cgi-bin/message/custom/send``.

        Auto-refreshes the access token on a ``40001 invalid credential``
        ack — WeChat sometimes invalidates tokens early after a server
        rotation. One retry, then bubble up.
        """
        token = await self.access_token()
        url = f"{self._api_base}/cgi-bin/message/custom/send"
        for attempt in (1, 2):
            params = {"access_token": token}
            try:
                resp = await self._client.post(url, params=params, json=payload)
            except httpx.HTTPError as exc:
                raise TransportError(
                    f"wechat customer/send failed: {exc}"
                ) from exc
            if resp.status_code >= 400:
                raise TransportError(
                    f"wechat customer/send HTTP {resp.status_code}"
                )
            try:
                env = resp.json()
            except ValueError as exc:
                raise TransportError(
                    f"wechat customer/send invalid JSON: {exc}"
                ) from exc
            if not isinstance(env, dict):
                raise TransportError("wechat customer/send: non-object response")
            errcode = int(env.get("errcode", 0))
            if errcode == 0:
                return
            # 40001 / 42001 → token invalid / expired. Refresh once.
            if errcode in (40001, 42001) and attempt == 1:
                _log.info(
                    "wechat customer/send token invalid (code=%d), refreshing",
                    errcode,
                )
                # Force refresh by zeroing the cache.
                self._token = ""
                self._token_expiry = 0.0
                token = await self.access_token()
                continue
            raise TransportError(
                f"wechat customer/send errcode={errcode} "
                f"errmsg={env.get('errmsg', '?')}"
            )
