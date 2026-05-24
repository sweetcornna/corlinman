"""Slack Socket Mode (WebSocket) + Web API adapter.

corlinman has no Rust reference for Slack — this is a new channel
modelled on :mod:`corlinman_channels.telegram` so the inbound shape
(``async for event in adapter.inbound()``) and the outbound
:class:`SlackSender` mirror the Telegram pair.

## Transport — Socket Mode

Socket Mode lets a Slack app receive events over an outbound WebSocket
instead of exposing a public HTTP webhook — ideal for corlinman, which
may run behind NAT in a China-region deployment. The flow:

1. ``POST apps.connections.open`` (Web API, ``xapp-`` app-level token)
   returns a single-use ``wss://`` URL.
2. The adapter dials that URL. Slack sends a ``hello`` envelope, then
   ``events_api`` envelopes wrapping the Events API payload.
3. Every envelope carries an ``envelope_id`` — the adapter must echo it
   back as an ``{"envelope_id": ...}`` ack within 3s or Slack retries
   the delivery.
4. ``message`` events are decoded into :class:`InboundEvent` objects.

Outbound replies go through the Web API ``chat.postMessage`` using the
bot token (``xoxb-``).

We implement against the raw protocol over ``websockets`` / ``httpx``
(both already dependencies) rather than pulling in ``slack_sdk`` — the
slice corlinman needs is small.

## Two tokens

* ``app_token`` (``xapp-...``) — opens the Socket Mode connection.
* ``bot_token`` (``xoxb-...``) — authenticates Web API calls
  (``auth.test`` to discover the bot user id, ``chat.postMessage`` to
  reply).

Both are required; a missing one degrades the channel (the caller logs
and skips, never crashes).

## Reply gating

Slack messages arrive from channels, group DMs, or 1:1 DMs. The adapter
mirrors the Telegram gate:

* IM (1:1 DM, ``channel_type == "im"``) always responds.
* Channels respond only when the bot is @-mentioned (Slack delivers a
  distinct ``app_mention`` event for that), unless ``respond_to_all``.
  An optional ``keyword_filter`` narrows channel messages further.
* The bot never replies to its own messages or to other bots.
"""

from __future__ import annotations

import asyncio
import json
import re
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
    ChannelBinding,
    ConfigError,
    InboundEvent,
    TransportError,
)

__all__ = [
    "DEFAULT_API_BASE",
    "SlackAdapter",
    "SlackConfig",
    "SlackSender",
    "binding_from_event",
    "is_mentioning_bot",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Slack Web API base. ``apps.connections.open`` / ``auth.test`` /
#: ``chat.postMessage`` all hang off this.
DEFAULT_API_BASE: str = "https://slack.com/api"

#: Backoff after a Socket Mode failure (seconds). Mirrors the Telegram
#: adapter's ``ERROR_BACKOFF_SECS``.
ERROR_BACKOFF_SECS: float = 5.0


# ===========================================================================
# Config
# ===========================================================================


@dataclass(slots=True)
class SlackConfig:
    """Configuration for :class:`SlackAdapter`.

    ``app_token`` (``xapp-...``) opens the Socket Mode WebSocket;
    ``bot_token`` (``xoxb-...``) authenticates Web API calls. Both are
    required. ``allowed_channel_ids`` (empty == allow all) and
    ``keyword_filter`` (case-insensitive substring; empty == allow all)
    mirror the Telegram gates. ``respond_to_all`` disables the
    mention-required gate for non-DM channels.
    """

    app_token: str
    bot_token: str
    allowed_channel_ids: list[str] = field(default_factory=list)
    keyword_filter: list[str] = field(default_factory=list)
    respond_to_all: bool = False
    api_base: str = DEFAULT_API_BASE


# ===========================================================================
# Mention / binding helpers — pure functions, easy to unit-test.
# ===========================================================================

#: Slack renders a user mention as ``<@U012ABC>`` in message text.
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


def is_mentioning_bot(event: dict[str, Any], bot_user_id: str) -> bool:
    """True iff the Slack ``event`` @-mentions the bot.

    Slack also delivers a dedicated ``app_mention`` event type — the
    adapter treats that as an implicit mention too. This helper covers
    the plain ``message`` event by scanning the rendered ``<@id>`` tokens.
    """
    if not bot_user_id:
        return False
    if event.get("type") == "app_mention":
        return True
    text = event.get("text") or ""
    return bot_user_id in _MENTION_RE.findall(text)


def binding_from_event(event: dict[str, Any], bot_user_id: str) -> ChannelBinding:
    """Build a :class:`ChannelBinding` from a Slack message event.

    ``account`` is the bot user id, ``thread`` the Slack channel id,
    ``sender`` the author's user id. A threaded reply keeps the parent
    channel as ``thread`` so the whole conversation shares one session
    key (Slack's own ``thread_ts`` is preserved in the raw payload).
    """
    channel_id = str(event.get("channel", ""))
    sender_id = str(event.get("user", "")) or channel_id
    return ChannelBinding(
        channel="slack",
        account=bot_user_id,
        thread=channel_id,
        sender=sender_id,
    )


def _strip_mention(text: str, bot_user_id: str) -> str:
    """Remove the leading ``<@bot_user_id>`` mention token from ``text``."""
    if not bot_user_id:
        return text.strip()
    return text.replace(f"<@{bot_user_id}>", " ").strip()


# ===========================================================================
# Adapter
# ===========================================================================


class SlackAdapter:
    """Slack Socket Mode adapter.

    Same surface as the other adapters: ``async with`` for lifecycle,
    ``inbound()`` for the normalized event stream. The Socket Mode
    connection (open → hello → events) runs in a background task; decoded
    ``message`` events land on an internal queue the ``inbound`` iterator
    drains.

    Outbound replies are a separate concern — see :class:`SlackSender`.
    """

    def __init__(
        self,
        config: SlackConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not config.app_token:
            raise ConfigError("SlackConfig.app_token is empty")
        if not config.bot_token:
            raise ConfigError("SlackConfig.bot_token is empty")
        self._cfg = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._closed = False
        self._bot_user_id: str | None = None
        self._inbound_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._reader_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> SlackAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Resolve the bot user id and spawn the Socket Mode loop.

        ``auth.test`` discovers the bot's own user id so the mention gate
        and self-message filter work. A network failure here raises
        :class:`TransportError` so the caller fails fast — the Telegram
        adapter does the same with ``getMe``.
        """
        if self._reader_task is not None:
            return
        self._bot_user_id = await self._auth_test()
        self._closed = False
        self._reader_task = asyncio.create_task(
            self._socket_loop(), name="slack-socket"
        )

    async def close(self) -> None:
        """Stop the Socket Mode loop and (if we own it) the HTTP client."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._owns_client:
            await self._client.aclose()

    @property
    def bot_user_id(self) -> str | None:
        """The bot's Slack user id, available after :meth:`connect`."""
        return self._bot_user_id

    # ------------------------------------------------------------------
    # Inbound iterator
    # ------------------------------------------------------------------

    async def inbound(self) -> AsyncIterator[InboundEvent[dict[str, Any]]]:
        """Yield one :class:`InboundEvent` per accepted inbound message.

        Filtering rules (parallel to the Telegram adapter):

        * skip the bot's own messages, other bots' messages, and
          non-user message subtypes (joins, edits, ...);
        * ``allowed_channel_ids`` whitelist (empty = allow all);
        * non-DM channels: require an @-mention unless ``respond_to_all``,
          then apply the optional keyword filter;
        * empty / whitespace-only text is silently skipped.
        """
        if self._reader_task is None:
            await self.connect()
        assert self._bot_user_id is not None  # connect() guarantees this
        bot_user_id = self._bot_user_id
        while not self._closed:
            try:
                event = await self._inbound_q.get()
            except asyncio.CancelledError:
                return

            # Drop message subtypes (channel_join, message_changed, ...)
            # and bot-authored messages — only real user text routes.
            subtype = event.get("subtype")
            if subtype is not None and subtype != "":
                continue
            if event.get("bot_id"):
                continue
            author_id = str(event.get("user", ""))
            if author_id == bot_user_id or not author_id:
                continue

            channel_id = str(event.get("channel", ""))
            if not self._channel_allowed(channel_id):
                continue

            is_dm = event.get("channel_type") == "im"
            mentioned = is_mentioning_bot(event, bot_user_id)
            if not is_dm:
                if not self._cfg.respond_to_all and not mentioned:
                    continue
                if not mentioned and not self._keyword_match(event):
                    continue

            text = _strip_mention(event.get("text") or "", bot_user_id)
            if not text.strip():
                continue

            binding = binding_from_event(event, bot_user_id)
            yield InboundEvent(
                channel="slack",
                binding=binding,
                text=text,
                message_id=str(event.get("ts", "")) or None,
                timestamp=_parse_ts(event.get("ts")),
                mentioned=mentioned or is_dm,
                attachments=[],  # multimodal download is out of scope here
                payload=event,
            )

    # ------------------------------------------------------------------
    # Socket Mode loop
    # ------------------------------------------------------------------

    async def _socket_loop(self) -> None:
        """Open a Socket Mode connection and pump envelopes forever.

        Reconnects on any transport failure with a fixed backoff —
        matching the Telegram poll loop's resilience contract. Exits
        promptly once :meth:`close` flips ``self._closed``.
        """
        while not self._closed:
            try:
                ws_url = await self._open_connection()
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
        """Drive a single Socket Mode WebSocket from hello to close."""
        async with websockets.connect(ws_url, max_size=2 ** 23) as ws:
            while not self._closed:
                try:
                    frame = await self._recv_json(ws)
                except websockets.ConnectionClosed:
                    return

                env_type = frame.get("type")
                # Slack expects an ack echoing ``envelope_id`` within 3s.
                envelope_id = frame.get("envelope_id")
                if envelope_id is not None:
                    with suppress(websockets.ConnectionClosed, OSError):
                        await self._send_json(ws, {"envelope_id": envelope_id})

                if env_type == "hello":
                    continue
                if env_type == "disconnect":
                    # Slack rotates Socket Mode connections — reconnect.
                    return
                if env_type in ("events_api", "slash_commands", "interactive"):
                    self._handle_events_api(frame)

    def _handle_events_api(self, frame: dict[str, Any]) -> None:
        """Extract the inner Events API ``event`` and enqueue messages."""
        payload = frame.get("payload") or {}
        if not isinstance(payload, dict):
            return
        event = payload.get("event")
        if not isinstance(event, dict):
            return
        # Only ``message`` / ``app_mention`` carry routable user text.
        if event.get("type") not in ("message", "app_mention"):
            return
        if self._closed:
            return
        with suppress(asyncio.QueueFull):
            self._inbound_q.put_nowait(event)

    # ------------------------------------------------------------------
    # Web API primitives
    # ------------------------------------------------------------------

    async def _open_connection(self) -> str:
        """``apps.connections.open`` → a single-use ``wss://`` URL."""
        env = await self._post_api(
            "apps.connections.open", self._cfg.app_token, {}
        )
        ws_url = env.get("url")
        if not isinstance(ws_url, str) or not ws_url:
            raise TransportError("slack apps.connections.open returned no url")
        return ws_url

    async def _auth_test(self) -> str:
        """``auth.test`` → the bot's own Slack user id."""
        env = await self._post_api("auth.test", self._cfg.bot_token, {})
        user_id = env.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise TransportError("slack auth.test returned no user_id")
        return user_id

    async def _post_api(
        self, method: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """POST to a Slack Web API ``method`` and lift the ``ok`` envelope.

        Slack always returns HTTP 200 with an ``{"ok": bool}`` body; an
        ``ok: false`` carries an ``error`` slug. Raises
        :class:`TransportError` on transport failure or ``ok: false``.
        """
        try:
            resp = await self._client.post(
                f"{self._cfg.api_base}/{method}",
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"slack {method} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(f"slack {method} HTTP {resp.status_code}")
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(f"slack {method} invalid JSON: {exc}") from exc
        if not isinstance(env, dict):
            raise TransportError(f"slack {method} response was not an object")
        if not env.get("ok"):
            raise TransportError(
                f"slack {method} error: {env.get('error', '<no error>')}"
            )
        return env

    @staticmethod
    async def _recv_json(ws: ClientConnection) -> dict[str, Any]:
        """Receive one WS text frame and decode it as a JSON object."""
        raw = await ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise TransportError("slack socket: frame was not a JSON object")
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

    def _keyword_match(self, event: dict[str, Any]) -> bool:
        filter_ = self._cfg.keyword_filter
        if not filter_:
            return True
        lower = (event.get("text") or "").lower()
        return any(kw.lower() in lower for kw in filter_)


# ===========================================================================
# Outbound sender
# ===========================================================================


class SlackSender:
    """Thin client over the Slack Web API, scoped to outbound.

    Parallel to :class:`corlinman_channels.telegram_send.TelegramSender`.
    Construct once per bot token and reuse — the underlying
    :class:`httpx.AsyncClient` connection pool is the real cost.

    The "decorative" endpoints (``post_typing`` + ``update_message``)
    share a single 429 back-off deadline like the Telegram / Discord
    senders. Slack returns 429 with a ``Retry-After`` header; the
    helper extracts it and silently skips further calls until the
    window expires.
    """

    __slots__ = ("_edit_rate_limit_until", "base", "client", "token")

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        base: str = DEFAULT_API_BASE,
    ) -> None:
        self.client = client
        self.token = token
        self.base = base
        self._edit_rate_limit_until: float = 0.0

    async def send_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> str:
        """POST ``chat.postMessage``. Returns the new message ``ts``.

        When ``thread_ts`` is supplied the reply is posted into that
        thread so the conversation stays grouped — parallel to the
        Telegram ``reply_to_message_id``.
        """
        body: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts is not None:
            body["thread_ts"] = thread_ts
        try:
            resp = await self.client.post(
                f"{self.base}/chat.postMessage",
                json=body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"slack chat.postMessage failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(
                f"slack chat.postMessage HTTP {resp.status_code}"
            )
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(
                f"slack chat.postMessage invalid JSON: {exc}"
            ) from exc
        if not isinstance(env, dict) or not env.get("ok"):
            err = env.get("error", "<no error>") if isinstance(env, dict) else "?"
            raise TransportError(f"slack chat.postMessage error: {err}")
        return str(env.get("ts", ""))

    async def post_typing(
        self, channel: str, thread_ts: str | None = None
    ) -> None:
        """Slack has no per-thread typing indicator for bot users —
        ``users.profile.set`` with a fake status would work but is
        invasive (mutates the user's visible status) and the official
        SDK doesn't expose anything cleaner.

        This stub exists for parity with Telegram / Discord so the
        per-channel handler can call ``sender.post_typing(...)`` without
        a transport-specific branch. The call is a no-op; the mutable-
        spinner edits are still the user-visible "I'm working" signal.
        """
        # Intentionally empty — see docstring.
        _ = (channel, thread_ts)

    async def update_message(
        self, channel: str, ts: str, text: str
    ) -> None:
        """POST ``chat.update``. Mutates an earlier message in place —
        used as the "mutable spinner line" while tool calls land.

        Best-effort: any non-2xx is swallowed so a re-fire of the same
        content (or a 429) never breaks the turn. HTTP 429 updates a
        shared back-off so subsequent updates / typing pulses silently
        skip until the window expires. Mirrors
        :meth:`TelegramSender.edit_message_text`.
        """
        if time.time() < self._edit_rate_limit_until:
            return
        body: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        try:
            resp = await self.client.post(
                f"{self.base}/chat.update",
                json=body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
        except httpx.HTTPError:
            return
        if resp.status_code == 429:
            self._note_retry_after(resp)

    async def upload_file(
        self,
        channels: str,
        path: Path,
        *,
        filename: str | None = None,
        initial_comment: str | None = None,
        thread_ts: str | None = None,
    ) -> str:
        """POST ``files.upload`` with a multipart ``file`` attachment.

        Returns the new file id. The legacy ``files.upload`` endpoint
        still works and matches the simple multipart pattern the
        Telegram / Discord senders already use; the newer
        ``files.getUploadURLExternal`` / ``files.completeUploadExternal``
        flow is more efficient but requires two round-trips and is
        unnecessary for the modest file sizes the agent emits.

        Raises :class:`TransportError` on transport / API failure — the
        channel handler folds the error into a friendly status line.
        """
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise TransportError(f"slack file read failed: {exc}") from exc

        name = filename or path.name or "file.bin"
        # ``files.upload`` accepts ``multipart/form-data`` with the file
        # part named ``file``. Other fields (channels, initial_comment,
        # thread_ts, filename) ride as form parts alongside it.
        form: dict[str, Any] = {
            "channels": channels,
            "filename": name,
        }
        if initial_comment is not None:
            form["initial_comment"] = initial_comment
        if thread_ts is not None:
            form["thread_ts"] = thread_ts
        try:
            resp = await self.client.post(
                f"{self.base}/files.upload",
                data=form,
                files={"file": (name, data, "application/octet-stream")},
                headers={"Authorization": f"Bearer {self.token}"},
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"slack files.upload failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(f"slack files.upload HTTP {resp.status_code}")
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(f"slack files.upload invalid JSON: {exc}") from exc
        if not isinstance(env, dict) or not env.get("ok"):
            err = env.get("error", "<no error>") if isinstance(env, dict) else "?"
            raise TransportError(f"slack files.upload error: {err}")
        file_obj = env.get("file")
        if isinstance(file_obj, dict):
            return str(file_obj.get("id", ""))
        return ""

    def _note_retry_after(self, resp: httpx.Response) -> None:
        """Extend the shared 429 back-off using Slack's ``Retry-After``
        header. Falls back to a 1s penalty when missing or unparseable.
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


def _parse_ts(raw: Any) -> int:
    """Best-effort Slack ``ts`` → Unix-seconds conversion.

    Slack timestamps are ``"<seconds>.<microseconds>"`` strings. Falls
    back to ``0`` (the :class:`InboundEvent` "no timestamp" sentinel) when
    the value is missing or unparseable.
    """
    if not isinstance(raw, str) or not raw:
        return 0
    try:
        return int(float(raw))
    except (ValueError, OverflowError):
        return 0
