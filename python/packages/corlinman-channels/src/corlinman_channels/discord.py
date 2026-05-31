"""Discord Gateway (WebSocket) + REST adapter.

corlinman has no Rust reference for Discord — this is a new channel
modelled on the existing :mod:`corlinman_channels.telegram` adapter so
the inbound shape (``async for event in adapter.inbound()``) and the
outbound :class:`DiscordSender` mirror the Telegram pair.

## Transport

Discord splits inbound and outbound:

* **Gateway** — a WebSocket at ``wss://gateway.discord.gg`` carrying the
  IDENTIFY / HELLO / HEARTBEAT / DISPATCH protocol. The adapter performs
  the handshake, runs a heartbeat task at the server-supplied interval,
  and decodes ``MESSAGE_CREATE`` dispatch frames into
  :class:`InboundEvent` objects.
* **REST** — ``POST /channels/{id}/messages`` over HTTPS for replies.

We implement against the raw protocol over ``websockets`` / ``httpx``
(both already dependencies) rather than pulling in ``discord.py`` — the
slice of the protocol corlinman needs (one intent, text messages, a
heartbeat) is small and a heavyweight bot framework would bloat the
dependency graph for no benefit.

## Gateway opcodes

Only the handful the adapter exercises are named here; the rest decode
as ignored integers.

* ``0`` DISPATCH    — an event (we care about ``MESSAGE_CREATE``).
* ``1`` HEARTBEAT   — server asks for an immediate heartbeat.
* ``7`` RECONNECT   — server asks us to reconnect.
* ``9`` INVALID_SESSION — re-IDENTIFY from scratch.
* ``10`` HELLO      — carries ``heartbeat_interval`` (ms).
* ``11`` HEARTBEAT_ACK.

## Reply gating

Discord channels are either guild (server) channels or DMs. The adapter
mirrors the Telegram gate:

* DMs always respond (``mentioned`` is implicitly ``True``).
* Guild channels respond only when the bot is @-mentioned, unless
  ``respond_to_all`` is set. An optional ``keyword_filter`` narrows
  guild messages further (case-insensitive substring).
* The bot never replies to its own messages or to other bots.
"""

from __future__ import annotations

import asyncio
import json
import secrets
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
    split_on_msg_break,
    sticker_placeholder,
)

__all__ = [
    "DEFAULT_GATEWAY_URL",
    "DEFAULT_REST_BASE",
    "GATEWAY_INTENT_GUILD_MESSAGES",
    "MAX_UPLOAD_BYTES",
    "DiscordAdapter",
    "DiscordConfig",
    "DiscordSender",
    "binding_from_message",
    "is_mentioning_bot",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default Discord Gateway endpoint. ``?v=10&encoding=json`` selects the
#: stable API version and the JSON (not ETF) encoding so we can decode
#: with the stdlib ``json`` module.
DEFAULT_GATEWAY_URL: str = "wss://gateway.discord.gg/?v=10&encoding=json"

#: Default Discord REST base. API v10 matches the gateway version above.
DEFAULT_REST_BASE: str = "https://discord.com/api/v10"

#: Hard cap on Discord file uploads — Nitro tier raises this to 25 MiB
#: for the average bot; free / no-boost servers cap at 8 MiB but Discord
#: silently truncates the multipart at the channel cap on the API side.
#: 25 MiB matches the Nitro ceiling so we don't preemptively block
#: medium-sized files; the API will return 413 if the channel is below.
MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024

#: Gateway intents bitfield. ``GUILD_MESSAGES`` (1<<9) + ``DIRECT_MESSAGES``
#: (1<<12) + ``MESSAGE_CONTENT`` (1<<15). ``MESSAGE_CONTENT`` is a
#: privileged intent — the bot owner must enable it in the developer
#: portal or the gateway closes the connection with code 4014.
GATEWAY_INTENT_GUILD_MESSAGES: int = (1 << 9) | (1 << 12) | (1 << 15)

#: Backoff after a gateway failure (seconds). Mirrors the Telegram
#: adapter's ``ERROR_BACKOFF_SECS``.
ERROR_BACKOFF_SECS: float = 5.0

# Gateway opcodes (only the ones the adapter interprets).
_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11


# ===========================================================================
# Config
# ===========================================================================


@dataclass(slots=True)
class DiscordConfig:
    """Configuration for :class:`DiscordAdapter`.

    ``bot_token`` is required (the ``Bot <token>`` credential from the
    developer portal). ``allowed_channel_ids`` (empty == allow all) and
    ``keyword_filter`` (case-insensitive substring; empty == allow all)
    mirror the Telegram gates. ``respond_to_all`` disables the
    mention-required gate for guild channels.
    """

    bot_token: str
    allowed_channel_ids: list[str] = field(default_factory=list)
    keyword_filter: list[str] = field(default_factory=list)
    respond_to_all: bool = False
    gateway_url: str = DEFAULT_GATEWAY_URL
    rest_base: str = DEFAULT_REST_BASE
    intents: int = GATEWAY_INTENT_GUILD_MESSAGES


# ===========================================================================
# Mention / binding helpers — pure functions, easy to unit-test.
# ===========================================================================


def is_mentioning_bot(message: dict[str, Any], bot_id: str) -> bool:
    """True iff ``message`` @-mentions the bot.

    Discord puts resolved mentions in ``message["mentions"]`` — a list of
    user objects. A raw ``<@id>`` / ``<@!id>`` substring fallback covers
    edited messages or partial payloads where ``mentions`` was stripped.
    """
    if not bot_id:
        return False
    for user in message.get("mentions") or []:
        if isinstance(user, dict) and str(user.get("id", "")) == bot_id:
            return True
    content = message.get("content") or ""
    return f"<@{bot_id}>" in content or f"<@!{bot_id}>" in content


def binding_from_message(message: dict[str, Any], bot_id: str) -> ChannelBinding:
    """Build a :class:`ChannelBinding` from a Discord ``MESSAGE_CREATE``.

    ``account`` is the bot id, ``thread`` the channel id, ``sender`` the
    author id. DMs have ``guild_id`` absent — ``thread`` is still the
    channel id so the session key stays stable per-DM.
    """
    channel_id = str(message.get("channel_id", ""))
    author = message.get("author") or {}
    sender_id = str(author.get("id", "")) if isinstance(author, dict) else ""
    return ChannelBinding(
        channel="discord",
        account=bot_id,
        thread=channel_id,
        sender=sender_id or channel_id,
    )


def _strip_mention(content: str, bot_id: str) -> str:
    """Remove the leading ``<@bot_id>`` mention token from ``content``.

    Keeps the user-facing text clean so the chat backend doesn't see the
    raw mention markup. Matches the convenience the QQ router applies via
    ``segments_to_text``.
    """
    if not bot_id:
        return content.strip()
    for token in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        content = content.replace(token, " ")
    return content.strip()


def _classify_attachment(att: dict[str, Any]) -> AttachmentKind:
    """Classify a Discord attachment dict by its ``content_type`` / name.

    Discord ships ``content_type`` (a MIME string) on most attachments;
    we fall back to the filename extension when it's absent.
    """
    ctype = str(att.get("content_type") or "").lower()
    name = str(att.get("filename") or "").lower()
    if ctype.startswith("image/") or name.endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp")
    ):
        return AttachmentKind.IMAGE
    if ctype.startswith("audio/") or name.endswith((".ogg", ".mp3", ".wav", ".m4a")):
        return AttachmentKind.AUDIO
    if ctype.startswith("video/") or name.endswith((".mp4", ".mov", ".webm")):
        return AttachmentKind.VIDEO
    return AttachmentKind.DOCUMENT


def extract_attachments(message: dict[str, Any]) -> list[Attachment]:
    """Extract :class:`Attachment` descriptors from a Discord message.

    Discord pre-uploads every attachment to its CDN, so each carries a
    fetchable ``url`` — no follow-up download token needed (unlike
    Telegram). Stickers are surfaced as IMAGE attachments via the sticker
    CDN; their textual hint is added by the adapter.
    """
    out: list[Attachment] = []
    for att in message.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        url = str(att.get("url") or att.get("proxy_url") or "")
        if not url:
            continue
        out.append(
            Attachment(
                kind=_classify_attachment(att),
                url=url,
                mime=att.get("content_type") or None,
                file_name=att.get("filename") or None,
            )
        )
    for st in message.get("sticker_items") or message.get("stickers") or []:
        if not isinstance(st, dict):
            continue
        sticker_id = str(st.get("id") or "")
        if not sticker_id:
            continue
        out.append(
            Attachment(
                kind=AttachmentKind.IMAGE,
                url=f"https://media.discordapp.net/stickers/{sticker_id}.png",
                mime="image/png",
                file_name=f"{st.get('name') or 'sticker'}.png",
            )
        )
    return out


def sender_display_name(message: dict[str, Any]) -> str | None:
    """Best-effort author display name for group attribution.

    Prefers the per-guild ``member.nick``, then the account-level
    ``global_name``, then ``username``.
    """
    member = message.get("member")
    if isinstance(member, dict) and member.get("nick"):
        return str(member["nick"])
    author = message.get("author")
    if isinstance(author, dict):
        for key in ("global_name", "username"):
            val = author.get(key)
            if val:
                return str(val)
    return None


def reply_to_text(message: dict[str, Any], bot_id: str) -> str | None:
    """Text of the message this one replies to (``referenced_message``)."""
    ref = message.get("referenced_message")
    if not isinstance(ref, dict):
        return None
    content = _strip_mention(str(ref.get("content") or ""), bot_id)
    return content or None


def sticker_hint(message: dict[str, Any]) -> str | None:
    """Synthesised text hint for a sticker-only Discord message."""
    items = message.get("sticker_items") or message.get("stickers") or []
    for st in items:
        if isinstance(st, dict) and st.get("name"):
            return sticker_placeholder(set_name=str(st["name"]))
    if items:
        return sticker_placeholder()
    return None


# ===========================================================================
# Adapter
# ===========================================================================


class DiscordAdapter:
    """Discord Gateway WebSocket adapter.

    Same surface as the other adapters: ``async with`` for lifecycle,
    ``inbound()`` for the normalized event stream. The gateway handshake
    (IDENTIFY → HELLO → heartbeat loop) runs in a background task; decoded
    ``MESSAGE_CREATE`` events land on an internal queue the ``inbound``
    iterator drains.

    Outbound replies are a separate concern — see :class:`DiscordSender`.
    """

    def __init__(
        self,
        config: DiscordConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not config.bot_token:
            raise ConfigError("DiscordConfig.bot_token is empty")
        self._cfg = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._closed = False
        self._bot_id: str | None = None
        self._inbound_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._reader_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> DiscordAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Resolve the bot id and spawn the gateway loop.

        ``GET /users/@me`` discovers the bot's own id so the mention gate
        and self-message filter work. A network failure here raises
        :class:`TransportError` so the caller fails fast — the Telegram
        adapter does the same with ``getMe``.
        """
        if self._reader_task is not None:
            return
        self._bot_id = await self._get_self_id()
        self._closed = False
        self._reader_task = asyncio.create_task(
            self._gateway_loop(), name="discord-gateway"
        )

    async def close(self) -> None:
        """Stop the gateway loop and (if we own it) the HTTP client."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._owns_client:
            await self._client.aclose()

    @property
    def bot_id(self) -> str | None:
        """The bot's snowflake id, available after :meth:`connect`."""
        return self._bot_id

    # ------------------------------------------------------------------
    # Inbound iterator
    # ------------------------------------------------------------------

    async def inbound(self) -> AsyncIterator[InboundEvent[dict[str, Any]]]:
        """Yield one :class:`InboundEvent` per accepted inbound message.

        Filtering rules (parallel to the Telegram adapter):

        * skip the bot's own messages and other bots' messages;
        * ``allowed_channel_ids`` whitelist (empty = allow all);
        * guild channels: require an @-mention unless ``respond_to_all``,
          then apply the optional keyword filter;
        * empty / whitespace-only content is silently skipped.
        """
        if self._reader_task is None:
            await self.connect()
        assert self._bot_id is not None  # connect() guarantees this
        bot_id = self._bot_id
        while not self._closed:
            try:
                msg = await self._inbound_q.get()
            except asyncio.CancelledError:
                return

            author = msg.get("author") or {}
            author_id = str(author.get("id", "")) if isinstance(author, dict) else ""
            # Never reply to ourselves or to other bots — prevents loops.
            if author_id == bot_id:
                continue
            if isinstance(author, dict) and author.get("bot"):
                continue

            channel_id = str(msg.get("channel_id", ""))
            if not self._channel_allowed(channel_id):
                continue

            # A guild message carries ``guild_id``; a DM does not.
            is_dm = not msg.get("guild_id")
            mentioned = is_mentioning_bot(msg, bot_id)
            if not is_dm:
                if not self._cfg.respond_to_all and not mentioned:
                    continue
                if not mentioned and not self._keyword_match(msg):
                    continue

            content = _strip_mention(msg.get("content") or "", bot_id)
            attachments = extract_attachments(msg)
            if not content.strip():
                # No text — try a sticker hint so a sticker-only message
                # still routes; otherwise fall through to the
                # attachment-only guard below.
                hint = sticker_hint(msg)
                if hint is not None:
                    content = hint
            if not content.strip() and not attachments:
                continue

            binding = binding_from_message(msg, bot_id)
            yield InboundEvent(
                channel="discord",
                binding=binding,
                text=content,
                message_id=str(msg.get("id", "")) or None,
                timestamp=_parse_timestamp(msg.get("timestamp")),
                mentioned=mentioned or is_dm,
                attachments=attachments,
                payload=msg,
                sender_name=sender_display_name(msg),
                reply_to_text=reply_to_text(msg, bot_id),
            )

    # ------------------------------------------------------------------
    # Gateway loop
    # ------------------------------------------------------------------

    async def _gateway_loop(self) -> None:
        """Connect to the gateway and pump dispatch frames forever.

        Reconnects on any transport failure with a fixed backoff —
        matching the Telegram poll loop's resilience contract. Exits
        promptly once :meth:`close` flips ``self._closed``.
        """
        while not self._closed:
            try:
                await self._run_one_connection()
            except asyncio.CancelledError:
                return
            except Exception:
                # Transient — back off and retry, same as the TG adapter.
                try:
                    await asyncio.sleep(ERROR_BACKOFF_SECS)
                except asyncio.CancelledError:
                    return

    async def _run_one_connection(self) -> None:
        """Drive a single gateway WebSocket connection from HELLO to close."""
        heartbeat_task: asyncio.Task[None] | None = None
        async with websockets.connect(
            self._cfg.gateway_url, max_size=2 ** 23
        ) as ws:
            # First frame must be HELLO (op 10) carrying the heartbeat interval.
            hello = await self._recv_json(ws)
            if hello.get("op") != _OP_HELLO:
                raise TransportError("discord gateway: expected HELLO frame")
            interval_ms = int(hello.get("d", {}).get("heartbeat_interval", 41250))

            await self._send_identify(ws)
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(ws, interval_ms / 1000.0),
                name="discord-heartbeat",
            )
            try:
                await self._pump(ws)
            finally:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _pump(self, ws: ClientConnection) -> None:
        """Read dispatch frames and enqueue ``MESSAGE_CREATE`` payloads."""
        while not self._closed:
            try:
                frame = await self._recv_json(ws)
            except websockets.ConnectionClosed:
                return
            op = frame.get("op")
            if op == _OP_DISPATCH:
                if frame.get("t") == "MESSAGE_CREATE":
                    payload = frame.get("d")
                    if isinstance(payload, dict):
                        if self._closed:
                            return
                        with suppress(asyncio.QueueFull):
                            self._inbound_q.put_nowait(payload)
            elif op in (_OP_RECONNECT, _OP_INVALID_SESSION):
                # Server asked us to reconnect — break so the outer loop
                # re-IDENTIFYs from scratch.
                return
            elif op == _OP_HEARTBEAT:
                # Server requested an immediate heartbeat.
                await self._send_json(ws, {"op": _OP_HEARTBEAT, "d": None})
            # _OP_HEARTBEAT_ACK / unknown → nothing to do.

    async def _heartbeat_loop(self, ws: ClientConnection, interval: float) -> None:
        """Send an op-1 heartbeat every ``interval`` seconds.

        Discord closes the connection if heartbeats stop; the outer
        :meth:`_gateway_loop` reconnects when that happens.
        """
        while not self._closed:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            try:
                await self._send_json(ws, {"op": _OP_HEARTBEAT, "d": None})
            except (websockets.ConnectionClosed, OSError):
                return

    async def _send_identify(self, ws: ClientConnection) -> None:
        """Send the op-2 IDENTIFY frame with our token + intents."""
        await self._send_json(
            ws,
            {
                "op": _OP_IDENTIFY,
                "d": {
                    "token": self._cfg.bot_token,
                    "intents": self._cfg.intents,
                    "properties": {
                        "os": "linux",
                        "browser": "corlinman",
                        "device": "corlinman",
                    },
                },
            },
        )

    # ------------------------------------------------------------------
    # HTTP / WS primitives
    # ------------------------------------------------------------------

    async def _get_self_id(self) -> str:
        """``GET /users/@me`` → the bot's own snowflake id."""
        try:
            resp = await self._client.get(
                f"{self._cfg.rest_base}/users/@me",
                headers={"Authorization": f"Bot {self._cfg.bot_token}"},
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"discord users/@me failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(
                f"discord users/@me HTTP {resp.status_code}: {resp.text}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise TransportError(f"discord users/@me invalid JSON: {exc}") from exc
        bot_id = str(body.get("id", "")) if isinstance(body, dict) else ""
        if not bot_id:
            raise TransportError("discord users/@me returned no id")
        return bot_id

    @staticmethod
    async def _recv_json(ws: ClientConnection) -> dict[str, Any]:
        """Receive one WS text frame and decode it as a JSON object."""
        raw = await ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise TransportError("discord gateway: frame was not a JSON object")
        return obj

    @staticmethod
    async def _send_json(ws: ClientConnection, obj: dict[str, Any]) -> None:
        """Encode ``obj`` as JSON and send it on the WS connection."""
        await ws.send(json.dumps(obj))

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def _channel_allowed(self, channel_id: str) -> bool:
        allow = self._cfg.allowed_channel_ids
        return not allow or channel_id in allow

    def _keyword_match(self, msg: dict[str, Any]) -> bool:
        filter_ = self._cfg.keyword_filter
        if not filter_:
            return True
        lower = (msg.get("content") or "").lower()
        return any(kw.lower() in lower for kw in filter_)


# ===========================================================================
# Outbound sender
# ===========================================================================


class DiscordSender:
    """Thin client over the Discord REST surface, scoped to outbound.

    Parallel to :class:`corlinman_channels.telegram_send.TelegramSender`.
    Construct once per bot token and reuse — the underlying
    :class:`httpx.AsyncClient` connection pool is the real cost.

    The "decorative" endpoints (``trigger_typing`` + ``edit_message``)
    share a single 429 back-off deadline — Discord rate-limits per-route
    *and* per-channel, and once we trip the limit any further calls in
    the window deepen the penalty. Skipping them silently until the
    deadline passes mirrors :class:`TelegramSender._edit_rate_limit_until`.
    """

    __slots__ = ("_edit_rate_limit_until", "base", "client", "token")

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        base: str = DEFAULT_REST_BASE,
    ) -> None:
        self.client = client
        self.token = token
        self.base = base
        self._edit_rate_limit_until: float = 0.0

    async def send_message(
        self,
        channel_id: str,
        text: str,
        reply_to_message_id: str | None = None,
    ) -> str:
        """POST ``/channels/{id}/messages``. Returns the last new message id.

        When ``reply_to_message_id`` is supplied the message is posted as
        an inline reply via ``message_reference`` so the addressing stays
        clear in the channel — parallel to the Telegram ``reply_to``.

        Text containing ``[MSG_BREAK]`` markers is split into multiple
        bubbles sent sequentially; only the first bubble carries the reply
        reference. The last message id is returned.
        """
        bubbles = split_on_msg_break(text)
        last_id = ""
        for i, bubble in enumerate(bubbles):
            body: dict[str, Any] = {"content": bubble}
            if reply_to_message_id is not None and i == 0:
                body["message_reference"] = {"message_id": reply_to_message_id}
            try:
                resp = await self.client.post(
                    f"{self.base}/channels/{channel_id}/messages",
                    json=body,
                    headers={"Authorization": f"Bot {self.token}"},
                )
            except httpx.HTTPError as exc:
                raise TransportError(f"discord sendMessage failed: {exc}") from exc
            if resp.status_code >= 400:
                raise TransportError(
                    f"discord sendMessage HTTP {resp.status_code}: {resp.text}"
                )
            try:
                env = resp.json()
            except ValueError as exc:
                raise TransportError(f"discord sendMessage invalid JSON: {exc}") from exc
            last_id = str(env.get("id", "")) if isinstance(env, dict) else ""
        return last_id

    async def trigger_typing(self, channel_id: str) -> None:
        """POST ``/channels/{id}/typing``. Shows "<Bot> is typing…" for
        about 10 seconds in the Discord client (longer than Telegram's
        ~5s but the channel handler still pulses at ~5s intervals for
        parity). No body is required.

        Best-effort: a failure here never blocks the reply path. Mirrors
        :meth:`TelegramSender.send_chat_action`.
        """
        if time.time() < self._edit_rate_limit_until:
            return
        try:
            resp = await self.client.post(
                f"{self.base}/channels/{channel_id}/typing",
                headers={"Authorization": f"Bot {self.token}"},
            )
            if resp.status_code == 429:
                self._note_retry_after(resp)
                return
        except httpx.HTTPError:
            return

    async def edit_message(
        self, channel_id: str, message_id: str, content: str
    ) -> None:
        """PATCH ``/channels/{id}/messages/{id}``. Mutates an earlier
        message in place — used as the "mutable spinner line" while tool
        calls land.

        Best-effort: any non-2xx is swallowed so a re-fire of the same
        content (or a 429) never breaks the turn. HTTP 429 updates a
        shared back-off so subsequent edits / typing pulses silently skip
        until the window expires. Mirrors
        :meth:`TelegramSender.edit_message_text`.
        """
        if time.time() < self._edit_rate_limit_until:
            return
        try:
            resp = await self.client.patch(
                f"{self.base}/channels/{channel_id}/messages/{message_id}",
                json={"content": content},
                headers={"Authorization": f"Bot {self.token}"},
            )
        except httpx.HTTPError:
            return
        if resp.status_code == 429:
            self._note_retry_after(resp)

    async def send_file(
        self,
        channel_id: str,
        path: Path,
        *,
        filename: str | None = None,
        content: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> str:
        """POST ``/channels/{id}/messages`` with a multipart ``files[0]``
        attachment. Returns the new message id.

        Discord accepts both ``content`` (text) and ``files[N]`` (binary)
        in the same multipart, so the caller can include a caption
        alongside the file. ``filename`` overrides the on-disk basename
        for the user-visible attachment name. Raises
        :class:`TransportError` on transport / API failure — the channel
        handler folds the error into a friendly status line rather than
        crashing the turn.
        """
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise TransportError(f"discord file stat failed: {exc}") from exc
        if size > MAX_UPLOAD_BYTES:
            raise TransportError(
                f"discord file too large: {size} > {MAX_UPLOAD_BYTES} "
                f"(path={path.name})"
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise TransportError(f"discord file read failed: {exc}") from exc

        name = filename or path.name or "file.bin"
        # Discord's docs require a JSON ``payload_json`` field alongside
        # the file part. Build a minimal multipart by hand — mirrors the
        # Telegram approach in ``telegram_send.build_multipart`` so the
        # dep graph stays minimal (we only need ``files[0]``, not the
        # general N-file array).
        boundary = f"corlinman-dc-{secrets.token_hex(16)}"
        payload: dict[str, Any] = {}
        if content is not None:
            payload["content"] = content
        if reply_to_message_id is not None:
            payload["message_reference"] = {"message_id": reply_to_message_id}

        body = bytearray()
        crlf = b"\r\n"
        dash = b"--"
        # payload_json text part
        body.extend(dash)
        body.extend(boundary.encode())
        body.extend(crlf)
        body.extend(b'Content-Disposition: form-data; name="payload_json"')
        body.extend(crlf)
        body.extend(b"Content-Type: application/json")
        body.extend(crlf)
        body.extend(crlf)
        body.extend(json.dumps(payload).encode("utf-8"))
        body.extend(crlf)
        # files[0] binary part
        body.extend(dash)
        body.extend(boundary.encode())
        body.extend(crlf)
        # ``filename`` is reflected in the message attachment metadata.
        body.extend(
            f'Content-Disposition: form-data; name="files[0]"; filename="{name}"'
            .encode()
        )
        body.extend(crlf)
        body.extend(b"Content-Type: application/octet-stream")
        body.extend(crlf)
        body.extend(crlf)
        body.extend(data)
        body.extend(crlf)
        # closing boundary
        body.extend(dash)
        body.extend(boundary.encode())
        body.extend(dash)
        body.extend(crlf)

        try:
            resp = await self.client.post(
                f"{self.base}/channels/{channel_id}/messages",
                content=bytes(body),
                headers={
                    "Authorization": f"Bot {self.token}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"discord file upload failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(
                f"discord file upload HTTP {resp.status_code}: {resp.text}"
            )
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(f"discord file upload invalid JSON: {exc}") from exc
        return str(env.get("id", "")) if isinstance(env, dict) else ""

    def _note_retry_after(self, resp: httpx.Response) -> None:
        """Extend the shared 429 back-off using Discord's ``retry_after``.

        Discord encodes retry_after as a float (seconds) in the JSON body
        and also in the ``Retry-After`` header. Falls back to a 1s
        penalty when neither is parseable — Discord always sets the
        field on a real rate-limit response, but the parse is best-
        effort so a malformed reply never raises.
        """
        retry_after: float = 1.0
        try:
            env = resp.json()
            if isinstance(env, dict):
                ra = env.get("retry_after")
                if isinstance(ra, (int, float)):
                    retry_after = float(ra)
        except Exception:  # noqa: BLE001
            try:
                ra_header = resp.headers.get("Retry-After")
                if ra_header:
                    retry_after = float(ra_header)
            except (TypeError, ValueError):
                pass
        self._edit_rate_limit_until = time.time() + retry_after


# ===========================================================================
# Helpers
# ===========================================================================


def _parse_timestamp(raw: Any) -> int:
    """Best-effort ISO-8601 → Unix-seconds conversion.

    Discord timestamps are ISO-8601 strings. Falls back to ``0`` when the
    value is missing or unparseable — :class:`InboundEvent` documents
    ``0`` as the "no timestamp" sentinel.
    """
    if not isinstance(raw, str) or not raw:
        return 0
    from datetime import datetime

    try:
        # Discord uses an offset suffix; ``fromisoformat`` handles it.
        return int(datetime.fromisoformat(raw).timestamp())
    except (ValueError, OverflowError):
        return 0
