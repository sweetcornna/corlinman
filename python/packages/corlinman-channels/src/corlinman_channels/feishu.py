"""Feishu / Lark long-connection (WebSocket) + REST adapter.

corlinman has no Rust reference for Feishu — this is a new channel,
high-value for the project's China-region deployment. It is modelled on
:mod:`corlinman_channels.slack` (both use an outbound long-lived
WebSocket carrying an Events payload that must be acked), so the inbound
shape and the outbound :class:`FeishuSender` mirror the existing pairs.

## Transport — long connection

Feishu's "long connection" mode (长连接) lets an app receive events over
an outbound WebSocket instead of a public HTTP callback — the same NAT-
friendly story as Slack Socket Mode, which matters for a China-region
self-host. The flow:

1. ``POST /open-apis/auth/v3/tenant_access_token/internal`` exchanges the
   ``app_id`` + ``app_secret`` for a short-lived ``tenant_access_token``.
2. ``POST /callback/ws/endpoint`` (the gateway endpoint API) returns a
   single-use ``wss://`` URL.
3. The adapter dials that URL. Feishu pushes event frames; each frame
   that needs acknowledgement carries headers the adapter echoes back.
4. ``im.message.receive_v1`` events are decoded into
   :class:`InboundEvent` objects.

Outbound replies go through ``POST /open-apis/im/v1/messages`` with the
``tenant_access_token``.

We implement against the raw protocol over ``websockets`` / ``httpx``
(both already dependencies) rather than the ``lark-oapi`` SDK — the
slice corlinman needs is small and the SDK is heavyweight.

## Reply gating

Feishu messages arrive from p2p (1:1) chats or group chats. The adapter
mirrors the Telegram / Slack gate:

* ``chat_type == "p2p"`` (1:1) always responds.
* Group chats respond only when the bot is @-mentioned, unless
  ``respond_to_all``. An optional ``keyword_filter`` narrows group
  messages further (case-insensitive substring).
* The bot never replies to its own messages.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from corlinman_channels.common import (
    Attachment,
    AttachmentKind,
    ChannelBinding,
    ConfigError,
    InboundEvent,
    TransportError,
)

__all__ = [
    "DEFAULT_API_BASE",
    "FeishuAdapter",
    "FeishuConfig",
    "FeishuSender",
    "binding_from_event",
    "extract_text",
    "is_mentioning_bot",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Feishu (China) open-platform API base. Lark (international) callers
#: override this with ``https://open.larksuite.com``.
DEFAULT_API_BASE: str = "https://open.feishu.cn"

#: Backoff after a long-connection failure (seconds). Mirrors the
#: Telegram adapter's ``ERROR_BACKOFF_SECS``.
ERROR_BACKOFF_SECS: float = 5.0


# ===========================================================================
# Config
# ===========================================================================


@dataclass(slots=True)
class FeishuConfig:
    """Configuration for :class:`FeishuAdapter`.

    ``app_id`` + ``app_secret`` are the internal-app credentials used to
    mint a ``tenant_access_token``. Both are required.
    ``allowed_chat_ids`` (empty == allow all) and ``keyword_filter``
    (case-insensitive substring; empty == allow all) mirror the Telegram
    gates. ``respond_to_all`` disables the mention-required gate for
    group chats. ``api_base`` switches between Feishu (China) and Lark
    (international).
    """

    app_id: str
    app_secret: str
    allowed_chat_ids: list[str] = field(default_factory=list)
    keyword_filter: list[str] = field(default_factory=list)
    respond_to_all: bool = False
    api_base: str = DEFAULT_API_BASE


# ===========================================================================
# Mention / binding / parsing helpers — pure functions, easy to unit-test.
# ===========================================================================


def extract_text(message: dict[str, Any]) -> str:
    """Flatten a Feishu message payload into plain text.

    Feishu wraps the message body in a JSON-encoded ``content`` string.
    ``msg_type == "text"`` carries ``{"text": "..."}``; ``post`` (rich
    text) carries a nested block structure we walk for ``text`` runs.
    Anything else flattens to an empty string (image-only, sticker, ...).
    """
    raw = message.get("content")
    if not raw:
        return ""
    try:
        body = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return ""
    if not isinstance(body, dict):
        return ""
    msg_type = message.get("message_type") or message.get("msg_type")
    if msg_type == "text":
        return str(body.get("text", "")).strip()
    if msg_type == "post":
        # Rich-text post: {"<lang>": {"title": ..., "content": [[run, ...]]}}.
        parts: list[str] = []
        for lang_block in body.values():
            if not isinstance(lang_block, dict):
                continue
            for line in lang_block.get("content", []) or []:
                for run in line or []:
                    if isinstance(run, dict) and run.get("tag") == "text":
                        parts.append(str(run.get("text", "")))
        return " ".join(parts).strip()
    return ""


def extract_attachments(message: dict[str, Any]) -> list[Attachment]:
    """Extract :class:`Attachment` descriptors from a Feishu message.

    Feishu references media by key inside the JSON-encoded ``content``:
    ``image`` → ``image_key``; ``file`` / ``audio`` / ``media`` →
    ``file_key``. The bytes are fetched with a follow-up
    ``im/v1/messages/{message_id}/resources/{key}`` call (auth + the
    message id), so at parse time we carry an opaque resolver token in
    :attr:`Attachment.url` of the form
    ``feishu-resource:<message_id>:<key>`` plus the source ``mime``-glob.
    """
    raw = message.get("content")
    if not raw:
        return []
    try:
        body = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []
    if not isinstance(body, dict):
        return []
    msg_type = message.get("message_type") or message.get("msg_type")
    message_id = str(message.get("message_id", ""))

    def _token(key: str) -> str:
        return f"feishu-resource:{message_id}:{key}"

    if msg_type == "image":
        key = str(body.get("image_key", ""))
        if key:
            return [
                Attachment(kind=AttachmentKind.IMAGE, url=_token(key), mime="image/*")
            ]
        return []
    if msg_type in ("file", "audio", "media"):
        key = str(body.get("file_key", ""))
        if not key:
            return []
        kind = {
            "audio": AttachmentKind.AUDIO,
            "media": AttachmentKind.VIDEO,
        }.get(msg_type, AttachmentKind.DOCUMENT)
        return [
            Attachment(
                kind=kind,
                url=_token(key),
                file_name=body.get("file_name") or None,
            )
        ]
    return []


def sender_display_name(event_message: dict[str, Any], sender: dict[str, Any]) -> str | None:
    """Best-effort author display name for group attribution.

    Feishu's message event ``sender`` block rarely inlines a name; when a
    ``sender_id`` or enriched ``name`` is present we surface it. Returns
    ``None`` when only opaque open ids are available (the agent can still
    resolve via the binding)."""
    name = sender.get("name") or sender.get("sender_name")
    if name:
        return str(name)
    return None


def is_mentioning_bot(message: dict[str, Any], bot_open_id: str) -> bool:
    """True iff ``message`` @-mentions the bot.

    Feishu resolves mentions into a ``mentions`` array; each entry has an
    ``id`` object whose ``open_id`` identifies the mentioned user/bot.
    """
    if not bot_open_id:
        return False
    for mention in message.get("mentions") or []:
        if not isinstance(mention, dict):
            continue
        ident = mention.get("id")
        if isinstance(ident, dict) and ident.get("open_id") == bot_open_id:
            return True
        # Some payloads inline the id directly on the mention.
        if mention.get("open_id") == bot_open_id:
            return True
    return False


def binding_from_event(event_message: dict[str, Any], bot_open_id: str) -> ChannelBinding:
    """Build a :class:`ChannelBinding` from a Feishu message event.

    ``account`` is the bot's open id, ``thread`` the ``chat_id``,
    ``sender`` the sender's open id. p2p chats keep the chat id as
    ``thread`` so the session key stays stable per-peer.
    """
    chat_id = str(event_message.get("chat_id", ""))
    sender = event_message.get("_sender_open_id", "") or chat_id
    return ChannelBinding(
        channel="feishu",
        account=bot_open_id,
        thread=chat_id,
        sender=str(sender),
    )


def _strip_mention_keys(text: str) -> str:
    """Strip Feishu ``@_user_N`` mention placeholders from flattened text.

    Feishu substitutes mentions in the text body with ``@_user_1`` style
    keys (the real names live in the ``mentions`` array). Removing them
    keeps the text the chat backend sees clean.
    """
    import re

    return re.sub(r"@_user_\d+", " ", text).strip()


# ===========================================================================
# Adapter
# ===========================================================================


class FeishuAdapter:
    """Feishu / Lark long-connection adapter.

    Same surface as the other adapters: ``async with`` for lifecycle,
    ``inbound()`` for the normalized event stream. The long connection
    (token → endpoint → events) runs in a background task; decoded
    ``im.message.receive_v1`` events land on an internal queue the
    ``inbound`` iterator drains.

    Outbound replies are a separate concern — see :class:`FeishuSender`.
    """

    def __init__(
        self,
        config: FeishuConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not config.app_id:
            raise ConfigError("FeishuConfig.app_id is empty")
        if not config.app_secret:
            raise ConfigError("FeishuConfig.app_secret is empty")
        self._cfg = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._closed = False
        self._bot_open_id: str = ""
        self._inbound_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._reader_task: asyncio.Task[None] | None = None
        # tenant_access_token cache — (token, expiry_unix).
        self._token: str = ""
        self._token_expiry: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> FeishuAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Mint the first access token and spawn the long-connection loop.

        The initial token exchange must succeed so we fail fast on bad
        credentials — the Telegram adapter does the same with ``getMe``.
        """
        if self._reader_task is not None:
            return
        await self._refresh_token()
        self._closed = False
        self._reader_task = asyncio.create_task(
            self._connection_loop(), name="feishu-longconn"
        )

    async def close(self) -> None:
        """Stop the long-connection loop and (if we own it) the client."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._owns_client:
            await self._client.aclose()

    @property
    def tenant_access_token(self) -> str:
        """The current ``tenant_access_token`` (refreshed on demand)."""
        return self._token

    # ------------------------------------------------------------------
    # Inbound iterator
    # ------------------------------------------------------------------

    async def inbound(self) -> AsyncIterator[InboundEvent[dict[str, Any]]]:
        """Yield one :class:`InboundEvent` per accepted inbound message.

        Filtering rules (parallel to the Telegram / Slack adapters):

        * ``allowed_chat_ids`` whitelist (empty = allow all);
        * group chats: require an @-mention unless ``respond_to_all``,
          then apply the optional keyword filter;
        * empty / whitespace-only text is silently skipped.
        """
        if self._reader_task is None:
            await self.connect()
        while not self._closed:
            try:
                event = await self._inbound_q.get()
            except asyncio.CancelledError:
                return

            # ``event`` is the inner Feishu event object: it carries
            # ``sender`` + ``message`` sub-objects.
            sender = event.get("sender") or {}
            message = event.get("message") or {}
            if not isinstance(sender, dict) or not isinstance(message, dict):
                continue

            sender_id = sender.get("sender_id") or {}
            sender_open_id = (
                str(sender_id.get("open_id", ""))
                if isinstance(sender_id, dict)
                else ""
            )
            # The bot's own open id sits on the mentioned-bot side; we
            # detect self-messages by sender_type == "app" / "bot".
            if sender.get("sender_type") in ("app", "bot"):
                continue

            chat_id = str(message.get("chat_id", ""))
            if not self._chat_allowed(chat_id):
                continue

            chat_type = message.get("chat_type")
            is_p2p = chat_type == "p2p"
            mentioned = is_mentioning_bot(message, self._bot_open_id)
            if not is_p2p:
                if not self._cfg.respond_to_all and not mentioned:
                    continue
                if not mentioned and not self._keyword_match(message):
                    continue

            text = _strip_mention_keys(extract_text(message))
            attachments = extract_attachments(message)
            if not text.strip() and not attachments:
                continue

            # Stash the sender open id so ``binding_from_event`` can read it.
            message["_sender_open_id"] = sender_open_id
            binding = binding_from_event(message, self._bot_open_id)
            yield InboundEvent(
                channel="feishu",
                binding=binding,
                text=text,
                message_id=str(message.get("message_id", "")) or None,
                timestamp=_parse_create_time(message.get("create_time")),
                mentioned=mentioned or is_p2p,
                attachments=attachments,
                payload=event,
                sender_name=sender_display_name(message, sender),
            )

    # ------------------------------------------------------------------
    # Long-connection loop
    # ------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        """Open a long connection and pump event frames forever.

        Reconnects on any transport failure with a fixed backoff —
        matching the Telegram poll loop's resilience contract. Exits
        promptly once :meth:`close` flips ``self._closed``.
        """
        while not self._closed:
            try:
                ws_url = await self._open_endpoint()
                await self._run_one_connection(ws_url)
            except asyncio.CancelledError:
                return
            except Exception:
                # Transient — back off and retry, same as the TG adapter.
                try:
                    await asyncio.sleep(ERROR_BACKOFF_SECS)
                except asyncio.CancelledError:
                    return

    async def _run_one_connection(self, ws_url: str) -> None:
        """Drive a single long-connection WebSocket until it closes."""
        async with websockets.connect(ws_url, max_size=2 ** 23) as ws:
            while not self._closed:
                try:
                    frame = await self._recv_json(ws)
                except websockets.ConnectionClosed:
                    return
                self._handle_frame(frame)

    def _handle_frame(self, frame: dict[str, Any]) -> None:
        """Decode one long-connection frame and enqueue message events.

        Feishu wraps the Events payload in an envelope; ``ping`` control
        frames are skipped. The inner ``event`` for an
        ``im.message.receive_v1`` is what the inbound iterator consumes.
        """
        # Control frames (ping / pong) carry no event.
        header = frame.get("header") or {}
        event_type = header.get("event_type") if isinstance(header, dict) else None
        if event_type != "im.message.receive_v1":
            return
        event = frame.get("event")
        if not isinstance(event, dict):
            return
        if self._closed:
            return
        with suppress(asyncio.QueueFull):
            self._inbound_q.put_nowait(event)

    # ------------------------------------------------------------------
    # REST primitives
    # ------------------------------------------------------------------

    async def _refresh_token(self) -> str:
        """Mint / refresh the ``tenant_access_token``.

        Cached until ~5 minutes before expiry so most calls are free.
        Raises :class:`TransportError` on bad credentials or transport
        failure.
        """
        now = time.time()
        if self._token and now < self._token_expiry:
            return self._token
        try:
            resp = await self._client.post(
                f"{self._cfg.api_base}/open-apis/auth/v3/"
                "tenant_access_token/internal",
                json={
                    "app_id": self._cfg.app_id,
                    "app_secret": self._cfg.app_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"feishu token exchange failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(
                f"feishu token exchange HTTP {resp.status_code}"
            )
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(f"feishu token invalid JSON: {exc}") from exc
        if not isinstance(env, dict) or env.get("code") != 0:
            code = env.get("code") if isinstance(env, dict) else "?"
            raise TransportError(f"feishu token exchange error code {code}")
        token = str(env.get("tenant_access_token", ""))
        if not token:
            raise TransportError("feishu token exchange returned no token")
        expire = int(env.get("expire", 7200))
        self._token = token
        self._token_expiry = now + max(expire - 300, 60)
        return token

    async def _open_endpoint(self) -> str:
        """``POST /callback/ws/endpoint`` → a single-use ``wss://`` URL."""
        token = await self._refresh_token()
        try:
            resp = await self._client.post(
                f"{self._cfg.api_base}/callback/ws/endpoint",
                json={"AppID": self._cfg.app_id},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"feishu ws endpoint failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(
                f"feishu ws endpoint HTTP {resp.status_code}"
            )
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(f"feishu ws endpoint invalid JSON: {exc}") from exc
        # The endpoint URL sits under ``data.URL`` (Feishu's casing).
        data = env.get("data") if isinstance(env, dict) else None
        ws_url = data.get("URL") if isinstance(data, dict) else None
        if not isinstance(ws_url, str) or not ws_url:
            raise TransportError("feishu ws endpoint returned no URL")
        return ws_url

    @staticmethod
    async def _recv_json(ws: ClientConnection) -> dict[str, Any]:
        """Receive one WS frame and decode it as a JSON object."""
        raw = await ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise TransportError("feishu long-conn: frame was not a JSON object")
        return obj

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def _chat_allowed(self, chat_id: str) -> bool:
        allow = self._cfg.allowed_chat_ids
        return not allow or chat_id in allow

    def _keyword_match(self, message: dict[str, Any]) -> bool:
        filter_ = self._cfg.keyword_filter
        if not filter_:
            return True
        lower = extract_text(message).lower()
        return any(kw.lower() in lower for kw in filter_)


# ===========================================================================
# Outbound sender
# ===========================================================================


class FeishuSender:
    """Thin client over the Feishu IM REST surface, scoped to outbound.

    Parallel to :class:`corlinman_channels.telegram_send.TelegramSender`.
    The sender needs a fresh ``tenant_access_token`` per call; the
    adapter owns the token lifecycle, so the sender takes a
    ``token_provider`` async callable that yields a current token.

    The "decorative" endpoint (``update_message``) shares a single 429
    back-off deadline like the Telegram / Discord / Slack senders.
    Feishu doesn't expose a typing indicator to bots, so the mutable-
    spinner edits are the only user-visible "I'm working" signal.
    """

    __slots__ = (
        "_edit_rate_limit_until",
        "api_base",
        "client",
        "token_provider",
    )

    def __init__(
        self,
        client: httpx.AsyncClient,
        token_provider: Any,
        api_base: str = DEFAULT_API_BASE,
    ) -> None:
        self.client = client
        self.token_provider = token_provider
        self.api_base = api_base
        self._edit_rate_limit_until: float = 0.0

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Send a text message to ``chat_id``. Returns the new message id.

        When ``reply_to_message_id`` is supplied the message is posted as
        a reply via the ``/messages/{id}/reply`` endpoint so the
        addressing stays clear — parallel to the Telegram ``reply_to``.
        Otherwise it posts a fresh message into the chat.
        """
        token = await self.token_provider()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        content = json.dumps({"text": text}, ensure_ascii=False)
        if reply_to_message_id is not None:
            url = (
                f"{self.api_base}/open-apis/im/v1/messages/"
                f"{reply_to_message_id}/reply"
            )
            body: dict[str, Any] = {"content": content, "msg_type": "text"}
        else:
            url = (
                f"{self.api_base}/open-apis/im/v1/messages"
                "?receive_id_type=chat_id"
            )
            body = {
                "receive_id": chat_id,
                "content": content,
                "msg_type": "text",
            }
        try:
            resp = await self.client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise TransportError(f"feishu send failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(f"feishu send HTTP {resp.status_code}")
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(f"feishu send invalid JSON: {exc}") from exc
        if not isinstance(env, dict) or env.get("code") != 0:
            code = env.get("code") if isinstance(env, dict) else "?"
            raise TransportError(f"feishu send error code {code}")
        data = env.get("data") or {}
        return str(data.get("message_id", "")) if isinstance(data, dict) else ""

    async def update_message(self, message_id: str, text: str) -> None:
        """PUT ``/open-apis/im/v1/messages/{message_id}``. Mutates an
        earlier message in place — used as the "mutable spinner line"
        while tool calls land.

        Best-effort: any non-2xx (or Feishu's non-zero ``code``) is
        swallowed so a re-fire of the same content (or a rate-limit)
        never breaks the turn. Mirrors
        :meth:`TelegramSender.edit_message_text`.

        Feishu's edit endpoint takes the same ``msg_type`` + JSON-encoded
        ``content`` shape as the send endpoint. We only need ``text``.
        """
        if time.time() < self._edit_rate_limit_until:
            return
        try:
            token = await self.token_provider()
        except Exception:  # noqa: BLE001
            return
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        body = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        try:
            resp = await self.client.put(
                f"{self.api_base}/open-apis/im/v1/messages/{message_id}",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError:
            return
        if resp.status_code == 429:
            self._note_retry_after(resp)

    async def upload_file(
        self,
        path: Path,
        *,
        filename: str | None = None,
        file_type: str = "stream",
    ) -> str:
        """POST ``/open-apis/im/v1/files`` with a multipart file part.

        Returns the new file key. The agent then references this key in
        a ``msg_type="file"`` message via :meth:`send_file_message`. The
        two-step shape mirrors Feishu's documented API — they don't have
        a single-call "upload + send" endpoint.

        ``file_type`` is one of ``opus`` / ``mp4`` / ``pdf`` / ``doc`` /
        ``xls`` / ``ppt`` / ``stream``. ``stream`` is the safe fallback
        for arbitrary binaries; the caller can override when they know
        the MIME family.

        Raises :class:`TransportError` on transport / API failure — the
        channel handler folds the error into a friendly status line.
        """
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise TransportError(f"feishu file read failed: {exc}") from exc
        try:
            token = await self.token_provider()
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"feishu token refresh failed: {exc}") from exc

        name = filename or path.name or "file.bin"
        form: dict[str, Any] = {
            "file_type": file_type,
            "file_name": name,
        }
        try:
            resp = await self.client.post(
                f"{self.api_base}/open-apis/im/v1/files",
                data=form,
                files={"file": (name, data, "application/octet-stream")},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"feishu files upload failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(
                f"feishu files upload HTTP {resp.status_code}"
            )
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(
                f"feishu files upload invalid JSON: {exc}"
            ) from exc
        if not isinstance(env, dict) or env.get("code") != 0:
            code = env.get("code") if isinstance(env, dict) else "?"
            raise TransportError(f"feishu files upload error code {code}")
        data_obj = env.get("data") or {}
        return str(data_obj.get("file_key", "")) if isinstance(data_obj, dict) else ""

    async def send_file_message(
        self,
        chat_id: str,
        file_key: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Send a ``msg_type="file"`` message referencing an earlier
        ``upload_file`` ``file_key``. Mirrors the shape of
        :meth:`send_message` but with the file payload.
        """
        try:
            token = await self.token_provider()
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"feishu token refresh failed: {exc}") from exc
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        content = json.dumps({"file_key": file_key}, ensure_ascii=False)
        if reply_to_message_id is not None:
            url = (
                f"{self.api_base}/open-apis/im/v1/messages/"
                f"{reply_to_message_id}/reply"
            )
            body: dict[str, Any] = {"content": content, "msg_type": "file"}
        else:
            url = (
                f"{self.api_base}/open-apis/im/v1/messages"
                "?receive_id_type=chat_id"
            )
            body = {
                "receive_id": chat_id,
                "content": content,
                "msg_type": "file",
            }
        try:
            resp = await self.client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise TransportError(f"feishu file message failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(f"feishu file message HTTP {resp.status_code}")
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(
                f"feishu file message invalid JSON: {exc}"
            ) from exc
        if not isinstance(env, dict) or env.get("code") != 0:
            code = env.get("code") if isinstance(env, dict) else "?"
            raise TransportError(f"feishu file message error code {code}")
        data = env.get("data") or {}
        return str(data.get("message_id", "")) if isinstance(data, dict) else ""

    def _note_retry_after(self, resp: httpx.Response) -> None:
        """Extend the shared 429 back-off using Feishu's ``Retry-After``
        header (Feishu mostly returns HTTP 200 with non-zero ``code`` but
        429 does fire on extreme abuse). Falls back to 1s when missing
        or unparseable.
        """
        retry_after: float = 1.0
        try:
            ra = resp.headers.get("Retry-After")
            if ra:
                retry_after = float(ra)
        except (TypeError, ValueError):
            pass
        self._edit_rate_limit_until = time.time() + retry_after


# ===========================================================================
# Helpers
# ===========================================================================


def _parse_create_time(raw: Any) -> int:
    """Best-effort Feishu ``create_time`` → Unix-seconds conversion.

    Feishu sends ``create_time`` as a millisecond epoch string. Falls
    back to ``0`` (the :class:`InboundEvent` "no timestamp" sentinel)
    when the value is missing or unparseable.
    """
    if raw is None:
        return 0
    try:
        ms = int(raw)
    except (ValueError, TypeError):
        return 0
    # Feishu uses milliseconds; values look 13-digit.
    return ms // 1000 if ms > 10_000_000_000 else ms
