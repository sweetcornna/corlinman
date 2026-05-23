"""Channel orchestration helpers — wire an adapter to a chat backend.

Python port of ``rust/.../service.rs`` + the orchestration in
``rust/.../telegram/service.rs``. Provides two ``run_*_channel``
helpers and a ``ChatServiceLike`` Protocol so the per-channel reply
loops stay structurally symmetric with the Rust crate.

## Flow per inbound event

1. The adapter (``OneBotAdapter`` / ``TelegramAdapter``) delivers a
   normalized :class:`InboundEvent`.
2. The router applies keyword / @mention gating and produces a
   :class:`RoutedRequest` (only OneBot today; the Telegram adapter
   already does its own gating in ``inbound()``).
3. A reply coroutine is spawned per accepted message so a slow
   reasoning loop doesn't block the next inbound event.
4. The coroutine calls ``chat_service.run(...)``, collects every
   ``TokenDelta``, and on ``Done`` posts an outbound action / reply.

## Deliberate deviations

- Rust spawns ``tokio::task`` per accepted message; we use
  ``asyncio.create_task``. Behaviour is equivalent on a single-threaded
  asyncio runtime.
- Rust uses ``mpsc`` reply channels typed to the OneBot ``Action``;
  Python keeps them as ``asyncio.Queue`` with the same wire types
  (``Action`` from :mod:`corlinman_channels.onebot`).
- The Telegram outbound path goes through :class:`TelegramSender`
  rather than a reply channel — the Rust crate does the same as of
  the webhook split, so this is parity, not deviation.
- ``ChatService`` is structural (Protocol) so we can decouple from
  ``corlinman-server`` at module load. Pass any object whose ``run``
  yields ``(role, text)``-shaped events.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

_log = logging.getLogger(__name__)


async def _try_open_inbox() -> Any:
    """T4.3 — best-effort lazy open of the durable inbox.

    Tolerates corlinman-server not being importable (standalone
    channel tests) — returns None and the dispatch loop runs without
    inbox recording, exactly as before T4.3 landed.
    """
    try:
        from corlinman_server.inbox import Inbox  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — module isn't required
        return None
    import os
    from pathlib import Path

    raw = os.environ.get("CORLINMAN_DATA_DIR")
    data_dir = Path(raw) if raw else Path.home() / ".corlinman"
    try:
        inbox = await Inbox.open(data_dir / "inbox.sqlite")
    except Exception as exc:  # noqa: BLE001 — degrade silently
        _log.warning("qq inbox open failed: %s", exc)
        return None
    # Boot-time housekeeping — only runs once per channel start.
    try:
        stale = await inbox.reset_stale_dispatched()
        pending = await inbox.list_pending(channel="qq", limit=20)
        if stale or pending:
            _log.info(
                "qq inbox boot: stale_reset=%d pending=%d", stale, len(pending)
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning("qq inbox boot sweep failed: %s", exc)
    return inbox

from corlinman_channels.common import InboundEvent
from corlinman_channels.discord import (
    DEFAULT_GATEWAY_URL,
    DEFAULT_REST_BASE,
    DiscordAdapter,
    DiscordConfig,
    DiscordSender,
)
from corlinman_channels.feishu import (
    DEFAULT_API_BASE as FEISHU_API_BASE,
)
from corlinman_channels.feishu import (
    FeishuAdapter,
    FeishuConfig,
    FeishuSender,
)
from corlinman_channels.onebot import (
    Action,
    MessageEvent,
    MessageType,
    OneBotAdapter,
    OneBotConfig,
    SendGroupMsg,
    SendPrivateMsg,
    TextSegment,
)
from corlinman_channels.rate_limit import TokenBucket
from corlinman_channels.router import ChannelRouter, GroupKeywords, RoutedRequest
from corlinman_channels.slack import (
    DEFAULT_API_BASE as SLACK_API_BASE,
)
from corlinman_channels.slack import (
    SlackAdapter,
    SlackConfig,
    SlackSender,
)
from corlinman_channels.telegram import TelegramAdapter, TelegramConfig
from corlinman_channels.telegram_send import TelegramSender

__all__ = [
    "ChatEventLike",
    "ChatServiceLike",
    "DiscordChannelParams",
    "FeishuChannelParams",
    "QqChannelParams",
    "SlackChannelParams",
    "TelegramChannelParams",
    "handle_one_discord",
    "handle_one_feishu",
    "handle_one_qq",
    "handle_one_slack",
    "handle_one_telegram",
    "run_discord_channel",
    "run_feishu_channel",
    "run_qq_channel",
    "run_slack_channel",
    "run_telegram_channel",
]


# ---------------------------------------------------------------------------
# Chat-service protocol — structural, decouples this package from the
# concrete corlinman-server types.
# ---------------------------------------------------------------------------


class ChatEventLike(Protocol):
    """One streamed event from the chat backend. The Rust crate has
    a closed enum (``TokenDelta`` / ``ToolCall`` / ``Done`` /
    ``Error``); we accept any object with a ``kind`` discriminator
    string and optional ``text`` / ``error`` attributes."""

    kind: str
    """``"token_delta"`` | ``"tool_call"`` | ``"done"`` | ``"error"``."""

    text: str
    """For ``token_delta``: the delta string."""

    error: str
    """For ``error``: the error message."""


class ChatServiceLike(Protocol):
    """Minimal chat-service surface the orchestration helpers consume.

    Mirrors ``ChatService::run`` in the Rust gateway-api crate; the
    Python ``ChatService`` Protocol in ``corlinman-server`` happens
    to satisfy this shape (modulo the event field names — see
    :func:`_event_kind`)."""

    async def run(
        self,
        request: Any,
        cancel: asyncio.Event,
    ) -> AsyncIterator[Any]:
        """Run one chat turn. Yields events until done. ``cancel.set()``
        should cause the iterator to terminate ASAP."""
        ...


# ---------------------------------------------------------------------------
# QQ channel
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class QqChannelParams:
    """Parameters for :func:`run_qq_channel`. Mirrors Rust
    ``QqChannelParams`` field-for-field, plus a structural ``config``
    so callers don't need a corlinman-core Python port to construct
    one."""

    config: Any
    """``cfg.channels.qq`` — must expose ``ws_url``, ``self_ids``,
    optional ``access_token``, optional ``group_keywords``, and an
    optional ``rate_limit`` sub-struct with ``group_per_min`` /
    ``sender_per_min``."""

    model: str = ""
    chat_service: ChatServiceLike | None = None
    rate_limit_hook: Any = None
    hook_bus: Any = None
    inbox: Any = None
    """T4.3 — optional ``corlinman_server.inbox.Inbox``. When set, every
    accepted QQ message is durably recorded (pending → dispatched →
    done/dead) so a gateway crash mid-turn leaves a breadcrumb. When
    ``None``, the channel runs exactly as before — purely additive."""


async def run_qq_channel(
    params: QqChannelParams,
    cancel: asyncio.Event,
) -> None:
    """Spawn the QQ channel loop and run until ``cancel`` is set.

    Mirrors the Rust ``run_qq_channel`` function. Raises ``ValueError``
    on missing required config (matches Rust ``anyhow::bail!`` shape).
    """
    cfg = params.config
    ws_url = _attr(cfg, "ws_url", "")
    if not ws_url:
        raise ValueError("channels.qq.ws_url is empty")
    # ``self_ids`` is an optional seed list. The bot's real QQ id is
    # auto-detected from the live OneBot event stream — every event
    # carries ``self_id``, learned in :meth:`ChannelRouter.dispatch`.
    # A stale or empty config value no longer breaks @mention
    # detection, and a NapCat re-login under a different account is
    # picked up at runtime with no config edit.
    self_ids = list(_attr(cfg, "self_ids", []) or [])

    # Token buckets — None on either dimension disables it.
    rate_cfg = _attr(cfg, "rate_limit", None)
    group_limiter: TokenBucket | None = None
    sender_limiter: TokenBucket | None = None
    if rate_cfg is not None:
        gpm = _attr(rate_cfg, "group_per_min", None)
        spm = _attr(rate_cfg, "sender_per_min", None)
        if gpm:
            group_limiter = TokenBucket.per_minute(int(gpm))
        if spm:
            sender_limiter = TokenBucket.per_minute(int(spm))

    # GC sweepers tied to cancel — they exit when the event fires.
    gc_tasks: list[asyncio.Task[None]] = []
    if group_limiter is not None:
        gc_tasks.append(group_limiter.start_gc(cancel))
    if sender_limiter is not None:
        gc_tasks.append(sender_limiter.start_gc(cancel))

    router = ChannelRouter(
        group_keywords=_coerce_keywords(_attr(cfg, "group_keywords", {})),
        self_ids=self_ids,
    ).with_rate_limits(group_limiter, sender_limiter)
    if params.rate_limit_hook is not None:
        router = router.with_rate_limit_hook(params.rate_limit_hook)
    if params.hook_bus is not None:
        router = router.with_hook_bus(params.hook_bus)

    adapter = OneBotAdapter(
        OneBotConfig(
            url=ws_url,
            access_token=_attr(cfg, "access_token", None),
            self_ids=self_ids,
        )
    )

    try:
        async with adapter:
            # NapCat health watcher: alongside the dispatch loop, run a
            # task that flags long silence (no heartbeat / no event)
            # which usually means the bot QQ account got kicked offline
            # by Tencent while the WS stayed up. Logs warn + flips
            # state so /admin/channels/qq/status can surface it.
            health_task = asyncio.create_task(
                _qq_health_watcher(adapter, cancel),
                name="qq-health-watcher",
            )
            try:
                await _qq_dispatch_loop(adapter, router, params, cancel)
            finally:
                health_task.cancel()
                try:
                    await health_task
                except (asyncio.CancelledError, Exception):
                    pass
    finally:
        for t in gc_tasks:
            t.cancel()


# Public, mutable: latest NapCat health probe result for the QQ channel.
# A dict so callers can ``health.get(...)`` without importing a type;
# updated by ``_qq_health_watcher`` and read by admin status routes.
QQ_HEALTH: dict[str, Any] = {
    "online": False,
    "last_event_at_ms": None,
    "seconds_since_event": None,
    "checked_at_ms": None,
}


async def _qq_health_watcher(
    adapter: OneBotAdapter, cancel: asyncio.Event
) -> None:
    """Periodic NapCat heartbeat watcher.

    A healthy NapCat sends a heartbeat meta event every ~30s. Long
    silence (default 120s) means the bot QQ account got kicked offline
    by Tencent — operators normally find out only when they notice the
    bot stopped replying. Log a structured warning the moment we
    detect it (and a recovery log when events resume).

    Tunable via env:
      - ``CORLINMAN_QQ_HEALTH_PROBE_S`` — poll interval (default 30)
      - ``CORLINMAN_QQ_HEALTH_LOST_S`` — silence threshold (default 120)
    """
    import os
    import time

    try:
        probe_s = max(1, int(os.environ.get("CORLINMAN_QQ_HEALTH_PROBE_S", "30")))
    except ValueError:
        probe_s = 30
    try:
        lost_s = max(1, int(os.environ.get("CORLINMAN_QQ_HEALTH_LOST_S", "120")))
    except ValueError:
        lost_s = 120

    was_lost = False
    lost_since_ms: int | None = None

    while not cancel.is_set():
        try:
            await asyncio.wait_for(cancel.wait(), timeout=probe_s)
            return  # cancel fired during the wait
        except asyncio.TimeoutError:
            pass

        now_ms = int(time.time() * 1000)
        last = adapter.last_event_at_ms
        seconds_since = (
            None if last is None else max(0, (now_ms - last) // 1000)
        )
        is_lost = seconds_since is None or seconds_since >= lost_s

        QQ_HEALTH.update(
            online=(not is_lost) and seconds_since is not None,
            last_event_at_ms=last,
            seconds_since_event=seconds_since,
            checked_at_ms=now_ms,
        )

        if is_lost and not was_lost:
            if lost_since_ms is None:
                lost_since_ms = now_ms
            if seconds_since is None:
                _log.warning(
                    "qq.heartbeat_lost: no NapCat event received yet "
                    "(threshold=%ss). NapCat ws may be down or unauthenticated; "
                    "check %s and the bot QQ login.",
                    lost_s,
                    getattr(adapter, "url", "the NapCat ws endpoint"),
                )
            else:
                _log.warning(
                    "qq.heartbeat_lost: no NapCat event in %ss "
                    "(threshold=%ss). Bot QQ likely kicked offline; "
                    "scan a fresh QR via NapCat WebUI to recover.",
                    seconds_since,
                    lost_s,
                )
            was_lost = True
        elif not is_lost and was_lost:
            offline_s = (now_ms - (lost_since_ms or now_ms)) // 1000
            _log.info(
                "qq.heartbeat_recovered: NapCat events resumed after ~%ss offline",
                offline_s,
            )
            was_lost = False
            lost_since_ms = None


async def _qq_dispatch_loop(
    adapter: OneBotAdapter,
    router: ChannelRouter,
    params: QqChannelParams,
    cancel: asyncio.Event,
) -> None:
    """Inner loop — reads inbound events and spawns per-message reply
    tasks. Equivalent of the Rust ``tokio::select! { cancelled() / recv() }``
    in ``run_qq_channel``."""
    inbound_iter = adapter.inbound()
    # T4.3: lazily open the durable inbox on first use. Resolved via
    # corlinman_server.inbox if available; tests that import the
    # channels package standalone keep ``params.inbox = None``.
    if params.inbox is None:
        params.inbox = await _try_open_inbox()
    pending: set[asyncio.Task[None]] = set()
    try:
        while not cancel.is_set():
            # Get the next inbound event with a cancel-aware wait.
            ev = await _race_iter_or_cancel(inbound_iter, cancel)
            if ev is None:
                break
            payload = ev.payload
            if not isinstance(payload, MessageEvent):
                continue
            req = router.dispatch(payload)
            if req is None:
                _log.debug("qq message filtered by router user=%s text=%r", payload.user_id, payload.raw_message[:80])
                continue
            _log.info("qq message accepted user=%s text=%r model=%s", payload.user_id, payload.raw_message[:80], params.model)
            if params.chat_service is None:
                # No backend wired — drop silently (matches Rust when
                # the gateway opts not to provide one).
                continue
            # T4.3: enqueue the inbound message into the durable inbox
            # BEFORE spawning the chat task, so a crash between accept
            # and reply leaves a breadcrumb. Best-effort; a failure here
            # never blocks the chat path.
            inbox_id: int | None = None
            if params.inbox is not None:
                try:
                    inbox_id = await params.inbox.enqueue(
                        channel="qq",
                        session_key=req.session_key,
                        message_id=str(payload.message_id),
                        user_text=req.content[:1000],
                    )
                except Exception as exc:  # noqa: BLE001 — never block chat
                    _log.warning("qq inbox enqueue failed: %s", exc)
                    inbox_id = None
            t = asyncio.create_task(
                handle_one_qq(
                    params.chat_service,
                    req,
                    payload,
                    params.model,
                    adapter,
                    cancel,
                    inbox=params.inbox,
                    inbox_id=inbox_id,
                )
            )
            pending.add(t)
            t.add_done_callback(pending.discard)
    finally:
        # Best-effort: cancel any in-flight handlers on shutdown.
        for t in pending:
            t.cancel()


async def handle_one_qq(
    chat_service: ChatServiceLike,
    req: RoutedRequest,
    event: MessageEvent,
    model: str,
    adapter: OneBotAdapter,
    cancel: asyncio.Event,
    *,
    inbox: Any = None,
    inbox_id: int | None = None,
) -> None:
    """Run one chat turn and post the reply back through the adapter.

    Mirrors Rust ``handle_one`` in ``service.rs``. On error, sends a
    short ``[corlinman error] <msg>`` reply so the user knows
    something failed.

    T4.3: when ``inbox``/``inbox_id`` are supplied, the inbox row is
    transitioned ``pending`` → ``dispatched`` (at start) → ``done``
    (on successful reply) or ``dead`` (after fatal error). A crash
    leaves the row stuck — the boot drainer can find it on next
    gateway start.
    """
    request = _build_internal_request(req, event, model)
    _log.info("qq handle_one start user=%s model=%s", event.user_id, model)
    if inbox is not None and inbox_id is not None:
        try:
            await inbox.mark_dispatched(inbox_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("qq inbox mark_dispatched failed: %s", exc)

    text_parts: list[str] = []
    error_message: str | None = None
    try:
        stream = chat_service.run(request, cancel)
        async for chat_ev in stream:
            kind = _event_kind(chat_ev)
            if kind == "token_delta":
                text_parts.append(getattr(chat_ev, "text", "") or "")
            elif kind == "done":
                break
            elif kind == "error":
                error_message = getattr(chat_ev, "error", "") or getattr(
                    chat_ev, "message", ""
                )
                break
            # tool_call → informational; gateway handles execution.
    except Exception as exc:  # noqa: BLE001 — never let a crash kill the row
        _log.exception("qq handle_one crashed: %s", exc)
        if inbox is not None and inbox_id is not None:
            try:
                await inbox.mark_dead(inbox_id, error=f"crash: {exc!r}")
            except Exception:  # noqa: BLE001
                pass
        raise

    if error_message is not None:
        body = f"[corlinman error] {error_message}"
        _log.error("qq handle_one error user=%s error=%r", event.user_id, error_message)
    else:
        body = "".join(text_parts)
        if not body.strip():
            _log.warning("qq handle_one empty reply user=%s", event.user_id)
            if inbox is not None and inbox_id is not None:
                try:
                    await inbox.mark_done(inbox_id)
                except Exception:  # noqa: BLE001
                    pass
            return  # Empty assistant reply → silent drop.
        _log.info("qq handle_one reply user=%s len=%d", event.user_id, len(body))

    action = _build_reply_action(event, body)
    await adapter.send_action(action)
    if inbox is not None and inbox_id is not None:
        try:
            await inbox.mark_done(inbox_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("qq inbox mark_done failed: %s", exc)


def _build_internal_request(
    req: RoutedRequest,
    event: MessageEvent,
    model: str,
) -> Any:
    """Build the request object handed to ``chat_service.run``.

    Returns a :class:`~types.SimpleNamespace` with attribute-style access
    matching the ``InternalChatRequest`` contract. Avoids a hard import
    dependency on ``corlinman-server`` so the channels package stays
    importable in isolation (unit tests, standalone deploys).
    """
    from types import SimpleNamespace

    from corlinman_channels.onebot import segments_to_attachments

    attachments = segments_to_attachments(event.message)
    message = SimpleNamespace(role="user", content=req.content)
    return SimpleNamespace(
        model=model,
        messages=[message],
        session_key=req.session_key,
        stream=True,
        max_tokens=None,
        temperature=None,
        attachments=attachments,
        binding=req.binding,
    )


def _build_reply_action(event: MessageEvent, body: str) -> Action:
    """Build a ``SendGroupMsg`` / ``SendPrivateMsg`` action with a
    single text segment. Group messages prepend an ``@sender`` so the
    reply is clearly addressed (matches qqBot.js / Rust)."""
    if event.message_type == MessageType.GROUP:
        from corlinman_channels.onebot import AtSegment

        gid = event.group_id or 0
        return SendGroupMsg(
            group_id=gid,
            message=[
                AtSegment(qq=str(event.user_id)),
                TextSegment(text=f" {body}"),
            ],
        )
    return SendPrivateMsg(
        user_id=event.user_id,
        message=[TextSegment(text=body)],
    )


# ---------------------------------------------------------------------------
# Telegram channel
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TelegramChannelParams:
    """Parameters for :func:`run_telegram_channel`. Mirrors Rust
    ``TelegramParams``."""

    config: Any
    """``cfg.channels.telegram`` — must expose ``bot_token``, optional
    ``allowed_chat_ids``, optional ``keyword_filter``, optional
    ``require_mention_in_groups``."""

    model: str = ""
    chat_service: ChatServiceLike | None = None
    base_url: str = "https://api.telegram.org"


async def run_telegram_channel(
    params: TelegramChannelParams,
    cancel: asyncio.Event,
) -> None:
    """Spawn the Telegram channel loop and run until ``cancel`` is set.

    Mirrors Rust ``run_telegram_channel`` in ``telegram/service.rs``.
    Inbound long-poll + outbound replies via :class:`TelegramSender`.
    """
    cfg = params.config
    bot_token = _attr(cfg, "bot_token", "")
    if not bot_token:
        raise ValueError("channels.telegram.bot_token is empty")

    tg_cfg = TelegramConfig(
        bot_token=str(bot_token),
        allowed_chat_ids=list(_attr(cfg, "allowed_chat_ids", []) or []),
        keyword_filter=list(_attr(cfg, "keyword_filter", []) or []),
        require_mention_in_groups=bool(_attr(cfg, "require_mention_in_groups", False)),
        base_url=str(_attr(cfg, "base_url", params.base_url)),
    )
    # The adapter owns its HTTP client (long-poll cadence is heavy);
    # the sender gets its own (short, eager-shutdown).
    adapter = TelegramAdapter(tg_cfg)
    send_client = httpx.AsyncClient()
    sender = TelegramSender(send_client, tg_cfg.bot_token, base=tg_cfg.base_url)
    pending: set[asyncio.Task[None]] = set()
    try:
        async with adapter:
            iterator = adapter.inbound()
            while not cancel.is_set():
                ev = await _race_iter_or_cancel(iterator, cancel)
                if ev is None:
                    break
                if params.chat_service is None:
                    continue
                t = asyncio.create_task(
                    handle_one_telegram(
                        params.chat_service,
                        ev,
                        params.model,
                        sender,
                        cancel,
                    )
                )
                pending.add(t)
                t.add_done_callback(pending.discard)
    finally:
        for t in pending:
            t.cancel()
        await send_client.aclose()


async def handle_one_telegram(
    chat_service: ChatServiceLike,
    inbound: InboundEvent[Any],
    model: str,
    sender: TelegramSender,
    cancel: asyncio.Event,
) -> None:
    """Run one Telegram chat turn and post the reply via
    :class:`TelegramSender`. Parallel structure to :func:`handle_one_qq`.
    """
    request = {
        "model": model,
        "messages": [{"role": "user", "content": inbound.text}],
        "session_key": inbound.binding.session_key(),
        "stream": True,
        "max_tokens": None,
        "temperature": None,
        "attachments": list(inbound.attachments),
        "binding": inbound.binding,
    }
    stream = chat_service.run(request, cancel)
    text_parts: list[str] = []
    error_message: str | None = None
    async for ev in stream:
        kind = _event_kind(ev)
        if kind == "token_delta":
            text_parts.append(getattr(ev, "text", "") or "")
        elif kind == "done":
            break
        elif kind == "error":
            error_message = getattr(ev, "error", "") or getattr(ev, "message", "")
            break

    if error_message is not None:
        body = f"[corlinman error] {error_message}"
    else:
        body = "".join(text_parts)
        if not body.strip():
            return

    # ``inbound.binding.thread`` is the chat_id (Telegram thread = chat).
    chat_id = int(inbound.binding.thread)
    reply_to: int | None = None
    if inbound.message_id is not None:
        try:
            reply_to = int(inbound.message_id)
        except ValueError:
            reply_to = None
    await sender.send_message(chat_id, body, reply_to_message_id=reply_to)


# ---------------------------------------------------------------------------
# Discord channel
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DiscordChannelParams:
    """Parameters for :func:`run_discord_channel`.

    corlinman has no Rust reference for Discord; this mirrors the shape
    of :class:`TelegramChannelParams`."""

    config: Any
    """``cfg.channels.discord`` — must expose ``bot_token``, optional
    ``allowed_channel_ids``, optional ``keyword_filter``, optional
    ``respond_to_all``, optional ``gateway_url`` / ``rest_base``."""

    model: str = ""
    chat_service: ChatServiceLike | None = None


async def run_discord_channel(
    params: DiscordChannelParams,
    cancel: asyncio.Event,
) -> None:
    """Spawn the Discord channel loop and run until ``cancel`` is set.

    Inbound over the Discord Gateway WebSocket, outbound replies via
    :class:`DiscordSender`. Parallel structure to :func:`run_telegram_channel`.
    Raises ``ValueError`` on missing required config (matches the Rust
    ``anyhow::bail!`` shape used by the QQ / Telegram runners).
    """
    cfg = params.config
    bot_token = _attr(cfg, "bot_token", "")
    if not bot_token:
        raise ValueError("channels.discord.bot_token is empty")

    dc_cfg = DiscordConfig(
        bot_token=str(bot_token),
        allowed_channel_ids=[str(c) for c in (_attr(cfg, "allowed_channel_ids", []) or [])],
        keyword_filter=list(_attr(cfg, "keyword_filter", []) or []),
        respond_to_all=bool(_attr(cfg, "respond_to_all", False)),
        gateway_url=str(_attr(cfg, "gateway_url", DEFAULT_GATEWAY_URL)),
        rest_base=str(_attr(cfg, "rest_base", DEFAULT_REST_BASE)),
    )
    adapter = DiscordAdapter(dc_cfg)
    send_client = httpx.AsyncClient()
    sender = DiscordSender(send_client, dc_cfg.bot_token, base=dc_cfg.rest_base)
    pending: set[asyncio.Task[None]] = set()
    try:
        async with adapter:
            iterator = adapter.inbound()
            while not cancel.is_set():
                ev = await _race_iter_or_cancel(iterator, cancel)
                if ev is None:
                    break
                if params.chat_service is None:
                    continue
                t = asyncio.create_task(
                    handle_one_discord(
                        params.chat_service, ev, params.model, sender, cancel
                    )
                )
                pending.add(t)
                t.add_done_callback(pending.discard)
    finally:
        for t in pending:
            t.cancel()
        await send_client.aclose()


async def handle_one_discord(
    chat_service: ChatServiceLike,
    inbound: InboundEvent[Any],
    model: str,
    sender: DiscordSender,
    cancel: asyncio.Event,
) -> None:
    """Run one Discord chat turn and post the reply via
    :class:`DiscordSender`. Parallel structure to :func:`handle_one_telegram`.
    """
    body = await _collect_reply(chat_service, inbound, model, cancel)
    if body is None:
        return
    # ``binding.thread`` is the Discord channel id.
    await sender.send_message(
        inbound.binding.thread,
        body,
        reply_to_message_id=inbound.message_id,
    )


# ---------------------------------------------------------------------------
# Slack channel
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SlackChannelParams:
    """Parameters for :func:`run_slack_channel`.

    corlinman has no Rust reference for Slack; this mirrors the shape of
    :class:`TelegramChannelParams`."""

    config: Any
    """``cfg.channels.slack`` — must expose ``app_token`` + ``bot_token``,
    optional ``allowed_channel_ids``, optional ``keyword_filter``,
    optional ``respond_to_all``, optional ``api_base``."""

    model: str = ""
    chat_service: ChatServiceLike | None = None


async def run_slack_channel(
    params: SlackChannelParams,
    cancel: asyncio.Event,
) -> None:
    """Spawn the Slack channel loop and run until ``cancel`` is set.

    Inbound over Slack Socket Mode (WebSocket), outbound replies via the
    Web API :class:`SlackSender`. Parallel structure to
    :func:`run_telegram_channel`. Raises ``ValueError`` on missing
    required config.
    """
    cfg = params.config
    app_token = _attr(cfg, "app_token", "")
    bot_token = _attr(cfg, "bot_token", "")
    if not app_token:
        raise ValueError("channels.slack.app_token is empty")
    if not bot_token:
        raise ValueError("channels.slack.bot_token is empty")

    sl_cfg = SlackConfig(
        app_token=str(app_token),
        bot_token=str(bot_token),
        allowed_channel_ids=[str(c) for c in (_attr(cfg, "allowed_channel_ids", []) or [])],
        keyword_filter=list(_attr(cfg, "keyword_filter", []) or []),
        respond_to_all=bool(_attr(cfg, "respond_to_all", False)),
        api_base=str(_attr(cfg, "api_base", SLACK_API_BASE)),
    )
    adapter = SlackAdapter(sl_cfg)
    send_client = httpx.AsyncClient()
    sender = SlackSender(send_client, sl_cfg.bot_token, base=sl_cfg.api_base)
    pending: set[asyncio.Task[None]] = set()
    try:
        async with adapter:
            iterator = adapter.inbound()
            while not cancel.is_set():
                ev = await _race_iter_or_cancel(iterator, cancel)
                if ev is None:
                    break
                if params.chat_service is None:
                    continue
                t = asyncio.create_task(
                    handle_one_slack(
                        params.chat_service, ev, params.model, sender, cancel
                    )
                )
                pending.add(t)
                t.add_done_callback(pending.discard)
    finally:
        for t in pending:
            t.cancel()
        await send_client.aclose()


async def handle_one_slack(
    chat_service: ChatServiceLike,
    inbound: InboundEvent[Any],
    model: str,
    sender: SlackSender,
    cancel: asyncio.Event,
) -> None:
    """Run one Slack chat turn and post the reply via :class:`SlackSender`.

    The reply is threaded under the inbound message ``ts`` so the
    conversation stays grouped — parallel to the Telegram ``reply_to``.
    """
    body = await _collect_reply(chat_service, inbound, model, cancel)
    if body is None:
        return
    # ``binding.thread`` is the Slack channel id; ``message_id`` is the ts.
    await sender.send_message(
        inbound.binding.thread,
        body,
        thread_ts=inbound.message_id,
    )


# ---------------------------------------------------------------------------
# Feishu / Lark channel
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FeishuChannelParams:
    """Parameters for :func:`run_feishu_channel`.

    corlinman has no Rust reference for Feishu; this mirrors the shape of
    :class:`TelegramChannelParams`."""

    config: Any
    """``cfg.channels.feishu`` — must expose ``app_id`` + ``app_secret``,
    optional ``allowed_chat_ids``, optional ``keyword_filter``, optional
    ``respond_to_all``, optional ``api_base``."""

    model: str = ""
    chat_service: ChatServiceLike | None = None


async def run_feishu_channel(
    params: FeishuChannelParams,
    cancel: asyncio.Event,
) -> None:
    """Spawn the Feishu / Lark channel loop and run until ``cancel`` is set.

    Inbound over the Feishu long-connection (WebSocket), outbound replies
    via the IM REST :class:`FeishuSender`. Parallel structure to
    :func:`run_telegram_channel`. Raises ``ValueError`` on missing
    required config.
    """
    cfg = params.config
    app_id = _attr(cfg, "app_id", "")
    app_secret = _attr(cfg, "app_secret", "")
    if not app_id:
        raise ValueError("channels.feishu.app_id is empty")
    if not app_secret:
        raise ValueError("channels.feishu.app_secret is empty")

    fs_cfg = FeishuConfig(
        app_id=str(app_id),
        app_secret=str(app_secret),
        allowed_chat_ids=[str(c) for c in (_attr(cfg, "allowed_chat_ids", []) or [])],
        keyword_filter=list(_attr(cfg, "keyword_filter", []) or []),
        respond_to_all=bool(_attr(cfg, "respond_to_all", False)),
        api_base=str(_attr(cfg, "api_base", FEISHU_API_BASE)),
    )
    adapter = FeishuAdapter(fs_cfg)
    send_client = httpx.AsyncClient()
    # The sender needs a fresh tenant_access_token per call; the adapter
    # owns the token lifecycle, so hand it the adapter's refresh hook.
    sender = FeishuSender(send_client, adapter._refresh_token, api_base=fs_cfg.api_base)
    pending: set[asyncio.Task[None]] = set()
    try:
        async with adapter:
            iterator = adapter.inbound()
            while not cancel.is_set():
                ev = await _race_iter_or_cancel(iterator, cancel)
                if ev is None:
                    break
                if params.chat_service is None:
                    continue
                t = asyncio.create_task(
                    handle_one_feishu(
                        params.chat_service, ev, params.model, sender, cancel
                    )
                )
                pending.add(t)
                t.add_done_callback(pending.discard)
    finally:
        for t in pending:
            t.cancel()
        await send_client.aclose()


async def handle_one_feishu(
    chat_service: ChatServiceLike,
    inbound: InboundEvent[Any],
    model: str,
    sender: FeishuSender,
    cancel: asyncio.Event,
) -> None:
    """Run one Feishu chat turn and post the reply via :class:`FeishuSender`.

    The reply is posted via the ``/messages/{id}/reply`` endpoint so the
    addressing stays clear — parallel to the Telegram ``reply_to``.
    """
    body = await _collect_reply(chat_service, inbound, model, cancel)
    if body is None:
        return
    # ``binding.thread`` is the Feishu chat id; ``message_id`` is the
    # original message id we reply to.
    await sender.send_message(
        inbound.binding.thread,
        body,
        reply_to_message_id=inbound.message_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _collect_reply(
    chat_service: ChatServiceLike,
    inbound: InboundEvent[Any],
    model: str,
    cancel: asyncio.Event,
) -> str | None:
    """Run one chat turn for an :class:`InboundEvent` and collect the reply.

    Shared by the Discord / Slack / Feishu ``handle_one_*`` helpers — the
    inbound→chat→reply collapse is identical across these three text-only
    channels. Returns the reply body, or ``None`` when the assistant
    produced an empty reply (caller should send nothing). On a backend
    error the body is a short ``[corlinman error] <msg>`` string so the
    user knows something failed — matching :func:`handle_one_telegram`.
    """
    request = {
        "model": model,
        "messages": [{"role": "user", "content": inbound.text}],
        "session_key": inbound.binding.session_key(),
        "stream": True,
        "max_tokens": None,
        "temperature": None,
        "attachments": list(inbound.attachments),
        "binding": inbound.binding,
    }
    stream = chat_service.run(request, cancel)
    text_parts: list[str] = []
    error_message: str | None = None
    async for ev in stream:
        kind = _event_kind(ev)
        if kind == "token_delta":
            text_parts.append(getattr(ev, "text", "") or "")
        elif kind == "done":
            break
        elif kind == "error":
            error_message = getattr(ev, "error", "") or getattr(ev, "message", "")
            break

    if error_message is not None:
        return f"[corlinman error] {error_message}"
    body = "".join(text_parts)
    if not body.strip():
        return None
    return body


async def _race_iter_or_cancel(
    iterator: AsyncIterator[Any],
    cancel: asyncio.Event,
) -> Any | None:
    """Get the next item from ``iterator`` or ``None`` if ``cancel``
    fires first. Equivalent of Rust ``tokio::select! { recv() => ...,
    cancelled() => break }``.
    """
    next_task = asyncio.create_task(iterator.__anext__())
    cancel_task = asyncio.create_task(cancel.wait())
    done, pending = await asyncio.wait(
        {next_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
    if cancel_task in done:
        if next_task in done and not next_task.cancelled():
            # Race tie — both fired; consume the value we already got.
            try:
                return next_task.result()
            except (StopAsyncIteration, BaseException):
                return None
        return None
    try:
        return next_task.result()
    except StopAsyncIteration:
        return None


def _event_kind(ev: Any) -> str:
    """Best-effort discriminator extraction.

    Supports either ``ev.kind`` (string) or class-name fallbacks
    (``TokenDelta``, ``ToolCall``, ``Done``, ``Error``). Returns
    ``"unknown"`` for anything else."""
    k = getattr(ev, "kind", None)
    if isinstance(k, str):
        return k.lower()
    name = type(ev).__name__
    mapping = {
        "TokenDelta": "token_delta",
        "ToolCall": "tool_call",
        "Done": "done",
        "Error": "error",
        "InternalChatEvent": "token_delta",
    }
    return mapping.get(name, name.lower())


def _attr(obj: Any, name: str, default: Any) -> Any:
    """Walk attribute / mapping access uniformly. Tolerates both
    ``SimpleNamespace`` configs and TOML-loaded dicts."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _coerce_keywords(raw: Any) -> GroupKeywords:
    """Normalize a keyword map to ``dict[str, list[str]]``. Accepts
    either a dict from the loaded config or ``None``."""
    if not raw:
        return {}
    out: GroupKeywords = {}
    for k, v in raw.items():
        out[str(k)] = [str(x) for x in v]
    return out


# ---------------------------------------------------------------------------
# Re-export for the channel.py wrapper
# ---------------------------------------------------------------------------

#: ``corlinman_channels.channel.QqChannel`` imports this lazily; the
#: orchestration helpers above are the public surface.
_ = field  # keep dataclasses import alive for mypy
