"""Tencent QQ 官方机器人 Gateway (WebSocket) adapter.

corlinman has no Rust reference for the **官方** (official) QQ Bot
platform — this is a new channel covering the bot kind that runs on
``api.sgroup.qq.com`` (新版 QQ Bot 开放平台) as opposed to the legacy
gocq / NapCat path (see :mod:`corlinman_channels.onebot`).

## Why a separate adapter?

The 官方 QQ Bot platform is a completely different protocol from the
unofficial gocq / OneBot wire:

* **Auth** — AppID + AppSecret → exchange for a short-lived access
  token via ``POST https://bots.qq.com/app/getAppAccessToken``;
  Tencent's token expires every 7200s and must be refreshed.
* **Receive** — Either a webhook (HTTPS POST + Ed25519 signature) or
  a WebSocket gateway. This adapter uses the WS gateway because it is
  simpler in the v1 deployment (no public TLS endpoint required;
  matches the OneBot story).
* **Send** — REST POST to ``/channels/{channel_id}/messages`` (频道),
  ``/v2/groups/{group_openid}/messages`` (群@机器人), or
  ``/v2/users/{openid}/messages`` (C2C 私信). The platform requires
  every reply to thread the inbound ``msg_id`` (or ``event_id``) for
  the 5-minute reply window, so this adapter carries that id through
  on the :class:`InboundEvent`.

## Gateway opcodes

The 官方 QQ Bot gateway uses an opcode set very close to Discord's
gateway (same heritage); the adapter only interprets the few it needs:

* ``0``  DISPATCH    — an event (we care about ``MESSAGE_CREATE``,
  ``C2C_MESSAGE_CREATE``, ``GROUP_AT_MESSAGE_CREATE``,
  ``DIRECT_MESSAGE_CREATE``).
* ``1``  HEARTBEAT   — server asks for an immediate heartbeat.
* ``2``  IDENTIFY    — sent by us after HELLO with token + intents.
* ``7``  RECONNECT   — server asks us to reconnect (clean RESUME).
* ``9``  INVALID_SESSION — IDENTIFY again from scratch.
* ``10`` HELLO       — carries ``heartbeat_interval`` (ms).
* ``11`` HEARTBEAT_ACK.

## Reconnect schedule

``1s → 2s → 5s → 10s → 30s`` (then saturates) — mirrors
:mod:`corlinman_channels.onebot` so the channel surface stays uniform
across QQ deployments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
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

_log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_API_BASE",
    "DEFAULT_INTENTS",
    "DEFAULT_SANDBOX_API_BASE",
    "DEFAULT_TOKEN_ENDPOINT",
    "RECONNECT_SCHEDULE",
    "QqOfficialAdapter",
    "QqOfficialConfig",
    "binding_from_payload",
    "extract_message_text",
    "extract_msg_id",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Production API base — the 官方 QQ Bot platform's sgroup endpoint.
DEFAULT_API_BASE: str = "https://api.sgroup.qq.com"

#: Sandbox API base — the QQ Bot platform's testing endpoint. Operators
#: flip ``sandbox=True`` in :class:`QqOfficialConfig` to switch.
DEFAULT_SANDBOX_API_BASE: str = "https://sandbox.api.sgroup.qq.com"

#: Access-token exchange endpoint. Same host for sandbox + production —
#: Tencent uses the bots.qq.com auth service regardless of API plane.
DEFAULT_TOKEN_ENDPOINT: str = "https://bots.qq.com/app/getAppAccessToken"

#: Default intents bitmask. Tuned for the **public-domain** bot type
#: (the only mode most operators ship): receive C2C + group@bot events,
#: plus public guild messages and direct messages. Each bit:
#:
#: * ``1 << 9`` (512)        GUILDS — channel create/update/delete
#: * ``1 << 12`` (4096)      DIRECT_MESSAGE — DM in频道
#: * ``1 << 25`` (33554432)  PUBLIC_GUILD_AND_C2C_MESSAGE_RECEIVE — the
#:   ``C2C_MESSAGE_CREATE`` + ``GROUP_AT_MESSAGE_CREATE`` events that
#:   carry the new "QQ + 群@机器人 + C2C 私聊" surface (the most
#:   useful slice for a public bot today).
#: * ``1 << 30`` (1073741824) PUBLIC_GUILD_MESSAGES — ``@-mention`` in
#:   public 频道 messages.
#:
#: Sum: 1073741824 + 33554432 + 4096 + 512 = ``1107300864``.
#: Operators that want richer guild events override via ``intents`` in
#: :class:`QqOfficialConfig`.
DEFAULT_INTENTS: int = (1 << 9) | (1 << 12) | (1 << 25) | (1 << 30)

#: Backoff schedule (seconds) between reconnect attempts. Last entry
#: repeats. Matches :data:`corlinman_channels.onebot.RECONNECT_SCHEDULE`.
RECONNECT_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

# Gateway opcodes (only the ones the adapter interprets).
_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

# Dispatch event types that surface as user messages.
_EVENT_GUILD_AT_MESSAGE = "AT_MESSAGE_CREATE"
_EVENT_GUILD_MESSAGE = "MESSAGE_CREATE"
_EVENT_DIRECT_MESSAGE = "DIRECT_MESSAGE_CREATE"
_EVENT_GROUP_AT_MESSAGE = "GROUP_AT_MESSAGE_CREATE"
_EVENT_C2C_MESSAGE = "C2C_MESSAGE_CREATE"

#: All routable inbound event names. The dispatch pump enqueues only
#: payloads whose ``t`` is one of these — the rest (READY, RESUMED,
#: GUILD_CREATE, ...) drive lifecycle but never surface to the chat
#: backend.
_ROUTABLE_EVENTS = frozenset({
    _EVENT_GUILD_AT_MESSAGE,
    _EVENT_GUILD_MESSAGE,
    _EVENT_DIRECT_MESSAGE,
    _EVENT_GROUP_AT_MESSAGE,
    _EVENT_C2C_MESSAGE,
})


# ===========================================================================
# Config
# ===========================================================================


@dataclass(slots=True)
class QqOfficialConfig:
    """Configuration for :class:`QqOfficialAdapter`.

    ``app_id`` + ``app_secret`` are the credentials from the QQ Bot
    Developer Portal (https://bot.q.qq.com). The adapter exchanges them
    for a short-lived access token via the bots.qq.com auth service.

    ``sandbox=True`` swaps the API base to
    :data:`DEFAULT_SANDBOX_API_BASE` — operators flip it during dev so
    test bots don't hit the production rate-limit pool. The token
    endpoint stays the same regardless (Tencent only operates one).
    """

    app_id: str
    app_secret: str
    sandbox: bool = False
    intents: int = DEFAULT_INTENTS
    reconnect_schedule: tuple[float, ...] = RECONNECT_SCHEDULE
    api_base_override: str | None = None
    token_endpoint: str = DEFAULT_TOKEN_ENDPOINT

    @property
    def api_base(self) -> str:
        """Resolved API base (override > sandbox flag > production)."""
        if self.api_base_override:
            return self.api_base_override
        return DEFAULT_SANDBOX_API_BASE if self.sandbox else DEFAULT_API_BASE


# ===========================================================================
# Pure helpers — extracted so unit tests can hit them without a network.
# ===========================================================================


def extract_message_text(payload: dict[str, Any]) -> str:
    """Flatten a QQ Official message payload into plain text.

    The 官方 platform delivers the message body as ``content`` (a plain
    string for the C2C / group cases). Guild messages carry the same
    field with ``<@!user_id>`` mention tokens we strip — same pattern
    as the Discord adapter's ``_strip_mention``.
    """
    content = payload.get("content")
    if isinstance(content, str):
        return content
    return ""


def _classify_qq_attachment(att: dict[str, Any]) -> AttachmentKind:
    """Classify a QQ Official ``attachments`` entry.

    QQ ships ``content_type`` (``"image/jpeg"``, ``"voice"``, ...) on
    most; we map the coarse leading token, falling back to DOCUMENT.
    """
    ctype = str(att.get("content_type") or "").lower()
    if ctype.startswith("image") or att.get("width") or att.get("height"):
        return AttachmentKind.IMAGE
    if ctype.startswith(("audio", "voice")):
        return AttachmentKind.AUDIO
    if ctype.startswith("video"):
        return AttachmentKind.VIDEO
    return AttachmentKind.DOCUMENT


def extract_attachments(payload: dict[str, Any]) -> list[Attachment]:
    """Extract :class:`Attachment` descriptors from a QQ Official message.

    The 官方 platform pre-uploads media and ships an ``attachments`` array
    where each entry carries a fetchable ``url`` (sometimes scheme-less,
    so we normalise to ``https://``), a ``content_type`` and a
    ``filename``. Guild-message rich media also appears here.
    """
    out: list[Attachment] = []
    for att in payload.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        url = str(att.get("url") or "")
        if not url:
            continue
        if url.startswith("//"):
            url = f"https:{url}"
        elif not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        out.append(
            Attachment(
                kind=_classify_qq_attachment(att),
                url=url,
                mime=att.get("content_type") or None,
                file_name=att.get("filename") or None,
            )
        )
    return out


def sender_display_name(payload: dict[str, Any]) -> str | None:
    """Best-effort author display name for group attribution.

    Guild messages carry a rich ``author`` (``username``) and a
    ``member.nick``; C2C / group@bot messages only expose openids, so
    this returns ``None`` for those (the agent resolves via the binding).
    """
    member = payload.get("member")
    if isinstance(member, dict) and member.get("nick"):
        return str(member["nick"])
    author = payload.get("author")
    if isinstance(author, dict) and author.get("username"):
        return str(author["username"])
    return None


def extract_msg_id(payload: dict[str, Any]) -> str | None:
    """Return the inbound message id used to thread replies.

    The 官方 platform's REST send endpoints require every reply to echo
    back either ``msg_id`` (inbound message id) or ``event_id`` (push
    event id) within a 5-minute window — without it the message is
    rejected as "passive reply window expired". We prefer ``msg_id``
    (matches the explicit user message) and fall back to ``event_id``
    when the inbound payload didn't carry one.
    """
    raw = payload.get("id") or payload.get("msg_id") or payload.get("event_id")
    if raw is None:
        return None
    text = str(raw)
    return text if text else None


def binding_from_payload(
    *,
    event_type: str,
    payload: dict[str, Any],
    bot_app_id: str,
) -> ChannelBinding:
    """Build a :class:`ChannelBinding` for a QQ Official inbound event.

    The channel slug is ``"qq_official"`` so downstream
    :class:`~corlinman_channels.common.ChannelBinding.session_key`
    digests stay distinct from the OneBot ``"qq"`` slug — the two
    transports address the same user pool but with different ids
    (openid vs QQ uin).

    * ``GUILD_MESSAGE_CREATE`` / ``AT_MESSAGE_CREATE`` — thread =
      ``channel_id``, sender = author ``id``.
    * ``DIRECT_MESSAGE_CREATE`` — thread = ``guild_id``, sender = author
      ``id``. (Guild DMs carry a virtual guild id.)
    * ``C2C_MESSAGE_CREATE`` — thread = sender = ``author.user_openid``.
    * ``GROUP_AT_MESSAGE_CREATE`` — thread = ``group_openid``,
      sender = ``author.member_openid``.
    """
    author = payload.get("author") or {}
    if event_type == _EVENT_C2C_MESSAGE:
        openid = ""
        if isinstance(author, dict):
            openid = str(author.get("user_openid") or author.get("id") or "")
        return ChannelBinding(
            channel="qq_official",
            account=bot_app_id,
            thread=openid,
            sender=openid,
        )
    if event_type == _EVENT_GROUP_AT_MESSAGE:
        group = str(payload.get("group_openid", ""))
        member = ""
        if isinstance(author, dict):
            member = str(
                author.get("member_openid")
                or author.get("user_openid")
                or author.get("id")
                or ""
            )
        return ChannelBinding(
            channel="qq_official",
            account=bot_app_id,
            thread=group,
            sender=member or group,
        )
    # Guild / direct message — both use channel_id or guild_id as thread.
    channel_id = str(payload.get("channel_id") or payload.get("guild_id") or "")
    sender_id = ""
    if isinstance(author, dict):
        sender_id = str(author.get("id", ""))
    return ChannelBinding(
        channel="qq_official",
        account=bot_app_id,
        thread=channel_id,
        sender=sender_id or channel_id,
    )


def _strip_mention(content: str) -> str:
    """Remove ``<@!user_id>`` / ``<@user_id>`` mention tokens from
    Guild message content. Same idea as the Discord adapter — keeps the
    text the chat backend sees clean.
    """
    import re

    return re.sub(r"<@!?\d+>", " ", content).strip()


# ===========================================================================
# Adapter
# ===========================================================================


class QqOfficialAdapter:
    """Gateway (WebSocket) client for the 官方 QQ Bot platform.

    Same surface as the sibling adapters (``OneBot`` / ``Discord`` /
    ``Slack`` / ``Feishu``):

    * ``async with adapter:`` (or :meth:`connect`) → dial the WS and
      run the IDENTIFY handshake.
    * ``async for event in adapter.inbound():`` → yields normalized
      :class:`InboundEvent` objects (one per accepted message).
    * :meth:`close` (or the ``async with`` exit) → graceful shutdown.

    Internally:

    * a background reader task owns the gateway connection + heartbeat;
    * decoded message events land on a **bounded** queue with
      drop-oldest under burst (matches the OneBot fix) so a slow chat
      backend can't block the WS reader → reconnect storm;
    * the access token is refreshed lazily via an internal
      :class:`asyncio.Lock` so multiple concurrent sends share a single
      token-exchange request.
    """

    def __init__(
        self,
        config: QqOfficialConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not config.app_id:
            raise ConfigError("QqOfficialConfig.app_id is empty")
        if not config.app_secret:
            raise ConfigError("QqOfficialConfig.app_secret is empty")
        self._cfg = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0)
        )
        self._closed = False
        # Bounded queue with drop-oldest fallback — same shape as OneBot.
        self._inbound_q: asyncio.Queue[tuple[str, dict[str, Any]]] = (
            asyncio.Queue(maxsize=64)
        )
        self._inbound_dropped: int = 0
        self._reader_task: asyncio.Task[None] | None = None
        # Access-token cache + single-flight lock.
        self._token: str = ""
        self._token_expiry: float = 0.0
        self._token_lock: asyncio.Lock = asyncio.Lock()
        # Updated on every parsed event so the QQ health watcher can
        # flag a kicked-offline bot.
        self._last_event_at_ms: int | None = None
        # Session bookkeeping (currently we re-IDENTIFY on every
        # reconnect — RESUME is not implemented yet but the slots are
        # reserved so the protocol-compliant path can land later).
        self._session_id: str | None = None
        self._last_seq: int | None = None

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def inbound_dropped_count(self) -> int:
        """Total inbound events dropped because the consumer fell behind.

        Mirrors :attr:`OneBotAdapter.inbound_dropped_count` — a non-zero
        value means the chat service couldn't keep up with the inbound
        burst and the drop-oldest fallback kicked in.
        """
        return self._inbound_dropped

    @property
    def last_event_at_ms(self) -> int | None:
        """Wall-clock ms when the last inbound event was parsed.

        ``None`` before the first event lands. The QQ Official health
        watcher polls this so operators can spot long silences (account
        kicked / network blackhole).
        """
        return self._last_event_at_ms

    @property
    def api_base(self) -> str:
        """Resolved REST API base — sandbox vs production."""
        return self._cfg.api_base

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> QqOfficialAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Spawn the background gateway reader.

        Doesn't block on the actual WS connect — that happens inside
        the reader so reconnect logic stays in one place. The first call
        to :meth:`inbound` will start yielding once the gateway lands.
        """
        if self._reader_task is not None:
            return
        self._closed = False
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="qq_official-reader"
        )

    async def close(self) -> None:
        """Stop the gateway reader and (if we own it) the HTTP client."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._owns_client:
            with suppress(Exception):
                await self._client.aclose()

    # ------------------------------------------------------------------
    # Access token — single-flight refresh
    # ------------------------------------------------------------------

    async def access_token(self) -> str:
        """Return a current access token, refreshing under the lock if needed.

        The token is good for 7200s; we refresh ~5 minutes before
        expiry. Concurrent callers serialise on :attr:`_token_lock` so
        a burst of parallel sends triggers exactly one token-exchange
        request — see the ``test_access_token_refresh_single_flight``
        test for the contract.
        """
        now = time.monotonic()
        if self._token and now < self._token_expiry:
            return self._token
        async with self._token_lock:
            # Re-check under the lock: another caller may have refreshed
            # while we were waiting.
            now = time.monotonic()
            if self._token and now < self._token_expiry:
                return self._token
            try:
                resp = await self._client.post(
                    self._cfg.token_endpoint,
                    json={
                        "appId": self._cfg.app_id,
                        "clientSecret": self._cfg.app_secret,
                    },
                )
            except httpx.HTTPError as exc:
                raise TransportError(
                    f"qq_official token exchange failed: {exc}"
                ) from exc
            if resp.status_code >= 400:
                raise TransportError(
                    f"qq_official token exchange HTTP {resp.status_code}"
                )
            try:
                env = resp.json()
            except ValueError as exc:
                raise TransportError(
                    f"qq_official token invalid JSON: {exc}"
                ) from exc
            if not isinstance(env, dict):
                raise TransportError(
                    "qq_official token response was not a JSON object"
                )
            token = str(env.get("access_token") or "")
            if not token:
                raise TransportError(
                    f"qq_official token exchange returned no token: {env}"
                )
            try:
                expires_in = int(env.get("expires_in", 7200))
            except (TypeError, ValueError):
                expires_in = 7200
            # Refresh ~5 minutes early so an in-flight send never holds
            # an about-to-expire token.
            self._token = token
            self._token_expiry = now + max(expires_in - 300, 60)
            return token

    # ------------------------------------------------------------------
    # Inbound iterator
    # ------------------------------------------------------------------

    async def inbound(self) -> AsyncIterator[InboundEvent[dict[str, Any]]]:
        """Yield one :class:`InboundEvent` per accepted inbound message.

        Surfaces:

        * ``C2C_MESSAGE_CREATE`` — single-user direct messages
          (``thread`` = openid).
        * ``GROUP_AT_MESSAGE_CREATE`` — group @bot messages
          (``thread`` = group_openid).
        * ``DIRECT_MESSAGE_CREATE`` — guild private messages.
        * ``AT_MESSAGE_CREATE`` / ``MESSAGE_CREATE`` — guild channel
          messages (``thread`` = channel_id).

        The raw event payload + dispatch type are stashed on
        ``InboundEvent.payload`` so the channel handler can route to
        the correct REST endpoint when sending the reply.
        """
        if self._reader_task is None:
            await self.connect()
        while not self._closed:
            try:
                event_type, payload = await self._inbound_q.get()
            except asyncio.CancelledError:
                return
            text = extract_message_text(payload)
            if event_type in (_EVENT_GUILD_AT_MESSAGE, _EVENT_GUILD_MESSAGE):
                text = _strip_mention(text)
            attachments = extract_attachments(payload)
            if not text.strip() and not attachments:
                continue
            binding = binding_from_payload(
                event_type=event_type,
                payload=payload,
                bot_app_id=self._cfg.app_id,
            )
            msg_id = extract_msg_id(payload)
            ts = _parse_timestamp(payload.get("timestamp"))
            # Stash the event_type on the payload so the channel handler
            # can pick the right send endpoint without re-parsing.
            payload_with_type = dict(payload)
            payload_with_type["_qq_official_event_type"] = event_type
            yield InboundEvent(
                channel="qq_official",
                binding=binding,
                text=text,
                message_id=msg_id,
                timestamp=ts,
                # C2C + group@bot + DM are implicitly addressed; guild
                # channel messages of type AT_MESSAGE_CREATE explicitly
                # @-mention the bot. Plain MESSAGE_CREATE flows through
                # only when the bot owns the channel (rare); we mark it
                # as un-mentioned so the router can drop it if desired.
                mentioned=event_type != _EVENT_GUILD_MESSAGE,
                attachments=attachments,
                payload=payload_with_type,
                sender_name=sender_display_name(payload),
            )

    # ------------------------------------------------------------------
    # Gateway reader loop
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Connect → pump → on disconnect, sleep + retry.

        Mirrors :meth:`OneBotAdapter._reader_loop` — the backoff index
        resets to 0 after a clean disconnect and grows monotonically
        across consecutive failures.
        """
        attempt = 0
        schedule = self._cfg.reconnect_schedule or RECONNECT_SCHEDULE
        while not self._closed:
            try:
                await self._connect_once()
                if self._closed:
                    return
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning(
                    "qq_official gateway loop iteration failed: %s", exc
                )
            if self._closed:
                return
            delay = schedule[min(attempt, len(schedule) - 1)]
            attempt += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

    async def _connect_once(self) -> None:
        """One IDENTIFY-to-disconnect cycle."""
        ws_url = await self._discover_gateway_url()
        heartbeat_task: asyncio.Task[None] | None = None
        async with websockets.connect(ws_url, max_size=2 ** 23) as ws:
            hello = await self._recv_json(ws)
            if hello.get("op") != _OP_HELLO:
                raise TransportError(
                    "qq_official gateway: expected HELLO frame"
                )
            interval_ms = int(
                (hello.get("d") or {}).get("heartbeat_interval", 41250)
            )

            await self._send_identify(ws)
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(ws, interval_ms / 1000.0),
                name="qq_official-heartbeat",
            )
            try:
                await self._pump(ws)
            finally:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _discover_gateway_url(self) -> str:
        """``GET /gateway/bot`` → an authenticated ``wss://`` URL.

        The 官方 platform partitions gateway URLs per bot for shard
        scheduling; the returned URL is single-bot and short-lived, so
        we fetch a fresh one on every reconnect.
        """
        token = await self.access_token()
        try:
            resp = await self._client.get(
                f"{self._cfg.api_base}/gateway/bot",
                headers=self._auth_headers(token),
            )
        except httpx.HTTPError as exc:
            raise TransportError(
                f"qq_official gateway lookup failed: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise TransportError(
                f"qq_official gateway lookup HTTP {resp.status_code}"
            )
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(
                f"qq_official gateway invalid JSON: {exc}"
            ) from exc
        url = env.get("url") if isinstance(env, dict) else None
        if not isinstance(url, str) or not url:
            raise TransportError(
                "qq_official gateway lookup returned no URL"
            )
        return url

    async def _pump(self, ws: ClientConnection) -> None:
        """Read dispatch frames and enqueue routable message events."""
        while not self._closed:
            try:
                frame = await self._recv_json(ws)
            except websockets.ConnectionClosed:
                return
            op = frame.get("op")
            seq = frame.get("s")
            if isinstance(seq, int):
                self._last_seq = seq
            # Refresh the heartbeat / health-watcher timestamp on every
            # frame — even non-DISPATCH (heartbeat ACK, hello) traffic
            # is proof the gateway is alive.
            self._last_event_at_ms = int(time.time() * 1000)
            if op == _OP_DISPATCH:
                event_type = frame.get("t")
                if event_type == "READY":
                    inner = frame.get("d") or {}
                    if isinstance(inner, dict):
                        sess = inner.get("session_id")
                        if isinstance(sess, str):
                            self._session_id = sess
                    continue
                if event_type == "RESUMED":
                    continue
                if event_type in _ROUTABLE_EVENTS:
                    payload = frame.get("d")
                    if isinstance(payload, dict):
                        self._enqueue_event(event_type, payload)
            elif op == _OP_HEARTBEAT:
                await self._send_json(ws, {"op": _OP_HEARTBEAT, "d": self._last_seq})
            elif op == _OP_RECONNECT:
                return
            elif op == _OP_INVALID_SESSION:
                # Wipe session bookkeeping; outer loop re-IDENTIFYs.
                self._session_id = None
                self._last_seq = None
                return
            # _OP_HEARTBEAT_ACK / unknown → nothing to do.

    def _enqueue_event(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Burst-absorb a message event with drop-oldest fallback.

        Mirrors the OneBot adapter's bounded-queue strategy: when the
        consumer falls behind we drop the OLDEST event so the most
        recent user message still surfaces. ``_inbound_dropped`` counts
        the drops so operators can spot a consistently slow backend.
        """
        try:
            self._inbound_q.put_nowait((event_type, payload))
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                self._inbound_q.get_nowait()
            self._inbound_dropped += 1
            _log.warning(
                "qq_official.inbound_q.dropped_oldest count=%d",
                self._inbound_dropped,
            )
            with suppress(asyncio.QueueFull):
                self._inbound_q.put_nowait((event_type, payload))

    async def _heartbeat_loop(
        self, ws: ClientConnection, interval: float
    ) -> None:
        """Send an op-1 heartbeat every ``interval`` seconds.

        The 官方 platform closes the connection if heartbeats stop;
        the outer :meth:`_reader_loop` reconnects when that happens.
        Echoes the latest seq number so the server can detect
        gap-recovery scenarios.
        """
        while not self._closed:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            try:
                await self._send_json(
                    ws, {"op": _OP_HEARTBEAT, "d": self._last_seq}
                )
            except (websockets.ConnectionClosed, OSError):
                return

    async def _send_identify(self, ws: ClientConnection) -> None:
        """Send the op-2 IDENTIFY frame with bot token + intents.

        The 官方 platform uses a composite bot token of the form
        ``Bot <app_id>.<access_token>`` (mirrors Discord syntax) so
        we splice it together here from the cached access token.
        """
        token = await self.access_token()
        await self._send_json(
            ws,
            {
                "op": _OP_IDENTIFY,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": self._cfg.intents,
                    "shard": [0, 1],
                    "properties": {
                        "$os": "linux",
                        "$browser": "corlinman",
                        "$device": "corlinman",
                    },
                },
            },
        )

    def _auth_headers(self, token: str) -> dict[str, str]:
        """Authorization header for REST calls.

        The 官方 platform supports both ``Bearer <token>`` and the
        legacy ``Bot <app_id>.<token>`` form; the new ``QQBot``
        form pairs cleanly with the access-token flow and is what
        the platform recommends post-2024.
        """
        return {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self._cfg.app_id,
        }

    @staticmethod
    async def _recv_json(ws: ClientConnection) -> dict[str, Any]:
        """Receive one WS text frame and decode it as a JSON object."""
        raw = await ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise TransportError(
                "qq_official gateway: frame was not a JSON object"
            )
        return obj

    @staticmethod
    async def _send_json(ws: ClientConnection, obj: dict[str, Any]) -> None:
        """Encode ``obj`` as JSON and send it on the WS connection."""
        await ws.send(json.dumps(obj))


# ===========================================================================
# Helpers
# ===========================================================================


def _parse_timestamp(raw: Any) -> int:
    """Best-effort ``timestamp`` → Unix-seconds conversion.

    QQ Official ships timestamps either as ISO-8601 strings (guild
    messages) or as Unix-ms ints (C2C / group). Falls back to ``0``
    (the :class:`InboundEvent` "no timestamp" sentinel) when the value
    is missing or unparseable.
    """
    if raw is None:
        return 0
    if isinstance(raw, int | float):
        ms = int(raw)
        return ms // 1000 if ms > 10_000_000_000 else ms
    if isinstance(raw, str):
        if not raw:
            return 0
        # ISO-8601 first (guild messages).
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(raw).timestamp())
        except (ValueError, OverflowError):
            pass
        # Fall back to a digit string (Unix-ms or seconds).
        try:
            ms = int(raw)
        except ValueError:
            return 0
        return ms // 1000 if ms > 10_000_000_000 else ms
    return 0
