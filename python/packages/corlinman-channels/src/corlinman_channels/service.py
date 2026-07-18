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
import base64
import collections
import functools
import logging
import mimetypes
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

_log = logging.getLogger(__name__)

_QQ_AUDIO_ATTACHMENT_SUFFIXES = {
    ".aac",
    ".amr",
    ".flac",
    ".m4a",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".silk",
    ".wav",
}

#: Tool names that dispatch sub-agent(s). The moment one of these surfaces as
#: a ``tool_call`` event mid-turn, the channel handler surfaces the shareable
#: agent-status link EARLY (as a standalone message) so the user can watch the
#: fan-out live. Normal, non-fan-out turns do not get status links. Mirrors
#: the constants in ``corlinman_agent.subagent.runner`` (kept duplicated here so
#: this package never imports ``corlinman-agent`` just for three strings).
_SUBAGENT_SPAWN_TOOLS: frozenset[str] = frozenset(
    {"subagent_spawn", "subagent_spawn_many", "subagent_spawn_inline"}
)


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

from corlinman_channels._status import (
    REASONING_PREVIEW_CHARS as _REASONING_PREVIEW_CHARS,
)
from corlinman_channels._status import (
    SEND_ATTACHMENT_TOOL as _SEND_ATTACHMENT_TOOL,
)
from corlinman_channels._status import (
    STATUS_THINKING as _TG_STATUS_THINKING,
)
from corlinman_channels._status import (
    TEXT_LIMIT as _TELEGRAM_TEXT_LIMIT,
)
from corlinman_channels._status import (
    TODO_WRITE_TOOL as _TODO_WRITE_TOOL,
)
from corlinman_channels._status import (
    MutableSpinner,
    chunk_reply,
    format_status_footer_line,
    format_turn_footer,
    truncate_reply,
    try_append_footer,
)
from corlinman_channels._status import (
    parse_ask_user_args as _parse_ask_user_args,
)
from corlinman_channels._status import (
    parse_send_attachment_args as _parse_send_attachment_args,
)
from corlinman_channels._status import (
    resolve_attachment_path as _resolve_attachment_path,
)
from corlinman_channels._status import (
    tool_arg_preview as _tool_arg_preview,
)
from corlinman_channels.commands import (
    CommandContext,
    apply_command_prelude,
    is_command_admin,
    match_command_with_args,
    run_command_handler,
    slash_access_policy_from_env,
)
from corlinman_channels.common import AlbumDebouncer, InboundEvent, TransportError
from corlinman_channels.common import format_attribution_prefix as _attribution
from corlinman_channels.common import (
    normalize_outbound_for_channel as _normalize_for_channel,
)
from corlinman_channels.common import split_on_msg_break as _split_on_msg_break
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
    MessageSegment,
    MessageType,
    OneBotAdapter,
    OneBotConfig,
    RecordSegment,
    SendGroupMsg,
    SendPrivateMsg,
    SetInputStatus,
    TextSegment,
    UploadGroupFile,
    UploadPrivateFile,
)
from corlinman_channels.persona_inject import (
    inject_persona_if_enabled as _inject_persona_if_enabled,
)
from corlinman_channels.qq_official import (
    DEFAULT_INTENTS as QQ_OFFICIAL_DEFAULT_INTENTS,
)
from corlinman_channels.qq_official import (
    QqOfficialAdapter,
    QqOfficialConfig,
)
from corlinman_channels.qq_official_send import QqOfficialSender
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
from corlinman_channels.wechat_official import (
    WeChatOfficialAdapter,
    WeChatOfficialConfig,
)
from corlinman_channels.wechat_official_send import WeChatOfficialSender

__all__ = [
    "ChatEventLike",
    "ChatServiceLike",
    "DiscordChannelParams",
    "FeishuChannelParams",
    "QqChannelParams",
    "QqOfficialChannelParams",
    "SlackChannelParams",
    "TelegramChannelParams",
    "WeChatOfficialChannelParams",
    "bootstrap_command_extensions",
    "handle_one_discord",
    "handle_one_feishu",
    "handle_one_qq",
    "handle_one_qq_official",
    "handle_one_slack",
    "handle_one_telegram",
    "handle_one_wechat_official",
    "run_discord_channel",
    "run_feishu_channel",
    "run_qq_channel",
    "run_qq_official_channel",
    "run_slack_channel",
    "run_telegram_channel",
    "run_wechat_official_channel",
]


def _binding_persona_resolver(
    binding: Any,
    *,
    humanlike_enabled: bool,
    persona_id: str | None,
    humanlike_resolver: Any,
) -> Callable[[], tuple[bool, str | None]]:
    """Resolve channel humanlike config, then apply per-binding persona prefs."""
    from corlinman_channels import binding_prefs as _binding_prefs

    def _resolve() -> tuple[bool, str | None]:
        enabled = bool(humanlike_enabled)
        resolved_persona_id = persona_id
        if callable(humanlike_resolver):
            resolved = humanlike_resolver()
            if isinstance(resolved, tuple) and len(resolved) == 2:
                enabled = bool(resolved[0])
                resolved_persona_id = (
                    resolved[1] if isinstance(resolved[1], str) else None
                )
        return (
            enabled,
            _binding_prefs.effective_persona_id(
                binding, resolved_persona_id
            ),
        )

    return _resolve


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

    def run(
        self,
        request: Any,
        cancel: asyncio.Event,
    ) -> AsyncIterator[Any]:
        """Run one chat turn. Yields events until done. ``cancel.set()``
        should cause the iterator to terminate ASAP.

        Declared as a plain (non-``async``) method returning an
        ``AsyncIterator`` because implementations are async-generator
        functions: calling ``run(...)`` returns the iterator directly,
        so callers ``async for`` over it without an intermediate
        ``await``."""
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

    # ---- human-like persona toggle (T-persona) -------------------------
    #
    # Optional system_prompt-injection layer driven by an admin-curated
    # persona registry. Off by default — the channel runs exactly as
    # before when ``humanlike_enabled`` is False or ``persona_store`` is
    # None. Designed channel-agnostic so other channels can opt in by
    # taking the same three fields without schema migration.
    humanlike_enabled: bool = False
    """Master gate. When False (default) the persona block is never
    injected even if ``persona_id`` + ``persona_store`` are both set."""

    persona_id: str | None = None
    """Persona row id to inject. ``None`` falls back to "no persona
    today" even when the gate is on, which makes a half-configured
    ``[channels.qq.humanlike]`` section a no-op rather than a crash."""

    persona_store: Any = None
    """Open :class:`corlinman_server.persona.PersonaStore`. Typed as
    ``Any`` so this package doesn't take a hard dep on corlinman-server
    (unit tests pass a stripped-down fake). Looked up per-turn so a
    persona body edit goes live on the next inbound message without a
    channel restart."""

    humanlike_resolver: Any = None
    """Optional callable ``() -> tuple[bool, str | None]`` that returns
    the live ``(enabled, persona_id)`` pair at call time. When set, it
    overrides the static ``humanlike_enabled`` / ``persona_id`` fields
    on a per-turn basis — the gateway uses this to point at the live
    in-memory channels config dict so an admin PUT to the toggle takes
    effect on the very next inbound message without restarting the
    channel task (config-watcher hot-reload integration). When ``None``,
    the static fields are read directly. Typed as ``Any`` so this
    package stays type-checker-friendly without a Callable import."""

    asset_store: Any = None
    """Optional :class:`corlinman_server.persona.PersonaAssetStore`.
    When wired AND the resolved persona owns at least one ``emoji``
    asset, the persona injector appends a ``## Available emoji`` block
    to the system prompt listing each emoji label → absolute path so the
    agent can call ``send_attachment`` with the right path to ship a
    sticker. ``None`` keeps the persona injection working without the
    emoji extension — the system prompt body still goes in, but no
    emoji block is rendered. Typed as ``Any`` to avoid a hard dep on
    corlinman-server at import time."""

    event_emitter: Any = None
    """W4.1 — see :class:`TelegramChannelParams.event_emitter`. QQ uses
    the same emitter to source ``ToolStateHeartbeat`` / ``Cancelling`` /
    ``TurnComplete`` envelopes for its post-turn footer; ``None`` falls
    back to the legacy in-process stream."""


async def run_qq_channel(
    params: QqChannelParams,
    cancel: asyncio.Event,
) -> None:
    """Spawn the QQ channel loop and run until ``cancel`` is set.

    Mirrors the Rust ``run_qq_channel`` function. Raises ``ValueError``
    on missing required config (matches Rust ``anyhow::bail!`` shape).
    """
    bootstrap_command_extensions()
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
        group_replies_enabled=bool(_attr(cfg, "group_replies_enabled", True)),
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
#
# ``account_online`` is the truth-source for "can the bot actually
# receive QQ messages right now". NapCat WS heartbeats stay healthy
# even when the bot QQ account itself is kicked offline by Tencent
# (the ``[KickedOffLine]`` event), so ``online`` (WS heartbeat) and
# ``account_online`` (real QQ login state) can disagree. The admin UI
# should treat ``account_online == False`` as the operator action
# signal — it means re-scan-QR is required.
QQ_HEALTH: dict[str, Any] = {
    "online": False,
    "last_event_at_ms": None,
    "seconds_since_event": None,
    "checked_at_ms": None,
    "account_online": None,        # None = never probed; True/False = last result
    "account_qq": None,            # int — the QQ id NapCat reports for the bot
    "account_nickname": None,      # str — display name on the bot account
    "account_checked_at_ms": None,
    "account_last_error": None,    # str — most recent probe failure reason
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

    # Account-state probe — HTTP fetch interval is independent of the
    # heartbeat poll. Default 60s (covers KickedOffLine within a minute);
    # tunable via ``CORLINMAN_QQ_ACCOUNT_PROBE_S``.
    try:
        account_probe_s = max(
            10, int(os.environ.get("CORLINMAN_QQ_ACCOUNT_PROBE_S", "60"))
        )
    except ValueError:
        account_probe_s = 60
    last_account_probe_ms = 0

    while not cancel.is_set():
        try:
            await asyncio.wait_for(cancel.wait(), timeout=probe_s)
            return  # cancel fired during the wait
        except TimeoutError:
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

        # Update account-online state from the adapter's last-seen
        # heartbeat ``status.online`` flag (set by the inbound pump
        # whenever a heartbeat meta-event lands). NapCat's reverse-WS
        # deployments don't expose an HTTP plane, so we can't probe
        # /get_login_info — but every heartbeat already carries the
        # state we need.
        if now_ms - last_account_probe_ms >= account_probe_s * 1000:
            last_account_probe_ms = now_ms
            status_online = getattr(adapter, "last_status_online", None)
            status_ts = getattr(adapter, "last_status_online_at_ms", None)
            # Stale-data guard: if the last status flip was longer ago
            # than 2× the heartbeat-lost threshold, the adapter likely
            # disconnected entirely — surface as unknown (None) rather
            # than a misleading True/False from a frozen value.
            stale = (
                status_ts is None
                or (now_ms - status_ts) > (lost_s * 2 * 1000)
            )
            QQ_HEALTH.update(
                account_online=None if stale else bool(status_online),
                account_checked_at_ms=now_ms,
                account_last_error=(
                    "no_heartbeat_yet" if status_ts is None
                    else ("stale_status" if stale
                          else (None if status_online else "napcat_status_offline"))
                ),
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


def _napcat_http_base(ws_url: str | None) -> str | None:
    """Derive NapCat's HTTP-action base URL from its WS URL.

    NapCat's reverse-WS endpoint is typically ``ws://host:3001/onebot``
    and the matching HTTP action endpoint is ``http://host:3001``
    (NapCat serves both off the same port). The mapping is purely
    syntactic — flip the scheme, drop the ``/onebot[/v11]`` suffix.

    Returns ``None`` when ``ws_url`` is missing or unparseable; the
    caller treats that as "skip the probe this round". An explicit
    ``CORLINMAN_NAPCAT_HTTP_URL`` env override always wins.
    """
    explicit = os.environ.get("CORLINMAN_NAPCAT_HTTP_URL")
    if explicit:
        return explicit.rstrip("/")
    if not ws_url:
        return None
    base = ws_url
    if base.startswith("wss://"):
        base = "https://" + base[len("wss://"):]
    elif base.startswith("ws://"):
        base = "http://" + base[len("ws://"):]
    # Strip any /onebot or /onebot/v11 suffix.
    for suffix in ("/onebot/v11", "/onebot"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.rstrip("/") or None


async def _qq_probe_account_online(
    adapter: OneBotAdapter, now_ms: int
) -> None:
    """Probe NapCat HTTP ``/get_login_info`` and update QQ_HEALTH.

    Best-effort: any failure (network, missing config, malformed
    response) records ``account_online=False`` plus a short
    ``account_last_error`` string. Success records the bot's QQ id +
    nickname.
    """
    import httpx as _httpx

    base = _napcat_http_base(getattr(adapter, "url", None))
    if base is None:
        QQ_HEALTH.update(
            account_online=False,
            account_checked_at_ms=now_ms,
            account_last_error="napcat_http_url_unknown",
        )
        return
    url = f"{base}/get_login_info"
    headers: dict[str, str] = {}
    token = os.environ.get("CORLINMAN_NAPCAT_ACCESS_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with _httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url, headers=headers)
    except _httpx.HTTPError as exc:
        QQ_HEALTH.update(
            account_online=False,
            account_checked_at_ms=now_ms,
            account_last_error=f"http_error: {exc}",
        )
        return
    if resp.status_code != 200:
        QQ_HEALTH.update(
            account_online=False,
            account_checked_at_ms=now_ms,
            account_last_error=f"http_{resp.status_code}",
        )
        return
    try:
        env = resp.json()
    except ValueError:
        QQ_HEALTH.update(
            account_online=False,
            account_checked_at_ms=now_ms,
            account_last_error="bad_json",
        )
        return
    if not isinstance(env, dict) or env.get("status") != "ok":
        # NapCat returns status="failed" when the bot account is
        # offline (post-KickedOffLine). retcode 1 + message about
        # login state.
        msg = ""
        if isinstance(env, dict):
            msg = str(env.get("message") or env.get("wording") or "")
        QQ_HEALTH.update(
            account_online=False,
            account_checked_at_ms=now_ms,
            account_last_error=f"napcat: {msg or 'status_not_ok'}",
        )
        return
    data = env.get("data") or {}
    user_id = data.get("user_id") if isinstance(data, dict) else None
    nickname = data.get("nickname") if isinstance(data, dict) else None
    # NapCat reports user_id 0 when the account is offline mid-rotation
    # but the HTTP plane is still answering. Treat that as offline.
    account_online = isinstance(user_id, int) and user_id > 0
    QQ_HEALTH.update(
        account_online=account_online,
        account_qq=user_id if account_online else None,
        account_nickname=nickname if account_online else None,
        account_checked_at_ms=now_ms,
        account_last_error=None if account_online else "user_id_zero",
    )


async def _qq_dispatch_loop(
    adapter: OneBotAdapter,
    router: ChannelRouter,
    params: QqChannelParams,
    cancel: asyncio.Event,
) -> None:
    """Inner loop — reads inbound events and spawns per-message reply
    tasks. Equivalent of the Rust ``tokio::select! { cancelled() / recv() }``
    in ``run_qq_channel``.

    Concurrency is bounded by a per-channel semaphore (R3 fix); the
    dispatch loop awaits the acquire so backpressure flows upstream —
    the OneBot reader slows down instead of fanning out unboundedly.
    """
    inbound_iter = adapter.inbound()
    # L4 fix: keep the lazily-opened inbox as a LOCAL variable rather
    # than writing back into ``params.inbox``. ``params`` is shared
    # across channel restarts; mutating it would leave in-flight
    # ``handle_one_qq`` calls referencing a now-stale inbox handle on
    # the next reconnect. ``params.inbox`` remains as an injectable
    # override (tests pass a pre-built inbox); only the loop's lazy
    # fallback lives in this local scope.
    inbox = params.inbox
    if inbox is None:
        inbox = await _try_open_inbox()
    semaphore = asyncio.Semaphore(_channel_max_concurrency("QQ"))
    # CMP-06 — resolve the slash-access policy once at loop start. ``None``
    # (no env config) keeps the historical allow-by-default behaviour.
    slash_policy = slash_access_policy_from_env()
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
            req = router.dispatch(payload, slash_policy=slash_policy)
            if req is None:
                _log.debug("qq message filtered by router user=%s text=%r", payload.user_id, payload.raw_message[:80])
                continue
            _log.info("qq message accepted user=%s text=%r model=%s", payload.user_id, payload.raw_message[:80], params.model)
            # CMP-06 — a matched command denied by the slash-access policy.
            # The router already replaced ``content`` with the refusal text
            # and cleared ``command_spec``; send the refusal back and skip
            # both the handler and the agent turn.
            if req.command_refused:
                try:
                    await adapter.send_action(
                        _build_reply_action(payload, req.content)
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning("qq command-refusal send failed: %s", exc)
                continue
            # Command-handler short-circuit (direct execution path).
            # When the router matched a command whose spec
            # carries a handler, run it now and reply directly via the
            # adapter — the agent turn is skipped entirely. Specs with
            # only a wizard_prelude fall through to the chat-task path
            # below; their content was already rewritten by the router.
            if (
                req.command_spec is not None
                and req.command_spec.handler is not None
            ):
                cmd_inbox_id: int | None = None
                if inbox is not None:
                    try:
                        cmd_inbox_id = await inbox.enqueue(
                            channel="qq",
                            session_key=req.session_key,
                            message_id=str(payload.message_id),
                            user_text=req.content[:1000],
                        )
                    except Exception as exc:  # noqa: BLE001
                        _log.warning("qq inbox enqueue (cmd) failed: %s", exc)
                try:
                    ctx = CommandContext(
                        spec=req.command_spec,
                        raw_text=req.content,
                        args_text=req.command_args,
                        binding=req.binding,
                        is_admin=is_command_admin(req.binding),
                    )
                    cmd_result = await run_command_handler(req.command_spec, ctx)
                except Exception as exc:  # noqa: BLE001 — never crash the loop
                    _log.exception("qq command handler crashed: %s", exc)
                    cmd_result = None
                if cmd_inbox_id is not None and inbox is not None:
                    try:
                        await inbox.mark_dispatched(cmd_inbox_id)
                    except Exception:  # noqa: BLE001
                        pass
                if cmd_result is not None and cmd_result.reply:
                    try:
                        await adapter.send_action(
                            _build_reply_action(payload, cmd_result.reply)
                        )
                    except Exception as exc:  # noqa: BLE001
                        _log.warning("qq command reply send failed: %s", exc)
                if cmd_inbox_id is not None and inbox is not None:
                    try:
                        await inbox.mark_done(cmd_inbox_id)
                    except Exception:  # noqa: BLE001
                        pass
                continue
            # Unknown-command notice: the text looked like a slash command
            # (leading slash, command-shaped) but matched nothing. Reply
            # with the hint the router computed and skip the agent turn —
            # don't forward a bare ``/foo`` to the LLM.
            if req.unknown_command_notice is not None:
                try:
                    await adapter.send_action(
                        _build_reply_action(payload, req.unknown_command_notice)
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning("qq unknown-command notice send failed: %s", exc)
                continue
            if params.chat_service is None:
                # No backend wired — drop silently (matches Rust when
                # the gateway opts not to provide one).
                continue
            # T4.3: enqueue the inbound message into the durable inbox
            # BEFORE spawning the chat task, so a crash between accept
            # and reply leaves a breadcrumb. Best-effort; a failure here
            # never blocks the chat path.
            inbox_id: int | None = None
            if inbox is not None:
                try:
                    inbox_id = await inbox.enqueue(
                        channel="qq",
                        session_key=req.session_key,
                        message_id=str(payload.message_id),
                        user_text=req.content[:1000],
                    )
                except Exception as exc:  # noqa: BLE001 — never block chat
                    _log.warning("qq inbox enqueue failed: %s", exc)
                    inbox_id = None
            # R3: acquire BEFORE create_task so a saturated semaphore
            # parks the dispatch loop here — the upstream reader sees
            # natural backpressure instead of unbounded task fan-out.
            await semaphore.acquire()
            t = asyncio.create_task(
                _qq_run_one(
                    semaphore,
                    params.chat_service,
                    req,
                    payload,
                    params.model,
                    adapter,
                    cancel,
                    inbox=inbox,
                    inbox_id=inbox_id,
                    params=params,
                )
            )
            pending.add(t)
            t.add_done_callback(pending.discard)
    finally:
        # Best-effort: cancel any in-flight handlers on shutdown.
        for t in pending:
            t.cancel()


async def _qq_run_one(
    semaphore: asyncio.Semaphore,
    chat_service: ChatServiceLike,
    req: RoutedRequest,
    payload: MessageEvent,
    model: str,
    adapter: OneBotAdapter,
    cancel: asyncio.Event,
    *,
    inbox: Any,
    inbox_id: int | None,
    params: QqChannelParams | None = None,
) -> None:
    """Wrapper that releases the per-channel semaphore in ``finally`` —
    keeps concurrency-control bookkeeping out of the public
    ``handle_one_qq`` signature so existing callers stay unchanged.

    ``params`` is forwarded so :func:`handle_one_qq` can read the
    persona-injection knobs without changing the historical positional
    signature — old callers in tests still work because the kwarg
    defaults to ``None`` and the persona path no-ops on that.
    """
    try:
        await handle_one_qq(
            chat_service,
            req,
            payload,
            model,
            adapter,
            cancel,
            inbox=inbox,
            inbox_id=inbox_id,
            params=params,
        )
    finally:
        semaphore.release()


def _qq_activity_label_for_call(ev: Any) -> str:
    """Render a ``tool_call`` event as a compact label for the summary
    block — same per-tool argument extraction as
    :func:`_format_tool_status`, but without the leading "🔧 调用工具:"
    prefix. Used by :func:`_qq_format_activity_summary`.
    """
    tool = (getattr(ev, "tool", "") or "?").replace("\n", " ")
    plugin = (getattr(ev, "plugin", "") or "").replace("\n", " ")
    label = f"{plugin}.{tool}" if plugin and plugin != tool else tool
    if len(label) > 60:
        label = label[:57] + "..."
    preview = _tool_arg_preview(tool, getattr(ev, "args_json", b""))
    return f"{label} {preview}".rstrip()


def _qq_format_duration(duration_ms: int | None) -> str:
    """Human-friendly duration for the QQ summary block (None → empty)."""
    if duration_ms is None:
        return ""
    dur_ms = int(duration_ms or 0)
    return f"{dur_ms}ms" if dur_ms < 1000 else f"{dur_ms / 1000:.1f}s"


def _qq_format_activity_summary(
    activity: list[tuple[str, str, int | None, bool, str]],
) -> str:
    """Render the per-turn tool-activity prelude for QQ replies.

    ``activity`` is a list of ``(kind, label, duration_ms, is_error,
    error_summary)`` tuples in arrival order. ``kind`` is one of:

    * ``"call"``       — a ``tool_call`` event (label is the tool + arg preview).
    * ``"result"``     — a ``tool_result`` event (paired with the most
      recent unpaired ``"call"`` by position).
    * ``"attachment"`` — an outbound file upload (label is the filename).

    Returns the empty string if ``activity`` is empty. Caller is
    responsible for honouring ``CORLINMAN_QQ_TOOL_SUMMARY=0``.

    Output shape (each line ≤80 chars)::

        📋 本次操作:
        🔧 web_search 'gpt-5.5 news' (302ms)
        ✅ write_file hello.html (2ms)
        📎 已发送文件: hello.html
        ─────────────

    The todo-list block is intentionally NOT prepended on QQ-family
    channels. Pending ``☐`` rows are forward-looking noise on
    non-editable transports where the user can't watch the boxes flip;
    the operation log IS the "what just happened" signal. Editable
    channels (Telegram, Discord, Slack, Feishu) get the rendered
    checkbox view via the live spinner in :mod:`_status`.
    """
    if not activity:
        return ""
    # Walk in arrival order and pair calls with results by index. Each
    # call line absorbs the next result (success / failure / duration);
    # send_attachment renders as its own 📎 line instead of a call/result
    # pair because the channel-side upload is what the user cares about.
    rendered: list[str] = []
    # Index + label of the most recent unpaired "call" entry, so a
    # following "result" can replace its rendered line in-place while
    # preserving the call's full label (tool name + per-tool arg
    # preview).
    pending_call_idx: int | None = None
    pending_call_label: str = ""
    for kind, label, duration_ms, is_error, error_summary in activity:
        if kind == "attachment":
            rendered.append(f"📎 已发送文件: {label}")
            pending_call_idx = None
            pending_call_label = ""
            continue
        if kind == "call":
            rendered.append(f"🔧 {label}".rstrip())
            pending_call_idx = len(rendered) - 1
            pending_call_label = label
            continue
        if kind == "result":
            # Prefer the call's richer label (tool + arg preview) over
            # the bare tool name carried on the result event — keeps the
            # "web_search 'gpt-5.5 news'" context visible after pairing.
            display_label = pending_call_label or label
            dur = _qq_format_duration(duration_ms)
            suffix = f" ({dur})" if dur else ""
            if is_error:
                err = (error_summary or "").replace("\n", " ")
                if len(err) > 80:
                    err = err[:79] + "…"
                detail = f": {err}" if err else ""
                line = f"❌ {display_label} 失败{suffix}{detail}"
            else:
                line = f"✅ {display_label}{suffix}"
            if pending_call_idx is not None:
                # Replace the bare call line with the resolved one — the
                # user only needs one entry per tool, not two.
                rendered[pending_call_idx] = line
                pending_call_idx = None
                pending_call_label = ""
            else:
                rendered.append(line)
            continue
    if not rendered:
        return ""
    lines = ["📋 本次操作:", *rendered, "─────────────"]
    return "\n".join(lines)


def _qq_tool_summary_enabled() -> bool:
    """Honour ``CORLINMAN_QQ_TOOL_SUMMARY`` — default on, ``0`` / ``false``
    / ``no`` / ``off`` disables the prelude block."""
    import os

    raw = os.environ.get("CORLINMAN_QQ_TOOL_SUMMARY", "")
    if not raw:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


async def _qq_send_attachment(
    adapter: OneBotAdapter,
    event: MessageEvent,
    ev: Any,
) -> str:
    """Ship a file the agent produced through the QQ channel.

    WS-1 task 1 — routes by MIME so the user sees the right thing:

    * **image / emoji** (``image/*``) → an inline :class:`ImageSegment`
      sent via :class:`SendGroupMsg` / :class:`SendPrivateMsg`. NapCat
      reads the ``file://`` URL off the local path so the picture lands
      *in the chat* (a sticker / emoji rendered inline) instead of being
      buried in the group's file area. Mirrors the Telegram handler's
      ``send_photo`` branch.
    * **audio / voice** (``audio/*``) → an inline :class:`RecordSegment`
      sent via :class:`SendGroupMsg` / :class:`SendPrivateMsg`. The bytes
      are embedded with OneBot's ``base64://`` form so NapCat does not need
      to read corlinman's container-local ``/data`` paths.
    * **everything else** → :class:`UploadGroupFile` /
      :class:`UploadPrivateFile` (the OneBot file-share extension) so a
      true document lands in the QQ file panel.

    Returns a status line for the summary block. Best-effort: failures
    fold into status text rather than raising.
    """
    path_str, caption, filename = _parse_send_attachment_args(ev)
    if not path_str:
        return "⚠️ 发送文件失败: missing `path`"
    p = _resolve_attachment_path(path_str)
    if p is None:
        return f"⚠️ 发送文件失败: {Path(path_str).name} 不存在"
    display = filename or p.name
    # MIME-sniff the resolved file. ``image/*`` and ``audio/*`` are sent
    # inline; anything else remains a file-share upload.
    mime, _ = mimetypes.guess_type(p.name)
    is_image = bool(mime and mime.startswith("image/"))
    is_audio = bool(mime and mime.startswith("audio/")) or (
        p.suffix.lower() in _QQ_AUDIO_ATTACHMENT_SUFFIXES
    )
    try:
        if is_image:
            # Use OneBot's base64 image form instead of a local file URL.
            # In Docker, corlinman and NapCat are separate containers; a
            # persona asset path such as /data/personas/... exists in
            # corlinman but not necessarily in NapCat.
            image_file = _qq_local_image_segment_file(p)
            text = caption or ""
            action = _build_reply_action(
                event,
                text,
                # No @mention prefix for a bare image — the picture is the
                # payload; a leading "@user" on an empty caption is noise.
                prepend_at_mention=bool(text),
                image_files=[image_file],
            )
            await adapter.send_action(action)
            _log.info("qq send_attachment inline-image path=%s mime=%s", p, mime)
            return f"📎 已发送图片: {display}"
        if is_audio:
            record_file = _qq_local_record_segment_file(p)
            record_msg: list[MessageSegment] = [RecordSegment(file=record_file)]
            if event.message_type == MessageType.GROUP and event.group_id is not None:
                await adapter.send_action(
                    SendGroupMsg(group_id=event.group_id, message=record_msg)
                )
            else:
                await adapter.send_action(
                    SendPrivateMsg(user_id=event.user_id, message=record_msg)
                )
            _log.info("qq send_attachment inline-record path=%s mime=%s", p, mime)
            return f"🎙️ 已发送语音: {display}"
        if event.message_type == MessageType.GROUP and event.group_id is not None:
            await adapter.send_action(
                UploadGroupFile(
                    group_id=event.group_id,
                    file=str(p),
                    name=display,
                )
            )
        else:
            await adapter.send_action(
                UploadPrivateFile(
                    user_id=event.user_id,
                    file=str(p),
                    name=display,
                )
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning("qq send_attachment failed: %s", exc)
        return f"⚠️ 发送文件失败: {display} ({exc})"
    _log.info("qq send_attachment ok path=%s display=%s mime=%s", p, display, mime)
    return f"📎 已发送文件: {display}"


def _qq_local_image_segment_file(path: Path) -> str:
    """Return a OneBot/NapCat ``base64://`` image payload for ``path``.

    Local-path image sends run inside the corlinman container, while
    NapCat may run in a separate container that cannot read corlinman's
    ``/data`` mount. Embedding the bytes avoids relying on shared
    filesystem paths for persona emoji / generated images.
    """
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"base64://{encoded}"


def _qq_local_record_segment_file(path: Path) -> str:
    """Return a OneBot/NapCat ``base64://`` voice payload for ``path``."""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"base64://{encoded}"


async def _pulse(
    action: Callable[[], Awaitable[None]],
    cancel: asyncio.Event,
    interval_s: float,
) -> None:
    """Re-fire ``action`` until ``cancel`` is set. Best-effort: action
    failures are swallowed."""
    try:
        while not cancel.is_set():
            try:
                await action()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(cancel.wait(), timeout=interval_s)
                return
            except TimeoutError:
                continue
    except asyncio.CancelledError:
        return


# NapCat / OneBot's practical per-QQ-message ceiling. Real protocol
# limit varies by upstream client (NapCat tops out around 4500-5000
# chars in our testing); leave a safety margin so we never silently
# overshoot.
_QQ_TEXT_LIMIT: int = 3800


async def _qq_input_status_pulse(
    adapter: OneBotAdapter,
    user_id: int,
    cancel: asyncio.Event,
    *,
    interval_s: float = 5.0,
) -> None:
    """Re-fire ``set_input_status`` (NapCat extension) until cancelled.

    Shows "对方正在输入..." in the QQ client while a reply is being
    generated. NapCat clears the indicator after ~5s, so the loop
    period matches. Non-NapCat OneBot backends return an "unsupported"
    envelope; the adapter logs and moves on.
    """
    await _pulse(
        lambda: adapter.send_action(SetInputStatus(user_id=user_id, event_type=1)),
        cancel,
        interval_s,
    )


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
    params: QqChannelParams | None = None,
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

    Surfaces a NapCat "正在输入..." indicator in private chats for the
    duration of the turn (QQ groups don't render typing indicators —
    skipped there).

    QQ has no clean editMessage equivalent (NapCat / OneBot edit support
    is patchy across clients and noisy in groups), so per-step spinner
    text isn't feasible here. Instead we collect every ``tool_call`` /
    ``tool_result`` / ``send_attachment`` event and prepend a compact
    "📋 本次操作:" summary block to the final reply so the user can see
    what the agent did. Disable per-deployment with
    ``CORLINMAN_QQ_TOOL_SUMMARY=0``.
    """
    request = _build_internal_request(req, event, model)
    # Optionally prepend a persona system_prompt at the head of the
    # request messages. Off by default; opt-in via the per-channel
    # ``[channels.qq.humanlike]`` config or the live ``humanlike_resolver``
    # callback. The resolver wins when set so an admin PUT to
    # ``/admin/channels/qq/humanlike`` takes effect on the very next
    # inbound message without restarting the channel task. Empty body or
    # missing persona_id silently no-ops — half-configured TOML stays
    # operational rather than crashing.
    if params is not None:
        await _qq_inject_persona_if_enabled(request, params)
    _log.info("qq handle_one start user=%s model=%s", event.user_id, model)
    if inbox is not None and inbox_id is not None:
        try:
            await inbox.mark_dispatched(inbox_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("qq inbox mark_dispatched failed: %s", exc)

    # NapCat-only input-status indicator in private chats.
    typing_task: asyncio.Task[None] | None = None
    if event.message_type == MessageType.PRIVATE:
        typing_task = asyncio.create_task(
            _qq_input_status_pulse(adapter, event.user_id, cancel)
        )

    text_parts: list[str] = []
    error_message: str | None = None
    supplemented = False
    # Per-turn tool-activity log. Each entry: (kind, label, duration_ms,
    # is_error, error_summary). Rendered by _qq_format_activity_summary
    # and prepended to the final reply if non-empty.
    activity: list[tuple[str, str, int | None, bool, str]] = []
    # Surface the live status link only when a sub-agent is dispatched (QQ has
    # no editable spinner, so this is a standalone message). If the standalone
    # send fails, the final reply may append one fallback link.
    _qq_status_link_requested = False
    _qq_status_link_sent = False
    try:
        stream = chat_service.run(request, cancel)
        async for chat_ev in stream:
            kind = _event_kind(chat_ev)
            if kind == "token_delta":
                text_parts.append(getattr(chat_ev, "text", "") or "")
            elif kind == "done":
                # Claude-Code-style mid-turn supplement: the agent
                # servicer absorbed this RPC's user text into the
                # already-running turn for the same session_key. Don't
                # render a reply — the original turn is still in flight
                # and will produce one of its own.
                if _is_supplemented_done(chat_ev):
                    supplemented = True
                    _log.info(
                        "qq handle_one supplemented session=%s",
                        req.binding.session_key(),
                    )
                break
            elif kind == "error":
                error_message = getattr(chat_ev, "error", "") or getattr(
                    chat_ev, "message", ""
                )
                break
            elif kind == "tool_call":
                tool_name = getattr(chat_ev, "tool", "") or ""
                if (
                    not _qq_status_link_requested
                    and not _qq_status_link_sent
                    and tool_name in _SUBAGENT_SPAWN_TOOLS
                ):
                    # Epoch-adjusted: the running turn's session is
                    # ``<base>:eN`` after /new, so the status link must
                    # be minted for that key (see
                    # _effective_session_key_for) — a base-key link
                    # points at the dead pre-/new session. Same applies
                    # to every converted call site below.
                    _qq_link = _status_link_line(
                        _effective_session_key_for(req.binding)
                    )
                    if _qq_link:
                        _qq_status_link_requested = True
                        try:
                            await adapter.send_action(
                                _build_reply_action(
                                    event, _qq_link, prepend_at_mention=False
                                )
                            )
                            _qq_status_link_sent = True
                        except Exception as exc:  # noqa: BLE001
                            _log.warning(
                                "qq early status link send failed: %s", exc
                            )
                if tool_name == _SEND_ATTACHMENT_TOOL:
                    # Real upload; the agent-side dispatch is a no-op
                    # stub so the loop continues — we do the work here.
                    # The attachment line stands in for the call/result
                    # pair in the summary block.
                    await _qq_send_attachment(adapter, event, chat_ev)
                    _path, _caption, _filename = _parse_send_attachment_args(
                        chat_ev
                    )
                    display = (_filename or "").strip()
                    if not display and _path:
                        from pathlib import Path as _P

                        display = _P(_path).name
                    activity.append(
                        ("attachment", display or "(file)", None, False, "")
                    )
                elif tool_name == _TODO_WRITE_TOOL:
                    # Drop ``todo_write`` calls from the QQ summary
                    # entirely — pending ``☐`` rows are forward-looking
                    # noise on a non-editable channel. The operation
                    # flow (other tool calls) IS the "what just
                    # happened" signal the user actually wants.
                    pass
                else:
                    activity.append(
                        ("call", _qq_activity_label_for_call(chat_ev),
                         None, False, "")
                    )
                # Other tool_call frames stay informational — QQ has no
                # editMessage equivalent, so we can't render a mutable
                # spinner. The set_input_status pulse is the user-
                # visible signal that work is happening; the summary
                # block (prepended below) is the post-hoc audit trail.
            elif kind == "tool_result":
                tool_name = getattr(chat_ev, "tool", "") or ""
                if tool_name == _SEND_ATTACHMENT_TOOL:
                    # send_attachment already rendered as its own 📎
                    # line — the completion is implicit.
                    continue
                if tool_name == _TODO_WRITE_TOOL:
                    # The checkbox list IS the signal; a paired
                    # ✅ todo_write line would just clutter the block.
                    continue
                dur_ms = int(getattr(chat_ev, "duration_ms", 0) or 0)
                is_error = bool(getattr(chat_ev, "is_error", False))
                err_summary = getattr(chat_ev, "error_summary", "") or ""
                tool_label = (tool_name or "?").replace("\n", " ")
                if len(tool_label) > 60:
                    tool_label = tool_label[:57] + "..."
                activity.append(
                    ("result", tool_label, dur_ms, is_error, err_summary)
                )
    except Exception as exc:  # noqa: BLE001 — never let a crash kill the row
        _log.exception("qq handle_one crashed: %s", exc)
        if inbox is not None and inbox_id is not None:
            try:
                await inbox.mark_dead(inbox_id, error=f"crash: {exc!r}")
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        if typing_task is not None:
            typing_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await typing_task

    if supplemented:
        # Silent acknowledgement — the running turn for this session
        # absorbed our user text. Do NOT post any reply / summary.
        # The inbox row is marked done (the supplement was successfully
        # absorbed; no error to report).
        if inbox is not None and inbox_id is not None:
            try:
                await inbox.mark_done(inbox_id)
            except Exception as exc:  # noqa: BLE001
                _log.warning("qq inbox mark_done failed: %s", exc)
        _log.info("channel.user_supplemented channel=qq user=%s", event.user_id)
        return

    # Build the optional tool-activity prelude (honours env knob). The
    # todo block is intentionally DROPPED on QQ — pending ☐ rows are
    # forward-looking noise for non-editable channels; the operation
    # flow IS the "what just happened" signal.
    summary = (
        _qq_format_activity_summary(activity)
        if _qq_tool_summary_enabled()
        else ""
    )

    if error_message is not None:
        body = f"[corlinman error] {error_message}"
        if summary:
            body = summary + "\n" + body
        _log.error("qq handle_one error user=%s error=%r", event.user_id, error_message)
    else:
        body = _normalize_for_channel("".join(text_parts), "qq")
        if not body.strip():
            # Empty assistant reply. If we ran tools this turn, the user
            # still deserves to see what happened — send just the
            # summary block. Otherwise stay silent as before.
            if summary:
                body = summary.rstrip()
                _log.info(
                    "qq handle_one summary-only reply user=%s tools=%d",
                    event.user_id,
                    len(activity),
                )
            else:
                _log.warning(
                    "qq handle_one empty reply user=%s", event.user_id
                )
                if inbox is not None and inbox_id is not None:
                    try:
                        await inbox.mark_done(inbox_id)
                    except Exception:  # noqa: BLE001
                        pass
                return  # Empty assistant reply → silent drop.
        else:
            if summary:
                body = summary + "\n" + body
            _log.info(
                "qq handle_one reply user=%s len=%d tools=%d",
                event.user_id,
                len(body),
                len(activity),
            )

    # NapCat / OneBot don't expose an exact per-message char limit
    # in the protocol, but in practice 4000-5000 chars per QQ message
    # is the practical ceiling before NapCat itself truncates. Split
    # on natural boundaries below that — same behaviour as Telegram /
    # Discord / Slack / Feishu — instead of letting NapCat silently
    # cut off the reply.
    #
    # [MSG_BREAK] bubble-split: if the body contains the persona marker,
    # send each segment as a separate QQ message with a short pause.
    bubbles = _split_on_msg_break(body)
    # Append a fallback link only for an actual sub-agent fan-out whose
    # standalone early send failed. Ordinary replies must not carry status
    # links.
    _qq_status_line = (
        _status_link_line(_effective_session_key_for(req.binding))
        if _qq_status_link_requested and not _qq_status_link_sent
        else ""
    )
    for bubble_idx, bubble in enumerate(bubbles):
        chunks = chunk_reply(bubble, _QQ_TEXT_LIMIT)
        # Surface the shareable agent-status link on the LAST chunk of the
        # LAST bubble only (one link per turn).
        if _qq_status_line and chunks and bubble_idx == len(bubbles) - 1:
            chunks[-1] = try_append_footer(
                chunks[-1], _qq_status_line, _QQ_TEXT_LIMIT
            )
        if len(chunks) > 1:
            _log.info(
                "qq reply split user=%s len=%d chunks=%d",
                event.user_id,
                len(bubble),
                len(chunks),
            )
        for idx, chunk in enumerate(chunks):
            action = _build_reply_action(
                event, chunk, prepend_at_mention=(idx == 0 and bubble_idx == 0)
            )
            await adapter.send_action(action)
        if bubble_idx < len(bubbles) - 1:
            await asyncio.sleep(0.3)
    if inbox is not None and inbox_id is not None:
        try:
            await inbox.mark_done(inbox_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("qq inbox mark_done failed: %s", exc)


async def _qq_inject_persona_if_enabled(
    request: Any, params: QqChannelParams
) -> None:
    """Thin wrapper around :func:`persona_inject.inject_persona_if_enabled`.

    Kept as a named QQ-specific helper for ``handle_one_qq`` callers and
    for the historical test imports — the actual injection logic now
    lives in :mod:`corlinman_channels.persona_inject` so it can be
    shared across every humanlike-capable channel (QQ / Telegram /
    Discord / Slack / Feishu).
    """
    await _inject_persona_if_enabled(
        request,
        humanlike_enabled=params.humanlike_enabled,
        persona_id=params.persona_id,
        persona_store=params.persona_store,
        humanlike_resolver=_binding_persona_resolver(
            getattr(request, "binding", None),
            humanlike_enabled=params.humanlike_enabled,
            persona_id=params.persona_id,
            humanlike_resolver=params.humanlike_resolver,
        ),
        asset_store=params.asset_store,
        channel_name="qq",
    )


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

    Bug fix (2026-05-26): we used to hand the chat service a list of
    ``corlinman_channels.common.Attachment`` dataclasses, which carry a
    ``data`` field — but the gateway's ``_attachment_to_proto`` reads
    ``a.bytes_`` (the server-side ``gateway_api.Attachment`` field
    name). On a QQ inbound that contained an image segment this raised
    ``AttributeError`` deep inside the async generator and surfaced as
    ``"generator didn't stop after throw()"`` — the whole turn died and
    the inbox row went ``dead``. We now normalise to the lighter
    "shape" the server-side proto builder is actually written against.
    """
    from types import SimpleNamespace

    from corlinman_channels.onebot import segments_to_attachments

    attachments = [
        _to_server_attachment_shape(a)
        for a in segments_to_attachments(event.message)
    ]
    # Inbound attribution: prefix with sender / reply-to context when the
    # router carried it (group attribution). A no-op for the OneBot path
    # today since the QQ ``MessageEvent`` port doesn't expose a display
    # name yet — the field exists so a future onebot-parse lane lights it
    # up with zero servicer change.
    prefix = _attribution(
        sender_name=getattr(req, "sender_name", None),
        reply_to_text=getattr(req, "reply_to_text", None),
    )
    content = f"{prefix}\n{req.content}" if prefix else req.content
    message = SimpleNamespace(role="user", content=content)
    # Per-binding session prefs (/model override + /new epoch) — fail-open
    # shims that return the inputs unchanged when the server-side store is
    # unavailable. Applied here (the single choke point) so every QQ call
    # site honours the user's /model and /new choices.
    from corlinman_channels import binding_prefs as _binding_prefs

    return SimpleNamespace(
        model=_binding_prefs.effective_model(req.binding, model),
        messages=[message],
        session_key=_binding_prefs.effective_session_key(
            req.binding, req.session_key
        ),
        stream=True,
        max_tokens=None,
        temperature=None,
        attachments=attachments,
        binding=req.binding,
        # Shape-match InternalChatRequest. Persona injection overwrites this
        # with a real id when humanlike is enabled; default None keeps the
        # server-side proto builder's tolerant read happy.
        persona_id=None,
    )


# Server-side enum strings the proto builder pattern-matches on (see
# ``corlinman_server.gateway_api.types.AttachmentKind``). Channels-side
# carries the same string values but as a SEPARATE enum class, so
# equality across classes is False even when the wire value matches —
# proto builder would silently coerce every kind to UNSPECIFIED.
_KIND_REMAP: dict[str, str] = {
    "image": "image",
    "audio": "audio",
    "video": "video",
    "document": "file",   # channels uses DOCUMENT; server uses FILE
    "file": "file",
}


def _to_server_attachment_shape(att: Any) -> Any:
    """Convert a :class:`corlinman_channels.common.Attachment` to the
    shape ``corlinman_server.gateway.services.chat_service._build_chat_start``
    expects (``bytes_`` field name + lowercase string ``kind`` from the
    server-side enum).

    Done as a SimpleNamespace so neither side needs to know about the
    other's concrete class (channels is import-decoupled from server).
    """
    from types import SimpleNamespace

    kind_raw: Any = getattr(att, "kind", None)
    kind_str = (
        str(kind_raw.value) if hasattr(kind_raw, "value") else str(kind_raw or "")
    ).lower()
    kind = _KIND_REMAP.get(kind_str, kind_str)

    # Server's _attachment_to_proto compares via `== ApiAttachmentKind.IMAGE`
    # etc. — those enums are StrEnum so comparing against the raw string
    # value would fail. We lazy-import the server-side enum class to
    # produce a real ApiAttachmentKind instance; if the server package
    # isn't importable (standalone channel tests) we fall back to the
    # raw string and accept that the proto builder will hit the
    # UNSPECIFIED branch.
    try:
        from corlinman_server.gateway_api.types import (
            AttachmentKind as ApiKind,
        )

        try:
            kind_value = ApiKind(kind)
        except ValueError:
            kind_value = ApiKind.IMAGE if kind == "image" else ApiKind.FILE
    except Exception:  # noqa: BLE001
        kind_value = kind  # type: ignore[assignment]

    return SimpleNamespace(
        kind=kind_value,
        url=getattr(att, "url", None) or None,
        bytes_=getattr(att, "data", None) or None,
        mime=getattr(att, "mime", None) or None,
        file_name=getattr(att, "file_name", None) or None,
    )


def _build_reply_action(
    event: MessageEvent,
    body: str,
    *,
    prepend_at_mention: bool = True,
    image_urls: list[str] | None = None,
    image_files: list[str] | None = None,
) -> Action:
    """Build a ``SendGroupMsg`` / ``SendPrivateMsg`` action.

    The message carries a text segment (when ``body`` is non-empty) plus
    one :class:`ImageSegment` per entry in ``image_urls`` / ``image_files``
    — WS-1 task 1's inline media-send path. ``image_files`` is for
    OneBot ``file`` payloads such as ``base64://...``. The image segments
    are appended on BOTH the group and private branches, and the OneBot
    wire layer (``_segment_to_wire``) already serializes them, so an
    image reply lands inline in the chat.

    Group messages prepend an ``@sender`` so the reply is clearly
    addressed (matches qqBot.js / Rust). When a long reply is split
    into multiple chunks, the caller MUST set ``prepend_at_mention=False``
    on every chunk after the first — otherwise the user gets
    ``@User`` × N pings in the group, which QQ clients render as spam
    and Tencent's anti-spam may rate-limit. Telegram does the
    equivalent by only setting ``reply_to_message_id`` on chunk[0].
    """
    from corlinman_channels.onebot import ImageSegment

    image_segs = [ImageSegment(url=u) for u in (image_urls or []) if u]
    image_segs.extend(ImageSegment(file=f) for f in (image_files or []) if f)
    if event.message_type == MessageType.GROUP:
        from corlinman_channels.onebot import AtSegment

        gid = event.group_id or 0
        segments: list[Any] = []
        if prepend_at_mention:
            segments.append(AtSegment(qq=str(event.user_id)))
            # Keep the body text only when there is one — an inline image
            # with no caption shouldn't ship a bare " " text segment.
            if body:
                segments.append(TextSegment(text=f" {body}"))
        elif body:
            segments.append(TextSegment(text=body))
        segments.extend(image_segs)
        return SendGroupMsg(group_id=gid, message=segments)
    pm_segments: list[Any] = []
    if body:
        pm_segments.append(TextSegment(text=body))
    pm_segments.extend(image_segs)
    return SendPrivateMsg(user_id=event.user_id, message=pm_segments)


# ---------------------------------------------------------------------------
# Telegram channel
# ---------------------------------------------------------------------------


# Public, mutable: latest Telegram health probe + traffic snapshot.
# Mirrors :data:`QQ_HEALTH` — admin status routes read it directly so
# the admin page sees real numbers instead of a hardcoded mock.
#
# Counter / aggregate semantics:
#
# * ``messages_today`` resets at UTC midnight (compared against
#   ``_TELEGRAM_DAY_KEY``); ``messages_week`` rolls over the trailing
#   7 days by pruning ``_TELEGRAM_DAY_COUNTS`` on every increment.
# * ``latency_p50_ms`` / ``latency_p95_ms`` are computed lazily from the
#   :data:`_TELEGRAM_LATENCIES` ring buffer; the buffer holds up to 200
#   samples (inbound-event timestamp → reply-send wallclock).
# * ``active_chats`` counts distinct ``chat_id`` keys seen in
#   :data:`_TELEGRAM_ACTIVE_CHATS` over the last 24h; entries older than
#   24h are pruned on every recompute.
TELEGRAM_HEALTH: dict[str, Any] = {
    "online": False,
    "last_event_at_ms": None,
    "seconds_since_event": None,
    "checked_at_ms": None,
    "messages_today": 0,
    "messages_week": 0,
    "latency_p50_ms": None,
    "latency_p95_ms": None,
    "active_chats": 0,
}

# Recent-messages ring buffer feeding ``/admin/channels/telegram/messages``.
# Each entry is a dict whose shape matches the frontend's
# ``TelegramMessage`` contract (id / kind / chat_id / chat_title /
# from_username / content / timestamp_ms / routing / mention_reason).
# Capped at 500 so a long-running gateway can't blow memory; the admin
# UI typically fetches the most recent 20.
TELEGRAM_RECENT_MESSAGES: collections.deque[dict[str, Any]] = collections.deque(
    maxlen=500
)

# Trailing 7-day per-day counters keyed by ``YYYY-MM-DD`` (UTC). Pruned
# on every update so a quiet bot doesn't accumulate stale keys.
_TELEGRAM_DAY_COUNTS: dict[str, int] = {}
_TELEGRAM_DAY_KEY: str | None = None

# Round-trip latency samples (ms) — capped so the snapshot stays cheap to
# compute and we don't hold onto an unbounded history.
_TELEGRAM_LATENCIES: collections.deque[int] = collections.deque(maxlen=200)

# ``chat_id -> last_seen_ms`` for the active-chats rollup. Pruned to
# entries within the last 24h on every recompute.
_TELEGRAM_ACTIVE_CHATS: dict[str, int] = {}

# Health window: a Telegram channel is "online" if it has received an
# update within the last 5 minutes. Long-poll typically refreshes every
# 25 seconds, so this leaves a comfortable margin.
_TELEGRAM_HEALTH_WINDOW_MS: int = 5 * 60 * 1000

# 24h in ms — used to prune active-chats entries.
_TELEGRAM_ACTIVE_WINDOW_MS: int = 24 * 60 * 60 * 1000


def _telegram_utc_day_key(ts_ms: int) -> str:
    """``YYYY-MM-DD`` for a UTC midnight bucket."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _telegram_recompute_aggregates(now_ms: int | None = None) -> None:
    """Refresh ``TELEGRAM_HEALTH`` aggregates from the underlying buffers.

    Called both on every accepted/sent event and on every ``/status``
    read so a stale snapshot never reaches the admin UI. Pure: only
    derives values from already-recorded samples and prunes stale entries.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    # Rollover the day key if a UTC midnight crossed since the last bump.
    today_key = _telegram_utc_day_key(now_ms)
    global _TELEGRAM_DAY_KEY  # noqa: PLW0603 — module-level cursor
    if _TELEGRAM_DAY_KEY is None:
        _TELEGRAM_DAY_KEY = today_key

    # Prune day counts older than 7 days.
    cutoff_ms = now_ms - 7 * 24 * 60 * 60 * 1000
    cutoff_key = _telegram_utc_day_key(cutoff_ms)
    stale = [k for k in _TELEGRAM_DAY_COUNTS if k < cutoff_key]
    for k in stale:
        _TELEGRAM_DAY_COUNTS.pop(k, None)

    TELEGRAM_HEALTH["messages_today"] = int(_TELEGRAM_DAY_COUNTS.get(today_key, 0))
    TELEGRAM_HEALTH["messages_week"] = int(sum(_TELEGRAM_DAY_COUNTS.values()))

    # Prune stale active chats and recount.
    active_cutoff = now_ms - _TELEGRAM_ACTIVE_WINDOW_MS
    expired = [cid for cid, last in _TELEGRAM_ACTIVE_CHATS.items() if last < active_cutoff]
    for cid in expired:
        _TELEGRAM_ACTIVE_CHATS.pop(cid, None)
    TELEGRAM_HEALTH["active_chats"] = len(_TELEGRAM_ACTIVE_CHATS)

    # Latency percentiles from the ring buffer.
    samples = sorted(_TELEGRAM_LATENCIES)
    if samples:
        def _pct(p: float) -> int:
            if len(samples) == 1:
                return int(samples[0])
            # Nearest-rank percentile — cheap + stable for a 200-sample
            # window. Matches what most ops dashboards expect.
            idx = max(0, min(len(samples) - 1, round(p * (len(samples) - 1))))
            return int(samples[idx])

        TELEGRAM_HEALTH["latency_p50_ms"] = _pct(0.50)
        TELEGRAM_HEALTH["latency_p95_ms"] = _pct(0.95)
    else:
        TELEGRAM_HEALTH["latency_p50_ms"] = None
        TELEGRAM_HEALTH["latency_p95_ms"] = None

    # Online flag — true when an event was seen recently.
    last = TELEGRAM_HEALTH.get("last_event_at_ms")
    if isinstance(last, int):
        delta_ms = now_ms - last
        TELEGRAM_HEALTH["seconds_since_event"] = max(0, delta_ms // 1000)
        TELEGRAM_HEALTH["online"] = delta_ms <= _TELEGRAM_HEALTH_WINDOW_MS
    else:
        TELEGRAM_HEALTH["seconds_since_event"] = None
        TELEGRAM_HEALTH["online"] = False
    TELEGRAM_HEALTH["checked_at_ms"] = now_ms


def telegram_record_inbound(
    inbound: InboundEvent[Any],
    *,
    now_ms: int | None = None,
) -> None:
    """Record an accepted inbound event.

    Bumps the day counter, marks the chat active, captures the event
    wallclock for the latency round-trip, appends an entry to
    :data:`TELEGRAM_RECENT_MESSAGES`, and refreshes the aggregates.

    Best-effort: any exception is logged and swallowed so a counter bug
    never breaks the chat path.
    """
    try:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        day_key = _telegram_utc_day_key(now_ms)
        _TELEGRAM_DAY_COUNTS[day_key] = _TELEGRAM_DAY_COUNTS.get(day_key, 0) + 1
        chat_id = str(inbound.binding.thread)
        _TELEGRAM_ACTIVE_CHATS[chat_id] = now_ms
        TELEGRAM_HEALTH["last_event_at_ms"] = now_ms

        # Snapshot the message into the recent-messages ring buffer. The
        # frontend `TelegramMessage` shape uses string ids, kind /
        # routing / mention_reason enums, and an epoch-ms timestamp.
        payload = inbound.payload if isinstance(inbound.payload, dict) else None
        chat_obj = payload.get("chat") if isinstance(payload, dict) else None
        chat_type = ""
        chat_title: str | None = None
        if isinstance(chat_obj, dict):
            chat_type = str(chat_obj.get("type", "") or "")
            t = chat_obj.get("title")
            if isinstance(t, str):
                chat_title = t
        is_group = chat_type in {"group", "supergroup", "channel"}

        from_username: str | None = None
        if isinstance(payload, dict):
            from_obj = payload.get("from")
            if isinstance(from_obj, dict):
                u = from_obj.get("username")
                if isinstance(u, str):
                    from_username = u
                else:
                    fn = from_obj.get("first_name")
                    if isinstance(fn, str):
                        from_username = fn

        if is_group:
            mention_reason = "mention" if inbound.mentioned else "none"
        else:
            mention_reason = "dm"

        TELEGRAM_RECENT_MESSAGES.append(
            {
                "id": str(inbound.message_id) if inbound.message_id is not None else f"tg-{now_ms}",
                "kind": "group" if is_group else "private",
                "chat_id": chat_id,
                "chat_title": chat_title,
                "from_username": from_username,
                "content": inbound.text,
                "timestamp_ms": now_ms,
                "routing": "queued",
                "mention_reason": mention_reason,
            }
        )

        _telegram_recompute_aggregates(now_ms)
    except Exception as exc:  # noqa: BLE001 — never block chat
        _log.debug("telegram_record_inbound failed: %s", exc)


def telegram_record_reply_sent(
    inbound: InboundEvent[Any] | None,
    *,
    inbound_ts_ms: int | None,
    now_ms: int | None = None,
) -> None:
    """Record a successful outbound reply for a turn.

    ``inbound_ts_ms`` is the wallclock at which the originating inbound
    landed (captured by :func:`telegram_record_inbound` via
    ``TELEGRAM_HEALTH["last_event_at_ms"]`` but the caller passes the
    pre-spinner snapshot so a long-running turn doesn't fold in the next
    event's timestamp). Appends one sample to the latency ring buffer
    and refreshes aggregates.
    """
    try:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        if inbound_ts_ms is not None:
            latency = max(0, now_ms - inbound_ts_ms)
            _TELEGRAM_LATENCIES.append(int(latency))
        if inbound is not None:
            chat_id = str(inbound.binding.thread)
            # Flip the most recent matching entry to "responded" so the
            # admin feed reflects the live routing decision.
            for entry in reversed(TELEGRAM_RECENT_MESSAGES):
                if entry.get("chat_id") == chat_id and entry.get("routing") == "queued":
                    entry["routing"] = "responded"
                    break
        _telegram_recompute_aggregates(now_ms)
    except Exception as exc:  # noqa: BLE001 — never block chat
        _log.debug("telegram_record_reply_sent failed: %s", exc)


def _telegram_reset_state_for_tests() -> None:
    """Test-only helper: clear every Telegram counter / buffer so each
    test starts from a known baseline. Not part of the public surface."""
    TELEGRAM_HEALTH.update(
        online=False,
        last_event_at_ms=None,
        seconds_since_event=None,
        checked_at_ms=None,
        messages_today=0,
        messages_week=0,
        latency_p50_ms=None,
        latency_p95_ms=None,
        active_chats=0,
    )
    TELEGRAM_RECENT_MESSAGES.clear()
    _TELEGRAM_DAY_COUNTS.clear()
    global _TELEGRAM_DAY_KEY  # noqa: PLW0603 — test-only cursor reset
    _TELEGRAM_DAY_KEY = None
    _TELEGRAM_LATENCIES.clear()
    _TELEGRAM_ACTIVE_CHATS.clear()


# ---------------------------------------------------------------------------
# Discord / Slack / Feishu health + recent-message snapshots
# ---------------------------------------------------------------------------
#
# Parallel to :data:`TELEGRAM_HEALTH` / :data:`TELEGRAM_RECENT_MESSAGES`
# but simpler: these channels expose a flat ``received`` / ``sent`` /
# ``errors`` counter triple + an ``online`` flag derived from a sliding
# "last event within N minutes" window. The admin status routes read the
# health dict directly and the messages route reads the recent buffer.
#
# Each health snapshot carries:
#   online: bool             — saw an inbound within the freshness window
#   last_event_at_ms: int|None — wallclock of the most recent inbound
#   received: int            — lifetime inbound count
#   sent: int                — lifetime successful outbound count
#   errors: int              — lifetime outbound failure count
#
# Recent-message entries mirror the Telegram shape so the frontend can
# render them with one component: id / kind / chat_id / chat_title /
# from_username / content / timestamp_ms / routing / mention_reason.

#: A channel is "online" when an inbound landed within the last 5 minutes
#: — same window as Telegram so the admin banner is consistent.
_CHANNEL_HEALTH_WINDOW_MS: int = 5 * 60 * 1000


def _new_channel_health() -> dict[str, Any]:
    return {
        "online": False,
        "last_event_at_ms": None,
        "received": 0,
        "sent": 0,
        "errors": 0,
    }


DISCORD_HEALTH: dict[str, Any] = _new_channel_health()
SLACK_HEALTH: dict[str, Any] = _new_channel_health()
FEISHU_HEALTH: dict[str, Any] = _new_channel_health()

DISCORD_RECENT_MESSAGES: collections.deque[dict[str, Any]] = collections.deque(
    maxlen=500
)
SLACK_RECENT_MESSAGES: collections.deque[dict[str, Any]] = collections.deque(
    maxlen=500
)
FEISHU_RECENT_MESSAGES: collections.deque[dict[str, Any]] = collections.deque(
    maxlen=500
)


def _channel_refresh_online(health: dict[str, Any], now_ms: int | None = None) -> None:
    """Recompute the ``online`` flag from ``last_event_at_ms`` against the
    freshness window. Called on every record + on every ``/status`` read so
    the admin UI never shows a stale online flag."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    last = health.get("last_event_at_ms")
    if isinstance(last, int):
        health["online"] = (now_ms - last) <= _CHANNEL_HEALTH_WINDOW_MS
    else:
        health["online"] = False


def _channel_record_inbound(
    health: dict[str, Any],
    recent: collections.deque[dict[str, Any]],
    inbound: InboundEvent[Any],
    *,
    now_ms: int | None = None,
) -> None:
    """Record an accepted inbound event for a Discord/Slack/Feishu channel.

    Bumps ``received``, stamps ``last_event_at_ms`` + ``online``, and
    appends a normalized recent-message dict. Best-effort: any exception is
    logged and swallowed so a counter bug never breaks the chat path."""
    try:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        health["received"] = int(health.get("received", 0)) + 1
        health["last_event_at_ms"] = now_ms
        chat_id = str(inbound.binding.thread)
        recent.append(
            {
                "id": str(inbound.message_id)
                if inbound.message_id is not None
                else f"{inbound.channel}-{now_ms}",
                "kind": "group" if not inbound.mentioned else "mention",
                "chat_id": chat_id,
                "chat_title": None,
                "from_username": str(inbound.binding.sender) or None,
                "content": inbound.text,
                "timestamp_ms": now_ms,
                "routing": "queued",
                "mention_reason": "mention" if inbound.mentioned else "none",
            }
        )
        _channel_refresh_online(health, now_ms)
    except Exception as exc:  # noqa: BLE001 — never block chat
        _log.debug("_channel_record_inbound failed: %s", exc)


def _channel_record_sent(health: dict[str, Any], *, now_ms: int | None = None) -> None:
    """Record a successful outbound send: bump ``sent`` and flip the most
    recent matching ``queued`` entry to ``responded`` is left to the caller;
    here we only bump the counter. Best-effort."""
    try:
        health["sent"] = int(health.get("sent", 0)) + 1
        _channel_refresh_online(health, now_ms)
    except Exception as exc:  # noqa: BLE001 — never block chat
        _log.debug("_channel_record_sent failed: %s", exc)


def _channel_record_error(health: dict[str, Any]) -> None:
    """Record an outbound failure: bump ``errors``. Best-effort."""
    try:
        health["errors"] = int(health.get("errors", 0)) + 1
    except Exception as exc:  # noqa: BLE001 — never block chat
        _log.debug("_channel_record_error failed: %s", exc)


def _channel_mark_responded(
    recent: collections.deque[dict[str, Any]], chat_id: str
) -> None:
    """Flip the most recent ``queued`` entry for ``chat_id`` to
    ``responded`` so the admin feed reflects the live routing decision —
    parallel to :func:`telegram_record_reply_sent`. Best-effort."""
    try:
        for entry in reversed(recent):
            if entry.get("chat_id") == chat_id and entry.get("routing") == "queued":
                entry["routing"] = "responded"
                break
    except Exception as exc:  # noqa: BLE001 — never block chat
        _log.debug("_channel_mark_responded failed: %s", exc)


def _channel_reset_state_for_tests(
    health: dict[str, Any], recent: collections.deque[dict[str, Any]]
) -> None:
    """Test-only helper: clear a Discord/Slack/Feishu health snapshot +
    recent buffer back to a known baseline. Not part of the public
    surface."""
    health.update(_new_channel_health())
    recent.clear()


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

    on_sender_ready: Any = None
    """W4-FE F2 — optional sync callback ``(TelegramSender) -> None``
    invoked once the channel constructs its live sender. Lets the
    gateway park the sender on :class:`AdminState` so the
    ``POST /admin/channels/telegram/send`` admin route can push test
    messages through the same HTTPS surface the chat path uses.
    ``None`` keeps the channel running unchanged — the admin send route
    then returns 503 ``telegram_disabled``."""

    # ---- human-like persona toggle (W7 Persona Studio) -----------------
    # Mirror of the QQ humanlike fields — see :class:`QqChannelParams`
    # for the full contract. Off by default; the per-turn injector
    # silently no-ops when the gate is off or the store is missing.
    humanlike_enabled: bool = False
    persona_id: str | None = None
    persona_store: Any = None
    humanlike_resolver: Any = None
    asset_store: Any = None

    event_emitter: Any = None
    """W4.1 — optional gateway-wide
    :class:`corlinman_server.gateway.observability.JournalBackedEmitter`.
    When set, ``handle_one_telegram`` subscribes per-turn to receive
    :class:`ToolStateHeartbeat` / :class:`Cancelling` / :class:`TurnComplete`
    envelopes and surfaces them via the existing mutable spinner +
    post-turn footer. ``None`` falls back to the legacy in-process
    stream and computes elapsed / tool-call counts locally — keeps
    test environments and pre-W4.1 deployments running unchanged."""


def _inbound_is_dm(ev: InboundEvent[Any]) -> bool:
    """Best-effort "is this a 1:1 / private chat" check for CMP-06.

    Reads the transport-specific chat-type hint off the raw payload when
    present (Telegram ``chat.type == "private"``, Feishu ``chat_type ==
    "p2p"``); falls back to the binding convention where private chats
    keep ``thread == sender``. Conservative: an unknown shape is treated
    as a group so a ``DM_ONLY`` command never leaks into a shared chat.
    """
    payload = ev.payload if isinstance(ev.payload, dict) else None
    if isinstance(payload, dict):
        chat_obj = payload.get("chat")
        if isinstance(chat_obj, dict):
            ct = str(chat_obj.get("type", "") or "")
            if ct:
                return ct == "private"
        msg = payload.get("message")
        if isinstance(msg, dict):
            ct = str(msg.get("chat_type", "") or "")
            if ct:
                return ct == "p2p"
    b = ev.binding
    return bool(b.thread) and b.thread == b.sender


async def _telegram_try_dispatch_command(
    ev: InboundEvent[Any],
    sender: TelegramSender,
    *,
    slash_policy: Any = None,
) -> bool:
    """Run a handler command for ``ev`` if one matches; else return False.

    Mirrors the QQ dispatch loop's command short-circuit. We hit
    :func:`match_command_with_args` on the inbound text. When the
    matched spec carries a handler, we invoke
    :func:`run_command_handler` and ship the result via the Telegram
    sender; the caller continues without spawning a chat task. Specs
    that only have a wizard prelude (e.g. ``/persona``) return
    ``False`` here so the agent path keeps owning them — adding a
    rewrite seam for Telegram is a separate piece of work.

    Returns ``True`` iff the command was handled here.
    """
    text = (ev.text or "").strip()
    if not text:
        return False
    # Telegram appends ``@botname`` to commands sent from the BotFather
    # menu in groups (e.g. ``/help@Cornna_bot``). Strip the suffix from
    # the leading token before matching so both forms route to the
    # same spec. We only touch the first whitespace-delimited token so
    # ``@mention`` inside args is preserved.
    if text.startswith("/"):
        head, sep, rest = text.partition(" ")
        if "@" in head:
            head = head.split("@", 1)[0]
            text = f"{head}{sep}{rest}" if sep else head
    match = match_command_with_args(text)
    if match is None:
        return False
    spec, args_text = match
    if spec.handler is None:
        # Prelude-only spec — normally let the agent path own it. But a
        # SlashAccessPolicy denial must still intercept here (CMP-06):
        # send the refusal and return True so the literal command never
        # reaches the agent as a wizard prelude.
        if slash_policy is not None and not slash_policy.allows(
            spec,
            ev.binding,
            is_dm=_inbound_is_dm(ev),
            is_admin=is_command_admin(ev.binding),
        ):
            await _telegram_send_command_refusal(ev, sender, spec, slash_policy)
            return True
        return False
    try:
        ctx = CommandContext(
            spec=spec,
            raw_text=text,
            args_text=args_text,
            binding=ev.binding,
            is_admin=is_command_admin(ev.binding),
        )
        # CMP-06 — run_command_handler consults the policy (if any) and
        # returns an ephemeral refusal without invoking the handler when
        # the caller isn't permitted.
        result = await run_command_handler(
            spec, ctx, policy=slash_policy, is_dm=_inbound_is_dm(ev)
        )
    except Exception as exc:  # noqa: BLE001 — never crash the dispatch loop
        _log.exception("telegram command handler crashed: %s", exc)
        return True
    reply = (result.reply or "").strip()
    if not reply:
        return True
    try:
        chat_id = int(ev.binding.thread)
    except ValueError:
        _log.warning(
            "telegram command reply skipped: binding.thread is not int chat_id=%r",
            ev.binding.thread,
        )
        return True
    # Telegram's text limit is 4096 chars; chunk via the existing helper
    # so /help (which can grow as new commands register) never gets
    # rejected for being over-cap.
    chunks = _chunk_for_telegram(reply)
    try:
        first = True
        for chunk in chunks:
            await sender.send_message(
                chat_id,
                chunk,
                reply_to_message_id=(
                    int(ev.message_id) if ev.message_id and first else None
                ),
            )
            first = False
    except Exception as exc:  # noqa: BLE001
        _log.warning("telegram command reply send failed: %s", exc)
    return True


async def _telegram_send_command_refusal(
    ev: InboundEvent[Any],
    sender: TelegramSender,
    spec: Any,
    slash_policy: Any,
) -> None:
    """Send a CMP-06 slash-access refusal for a prelude-only command.

    Mirrors the wording the handler path returns via
    :func:`run_command_handler`. Best-effort: a non-int chat id or a send
    failure is logged, never raised.
    """
    from corlinman_channels.commands import SlashAccessTier  # noqa: PLC0415

    alias = spec.aliases[0] if spec.aliases else spec.name
    tier = slash_policy.tier_for(spec)
    if tier == SlashAccessTier.DM_ONLY:
        reply = f"❌ {alias} 仅支持私聊使用。"
    else:
        reply = f"❌ {alias} is restricted to administrators."
    try:
        chat_id = int(ev.binding.thread)
    except ValueError:
        _log.warning(
            "telegram command-refusal skipped: thread not int chat_id=%r",
            ev.binding.thread,
        )
        return
    try:
        await sender.send_message(
            chat_id,
            reply,
            reply_to_message_id=int(ev.message_id) if ev.message_id else None,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("telegram command-refusal send failed: %s", exc)


def _telegram_apply_command_prelude(
    ev: InboundEvent[Any],
) -> InboundEvent[Any]:
    """Return ``ev`` with ``text`` swapped for the wizard prelude when the
    user typed a prelude-only slash command (e.g. ``/persona``).

    The QQ path gets this rewrite for free via :func:`router.dispatch`;
    Telegram parses InboundEvents directly so it needs a parallel seam.
    Without it, prelude-only commands reach the agent as the literal
    ``/persona`` string and (with a Humanlike persona active) degrade
    into "list current personas" — the exact failure mode this helper
    exists to prevent.

    Returns ``ev`` unchanged when:

    * ``ev.text`` is empty,
    * no registered command matches,
    * the matched spec has a handler (those are dispatched by
      :func:`_telegram_try_dispatch_command` before this helper runs),
    * the matched spec has no ``wizard_prelude`` (defensive — current
      registry validation guarantees prelude OR handler, never neither).
    """
    text = (ev.text or "").strip()
    if not text:
        return ev
    # Mirror the @botname stripping in _telegram_try_dispatch_command so
    # /persona@Cornna_bot matches the same spec as /persona.
    if text.startswith("/"):
        head, sep, rest = text.partition(" ")
        if "@" in head:
            head = head.split("@", 1)[0]
            text = f"{head}{sep}{rest}" if sep else head
    match = match_command_with_args(text)
    if match is None:
        return ev
    spec, args_text = match
    if spec.handler is not None or spec.wizard_prelude is None:
        return ev
    from dataclasses import replace as _dc_replace  # noqa: PLC0415 — local seam

    # CMP-07: substitute $ARGUMENTS / $1..$N with the typed args.
    rewritten = apply_command_prelude(text, spec, args_text=args_text)
    _log.info(
        "telegram command_prelude_substituted cmd=%s prelude_len=%d",
        spec.name,
        len(rewritten),
    )
    return _dc_replace(ev, text=rewritten)


# ---------------------------------------------------------------------------
# Shared text-channel command dispatch (Discord / Slack / Feishu / WeChat)
# ---------------------------------------------------------------------------
# Channel-agnostic mirrors of the Telegram seams above, so every text
# channel gets the same handler short-circuit + prelude rewrite Telegram
# and QQ already have. Per-channel send mechanics (chunking, reply
# threading, the WeChat passive XML) stay with the caller via the
# ``send_reply`` callable.


def _command_refusal_text(spec: Any, slash_policy: Any) -> str:
    """CMP-06 refusal wording for a prelude-only command.

    Mirrors :func:`_telegram_send_command_refusal` (and the handler-path
    wording in :func:`corlinman_channels.commands.run_command_handler`).
    """
    from corlinman_channels.commands import SlashAccessTier  # noqa: PLC0415

    alias = spec.aliases[0] if spec.aliases else spec.name
    tier = slash_policy.tier_for(spec)
    if tier == SlashAccessTier.DM_ONLY:
        return f"❌ {alias} 仅支持私聊使用。"
    return f"❌ {alias} is restricted to administrators."


async def _try_dispatch_text_command(
    ev: InboundEvent[Any],
    send_reply: Callable[[str], Awaitable[None]],
    *,
    slash_policy: Any = None,
    channel_label: str = "channel",
) -> bool:
    """Run a handler command for ``ev`` if one matches; else return False.

    Channel-agnostic mirror of :func:`_telegram_try_dispatch_command`
    (minus the Telegram-only ``@botname`` suffix stripping). When the
    inbound text matches a registered slash command whose spec carries a
    handler, the handler runs here, the reply ships via ``send_reply``,
    and the caller skips the agent turn. Prelude-only specs fall through
    (``False``) so the agent path keeps owning them — except a CMP-06
    policy denial, which replies with the refusal and returns ``True``
    so the literal command never reaches the agent.

    Best-effort: a handler crash or send failure is logged, never
    raised — the inbound loop must survive every command.
    """
    text = (ev.text or "").strip()
    if not text:
        return False
    match = match_command_with_args(text)
    if match is None:
        return False
    spec, args_text = match
    if spec.handler is None:
        if slash_policy is not None and not slash_policy.allows(
            spec,
            ev.binding,
            is_dm=_inbound_is_dm(ev),
            is_admin=is_command_admin(ev.binding),
        ):
            try:
                await send_reply(_command_refusal_text(spec, slash_policy))
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "%s command-refusal send failed: %s", channel_label, exc
                )
            return True
        return False
    try:
        ctx = CommandContext(
            spec=spec,
            raw_text=text,
            args_text=args_text,
            binding=ev.binding,
            is_admin=is_command_admin(ev.binding),
        )
        result = await run_command_handler(
            spec, ctx, policy=slash_policy, is_dm=_inbound_is_dm(ev)
        )
    except Exception as exc:  # noqa: BLE001 — never crash the inbound loop
        _log.exception("%s command handler crashed: %s", channel_label, exc)
        return True
    reply = (result.reply or "").strip()
    if not reply:
        return True
    try:
        await send_reply(reply)
    except Exception as exc:  # noqa: BLE001
        _log.warning("%s command reply send failed: %s", channel_label, exc)
    return True


def _apply_text_command_prelude(ev: InboundEvent[Any]) -> InboundEvent[Any]:
    """Prelude-rewrite seam for the shared text channels.

    Channel-agnostic mirror of :func:`_telegram_apply_command_prelude`
    (minus the ``@botname`` stripping — that suffix is a Telegram
    BotFather artefact). Swaps a prelude-only slash command (e.g.
    ``/persona``) for its wizard prelude so the agent sees the structured
    invocation instead of the literal text.
    """
    text = (ev.text or "").strip()
    if not text:
        return ev
    match = match_command_with_args(text)
    if match is None:
        return ev
    spec, args_text = match
    if spec.handler is not None or spec.wizard_prelude is None:
        return ev
    from dataclasses import replace as _dc_replace  # noqa: PLC0415 — local seam

    rewritten = apply_command_prelude(text, spec, args_text=args_text)
    _log.info(
        "%s command_prelude_substituted cmd=%s prelude_len=%d",
        ev.channel,
        spec.name,
        len(rewritten),
    )
    return _dc_replace(ev, text=rewritten)


async def _discord_send_command_reply(
    sender: DiscordSender, ev: InboundEvent[Any], reply: str
) -> None:
    """Ship a command reply via Discord — chunked, first chunk threaded."""
    first = True
    for chunk in chunk_reply(reply, _DISCORD_TEXT_LIMIT):
        await sender.send_message(
            ev.binding.thread,
            chunk,
            reply_to_message_id=ev.message_id if first else None,
        )
        first = False


async def _slack_send_command_reply(
    sender: SlackSender, ev: InboundEvent[Any], reply: str
) -> None:
    """Ship a command reply via Slack — chunked, threaded under the
    inbound ``ts`` (same grouping as the agent reply path)."""
    for chunk in chunk_reply(reply, _SLACK_TEXT_LIMIT):
        await sender.send_message(
            ev.binding.thread, chunk, thread_ts=ev.message_id
        )


async def _feishu_send_command_reply(
    sender: FeishuSender, ev: InboundEvent[Any], reply: str
) -> None:
    """Ship a command reply via Feishu — chunked, first chunk as a reply."""
    first = True
    for chunk in chunk_reply(reply, _FEISHU_TEXT_LIMIT):
        await sender.send_message(
            ev.binding.thread,
            chunk,
            reply_to_message_id=ev.message_id if first else None,
        )
        first = False


async def _wechat_send_command_reply(
    sender: WeChatOfficialSender,
    inbound: InboundEvent[Any],
    passive_future: asyncio.Future[str] | None,
    reply: str,
) -> None:
    """Ship a command reply on WeChat: passive XML head + CS push rest.

    Mirrors the final-emit mechanics of
    :func:`handle_one_wechat_official` — the passive slot is
    length-capped, so a long ``/help`` spills into the customer-service
    push path. When the passive future is already gone (timeout / no
    future supplied) the whole reply goes through the push path.
    """
    passive, remainder = _split_passive_and_rest(reply)
    if passive_future is not None and not passive_future.done():
        passive_future.set_result(passive)
        push_body = remainder
    else:
        push_body = reply
    if push_body and push_body.strip():
        await sender.send_text_customer(inbound.binding.sender, push_body)


async def run_telegram_channel(
    params: TelegramChannelParams,
    cancel: asyncio.Event,
) -> None:
    """Spawn the Telegram channel loop and run until ``cancel`` is set.

    Mirrors Rust ``run_telegram_channel`` in ``telegram/service.rs``.
    Inbound long-poll + outbound replies via :class:`TelegramSender`.
    """
    bootstrap_command_extensions()
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
    # W4-FE F2: hand the live sender back to the bootstrapper so the
    # admin send route can post test messages. Best-effort — a callback
    # exception must not abort the channel start.
    if params.on_sender_ready is not None:
        try:
            params.on_sender_ready(sender)
        except Exception as exc:  # noqa: BLE001 — never block the channel
            _log.warning("telegram on_sender_ready callback failed: %s", exc)
    semaphore = asyncio.Semaphore(_channel_max_concurrency("TELEGRAM"))
    # CMP-06 — resolve the slash-access policy once. ``None`` keeps the
    # historical allow-by-default behaviour.
    slash_policy = slash_access_policy_from_env()
    pending: set[asyncio.Task[None]] = set()
    try:
        async with adapter:
            # Album/media-group merge-debounce: a Telegram album fans out
            # as N rapid single-photo updates sharing a media_group_id;
            # ``_debounce_albums`` buffers + merges them into one event so
            # the agent gets a single turn carrying every attachment.
            # Standalone messages pass straight through with no added
            # latency. The wrapper owns the cancel/timeout race, so the
            # loop is a plain ``async for``.
            iterator = _debounce_albums(adapter.inbound(), cancel)
            async for ev in iterator:
                if cancel.is_set():
                    break
                # Command-handler short-circuit. When the inbound text
                # matches a registered slash command whose spec carries
                # a handler, run it now and reply directly via the
                # Telegram sender — the agent turn is skipped entirely
                # (same shape as the QQ dispatch path). Prelude-only
                # specs (e.g. /persona) keep flowing through to the
                # agent, but with their text rewritten to the wizard
                # prelude via _telegram_apply_command_prelude — that
                # closes the seam the QQ path gets for free from
                # router.dispatch.
                if await _telegram_try_dispatch_command(
                    ev, sender, slash_policy=slash_policy
                ):
                    continue
                chat_service = params.chat_service
                if chat_service is None:
                    continue
                ev = _telegram_apply_command_prelude(ev)
                # R3: bounded fan-out — backpressure flows upstream.
                await _bounded_spawn(
                    semaphore,
                    pending,
                    functools.partial(
                        handle_one_telegram,
                        chat_service,
                        ev,
                        params.model,
                        sender,
                        cancel,
                        event_emitter=params.event_emitter,
                        params=params,
                    ),
                )
    finally:
        for t in pending:
            t.cancel()
        await send_client.aclose()


# Mutable-spinner constants live in :mod:`corlinman_channels._status` so
# Discord / Slack / Feishu can share them. The aliases above keep the
# historical local names available to the rest of this module.

#: Alias for back-compat: the test suite imports this name.
_TG_REASONING_PREVIEW_CHARS = _REASONING_PREVIEW_CHARS


def _truncate_for_telegram(body: str) -> str:
    """Deprecated wrapper kept for tests that exercise the old truncate path.

    The live final-emit path now uses :func:`chunk_reply` + multi-send
    so users never see the truncation marker for legitimate long replies.
    """
    original_len = len(body)
    if original_len <= _TELEGRAM_TEXT_LIMIT:
        return body
    _log.warning("telegram reply truncated len=%d", original_len)
    return truncate_reply(body, _TELEGRAM_TEXT_LIMIT)


def _chunk_for_telegram(body: str) -> list[str]:
    """Split a long Telegram reply into ≤ 4000-char chunks (multi-send).

    Logs the original length when splitting actually happens so the
    operator can audit how much extra traffic the bot is generating.
    """
    chunks = chunk_reply(body, _TELEGRAM_TEXT_LIMIT)
    if len(chunks) > 1:
        _log.info(
            "telegram reply split len=%d chunks=%d", len(body), len(chunks)
        )
    return chunks


async def _telegram_typing_pulse(
    sender: TelegramSender,
    chat_id: int,
    cancel: asyncio.Event,
    *,
    interval_s: float = 4.0,
) -> None:
    """Re-fire ``sendChatAction(typing)`` until cancelled.

    Telegram clears the "is typing…" indicator after about 5 seconds, so
    the loop period must be < 5s. Designed to be cancelled from outside
    (``Task.cancel()``) when the reply lands or the surrounding
    ``cancel`` event fires.
    """
    await _pulse(
        lambda: sender.send_chat_action(chat_id, "typing"),
        cancel,
        interval_s,
    )


# The send_attachment intercept name, per-tool arg preview, and the
# ToolCall / ToolResult renderers live in :mod:`corlinman_channels._status`
# so Discord / Slack / Feishu can share them. The names imported at the
# top of this module preserve the historical local names.


#: Telegram bot API caps ``callback_data`` at 64 bytes UTF-8. Each option
#: is the entire payload (we don't prefix with a call_id — the question
#: that was just asked is unambiguous in the conversation context, and
#: prefixing burns bytes for ~zero deduplication value), so we shorten
#: any option label that would overflow. The visible button text is the
#: full label; only the per-press payload is shortened.
_TG_CALLBACK_DATA_BYTES: int = 64


def _build_ask_user_keyboard(
    ask_user_args: bytes | None,
) -> list[list[dict[str, str]]] | None:
    """Build the Telegram ``reply_markup.inline_keyboard`` for an
    ``ask_user`` call that supplied canned options.

    Returns ``None`` when the agent never called ``ask_user`` this turn,
    or when the options list was empty (we fall back to a plain text
    reply — the question itself still gets sent as the message body).

    Each option becomes one button on its own row (1-column layout so a
    long label never gets clipped). ``callback_data`` is the option text
    UTF-8 encoded; the rare option that exceeds the 64-byte Telegram
    cap is truncated with a trailing ``…`` so the press still echoes a
    recognisable substring back into the conversation.
    """
    if ask_user_args is None:
        return None
    _question, options, _multiple = _parse_ask_user_args(ask_user_args)
    if not options:
        return None
    keyboard: list[list[dict[str, str]]] = []
    for label in options:
        # Encode then truncate at the byte boundary so we never slice a
        # multi-byte UTF-8 codepoint in half.
        data = label.encode("utf-8")
        if len(data) > _TG_CALLBACK_DATA_BYTES:
            # Reserve 3 bytes for the ellipsis.
            cap = _TG_CALLBACK_DATA_BYTES - 3
            data = data[:cap]
            # Drop trailing continuation bytes so the final char is whole.
            while data and (data[-1] & 0xC0) == 0x80:
                data = data[:-1]
            callback = data.decode("utf-8", errors="ignore") + "…"
        else:
            callback = label
        keyboard.append([{"text": label, "callback_data": callback}])
    return keyboard


async def _telegram_send_attachment(
    sender: TelegramSender,
    chat_id: int,
    reply_to: int | None,
    ev: Any,
) -> str:
    """Send a file via the Telegram sender, picking photo/voice/document
    by MIME. Returns the status text to render in the placeholder.

    Best-effort: any failure is folded into a status line — never raises.
    """
    import mimetypes

    path_str, caption, filename = _parse_send_attachment_args(ev)
    if not path_str:
        return "⚠️ 发送文件失败: missing `path`"
    p = _resolve_attachment_path(path_str)
    if p is None:
        return f"⚠️ 发送文件失败: {Path(path_str).name} 不存在"
    mime, _ = mimetypes.guess_type(p.name)
    mime = mime or "application/octet-stream"
    display = filename or p.name
    try:
        if mime.startswith("image/"):
            from corlinman_channels.telegram_send import PhotoSource

            await sender.send_photo(chat_id, PhotoSource.Path(p), caption=caption)
        elif mime.startswith("audio/") and p.suffix.lower() in {".ogg", ".oga"}:
            await sender.send_voice(chat_id, p, caption=caption)
        else:
            await sender.send_document(
                chat_id, p, caption=caption, filename=display, mime=mime
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning("telegram send_attachment failed: %s", exc)
        return f"⚠️ 发送文件失败: {display} ({exc})"
    _log.info("telegram send_attachment ok path=%s display=%s mime=%s", p, display, mime)
    return f"📎 已发送文件: {display}"


async def handle_one_telegram(
    chat_service: ChatServiceLike,
    inbound: InboundEvent[Any],
    model: str,
    sender: TelegramSender,
    cancel: asyncio.Event,
    *,
    event_emitter: Any | None = None,
    params: TelegramChannelParams | None = None,
) -> None:
    """Run one Telegram chat turn and post the reply via
    :class:`TelegramSender`. Parallel structure to :func:`handle_one_qq`.

    Surfaces real-time work status to the user:
      * a background pulse keeps "is typing…" visible in the chat client;
      * a placeholder message is edited in place as ``ToolCallEvent`` /
        token-delta events land — mirroring hermes-agent's mutable
        spinner line — and finally rewritten with the assistant's reply.

    Refactored to use :class:`MutableSpinner` so the per-turn state
    machine is shared with the Discord / Slack / Feishu handlers below.

    W4.1: when ``event_emitter`` is supplied, a background task subscribes
    per-turn to surface :class:`ToolStateHeartbeat` / :class:`Cancelling`
    spinner refreshes plus a one-line ``(elapsed · tool calls · cost)``
    footer appended to the final reply. ``None`` keeps the legacy
    behaviour for tests and pre-W4.1 deployments.
    """
    chat_id = int(inbound.binding.thread)
    reply_to: int | None = None
    if inbound.message_id is not None:
        try:
            reply_to = int(inbound.message_id)
        except ValueError:
            reply_to = None

    # W4-FE F2: snapshot the inbound wallclock BEFORE the spinner starts
    # so the latency sample captures end-to-end round-trip (inbound→reply)
    # rather than spinner-to-reply. Then record the accepted inbound so
    # the day/week counters + active-chats rollup tick immediately.
    inbound_ts_ms = int(time.time() * 1000)
    telegram_record_inbound(inbound, now_ms=inbound_ts_ms)

    # Kick off the typing-indicator pulse + send the initial spinner
    # placeholder so the user sees activity within ~1s. The pulse task
    # must live inside the same try/finally that cancels it, so an
    # exception from the placeholder send or stream construction can
    # never strand it firing sendChatAction forever.
    placeholder_id: int | None = None
    error_message: str | None = None
    typing_task = asyncio.create_task(
        _telegram_typing_pulse(sender, chat_id, cancel)
    )

    async def _edit(text: str) -> None:
        # The spinner already dedupes by last_status; here we only need
        # to no-op when the placeholder never landed.
        if placeholder_id is None:
            return
        await sender.edit_message_text(chat_id, placeholder_id, text)

    async def _send_attachment(ev: Any) -> str:
        return await _telegram_send_attachment(sender, chat_id, reply_to, ev)

    spinner = MutableSpinner(_edit, send_attachment_handler=_send_attachment)
    footer_state = _FooterState()
    observability_task: asyncio.Task[None] | None = None
    if event_emitter is not None:
        observability_task = asyncio.create_task(
            _consume_observability_events(
                event_emitter,
                _effective_session_key_for(inbound.binding),
                spinner,
                footer_state,
            ),
            name="telegram-observability-consumer",
        )
    request = _build_text_channel_request(inbound, model)
    if params is not None:
        await _inject_persona_if_enabled(
            request,
            humanlike_enabled=params.humanlike_enabled,
            persona_id=params.persona_id,
            persona_store=params.persona_store,
            humanlike_resolver=_binding_persona_resolver(
                inbound.binding,
                humanlike_enabled=params.humanlike_enabled,
                persona_id=params.persona_id,
                humanlike_resolver=params.humanlike_resolver,
            ),
            asset_store=params.asset_store,
            channel_name="telegram",
        )
    try:
        try:
            placeholder_id = await sender.send_message(
                chat_id, _TG_STATUS_THINKING, reply_to_message_id=reply_to
            )
        except Exception as exc:  # noqa: BLE001
            # The placeholder is decorative — if Telegram rejected it (rate
            # limit, blocked, etc.) we fall back to sending a final message
            # at the end.
            _log.warning("telegram placeholder send failed: %s", exc)

        async def _emit_status_link(line: str) -> None:
            await sender.send_message(
                chat_id, line, reply_to_message_id=reply_to
            )

        outcome = await _drive_spinner(
            spinner,
            chat_service,
            inbound,
            model,
            cancel,
            request=request,
            on_subagent_spawn=_emit_status_link,
        )
    finally:
        typing_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await typing_task
        if observability_task is not None:
            observability_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await observability_task

    if outcome.supplemented:
        # Silent acknowledgement: the agent absorbed this RPC's user
        # text into the already-running turn for the same session. Do
        # not touch the placeholder (the user sees the spinner /
        # typing indicator continue) and do not emit a final reply.
        _log.info(
            "channel.user_supplemented channel=telegram session=%s",
            inbound.binding.session_key(),
        )
        return

    error_message = outcome.error_message

    if error_message is not None:
        body = f"[corlinman error] {error_message}"
    else:
        body = _normalize_for_channel("".join(spinner.text_parts), "telegram")
        if not body:
            # Empty reply — tidy the placeholder so the user knows the
            # turn ended rather than leaving "✍️ 生成回复中..." stuck.
            # Telegram may refuse the edit (rate limit, blocked user); a
            # failure here must not crash the turn.
            if placeholder_id is not None:
                try:
                    await sender.edit_message_text(
                        chat_id, placeholder_id, "（无回复）"
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning("telegram final emit failed: %s", exc)
            return

    # Final emit is best-effort: surface transport / API failures via
    # the logger instead of propagating, so a 429 or "blocked by user"
    # at the very end never crashes the channel loop.
    #
    # [MSG_BREAK] bubble-split: split on the persona bubble-break marker
    # first, then apply Telegram's char-limit chunking within each bubble.
    # The placeholder edit applies to the first chunk of the first bubble
    # only; subsequent bubbles are always fresh sendMessage calls.
    bubbles = _split_on_msg_break(body)
    footer = _build_footer_for_outcome(
        outcome, footer_state, session_key=_effective_session_key_for(inbound.binding)
    )
    keyboard = _build_ask_user_keyboard(spinner.last_ask_user_args)

    # keyboard path — ask_user inline buttons attach to the last chunk of
    # the last bubble so the buttons sit visually after all reply text.
    if keyboard is not None:
        try:
            all_bubble_chunks = [_chunk_for_telegram(b) for b in bubbles]
            # footer on the last chunk of the last bubble
            if footer and all_bubble_chunks:
                all_bubble_chunks[-1][-1] = try_append_footer(
                    all_bubble_chunks[-1][-1], footer, _TELEGRAM_TEXT_LIMIT
                )
            for b_idx, b_chunks in enumerate(all_bubble_chunks):
                is_first_bubble = b_idx == 0
                is_last_bubble = b_idx == len(all_bubble_chunks) - 1
                if is_first_bubble and placeholder_id is not None:
                    # Edit the placeholder with bubble-0 chunk-0.
                    # When this is also the last bubble, the keyboard goes on
                    # the very last chunk — which may be the same chunk-0 or
                    # a later follow-up.
                    if is_last_bubble and len(b_chunks) == 1:
                        # Whole reply fits one chunk: edit the placeholder
                        # with the text AND the keyboard in a single call.
                        # (Previously this edited the placeholder, then
                        # ALSO sent the same chunk fresh to carry the
                        # keyboard — the duplicate "asked twice" message.)
                        # If the edit is rejected (rate-limit / network),
                        # fall back to a fresh send so the question + its
                        # buttons are never silently dropped.
                        edited = await sender.edit_message_text(
                            chat_id,
                            placeholder_id,
                            b_chunks[0],
                            inline_keyboard=keyboard,
                        )
                        if not edited:
                            await sender.send_message(
                                chat_id,
                                b_chunks[0],
                                reply_to_message_id=reply_to,
                                inline_keyboard=keyboard,
                            )
                    elif is_last_bubble:
                        # Single bubble, multiple chunks: edit placeholder
                        # with chunk-0, send the middle chunks, then send
                        # the genuinely-last chunk with the keyboard.
                        await sender.edit_message_text(
                            chat_id, placeholder_id, b_chunks[0]
                        )
                        for chunk in b_chunks[1:-1]:
                            await sender.send_message(
                                chat_id, chunk, reply_to_message_id=reply_to
                            )
                        await sender.send_message(
                            chat_id,
                            b_chunks[-1],
                            reply_to_message_id=reply_to,
                            inline_keyboard=keyboard,
                        )
                    else:
                        # More bubbles follow: edit placeholder, send remaining
                        # chunks of this bubble (no keyboard yet).
                        await sender.edit_message_text(
                            chat_id, placeholder_id, b_chunks[0]
                        )
                        for chunk in b_chunks[1:]:
                            await sender.send_message(
                                chat_id, chunk, reply_to_message_id=reply_to
                            )
                elif is_last_bubble:
                    # Middle chunks of last bubble (no buttons yet).
                    for chunk in b_chunks[:-1]:
                        await sender.send_message(
                            chat_id, chunk, reply_to_message_id=reply_to
                        )
                    # Very last chunk carries the keyboard.
                    await sender.send_message(
                        chat_id,
                        b_chunks[-1],
                        reply_to_message_id=reply_to,
                        inline_keyboard=keyboard,
                    )
                else:
                    for chunk in b_chunks:
                        await sender.send_message(
                            chat_id, chunk, reply_to_message_id=reply_to
                        )
                if not is_last_bubble:
                    await asyncio.sleep(0.3)
            telegram_record_reply_sent(inbound, inbound_ts_ms=inbound_ts_ms)
        except Exception as exc:  # noqa: BLE001
            _log.warning("telegram final emit with buttons failed: %s", exc)
        return

    try:
        all_bubble_chunks = [_chunk_for_telegram(b) for b in bubbles]
        # W4.1 footer on the last chunk of the last bubble only.
        if footer and all_bubble_chunks:
            all_bubble_chunks[-1][-1] = try_append_footer(
                all_bubble_chunks[-1][-1], footer, _TELEGRAM_TEXT_LIMIT
            )
        for b_idx, b_chunks in enumerate(all_bubble_chunks):
            is_first_bubble = b_idx == 0
            is_last_bubble = b_idx == len(all_bubble_chunks) - 1
            if is_first_bubble and placeholder_id is not None:
                await sender.edit_message_text(chat_id, placeholder_id, b_chunks[0])
                for chunk in b_chunks[1:]:
                    await sender.send_message(
                        chat_id, chunk, reply_to_message_id=reply_to
                    )
            else:
                for chunk in b_chunks:
                    await sender.send_message(
                        chat_id, chunk, reply_to_message_id=reply_to
                    )
            if not is_last_bubble:
                await asyncio.sleep(0.3)
        telegram_record_reply_sent(inbound, inbound_ts_ms=inbound_ts_ms)
    except Exception as exc:  # noqa: BLE001
        _log.warning("telegram final emit failed: %s", exc)


@dataclass(slots=True)
class _DriveSpinnerOutcome:
    """Structured result from :func:`_drive_spinner`.

    ``error_message`` is non-empty when the backend emitted an
    ``error`` event; ``supplemented`` is ``True`` when the backend
    emitted ``Done(finish_reason="supplemented")`` — meaning the agent
    servicer absorbed this RPC's user text into an already-running
    turn for the same session_key and the channel handler MUST NOT
    render any reply.

    ``tool_call_count`` / ``started_at_ms`` feed the W4.1 post-turn
    footer. ``tool_call_count`` is incremented from the legacy
    ``tool_call`` stream so the count is meaningful even when the new
    :class:`JournalBackedEmitter` is not wired (the consumer task is a
    no-op in that case). ``started_at_ms`` is the wall-clock at which
    :func:`_drive_spinner` began streaming.
    """

    error_message: str | None = None
    supplemented: bool = False
    tool_call_count: int = 0
    started_at_ms: int = 0
    #: Set True once this turn actually encountered a sub-agent spawn and a
    #: status link could be minted. This gates the end-of-turn fallback so
    #: ordinary replies never show a live status link.
    status_link_requested: bool = False
    #: Set True once the early "watch me work" status link has been sent
    #: this turn. The caller's end-of-turn footer reads this to avoid
    #: re-appending the same link.
    status_link_emitted: bool = False


@dataclass(slots=True)
class _FooterState:
    """Mutable post-turn footer payload shared between the observability
    consumer task and the channel adapter's final emit.

    Populated by :func:`_consume_observability_events` when a
    :class:`corlinman_agent.events.TurnComplete` envelope arrives — the
    channel adapter reads the populated fields right after the legacy
    stream terminates and renders a one-line footer via
    :func:`format_turn_footer` + :func:`try_append_footer`.

    Set ``populated=True`` when at least one field flows in from the new
    emitter so the channel knows whether to trust the cost / elapsed
    figures over the fallback turn-side computation.
    """

    elapsed_ms: int = 0
    estimated_cost_usd: float | None = None
    cost_status: str | None = None
    tool_call_count: int = 0
    populated: bool = False


async def _consume_observability_events(
    emitter: Any | None,
    session_key: str,
    spinner: MutableSpinner,
    footer_state: _FooterState,
) -> None:
    """Subscribe to the new EventEmitter and drive spinner + footer state.

    Spawned as a background task alongside the legacy ``_drive_spinner``
    consumer; cancelled in the surrounding ``finally``. Listens for:

    * :class:`corlinman_agent.events.ToolStateRunning` — records the
      tool name keyed by ``tool_call_id`` so a subsequent heartbeat can
      address the spinner by name.
    * :class:`corlinman_agent.events.ToolStateHeartbeat` — refreshes the
      spinner via :meth:`MutableSpinner.on_tool_heartbeat`.
    * :class:`corlinman_agent.events.ToolStateCompleted` — drops the
      tool_call_id from the in-flight map.
    * :class:`corlinman_agent.events.Cancelling` — flips the spinner to
      :data:`STATUS_CANCELLING` via :meth:`MutableSpinner.on_cancelling`.
    * :class:`corlinman_agent.events.TurnComplete` — stashes
      elapsed / cost / cost_status / tool_call_count into
      ``footer_state`` for the channel's post-turn footer render.

    All other events ignore — the legacy gRPC stream is the source of
    truth for tokens / final text. When ``emitter`` is ``None`` (the
    common case in unit tests + during migration) the task returns
    immediately so the surrounding ``handle_one_*`` stays a no-op.

    Spec: §1.4 W4.1 of ``docs/PLAN_TASK_OBSERVABILITY.md``.
    """
    if emitter is None:
        return
    # Lazy-import — corlinman-agent is a hard dep but the event types
    # only matter when the new emitter is actually wired. Keeping the
    # import inside the function lets test environments stub the
    # emitter without forcing the import resolver to find every
    # observability symbol.
    from corlinman_agent.events import (
        Cancelling,
        ToolStateCompleted,
        ToolStateHeartbeat,
        ToolStateRunning,
        TurnComplete,
    )

    pending_tools: dict[str, str] = {}
    queue, unsubscribe = await emitter.subscribe(session_key)
    try:
        while True:
            envelope = await queue.get()
            event = envelope.event
            if isinstance(event, ToolStateRunning):
                pending_tools[event.tool_call_id] = event.tool_name
            elif isinstance(event, ToolStateHeartbeat):
                name = pending_tools.get(event.tool_call_id)
                if name:
                    await spinner.on_tool_heartbeat(name, event.elapsed_ms)
            elif isinstance(event, ToolStateCompleted):
                pending_tools.pop(event.tool_call_id, None)
                footer_state.tool_call_count += 1
            elif isinstance(event, Cancelling):
                await spinner.on_cancelling()
            elif isinstance(event, TurnComplete):
                footer_state.elapsed_ms = event.elapsed_ms
                footer_state.estimated_cost_usd = event.estimated_cost_usd
                footer_state.cost_status = event.cost_status
                # Prefer the emitter-side count when populated; the
                # legacy stream's tool_call count covers the no-emitter
                # path. If the emitter never emitted ToolStateCompleted
                # (e.g. early in the migration) we keep whatever the
                # consumer accumulated above (often 0) and let the
                # channel adapter's drive-spinner outcome win at footer
                # render time.
                footer_state.populated = True
    finally:
        await unsubscribe()


async def _drive_spinner(
    spinner: MutableSpinner,
    chat_service: ChatServiceLike,
    inbound: InboundEvent[Any],
    model: str,
    cancel: asyncio.Event,
    *,
    request: Any | None = None,
    on_subagent_spawn: Callable[[str], Awaitable[None]] | None = None,
) -> _DriveSpinnerOutcome:
    """Stream ``chat_service.run`` events into ``spinner``.

    Returns a :class:`_DriveSpinnerOutcome` describing how the stream
    terminated. The caller is responsible for assembling the final
    reply from ``spinner.text_parts`` when ``error_message`` is
    ``None`` and ``supplemented`` is ``False``.

    Shared by the four mutable-spinner channels (Telegram / Discord /
    Slack / Feishu) so the event-loop logic stays in one place.

    W4.1: ``tool_call_count`` is incremented per ``tool_call`` event
    (excluding ``send_attachment`` — that intercept is a transport
    side-effect, not an agent tool invocation users care about counting)
    and ``started_at_ms`` records the streaming start so the post-turn
    footer can render ``(elapsed: 12s · 3 tool calls · ~$0.012)`` even
    when the new :class:`JournalBackedEmitter` is not wired.

    W7: ``request`` may be pre-built (and pre-augmented with a persona
    system_prompt by :func:`_inject_persona_if_enabled`) by the caller;
    when omitted the legacy unaugmented request is built here so the
    pre-W7 call sites still work unchanged.

    ``on_subagent_spawn`` (optional) is an async callback invoked with the
    formatted status-link line the first time a sub-agent spawn tool
    (``subagent_spawn`` / ``_many`` / ``_inline``) is seen this turn. The
    channel uses it to push the link as a standalone message so the user
    can open the live status view immediately; ``outcome.status_link_emitted``
    is then set so the end-of-turn footer does not append the link twice.
    Turns without sub-agent fan-out never request or append a status link.
    """
    import time as _time

    started_at_ms = int(_time.time() * 1000)
    outcome = _DriveSpinnerOutcome(started_at_ms=started_at_ms)
    if request is None:
        request = _build_text_channel_request(inbound, model)
    stream = chat_service.run(request, cancel)
    async for ev in stream:
        kind = _event_kind(ev)
        if kind == "token_delta":
            await spinner.on_token_delta(
                getattr(ev, "text", "") or "",
                bool(getattr(ev, "is_reasoning", False)),
            )
        elif kind == "tool_call":
            tool_name = getattr(ev, "tool", "") or ""
            # send_attachment is a channel-side intercept (file upload),
            # not an agent reasoning step. Counting it in the footer
            # would inflate the tool-call count for any turn that
            # produced an attachment.
            if tool_name and tool_name != _SEND_ATTACHMENT_TOOL:
                outcome.tool_call_count += 1
            # The first time a sub-agent spawn tool fires, surface the live
            # status link EARLY (a brand-new standalone message via the
            # channel-supplied callback — NOT an edit of the spinner
            # placeholder, which the next on_tool_call render would clobber)
            # so the user can watch the fan-out instead of waiting for the
            # turn to finish. Best-effort: a send failure must never break
            # the stream, and we only mark it emitted on success so the
            # end-of-turn footer still appends the link as a fallback.
            if (
                on_subagent_spawn is not None
                and not outcome.status_link_requested
                and not outcome.status_link_emitted
                and tool_name in _SUBAGENT_SPAWN_TOOLS
            ):
                line = _status_link_line(
                    _effective_session_key_for(inbound.binding)
                )
                if line:
                    outcome.status_link_requested = True
                    try:
                        await on_subagent_spawn(line)
                        outcome.status_link_emitted = True
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(
                            "early status link emit failed: %s", exc
                        )
            await spinner.on_tool_call(ev)
        elif kind == "tool_result":
            await spinner.on_tool_result(ev)
        elif kind == "done":
            await spinner.flush_reasoning()
            if _is_supplemented_done(ev):
                outcome.supplemented = True
                return outcome
            return outcome
        elif kind == "error":
            await spinner.flush_reasoning()
            outcome.error_message = (
                getattr(ev, "error", "") or getattr(ev, "message", "")
            )
            return outcome
    await spinner.flush_reasoning()
    return outcome


# ---------------------------------------------------------------------------
# Command-extension bootstrap (CMP-07): commands-dir *.md + skill→command
# ---------------------------------------------------------------------------
# Loads operator-authored ``<data_dir>/commands/*.md`` into the slash-command
# registry and bridges a default skill command, once per process. Idempotent:
# a second call is a no-op so each ``run_*_channel`` can call it freely.

_COMMAND_EXTENSIONS_LOADED: bool = False


def _commands_data_dir() -> Path:
    """Resolve ``<data_dir>/commands`` (CMP-07 loader root).

    Mirrors the env-var precedence used elsewhere in this package:
    ``$CORLINMAN_DATA_DIR`` → ``~/.corlinman``.
    """
    raw = os.environ.get("CORLINMAN_DATA_DIR")
    base = Path(raw) if raw else Path.home() / ".corlinman"
    return base / "commands"


def bootstrap_command_extensions() -> None:
    """Register operator commands-dir ``*.md`` + a default skill command.

    CMP-07: ``register_commands_from_dir`` / ``register_skill_command`` are
    implemented but had no production caller. Each ``run_*_channel`` calls
    this once at start so a ``<data_dir>/commands/foo.md`` (with a
    ``$ARGUMENTS`` placeholder) becomes an invokable ``/foo`` on every
    channel + the web playground.

    Path-traversal safe: :func:`load_commands_dir` only globs ``*.md``
    directly under the dir and reads them as UTF-8 text — it never follows
    a name into another directory and never executes the file. A missing
    dir yields an empty list. Idempotent across calls (and re-seed on
    reload is a no-op because ``register_command`` skips name/alias
    collisions).
    """
    global _COMMAND_EXTENSIONS_LOADED
    if _COMMAND_EXTENSIONS_LOADED:
        return
    _COMMAND_EXTENSIONS_LOADED = True
    from corlinman_channels.commands import (  # noqa: PLC0415
        register_commands_from_dir,
        register_skill_command,
    )

    try:
        loaded = register_commands_from_dir(_commands_data_dir())
        if loaded:
            _log.info(
                "channels.commands_dir loaded %d command(s): %s",
                len(loaded),
                ", ".join(s.name for s in loaded),
            )
    except Exception as exc:  # noqa: BLE001 — never block channel start
        _log.warning("channels.commands_dir load failed: %s", exc)

    # Bridge the bundled ``configure-persona`` skill to a /configure-persona
    # slash command so the skill's invocation surface is reachable from
    # chat. Idempotent: returns None when the name/alias already exists.
    try:
        register_skill_command(
            name="configure-persona",
            summary="通过 configure-persona 技能配置 persona",
            aliases=("/配置技能",),
            category="Skills",
            args_hint="[name]",
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("channels.skill_command bridge failed: %s", exc)


# ---------------------------------------------------------------------------
# Agent status-card links (shareable read-only "what is the agent doing now")
# ---------------------------------------------------------------------------
# The gateway can surface a per-turn link ({public_url}/status/{token}) in every
# channel reply so a chat user can tap through to a live trajectory view. The
# token is a signed capability minted by corlinman_server.gateway.status_token;
# to keep this package from importing corlinman_server (import-linter layering),
# the channels-runtime bootstrap injects a minting CLOSURE here instead.
_STATUS_LINK_PUBLIC_URL: str = ""
_STATUS_LINK_ENABLED: bool = False
_STATUS_LINK_MINTER: Callable[[str], str] | None = None


def configure_status_links(
    *,
    public_url: str = "",
    enabled: bool = False,
    minter: Callable[[str], str] | None = None,
) -> None:
    """Wire (or disable) the shareable agent-status link feature.

    Called once by the gateway's channels-runtime bootstrap. ``minter`` is a
    ``session_key -> signed-token`` closure (captures the signing key + TTL on
    the server side) so this package never imports ``corlinman_server``. All
    three of ``public_url``, ``enabled``, and ``minter`` must be truthy for a
    link to appear; otherwise :func:`_status_link_line` returns ``""`` and
    every channel reply is unchanged. Idempotent — safe to call on reload.
    """
    global _STATUS_LINK_PUBLIC_URL, _STATUS_LINK_ENABLED, _STATUS_LINK_MINTER
    _STATUS_LINK_PUBLIC_URL = public_url or ""
    _STATUS_LINK_ENABLED = bool(enabled)
    _STATUS_LINK_MINTER = minter


def _effective_session_key_for(binding: Any) -> str:
    """Epoch-adjusted session key for the binding's CURRENT conversation.

    After ``/new`` the request builders derive ``<base>:eN``
    (see ``_build_internal_request`` / ``_build_text_channel_request``),
    so every status link, status-page subscription, and emitter
    subscribe for the live turn must use the same derived key —
    otherwise they point at the dead epoch-0 session. Fail-open: epoch 0
    or a missing prefs store returns the plain ``binding.session_key()``.
    """
    from corlinman_channels import binding_prefs as _binding_prefs  # noqa: PLC0415

    return _binding_prefs.effective_session_key(binding, binding.session_key())


def _status_link_line(session_key: str) -> str:
    """Mint + format the status-card link line for ``session_key``.

    Returns ``""`` when the feature is off, unconfigured, or minting fails —
    a status link must NEVER break or delay a user-facing reply. Callers pipe
    the result through :func:`try_append_footer` (empty-safe).
    """
    if (
        not _STATUS_LINK_ENABLED
        or not _STATUS_LINK_PUBLIC_URL
        or not session_key
        or _STATUS_LINK_MINTER is None
    ):
        return ""
    try:
        token = _STATUS_LINK_MINTER(session_key)
    except Exception:  # noqa: BLE001 — never fail a reply over a status link
        return ""
    if not token:
        return ""
    return format_status_footer_line(_STATUS_LINK_PUBLIC_URL, token)


def _build_footer_for_outcome(
    outcome: _DriveSpinnerOutcome,
    footer_state: _FooterState,
    *,
    session_key: str = "",
) -> str:
    """Compose the per-turn reply footer: W4.1 observability + status link.

    The W4.1 cost/elapsed line only renders when the new emitter wired the
    per-turn flow (``footer_state.populated`` is True). This gates the
    cost+elapsed line behind the observability emitter so:

    * Deployments without the emitter (older builds, unit-test harnesses
      that mock the chat service directly) keep their pre-W4.1 reply
      shape — the test suite's ``assert sender.edits[-1][2] == "ok"``
      shape stays meaningful.
    * Deployments WITH the emitter always show the footer because the
      gateway emits :class:`corlinman_agent.events.TurnComplete` for
      every turn (per W1.1 spec). The numbers there are
      authoritative — :class:`_DriveSpinnerOutcome` only provides the
      legacy ``tool_call_count`` fallback for the same turn.

    The shareable status-card link is appended only as a fallback for turns
    that actually spawned sub-agents but failed to send the standalone early
    link. Normal, non-fan-out replies never get a status link.

    Returns the empty string when nothing applies; callers (every channel)
    pass the return through :func:`try_append_footer` which is empty-safe.
    """
    w41 = ""
    if footer_state.populated:
        elapsed_ms = footer_state.elapsed_ms
        cost = footer_state.estimated_cost_usd
        cost_status = footer_state.cost_status
        # Prefer the emitter-side count when populated; the legacy stream's
        # tool_call count covers the case where ToolStateCompleted hasn't
        # fired yet (e.g. the agent finished too fast for the heartbeat
        # task to spin up — counted by the consumer regardless).
        tool_calls = (
            footer_state.tool_call_count
            if footer_state.tool_call_count > 0
            else outcome.tool_call_count
        )
        w41 = format_turn_footer(elapsed_ms, tool_calls, cost, cost_status)
    # Append a status link only as a fallback for sub-agent fan-out turns
    # where the standalone early send did not succeed.
    status_line = (
        _status_link_line(session_key)
        if outcome.status_link_requested and not outcome.status_link_emitted
        else ""
    )
    if w41 and status_line:
        return f"{w41}\n{status_line}"
    return w41 or status_line


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

    # ---- human-like persona toggle (W7 Persona Studio) -----------------
    humanlike_enabled: bool = False
    persona_id: str | None = None
    persona_store: Any = None
    humanlike_resolver: Any = None
    asset_store: Any = None

    event_emitter: Any = None
    """W4.1 — see :class:`TelegramChannelParams.event_emitter`."""

    on_sender_ready: Any = None
    """Admin send hook — see :class:`TelegramChannelParams.on_sender_ready`.
    Called with the live :class:`DiscordSender` at channel start so the
    ``POST /admin/channels/discord/send`` route can reach it."""


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
    # Admin send hook: hand the live sender to the bootstrapper so the
    # admin send route can reach it. Best-effort — see Telegram.
    if params.on_sender_ready is not None:
        try:
            params.on_sender_ready(sender)
        except Exception as exc:  # noqa: BLE001 — never block the channel
            _log.warning("discord on_sender_ready callback failed: %s", exc)
    semaphore = asyncio.Semaphore(_channel_max_concurrency("DISCORD"))
    # CMP-06 — resolve the slash-access policy once. ``None`` keeps the
    # historical allow-by-default behaviour.
    slash_policy = slash_access_policy_from_env()
    pending: set[asyncio.Task[None]] = set()
    try:
        async with adapter:
            iterator = adapter.inbound()
            while not cancel.is_set():
                ev = await _race_iter_or_cancel(iterator, cancel)
                if ev is None:
                    break
                # Admin health: record every accepted inbound so
                # /admin/channels/discord/{status,messages} see real data.
                _channel_record_inbound(DISCORD_HEALTH, DISCORD_RECENT_MESSAGES, ev)
                # Command-handler short-circuit + prelude rewrite —
                # same flow as the Telegram loop (see run_telegram_channel).
                if await _try_dispatch_text_command(
                    ev,
                    functools.partial(_discord_send_command_reply, sender, ev),
                    slash_policy=slash_policy,
                    channel_label="discord",
                ):
                    continue
                chat_service = params.chat_service
                if chat_service is None:
                    continue
                ev = _apply_text_command_prelude(ev)
                # R3: bounded fan-out — backpressure flows upstream.
                await _bounded_spawn(
                    semaphore,
                    pending,
                    functools.partial(
                        handle_one_discord,
                        chat_service,
                        ev,
                        params.model,
                        sender,
                        cancel,
                        event_emitter=params.event_emitter,
                        params=params,
                    ),
                )
    finally:
        for t in pending:
            t.cancel()
        await send_client.aclose()


#: Discord's hard cap on ``content`` is 2000 chars. We leave a small
#: safety margin so a near-cap reply doesn't trip the API on edge cases.
_DISCORD_TEXT_LIMIT = 1990


async def _discord_typing_pulse(
    sender: DiscordSender,
    channel_id: str,
    cancel: asyncio.Event,
    *,
    interval_s: float = 5.0,
) -> None:
    """Re-fire ``POST /channels/{id}/typing`` until cancelled.

    Discord's typing indicator auto-clears after ~10s — we re-fire at 5s
    intervals to match the Telegram cadence and keep the indicator
    rock-steady through long tool chains.
    """
    await _pulse(
        lambda: sender.trigger_typing(channel_id),
        cancel,
        interval_s,
    )


async def _discord_send_attachment(
    sender: DiscordSender,
    channel_id: str,
    reply_to: str | None,
    ev: Any,
) -> str:
    """Upload a file via Discord's multipart ``files[0]`` form.

    Returns the status text to render in the placeholder. Best-effort:
    any failure folds into a status line — never raises.
    """
    path_str, caption, filename = _parse_send_attachment_args(ev)
    if not path_str:
        return "⚠️ 发送文件失败: missing `path`"
    p = _resolve_attachment_path(path_str)
    if p is None:
        return f"⚠️ 发送文件失败: {Path(path_str).name} 不存在"
    display = filename or p.name
    try:
        await sender.send_file(
            channel_id,
            p,
            filename=display,
            content=caption,
            reply_to_message_id=reply_to,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("discord send_attachment failed: %s", exc)
        return f"⚠️ 发送文件失败: {display} ({exc})"
    _log.info("discord send_attachment ok path=%s display=%s", p, display)
    return f"📎 已发送文件: {display}"


async def handle_one_discord(
    chat_service: ChatServiceLike,
    inbound: InboundEvent[Any],
    model: str,
    sender: DiscordSender,
    cancel: asyncio.Event,
    *,
    event_emitter: Any | None = None,
    params: DiscordChannelParams | None = None,
) -> None:
    """Run one Discord chat turn and post the reply via
    :class:`DiscordSender`. Parallel structure to :func:`handle_one_telegram`.

    Mirrors the Telegram UX 1:1: typing pulse + placeholder + mutable-
    spinner edits + final ``edit_message`` that overwrites the
    placeholder with the assistant's reply. ``event_emitter`` opt-in
    same as :func:`handle_one_telegram` — see W4.1 doc there.
    """
    channel_id = inbound.binding.thread
    reply_to = inbound.message_id

    placeholder_id: str | None = None
    error_message: str | None = None
    typing_task = asyncio.create_task(
        _discord_typing_pulse(sender, channel_id, cancel)
    )

    async def _edit(text: str) -> None:
        if placeholder_id is None:
            return
        await sender.edit_message(channel_id, placeholder_id, text)

    async def _send_attachment(ev: Any) -> str:
        return await _discord_send_attachment(sender, channel_id, reply_to, ev)

    spinner = MutableSpinner(_edit, send_attachment_handler=_send_attachment)
    footer_state = _FooterState()
    observability_task: asyncio.Task[None] | None = None
    if event_emitter is not None:
        observability_task = asyncio.create_task(
            _consume_observability_events(
                event_emitter,
                _effective_session_key_for(inbound.binding),
                spinner,
                footer_state,
            ),
            name="discord-observability-consumer",
        )
    request = _build_text_channel_request(inbound, model)
    if params is not None:
        await _inject_persona_if_enabled(
            request,
            humanlike_enabled=params.humanlike_enabled,
            persona_id=params.persona_id,
            persona_store=params.persona_store,
            humanlike_resolver=_binding_persona_resolver(
                inbound.binding,
                humanlike_enabled=params.humanlike_enabled,
                persona_id=params.persona_id,
                humanlike_resolver=params.humanlike_resolver,
            ),
            asset_store=params.asset_store,
            channel_name="discord",
        )
    try:
        try:
            placeholder_id = await sender.send_message(
                channel_id, _TG_STATUS_THINKING, reply_to_message_id=reply_to
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("discord placeholder send failed: %s", exc)

        async def _emit_status_link(line: str) -> None:
            await sender.send_message(
                channel_id, line, reply_to_message_id=reply_to
            )

        outcome = await _drive_spinner(
            spinner,
            chat_service,
            inbound,
            model,
            cancel,
            request=request,
            on_subagent_spawn=_emit_status_link,
        )
    finally:
        typing_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await typing_task
        if observability_task is not None:
            observability_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await observability_task

    if outcome.supplemented:
        _log.info(
            "channel.user_supplemented channel=discord session=%s",
            inbound.binding.session_key(),
        )
        return
    error_message = outcome.error_message

    if error_message is not None:
        body = f"[corlinman error] {error_message}"
    else:
        body = _normalize_for_channel("".join(spinner.text_parts), "discord")
        if not body:
            if placeholder_id is not None:
                try:
                    await sender.edit_message(
                        channel_id, placeholder_id, "（无回复）"
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning("discord final emit failed: %s", exc)
            return

    # [MSG_BREAK] bubble-split: split on the persona bubble-break marker,
    # then apply Discord's char-limit chunking within each bubble.
    bubbles = _split_on_msg_break(body)
    footer = _build_footer_for_outcome(
        outcome, footer_state, session_key=_effective_session_key_for(inbound.binding)
    )
    try:
        all_bubble_chunks = [chunk_reply(b, _DISCORD_TEXT_LIMIT) for b in bubbles]
        # footer on the last chunk of the last bubble only.
        if footer and all_bubble_chunks:
            all_bubble_chunks[-1][-1] = try_append_footer(
                all_bubble_chunks[-1][-1], footer, _DISCORD_TEXT_LIMIT
            )
        for b_idx, b_chunks in enumerate(all_bubble_chunks):
            is_first_bubble = b_idx == 0
            is_last_bubble = b_idx == len(all_bubble_chunks) - 1
            if len(b_chunks) > 1:
                _log.info(
                    "discord reply split len=%d chunks=%d", len(bubbles[b_idx]), len(b_chunks)
                )
            if is_first_bubble and placeholder_id is not None:
                await sender.edit_message(channel_id, placeholder_id, b_chunks[0])
                for chunk in b_chunks[1:]:
                    await sender.send_message(
                        channel_id, chunk, reply_to_message_id=reply_to
                    )
            else:
                for chunk in b_chunks:
                    await sender.send_message(
                        channel_id, chunk, reply_to_message_id=reply_to
                    )
            if not is_last_bubble:
                await asyncio.sleep(0.3)
        # Admin health: count a successful outbound + mark the inbound
        # entry responded.
        _channel_record_sent(DISCORD_HEALTH)
        _channel_mark_responded(DISCORD_RECENT_MESSAGES, channel_id)
    except Exception as exc:  # noqa: BLE001
        _channel_record_error(DISCORD_HEALTH)
        _log.warning("discord final emit failed: %s", exc)


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

    # ---- human-like persona toggle (W7 Persona Studio) -----------------
    humanlike_enabled: bool = False
    persona_id: str | None = None
    persona_store: Any = None
    humanlike_resolver: Any = None
    asset_store: Any = None

    event_emitter: Any = None
    """W4.1 — see :class:`TelegramChannelParams.event_emitter`."""

    on_sender_ready: Any = None
    """Admin send hook — see :class:`TelegramChannelParams.on_sender_ready`.
    Called with the live :class:`SlackSender` at channel start so the
    ``POST /admin/channels/slack/send`` route can reach it."""


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
    # Admin send hook: hand the live sender to the bootstrapper so the
    # admin send route can reach it. Best-effort — see Telegram.
    if params.on_sender_ready is not None:
        try:
            params.on_sender_ready(sender)
        except Exception as exc:  # noqa: BLE001 — never block the channel
            _log.warning("slack on_sender_ready callback failed: %s", exc)
    semaphore = asyncio.Semaphore(_channel_max_concurrency("SLACK"))
    # CMP-06 — resolve the slash-access policy once. ``None`` keeps the
    # historical allow-by-default behaviour.
    slash_policy = slash_access_policy_from_env()
    pending: set[asyncio.Task[None]] = set()
    try:
        async with adapter:
            iterator = adapter.inbound()
            while not cancel.is_set():
                ev = await _race_iter_or_cancel(iterator, cancel)
                if ev is None:
                    break
                # Admin health: record every accepted inbound so
                # /admin/channels/slack/{status,messages} see real data.
                _channel_record_inbound(SLACK_HEALTH, SLACK_RECENT_MESSAGES, ev)
                # Command-handler short-circuit + prelude rewrite —
                # same flow as the Telegram loop (see run_telegram_channel).
                if await _try_dispatch_text_command(
                    ev,
                    functools.partial(_slack_send_command_reply, sender, ev),
                    slash_policy=slash_policy,
                    channel_label="slack",
                ):
                    continue
                chat_service = params.chat_service
                if chat_service is None:
                    continue
                ev = _apply_text_command_prelude(ev)
                # R3: bounded fan-out — backpressure flows upstream.
                await _bounded_spawn(
                    semaphore,
                    pending,
                    functools.partial(
                        handle_one_slack,
                        chat_service,
                        ev,
                        params.model,
                        sender,
                        cancel,
                        event_emitter=params.event_emitter,
                        params=params,
                    ),
                )
    finally:
        for t in pending:
            t.cancel()
        await send_client.aclose()


#: Slack's hard cap on ``chat.postMessage`` ``text`` is 40_000 chars but
#: 4000 keeps the message visually compact and matches the Telegram cap;
#: the reasoning agent rarely produces longer answers anyway.
_SLACK_TEXT_LIMIT = 4000


async def _slack_send_attachment(
    sender: SlackSender,
    channel: str,
    thread_ts: str | None,
    ev: Any,
) -> str:
    """Upload a file via Slack's ``files.upload`` and post it into the
    channel / thread. Returns the status text to render in the
    placeholder. Best-effort: any failure folds into a status line."""
    path_str, caption, filename = _parse_send_attachment_args(ev)
    if not path_str:
        return "⚠️ 发送文件失败: missing `path`"
    p = _resolve_attachment_path(path_str)
    if p is None:
        return f"⚠️ 发送文件失败: {Path(path_str).name} 不存在"
    display = filename or p.name
    try:
        await sender.upload_file(
            channel,
            p,
            filename=display,
            initial_comment=caption,
            thread_ts=thread_ts,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("slack send_attachment failed: %s", exc)
        return f"⚠️ 发送文件失败: {display} ({exc})"
    _log.info("slack send_attachment ok path=%s display=%s", p, display)
    return f"📎 已发送文件: {display}"


async def handle_one_slack(
    chat_service: ChatServiceLike,
    inbound: InboundEvent[Any],
    model: str,
    sender: SlackSender,
    cancel: asyncio.Event,
    *,
    event_emitter: Any | None = None,
    params: SlackChannelParams | None = None,
) -> None:
    """Run one Slack chat turn and post the reply via :class:`SlackSender`.

    The reply is threaded under the inbound message ``ts`` so the
    conversation stays grouped — parallel to the Telegram ``reply_to``.

    Mirrors the Telegram UX as closely as Slack permits: there's no real
    typing indicator (``post_typing`` is a stub), but the placeholder /
    mutable-spinner edits / final ``chat.update`` flow is identical.
    ``event_emitter`` opt-in same as :func:`handle_one_telegram` —
    see W4.1 doc there.
    """
    channel = inbound.binding.thread
    thread_ts = inbound.message_id
    placeholder_ts: str | None = None
    error_message: str | None = None

    async def _edit(text: str) -> None:
        if placeholder_ts is None:
            return
        await sender.update_message(channel, placeholder_ts, text)

    async def _send_attachment(ev: Any) -> str:
        return await _slack_send_attachment(sender, channel, thread_ts, ev)

    spinner = MutableSpinner(_edit, send_attachment_handler=_send_attachment)
    footer_state = _FooterState()
    observability_task: asyncio.Task[None] | None = None
    if event_emitter is not None:
        observability_task = asyncio.create_task(
            _consume_observability_events(
                event_emitter,
                _effective_session_key_for(inbound.binding),
                spinner,
                footer_state,
            ),
            name="slack-observability-consumer",
        )
    request = _build_text_channel_request(inbound, model)
    if params is not None:
        await _inject_persona_if_enabled(
            request,
            humanlike_enabled=params.humanlike_enabled,
            persona_id=params.persona_id,
            persona_store=params.persona_store,
            humanlike_resolver=_binding_persona_resolver(
                inbound.binding,
                humanlike_enabled=params.humanlike_enabled,
                persona_id=params.persona_id,
                humanlike_resolver=params.humanlike_resolver,
            ),
            asset_store=params.asset_store,
            channel_name="slack",
        )
    try:
        try:
            placeholder_ts = await sender.send_message(
                channel, _TG_STATUS_THINKING, thread_ts=thread_ts
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("slack placeholder send failed: %s", exc)

        async def _emit_status_link(line: str) -> None:
            await sender.send_message(channel, line, thread_ts=thread_ts)

        outcome = await _drive_spinner(
            spinner,
            chat_service,
            inbound,
            model,
            cancel,
            request=request,
            on_subagent_spawn=_emit_status_link,
        )
    finally:
        if observability_task is not None:
            observability_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await observability_task

    if outcome.supplemented:
        _log.info(
            "channel.user_supplemented channel=slack session=%s",
            inbound.binding.session_key(),
        )
        return
    error_message = outcome.error_message

    if error_message is not None:
        body = f"[corlinman error] {error_message}"
    else:
        body = _normalize_for_channel("".join(spinner.text_parts), "slack")
        if not body:
            if placeholder_ts is not None:
                try:
                    await sender.update_message(channel, placeholder_ts, "（无回复）")
                except Exception as exc:  # noqa: BLE001
                    _log.warning("slack final emit failed: %s", exc)
            return

    # [MSG_BREAK] bubble-split: split on the persona bubble-break marker,
    # then apply Slack's char-limit chunking within each bubble.
    bubbles = _split_on_msg_break(body)
    footer = _build_footer_for_outcome(
        outcome, footer_state, session_key=_effective_session_key_for(inbound.binding)
    )
    try:
        all_bubble_chunks = [chunk_reply(b, _SLACK_TEXT_LIMIT) for b in bubbles]
        # footer on the last chunk of the last bubble only.
        if footer and all_bubble_chunks:
            all_bubble_chunks[-1][-1] = try_append_footer(
                all_bubble_chunks[-1][-1], footer, _SLACK_TEXT_LIMIT
            )
        for b_idx, b_chunks in enumerate(all_bubble_chunks):
            is_first_bubble = b_idx == 0
            is_last_bubble = b_idx == len(all_bubble_chunks) - 1
            if len(b_chunks) > 1:
                _log.info(
                    "slack reply split len=%d chunks=%d", len(bubbles[b_idx]), len(b_chunks)
                )
            if is_first_bubble and placeholder_ts is not None:
                await sender.update_message(channel, placeholder_ts, b_chunks[0])
                for chunk in b_chunks[1:]:
                    await sender.send_message(channel, chunk, thread_ts=thread_ts)
            else:
                for chunk in b_chunks:
                    await sender.send_message(channel, chunk, thread_ts=thread_ts)
            if not is_last_bubble:
                await asyncio.sleep(0.3)
        # Admin health: count a successful outbound + mark the inbound
        # entry responded.
        _channel_record_sent(SLACK_HEALTH)
        _channel_mark_responded(SLACK_RECENT_MESSAGES, channel)
    except Exception as exc:  # noqa: BLE001
        _channel_record_error(SLACK_HEALTH)
        _log.warning("slack final emit failed: %s", exc)


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

    # ---- human-like persona toggle (W7 Persona Studio) -----------------
    humanlike_enabled: bool = False
    persona_id: str | None = None
    persona_store: Any = None
    humanlike_resolver: Any = None
    asset_store: Any = None

    event_emitter: Any = None
    """W4.1 — see :class:`TelegramChannelParams.event_emitter`."""

    on_sender_ready: Any = None
    """Admin send hook — see :class:`TelegramChannelParams.on_sender_ready`.
    Called with the live :class:`FeishuSender` at channel start so the
    ``POST /admin/channels/feishu/send`` route can reach it."""


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
    bootstrap_command_extensions()
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
    # Admin send hook: hand the live sender to the bootstrapper so the
    # admin send route can reach it. Best-effort — see Telegram.
    if params.on_sender_ready is not None:
        try:
            params.on_sender_ready(sender)
        except Exception as exc:  # noqa: BLE001 — never block the channel
            _log.warning("feishu on_sender_ready callback failed: %s", exc)
    semaphore = asyncio.Semaphore(_channel_max_concurrency("FEISHU"))
    # CMP-06 — resolve the slash-access policy once. ``None`` keeps the
    # historical allow-by-default behaviour.
    slash_policy = slash_access_policy_from_env()
    pending: set[asyncio.Task[None]] = set()
    try:
        async with adapter:
            iterator = adapter.inbound()
            while not cancel.is_set():
                ev = await _race_iter_or_cancel(iterator, cancel)
                if ev is None:
                    break
                # Admin health: record every accepted inbound so
                # /admin/channels/feishu/{status,messages} see real data.
                _channel_record_inbound(FEISHU_HEALTH, FEISHU_RECENT_MESSAGES, ev)
                # Command-handler short-circuit + prelude rewrite —
                # same flow as the Telegram loop (see run_telegram_channel).
                if await _try_dispatch_text_command(
                    ev,
                    functools.partial(_feishu_send_command_reply, sender, ev),
                    slash_policy=slash_policy,
                    channel_label="feishu",
                ):
                    continue
                chat_service = params.chat_service
                if chat_service is None:
                    continue
                ev = _apply_text_command_prelude(ev)
                # R3: bounded fan-out — backpressure flows upstream.
                await _bounded_spawn(
                    semaphore,
                    pending,
                    functools.partial(
                        handle_one_feishu,
                        chat_service,
                        ev,
                        params.model,
                        sender,
                        cancel,
                        event_emitter=params.event_emitter,
                        params=params,
                    ),
                )
    finally:
        for t in pending:
            t.cancel()
        await send_client.aclose()


#: Feishu's hard cap on ``msg_type=text`` content is ~30k chars; 4000
#: matches the Telegram cap so the UX feels identical across channels.
_FEISHU_TEXT_LIMIT = 4000


async def _feishu_send_attachment(
    sender: FeishuSender,
    chat_id: str,
    reply_to: str | None,
    ev: Any,
) -> str:
    """Upload a file via Feishu's two-step ``/im/v1/files`` + send-as-file
    flow. Returns the status text to render in the placeholder.

    Best-effort: any failure folds into a status line — never raises.
    Feishu requires a separate ``send`` call after the upload (the
    upload returns a ``file_key``; ``msg_type=file`` references it),
    which is exactly what :meth:`FeishuSender.send_file_message` handles.
    """
    path_str, _caption, filename = _parse_send_attachment_args(ev)
    if not path_str:
        return "⚠️ 发送文件失败: missing `path`"
    p = _resolve_attachment_path(path_str)
    if p is None:
        return f"⚠️ 发送文件失败: {Path(path_str).name} 不存在"
    display = filename or p.name
    try:
        file_key = await sender.upload_file(p, filename=display)
        await sender.send_file_message(
            chat_id, file_key, reply_to_message_id=reply_to
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("feishu send_attachment failed: %s", exc)
        return f"⚠️ 发送文件失败: {display} ({exc})"
    _log.info("feishu send_attachment ok path=%s display=%s", p, display)
    return f"📎 已发送文件: {display}"


async def handle_one_feishu(
    chat_service: ChatServiceLike,
    inbound: InboundEvent[Any],
    model: str,
    sender: FeishuSender,
    cancel: asyncio.Event,
    *,
    event_emitter: Any | None = None,
    params: FeishuChannelParams | None = None,
) -> None:
    """Run one Feishu chat turn and post the reply via :class:`FeishuSender`.

    The reply is posted via the ``/messages/{id}/reply`` endpoint so the
    addressing stays clear — parallel to the Telegram ``reply_to``.

    Mirrors the Slack flow: no typing indicator (Feishu doesn't expose
    one to bots), but the placeholder / mutable-spinner edits / final
    ``update_message`` flow is identical to Telegram. ``event_emitter``
    opt-in same as :func:`handle_one_telegram` — see W4.1 doc there.
    """
    chat_id = inbound.binding.thread
    reply_to = inbound.message_id
    placeholder_id: str | None = None
    error_message: str | None = None

    async def _edit(text: str) -> None:
        if placeholder_id is None:
            return
        await sender.update_message(placeholder_id, text)

    async def _send_attachment(ev: Any) -> str:
        return await _feishu_send_attachment(sender, chat_id, reply_to, ev)

    spinner = MutableSpinner(_edit, send_attachment_handler=_send_attachment)
    footer_state = _FooterState()
    observability_task: asyncio.Task[None] | None = None
    if event_emitter is not None:
        observability_task = asyncio.create_task(
            _consume_observability_events(
                event_emitter,
                _effective_session_key_for(inbound.binding),
                spinner,
                footer_state,
            ),
            name="feishu-observability-consumer",
        )
    request = _build_text_channel_request(inbound, model)
    if params is not None:
        await _inject_persona_if_enabled(
            request,
            humanlike_enabled=params.humanlike_enabled,
            persona_id=params.persona_id,
            persona_store=params.persona_store,
            humanlike_resolver=_binding_persona_resolver(
                inbound.binding,
                humanlike_enabled=params.humanlike_enabled,
                persona_id=params.persona_id,
                humanlike_resolver=params.humanlike_resolver,
            ),
            asset_store=params.asset_store,
            channel_name="feishu",
        )
    try:
        try:
            placeholder_id = await sender.send_message(
                chat_id, _TG_STATUS_THINKING, reply_to_message_id=reply_to
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("feishu placeholder send failed: %s", exc)

        async def _emit_status_link(line: str) -> None:
            await sender.send_message(
                chat_id, line, reply_to_message_id=reply_to
            )

        outcome = await _drive_spinner(
            spinner,
            chat_service,
            inbound,
            model,
            cancel,
            request=request,
            on_subagent_spawn=_emit_status_link,
        )
    finally:
        if observability_task is not None:
            observability_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await observability_task

    if outcome.supplemented:
        _log.info(
            "channel.user_supplemented channel=feishu session=%s",
            inbound.binding.session_key(),
        )
        return
    error_message = outcome.error_message

    if error_message is not None:
        body = f"[corlinman error] {error_message}"
    else:
        body = _normalize_for_channel("".join(spinner.text_parts), "feishu")
        if not body:
            if placeholder_id is not None:
                try:
                    await sender.update_message(placeholder_id, "（无回复）")
                except Exception as exc:  # noqa: BLE001
                    _log.warning("feishu final emit failed: %s", exc)
            return

    # [MSG_BREAK] bubble-split: split on the persona bubble-break marker,
    # then apply Feishu's char-limit chunking within each bubble.
    bubbles = _split_on_msg_break(body)
    footer = _build_footer_for_outcome(
        outcome, footer_state, session_key=_effective_session_key_for(inbound.binding)
    )
    try:
        all_bubble_chunks = [chunk_reply(b, _FEISHU_TEXT_LIMIT) for b in bubbles]
        # footer on the last chunk of the last bubble only.
        if footer and all_bubble_chunks:
            all_bubble_chunks[-1][-1] = try_append_footer(
                all_bubble_chunks[-1][-1], footer, _FEISHU_TEXT_LIMIT
            )
        for b_idx, b_chunks in enumerate(all_bubble_chunks):
            is_first_bubble = b_idx == 0
            is_last_bubble = b_idx == len(all_bubble_chunks) - 1
            if len(b_chunks) > 1:
                _log.info(
                    "feishu reply split len=%d chunks=%d", len(bubbles[b_idx]), len(b_chunks)
                )
            if is_first_bubble and placeholder_id is not None:
                await sender.update_message(placeholder_id, b_chunks[0])
                for chunk in b_chunks[1:]:
                    await sender.send_message(
                        chat_id, chunk, reply_to_message_id=reply_to
                    )
            else:
                for chunk in b_chunks:
                    await sender.send_message(
                        chat_id, chunk, reply_to_message_id=reply_to
                    )
            if not is_last_bubble:
                await asyncio.sleep(0.3)
        # Admin health: count a successful outbound + mark the inbound
        # entry responded.
        _channel_record_sent(FEISHU_HEALTH)
        _channel_mark_responded(FEISHU_RECENT_MESSAGES, chat_id)
    except Exception as exc:  # noqa: BLE001
        _channel_record_error(FEISHU_HEALTH)
        _log.warning("feishu final emit failed: %s", exc)


# ---------------------------------------------------------------------------
# QQ 官方机器人 (Official) channel
# ---------------------------------------------------------------------------


# QQ Official dispatch event-type slugs the handler routes on.
_QQ_OFFICIAL_EVT_C2C = "C2C_MESSAGE_CREATE"
_QQ_OFFICIAL_EVT_GROUP = "GROUP_AT_MESSAGE_CREATE"
_QQ_OFFICIAL_EVT_GUILD_AT = "AT_MESSAGE_CREATE"
_QQ_OFFICIAL_EVT_GUILD = "MESSAGE_CREATE"
_QQ_OFFICIAL_EVT_DIRECT = "DIRECT_MESSAGE_CREATE"


@dataclass(slots=True)
class QqOfficialChannelParams:
    """Parameters for :func:`run_qq_official_channel`.

    Mirrors :class:`QqChannelParams` but for the official QQ Bot
    platform (api.sgroup.qq.com) — a wholly different transport from
    the gocq / NapCat OneBot path, so the two run as independent
    channels.
    """

    config: Any
    """``cfg.channels.qq_official`` — must expose ``app_id`` +
    ``app_secret``, optional ``sandbox`` (bool), optional ``intents``
    (int bitmask)."""

    model: str = ""
    chat_service: ChatServiceLike | None = None

    # ---- human-like persona toggle (T-persona) -------------------------
    # Mirror of the QQ humanlike fields — see :class:`QqChannelParams`.
    # Off by default; opt-in via ``[channels.qq_official.humanlike]`` (or
    # the live ``humanlike_resolver``). When on, the per-turn inbound
    # handler prepends the persona's ``system_prompt`` (plus its emoji
    # block when assets are wired) at the head of the chat request.
    humanlike_enabled: bool = False
    persona_id: str | None = None
    persona_store: Any = None
    humanlike_resolver: Any = None
    asset_store: Any = None


async def run_qq_official_channel(
    params: QqOfficialChannelParams,
    cancel: asyncio.Event,
) -> None:
    """Spawn the QQ Official channel loop and run until ``cancel`` is set.

    Parallel structure to :func:`run_qq_channel` — inbound over the
    official Gateway WebSocket, outbound replies through
    :class:`QqOfficialSender`. Raises ``ValueError`` on missing
    required config (matches the QQ / Telegram runners).
    """
    cfg = params.config
    app_id = _attr(cfg, "app_id", "")
    app_secret = _attr(cfg, "app_secret", "")
    if not app_id:
        raise ValueError("channels.qq_official.app_id is empty")
    if not app_secret:
        raise ValueError("channels.qq_official.app_secret is empty")

    intents = _attr(cfg, "intents", None)
    try:
        intents_int = int(intents) if intents is not None else QQ_OFFICIAL_DEFAULT_INTENTS
    except (TypeError, ValueError):
        intents_int = QQ_OFFICIAL_DEFAULT_INTENTS
    if intents_int <= 0:
        # Operators sometimes paste "0" expecting a default; coerce so
        # the bot still receives something meaningful.
        intents_int = QQ_OFFICIAL_DEFAULT_INTENTS

    qq_cfg = QqOfficialConfig(
        app_id=str(app_id),
        app_secret=str(app_secret),
        sandbox=bool(_attr(cfg, "sandbox", False)),
        intents=intents_int,
    )
    adapter = QqOfficialAdapter(qq_cfg)
    send_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    sender = QqOfficialSender(
        send_client,
        adapter.access_token,
        app_id=qq_cfg.app_id,
        api_base=qq_cfg.api_base,
    )
    semaphore = asyncio.Semaphore(_channel_max_concurrency("QQ_OFFICIAL"))
    pending: set[asyncio.Task[None]] = set()
    try:
        async with adapter:
            iterator = adapter.inbound()
            while not cancel.is_set():
                ev = await _race_iter_or_cancel(iterator, cancel)
                if ev is None:
                    break
                chat_service = params.chat_service
                if chat_service is None:
                    continue
                await _bounded_spawn(
                    semaphore,
                    pending,
                    functools.partial(
                        handle_one_qq_official,
                        chat_service,
                        ev,
                        params.model,
                        sender,
                        cancel,
                        params=params,
                    ),
                )
    finally:
        for t in pending:
            t.cancel()
        await send_client.aclose()


def _qq_official_event_type(inbound: InboundEvent[Any]) -> str:
    """Pull the cached dispatch-event-type out of an inbound payload.

    The adapter stashes it under ``_qq_official_event_type`` so this
    helper doesn't need to re-classify based on binding shape.
    """
    payload = inbound.payload
    if isinstance(payload, dict):
        ty = payload.get("_qq_official_event_type")
        if isinstance(ty, str):
            return ty
    return ""


async def _qq_official_send_text(
    sender: QqOfficialSender,
    inbound: InboundEvent[Any],
    text: str,
) -> str:
    """Dispatch ``text`` to the right send endpoint for ``inbound``.

    Routes by the adapter-stashed event type:

    * C2C → :meth:`QqOfficialSender.send_c2c_text`
    * group@bot → :meth:`QqOfficialSender.send_group_text`
    * guild channel / DM → :meth:`QqOfficialSender.send_text`

    The platform enforces a 5-minute passive-reply window; we thread
    ``inbound.message_id`` (the original ``msg_id``) so the reply
    lands inside the window.
    """
    msg_id = inbound.message_id
    event_type = _qq_official_event_type(inbound)
    thread = inbound.binding.thread
    if event_type == _QQ_OFFICIAL_EVT_C2C:
        return await sender.send_c2c_text(thread, text, msg_id=msg_id)
    if event_type == _QQ_OFFICIAL_EVT_GROUP:
        return await sender.send_group_text(thread, text, msg_id=msg_id)
    # Default: guild channel / DM both go through the channel endpoint.
    return await sender.send_text(thread, text, msg_id=msg_id)


async def _qq_official_send_image(
    sender: QqOfficialSender,
    inbound: InboundEvent[Any],
    *,
    url: str | None = None,
    file_data: bytes | None = None,
    caption: str = "",
) -> str:
    """Upload (if needed) + send an image for ``inbound``.

    For C2C / 群 the platform requires a pre-upload returning a
    ``file_info`` token before the actual send call; guild channel
    messages accept the URL inline. The caller already verified
    the file is an image — non-image attachments are not supported
    by the platform (we render a status text upstream).
    """
    event_type = _qq_official_event_type(inbound)
    thread = inbound.binding.thread
    msg_id = inbound.message_id
    if event_type == _QQ_OFFICIAL_EVT_C2C:
        info = await sender.upload_c2c_image(
            thread, url=url, file_data=file_data
        )
        return await sender.send_c2c_image(
            thread, info, msg_id=msg_id, content=caption
        )
    if event_type == _QQ_OFFICIAL_EVT_GROUP:
        info = await sender.upload_group_image(
            thread, url=url, file_data=file_data
        )
        return await sender.send_group_image(
            thread, info, msg_id=msg_id, content=caption
        )
    # Guild channel — direct image URL is supported on this endpoint.
    if url is None:
        # Guild channel needs an HTTPS URL; for local files we can't
        # synthesize one. Surface a status caption instead.
        raise TransportError(
            "qq_official guild-channel image requires a public HTTPS url"
        )
    return await sender.send_image(
        thread, url, msg_id=msg_id, content=caption
    )


async def _qq_official_send_attachment(
    sender: QqOfficialSender,
    inbound: InboundEvent[Any],
    ev: Any,
) -> str:
    """Handle a ``send_attachment`` tool call for QQ Official.

    Returns the status text to fold into the summary block. The QQ
    Official platform only supports IMAGE attachments on the C2C /
    group endpoints — for non-image files we surface a friendly
    fallback so the user understands why the file didn't ship.
    """
    path_str, caption, filename = _parse_send_attachment_args(ev)
    if not path_str:
        return "⚠️ 发送文件失败: missing `path`"
    p = _resolve_attachment_path(path_str)
    if p is None:
        return f"⚠️ 发送文件失败: {Path(path_str).name} 不存在"
    mime, _ = mimetypes.guess_type(p.name)
    mime = mime or "application/octet-stream"
    display = filename or p.name
    if not mime.startswith("image/"):
        # Platform limitation — only images survive the C2C / group
        # pipeline. Surface a clear human-readable status instead of
        # silently dropping the file.
        return f"📎 [文件] {display} (QQ官方机器人暂不支持文件直发)"
    try:
        data = p.read_bytes()
        await _qq_official_send_image(
            sender, inbound, file_data=data, caption=caption or ""
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("qq_official send_attachment failed: %s", exc)
        return f"⚠️ 发送文件失败: {display} ({exc})"
    _log.info("qq_official send_attachment ok path=%s display=%s", p, display)
    return f"📎 已发送图片: {display}"


def _format_tool_summary_line(ev: Any) -> str:
    """Render one tool-activity line for the summary block.

    Mirrors :func:`_format_tool_status` shape but trimmed for a
    summary-prepend context (no spinner emoji — we're describing
    things that ALREADY happened by the time the reply lands).
    """
    tool = (getattr(ev, "tool", "") or "?").replace("\n", " ")
    plugin = (getattr(ev, "plugin", "") or "").replace("\n", " ")
    label = f"{plugin}.{tool}" if plugin and plugin != tool else tool
    if len(label) > 60:
        label = label[:57] + "..."
    preview = _tool_arg_preview(tool, getattr(ev, "args_json", b""))
    if preview:
        return f"• {label}  {preview}"
    return f"• {label}"


def _build_qq_official_summary(
    tool_lines: list[str],
    status_lines: list[str],
) -> str:
    """Assemble the summary block prepended to the final reply.

    Empty input → empty string (no block). When non-empty the block
    is delimited so the model output stays clearly separated:

    ::

        🔧 工具调用记录:
        • web_search  "tencent earnings"
        • read_file  /tmp/notes.md
        📎 已发送图片: chart.png
        ────────────────
        <model output here>

    The todo-list block is intentionally NOT prepended on the
    QQ-official channel. Pending ``☐`` rows are forward-looking noise
    on a non-editable transport where the user can't watch the boxes
    flip; the operation log IS the "what just happened" signal.
    """
    blocks: list[str] = []
    if tool_lines:
        blocks.append("🔧 工具调用记录:")
        blocks.extend(tool_lines)
    blocks.extend(status_lines)
    if not blocks:
        return ""
    blocks.append("────────────────")
    return "\n".join(blocks) + "\n"


async def handle_one_qq_official(
    chat_service: ChatServiceLike,
    inbound: InboundEvent[Any],
    model: str,
    sender: QqOfficialSender,
    cancel: asyncio.Event,
    *,
    inbox: Any = None,
    inbox_id: int | None = None,
    params: QqOfficialChannelParams | None = None,
) -> None:
    """Run one chat turn and post the reply via :class:`QqOfficialSender`.

    The official QQ Bot platform has **no message-edit API** — replies
    are atomic, so we can't render a mutable spinner like Telegram
    does. Instead we collect tool-activity events into a summary
    block and prepend it to the final assistant reply, so the user
    still sees what the agent did even though it streams as one
    message.

    Inbox bookkeeping mirrors :func:`handle_one_qq`: when ``inbox`` is
    supplied the row transitions pending → dispatched → done/dead.
    """
    if inbox is not None and inbox_id is not None:
        try:
            await inbox.mark_dispatched(inbox_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("qq_official inbox mark_dispatched failed: %s", exc)

    request = _build_text_channel_request(inbound, model)
    # Optionally prepend a persona system_prompt — mirrors the other
    # humanlike-capable channels. Off by default; opt-in via the
    # ``[channels.qq_official.humanlike]`` config or the live resolver.
    if params is not None:
        await _inject_persona_if_enabled(
            request,
            humanlike_enabled=params.humanlike_enabled,
            persona_id=params.persona_id,
            persona_store=params.persona_store,
            humanlike_resolver=_binding_persona_resolver(
                inbound.binding,
                humanlike_enabled=params.humanlike_enabled,
                persona_id=params.persona_id,
                humanlike_resolver=params.humanlike_resolver,
            ),
            asset_store=params.asset_store,
            channel_name="qq_official",
        )
    text_parts: list[str] = []
    tool_lines: list[str] = []
    status_lines: list[str] = []
    # Push the live status link only when a sub-agent spawns. If the
    # standalone send fails, the final reply may append one fallback link.
    _qqo_status_link_requested = False
    _qqo_status_link_sent = False
    error_message: str | None = None
    supplemented = False
    try:
        stream = chat_service.run(request, cancel)
        async for chat_ev in stream:
            kind = _event_kind(chat_ev)
            if kind == "token_delta":
                if getattr(chat_ev, "is_reasoning", False):
                    # Skip reasoning deltas — internal monologue, not
                    # user-facing. We have no live spinner to show
                    # them on; just absorb.
                    continue
                text_parts.append(getattr(chat_ev, "text", "") or "")
            elif kind == "tool_call":
                tool_name = getattr(chat_ev, "tool", "") or ""
                if (
                    not _qqo_status_link_requested
                    and not _qqo_status_link_sent
                    and tool_name in _SUBAGENT_SPAWN_TOOLS
                ):
                    _qqo_link = _status_link_line(
                        _effective_session_key_for(inbound.binding)
                    )
                    if _qqo_link:
                        _qqo_status_link_requested = True
                        try:
                            await _qq_official_send_text(
                                sender, inbound, _qqo_link
                            )
                            _qqo_status_link_sent = True
                        except Exception as exc:  # noqa: BLE001
                            _log.warning(
                                "qq_official early status link send failed: %s",
                                exc,
                            )
                if tool_name == _SEND_ATTACHMENT_TOOL:
                    status = await _qq_official_send_attachment(
                        sender, inbound, chat_ev
                    )
                    status_lines.append(status)
                elif tool_name == _TODO_WRITE_TOOL:
                    # Drop ``todo_write`` calls from the QQ-official
                    # summary — pending ``☐`` rows are forward-looking
                    # noise on a non-editable channel. The tool-call
                    # log (other tools) is the user-visible signal.
                    pass
                else:
                    tool_lines.append(_format_tool_summary_line(chat_ev))
            elif kind == "done":
                if _is_supplemented_done(chat_ev):
                    supplemented = True
                break
            elif kind == "error":
                error_message = (
                    getattr(chat_ev, "error", "")
                    or getattr(chat_ev, "message", "")
                )
                break
            # tool_result frames are intentionally absorbed — the summary
            # block stays short by listing the tool invocations only.
    except Exception as exc:  # noqa: BLE001 — never let a crash kill the row
        _log.exception("qq_official handle_one crashed: %s", exc)
        if inbox is not None and inbox_id is not None:
            try:
                await inbox.mark_dead(inbox_id, error=f"crash: {exc!r}")
            except Exception:  # noqa: BLE001
                pass
        raise

    if supplemented:
        _log.info(
            "channel.user_supplemented channel=qq_official session=%s",
            inbound.binding.session_key(),
        )
        if inbox is not None and inbox_id is not None:
            try:
                await inbox.mark_done(inbox_id)
            except Exception as exc:  # noqa: BLE001
                _log.warning("qq_official inbox mark_done failed: %s", exc)
        return

    if error_message is not None:
        body = f"[corlinman error] {error_message}"
    else:
        body = _normalize_for_channel("".join(text_parts), "qq_official")
        if not body:
            # If the model said nothing but we DID do work (uploaded
            # an image, called a tool), still ship the status so the
            # user sees a confirmation.
            if not status_lines and not tool_lines:
                if inbox is not None and inbox_id is not None:
                    try:
                        await inbox.mark_done(inbox_id)
                    except Exception:  # noqa: BLE001
                        pass
                return

    summary = _build_qq_official_summary(tool_lines, status_lines)
    final = (summary + body) if summary else body
    if not final.strip():
        return
    # Append a fallback status link only for actual sub-agent fan-out turns
    # whose standalone early send failed.
    _qqo_status_line = (
        _status_link_line(_effective_session_key_for(inbound.binding))
        if _qqo_status_link_requested and not _qqo_status_link_sent
        else ""
    )

    # [MSG_BREAK] bubble-split: send each persona bubble as a separate
    # QQ Official message with a short pause between them. The status link
    # is appended to the last bubble only.
    bubbles = _split_on_msg_break(final)
    try:
        for b_idx, bubble in enumerate(bubbles):
            is_last_bubble = b_idx == len(bubbles) - 1
            bubble_text = bubble
            if _qqo_status_line and is_last_bubble:
                bubble_text = try_append_footer(bubble_text, _qqo_status_line)
            await _qq_official_send_text(sender, inbound, bubble_text)
            if not is_last_bubble:
                await asyncio.sleep(0.3)
    except Exception as exc:  # noqa: BLE001
        _log.warning("qq_official final send failed: %s", exc)
        if inbox is not None and inbox_id is not None:
            try:
                await inbox.mark_dead(inbox_id, error=f"send: {exc!r}")
            except Exception:  # noqa: BLE001
                pass
        return

    if inbox is not None and inbox_id is not None:
        try:
            await inbox.mark_done(inbox_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("qq_official inbox mark_done failed: %s", exc)


# ---------------------------------------------------------------------------
# WeChat Official Account channel
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WeChatOfficialChannelParams:
    """Parameters for :func:`run_wechat_official_channel`.

    Parallel structure to :class:`FeishuChannelParams`. WeChat Official
    Account is webhook-only (no long-poll / WS), so the runner registers
    a route + idles waiting for cancellation. ``register_route`` is the
    sibling-call the gateway uses to actually mount
    ``adapter.handle_webhook`` on a FastAPI router — pulled out as a
    callback so this service module never imports FastAPI.
    """

    config: Any
    """``cfg.channels.wechat_official`` — must expose ``app_id``,
    ``app_secret``, ``token``, optional ``encoding_aes_key`` (empty for
    v1: AES is not implemented), optional ``passive_timeout_s``, and an
    optional ``bot_name`` slug used in the public webhook path
    ``/wechat/<bot_name>`` (defaults to ``"default"``)."""

    model: str = ""
    chat_service: ChatServiceLike | None = None
    register_route: Any = None
    """Sync callable ``(bot_name: str, adapter: WeChatOfficialAdapter) ->
    None``. Invoked once at startup. Optional — when ``None`` the runner
    keeps the adapter alive but no webhook is mounted (useful in tests
    that drive ``adapter.handle_webhook`` directly)."""

    # ---- human-like persona toggle (T-persona) -------------------------
    # Mirror of the QQ humanlike fields — see :class:`QqChannelParams`.
    # Off by default; opt-in via ``[channels.wechat_official.humanlike]``
    # (or the live ``humanlike_resolver``). When on, the per-turn inbound
    # handler prepends the persona's ``system_prompt`` (plus its emoji
    # block when assets are wired) at the head of the chat request.
    humanlike_enabled: bool = False
    persona_id: str | None = None
    persona_store: Any = None
    humanlike_resolver: Any = None
    asset_store: Any = None


async def run_wechat_official_channel(
    params: WeChatOfficialChannelParams,
    cancel: asyncio.Event,
) -> None:
    """Wire the WeChat webhook + run an idle loop until ``cancel`` is set.

    Different shape from the other ``run_*_channel`` helpers: there's
    no inbound stream to drain because every event is delivered via
    :meth:`WeChatOfficialAdapter.handle_webhook` from the FastAPI
    layer. The runner:

    1. constructs the adapter from ``params.config``;
    2. wires the per-turn sink (``handle_one_wechat_official``);
    3. asks the gateway to mount the webhook (``register_route``);
    4. blocks on ``cancel`` so it lives + dies with its sibling channels.

    Raises ``ValueError`` on missing required config (matches the QQ /
    Telegram runners). Raises :class:`NotImplementedError` when
    ``encoding_aes_key`` is configured (v1 doesn't decrypt).
    """
    cfg = params.config
    app_id = _attr(cfg, "app_id", "")
    app_secret = _attr(cfg, "app_secret", "")
    token = _attr(cfg, "token", "")
    if not app_id:
        raise ValueError("channels.wechat_official.app_id is empty")
    if not app_secret:
        raise ValueError("channels.wechat_official.app_secret is empty")
    if not token:
        raise ValueError("channels.wechat_official.token is empty")

    aes_key = _attr(cfg, "encoding_aes_key", "") or ""
    passive_timeout = float(_attr(cfg, "passive_timeout_s", 0.0) or 0.0)
    bot_name = str(_attr(cfg, "bot_name", "default") or "default")

    wx_cfg = WeChatOfficialConfig(
        app_id=str(app_id),
        app_secret=str(app_secret),
        token=str(token),
        encoding_aes_key=str(aes_key),
        passive_timeout_s=passive_timeout,
    )
    adapter = WeChatOfficialAdapter(wx_cfg)
    send_client = httpx.AsyncClient()
    sender = WeChatOfficialSender(
        app_id=wx_cfg.app_id,
        app_secret=wx_cfg.app_secret,
        client=send_client,
    )
    semaphore = asyncio.Semaphore(_channel_max_concurrency("WECHAT_OFFICIAL"))
    # CMP-06 — resolve the slash-access policy once. ``None`` keeps the
    # historical allow-by-default behaviour.
    slash_policy = slash_access_policy_from_env()
    pending: set[asyncio.Task[None]] = set()

    async def _sink(
        inbound: InboundEvent[Any],
        passive_future: asyncio.Future[str],
    ) -> None:
        """Per-event sink — bounded-spawn a turn task and immediately return.

        The webhook is already blocked on ``passive_future``; we just need
        to make sure exactly one ``handle_one_wechat_official`` runs per
        inbound and that it respects the channel concurrency cap so a
        burst of subscribers doesn't fan out unbounded tasks.
        """
        # Command-handler short-circuit + prelude rewrite — same flow as
        # the Telegram loop. The reply rides the passive XML (overflow
        # goes through the customer-service push) and the agent turn is
        # skipped entirely.
        if await _try_dispatch_text_command(
            inbound,
            functools.partial(
                _wechat_send_command_reply, sender, inbound, passive_future
            ),
            slash_policy=slash_policy,
            channel_label="wechat_official",
        ):
            # If the reply send failed (or the handler replied nothing)
            # release the webhook so it doesn't sit on the passive
            # deadline forever.
            if not passive_future.done():
                passive_future.set_result("")
            return
        chat_service = params.chat_service
        if chat_service is None:
            # No backend wired (degraded). Resolve the future with the
            # empty string so the webhook returns an empty 200 promptly
            # instead of waiting the full passive deadline.
            if not passive_future.done():
                passive_future.set_result("")
            return
        inbound = _apply_text_command_prelude(inbound)
        await _bounded_spawn(
            semaphore,
            pending,
            functools.partial(
                handle_one_wechat_official,
                chat_service,
                inbound,
                params.model,
                sender,
                cancel,
                passive_future=passive_future,
                params=params,
            ),
        )

    adapter.set_on_event(_sink)

    if params.register_route is not None:
        try:
            params.register_route(bot_name, adapter)
        except Exception as exc:
            _log.warning("wechat_official register_route failed: %s", exc)

    try:
        # Webhook-only — block on cancel, the adapter does its work from
        # the FastAPI side.
        await cancel.wait()
    finally:
        for t in pending:
            t.cancel()
        await send_client.aclose()


#: Conservative prefix length below which we publish the whole reply as
#: the passive XML (no second customer-service send). WeChat passive
#: replies cap silently at ~2048 chars; staying well under is safer.
_WECHAT_PASSIVE_CAP: int = 600


def _split_passive_and_rest(body: str) -> tuple[str, str]:
    """Pick the chunk to ship in the passive XML + the remainder.

    Mirrors the QQ summary-agent "prepend the summary" approach: send a
    short first sentence (or the whole reply when short) as the passive
    XML so the user sees an instant answer, then push the rest via the
    customer-service path. Returns ``(passive, remainder)`` with
    ``remainder == ""`` meaning the whole body fit in passive.
    """
    if not body:
        return ("", "")
    if len(body) <= _WECHAT_PASSIVE_CAP:
        return (body, "")
    # Look for the first sentence-ending punctuation in the first
    # ``_WECHAT_PASSIVE_CAP`` chars and break there.
    head = body[:_WECHAT_PASSIVE_CAP]
    for marker in ("\n\n", "\n", "。", ". ", "! ", "? ", "！", "？"):
        idx = head.rfind(marker)
        if idx >= 100:  # not the very first chars — needs to be a real sentence
            cut = idx + len(marker)
            return (body[:cut].rstrip(), body[cut:].lstrip())
    # No nice break — slice mid-word with an ellipsis so the user knows
    # more is coming. The ellipsis is included in the cap budget so
    # passive payload never exceeds _WECHAT_PASSIVE_CAP.
    cut = max(_WECHAT_PASSIVE_CAP - 1, 1)
    return (body[:cut].rstrip() + "…", body[cut:].lstrip())


async def handle_one_wechat_official(
    chat_service: ChatServiceLike,
    inbound: InboundEvent[Any],
    model: str,
    sender: WeChatOfficialSender,
    cancel: asyncio.Event,
    *,
    passive_future: asyncio.Future[str] | None = None,
    params: WeChatOfficialChannelParams | None = None,
) -> None:
    """Run one WeChat Official Account turn.

    Mirrors :func:`handle_one_qq` (WeChat, like QQ, cannot edit messages
    so the spinner-edit pattern from :func:`handle_one_telegram` is out
    of reach). Uses the QQ summary-agent's "prepend a short summary"
    pattern: the FIRST sentence of the reply (or the full reply if
    short) resolves ``passive_future`` so the webhook returns it inline;
    the remainder is pushed via :meth:`WeChatOfficialSender.send_text_customer`.

    When ``passive_future`` is ``None`` the whole reply goes via the
    customer-service path — useful when the webhook already timed out
    (the runner pops the future map entry on the timeout side).
    """
    request = _build_text_channel_request(inbound, model)
    # Optionally prepend a persona system_prompt — mirrors the other
    # humanlike-capable channels. Off by default; opt-in via the
    # ``[channels.wechat_official.humanlike]`` config or the live resolver.
    if params is not None:
        await _inject_persona_if_enabled(
            request,
            humanlike_enabled=params.humanlike_enabled,
            persona_id=params.persona_id,
            persona_store=params.persona_store,
            humanlike_resolver=_binding_persona_resolver(
                inbound.binding,
                humanlike_enabled=params.humanlike_enabled,
                persona_id=params.persona_id,
                humanlike_resolver=params.humanlike_resolver,
            ),
            asset_store=params.asset_store,
            channel_name="wechat_official",
        )
    text_parts: list[str] = []
    error_message: str | None = None
    supplemented = False
    # WeChat has no live status surface, but a sub-agent fan-out can run
    # for minutes — push the live status link as a customer-service message
    # the moment the first sub-agent spawns so the user can watch. If that
    # standalone send fails, the final customer-service push may append one
    # fallback link.
    _wx_status_link_requested = False
    _wx_status_link_sent = False
    try:
        stream = chat_service.run(request, cancel)
        async for ev in stream:
            kind = _event_kind(ev)
            if kind == "token_delta":
                text_parts.append(getattr(ev, "text", "") or "")
            elif kind == "done":
                if _is_supplemented_done(ev):
                    supplemented = True
                break
            elif kind == "error":
                error_message = getattr(ev, "error", "") or getattr(
                    ev, "message", ""
                )
                break
            elif (
                kind == "tool_call"
                and not _wx_status_link_requested
                and not _wx_status_link_sent
            ):
                tool_name = getattr(ev, "tool", "") or ""
                if tool_name in _SUBAGENT_SPAWN_TOOLS:
                    _wx_link = _status_link_line(
                        _effective_session_key_for(inbound.binding)
                    )
                    if _wx_link:
                        _wx_status_link_requested = True
                        try:
                            await sender.send_text_customer(
                                inbound.binding.sender, _wx_link
                            )
                            _wx_status_link_sent = True
                        except Exception as exc:  # noqa: BLE001
                            _log.warning(
                                "wechat_official early status link send "
                                "failed: %s",
                                exc,
                            )
            # tool_call / tool_result frames are informational only —
            # WeChat has no live status surface (no edit, no typing
            # indicator) so we silently drop them. ``todo_write`` is
            # NOT exempt: pending ``☐`` rows are forward-looking noise
            # on this transport, and the reply body alone is what the
            # user can act on. Editable channels (Telegram, Discord,
            # Slack, Feishu) get the live checkbox view via the
            # mutable spinner in :mod:`_status`.
    except Exception as exc:
        # Never let a crash kill the bot — log + release any waiting webhook.
        _log.exception("wechat_official handle_one crashed: %s", exc)
        if passive_future is not None and not passive_future.done():
            passive_future.set_result("")
        raise

    if supplemented:
        _log.info(
            "channel.user_supplemented channel=wechat_official session=%s",
            inbound.binding.session_key(),
        )
        # Release the webhook so it returns without a passive reply —
        # the running turn will push its own answer when ready.
        if passive_future is not None and not passive_future.done():
            passive_future.set_result("")
        return

    if error_message is not None:
        body = f"[corlinman error] {error_message}"
    else:
        body = _normalize_for_channel("".join(text_parts), "wechat_official")
        if not body:
            # Empty reply — release the webhook so it doesn't sit on
            # the passive deadline forever. (Previously we'd ship the
            # todo list as a fallback payload, but pending rows are
            # forward-looking noise on a non-editable channel.)
            if passive_future is not None and not passive_future.done():
                passive_future.set_result("")
            return

    passive, remainder = _split_passive_and_rest(body)

    # Resolve the passive future as soon as we have something to say.
    if passive_future is not None and not passive_future.done():
        passive_future.set_result(passive)
        passive_delivered = True
    else:
        passive_delivered = False

    # Push remainder via customer-service. If the passive future was
    # already gone (timeout / no future supplied), push the WHOLE body
    # so the user still gets the answer.
    openid = inbound.binding.sender
    push_body = remainder if passive_delivered else body
    # Append a fallback status link to the customer-service push (NOT the
    # length-capped passive XML), but only for an actual sub-agent fan-out
    # whose standalone early send failed.
    _wx_status_line = (
        _status_link_line(_effective_session_key_for(inbound.binding))
        if _wx_status_link_requested and not _wx_status_link_sent
        else ""
    )
    if push_body and push_body.strip():
        # [MSG_BREAK] bubble-split: send each persona bubble as a separate
        # WeChat customer-service message with a short pause between them.
        # The status link is appended to the last bubble only.
        push_bubbles = _split_on_msg_break(push_body)
        for pb_idx, bubble in enumerate(push_bubbles):
            is_last = pb_idx == len(push_bubbles) - 1
            bubble_text = bubble
            if _wx_status_line and is_last:
                bubble_text = try_append_footer(bubble_text, _wx_status_line)
            if not bubble_text.strip():
                continue
            try:
                await sender.send_text_customer(openid, bubble_text)
            except Exception as exc:
                _log.warning(
                    "wechat_official customer/send failed user=%s err=%s",
                    openid, exc,
                )
            if not is_last:
                await asyncio.sleep(0.3)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _attribution_prefix(inbound: InboundEvent[Any]) -> str:
    """Render the sender / reply-to attribution prefix for an inbound.

    Thin wrapper over
    :func:`corlinman_channels.common.format_attribution_prefix` reading
    the :class:`InboundEvent` fields. Returns ``""`` when there's nothing
    to attribute so callers can prepend unconditionally.
    """
    return _attribution(
        sender_name=getattr(inbound, "sender_name", None),
        reply_to_text=getattr(inbound, "reply_to_text", None),
    )


#: Default album-debounce window. Telegram fans an album out as N rapid
#: updates; 1.5s comfortably covers the inter-item gap without adding
#: perceptible latency to a standalone message (which never buffers).
_ALBUM_DEBOUNCE_SECS: float = 1.5


async def _debounce_albums(
    iterator: AsyncIterator[InboundEvent[Any]],
    cancel: asyncio.Event,
    *,
    window_secs: float = _ALBUM_DEBOUNCE_SECS,
) -> AsyncIterator[InboundEvent[Any]]:
    """Wrap an adapter ``inbound()`` iterator with album merge-debounce.

    Standalone (non-album) events pass straight through. Album members
    (sharing a ``media_group_id``) are buffered by
    :class:`AlbumDebouncer` and emitted as a single merged event once no
    new item arrives within ``window_secs``. On cancel / stream end the
    buffered albums are flushed so nothing is lost.

    Implemented as a race between the next inbound item and a short
    timeout so a trailing album (never followed by another item) still
    flushes promptly instead of stalling until the next unrelated
    message arrives.
    """
    async def _anext() -> InboundEvent[Any]:
        return await iterator.__anext__()

    debouncer = AlbumDebouncer(window_secs)
    cancel_task = asyncio.create_task(cancel.wait())
    # The inbound read is started ONCE and kept pending across idle
    # windows. A ``window_secs`` tick only flushes albums that have gone
    # quiet — it must NOT cancel this read. Cancelling it raises
    # ``CancelledError`` inside ``inbound()``, whose
    # ``except CancelledError: return`` ends the stream permanently; that
    # silently killed the whole Telegram channel one debounce window
    # after boot. The read is cancelled only on real shutdown (the
    # ``finally`` below, reached when ``cancel`` fires or the stream ends).
    next_task: asyncio.Task[InboundEvent[Any]] | None = None
    try:
        while not cancel.is_set():
            if next_task is None:
                next_task = asyncio.create_task(_anext())
            timeout_task = asyncio.create_task(asyncio.sleep(window_secs))
            try:
                done, _pending = await asyncio.wait(
                    {next_task, cancel_task, timeout_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                # Only the disposable timeout arm is torn down each loop;
                # next_task survives so it can still resolve later.
                if not timeout_task.done():
                    timeout_task.cancel()
            if cancel_task in done:
                break
            if next_task in done:
                try:
                    ev = next_task.result()
                except StopAsyncIteration:
                    next_task = None
                    break
                next_task = None
                for ready in debouncer.feed(ev):
                    yield ready
            else:
                # Idle window elapsed with the read still pending — flush
                # any album that has gone quiet and keep waiting on it.
                for ready in debouncer.flush_ready():
                    yield ready
    finally:
        for task in (next_task, cancel_task):
            if task is not None and not task.done():
                task.cancel()
    # Drain whatever remains so a trailing album isn't dropped on shutdown.
    for ready in debouncer.flush_all():
        yield ready


def _build_text_channel_request(
    inbound: InboundEvent[Any], model: str
) -> Any:
    """Build the request handed to ``chat_service.run`` for the
    text-only channels (Telegram / Discord / Slack / Feishu).

    Returns a :class:`~types.SimpleNamespace` so the downstream
    ``ChatService`` can use attribute access (``req.model``,
    ``req.messages``…) — same shape as :func:`_build_internal_request`
    for QQ. The earlier dict form crashed the gateway with
    ``AttributeError: 'dict' object has no attribute 'model'``.

    Attachments go through :func:`_to_server_attachment_shape` so the
    server-side proto builder reads ``bytes_`` + ``ApiAttachmentKind``
    correctly (see the same bug fix on ``_build_internal_request``).
    """
    from types import SimpleNamespace

    # Inbound attribution: prefix the agent-facing content with the
    # sender display-name + (when this is a reply) a truncated quote of
    # the parent message. Mirrors openclaw's group-chat attribution so a
    # multi-party thread stays attributable and a bare reply keeps its
    # referent. The prefix is empty (no-op) when neither field is set, so
    # private 1:1 chats with no reply context are byte-identical to the
    # pre-attribution behaviour.
    prefix = _attribution_prefix(inbound)
    content = f"{prefix}\n{inbound.text}" if prefix else inbound.text

    message = SimpleNamespace(role="user", content=content)
    # Same per-binding prefs choke point as _build_internal_request — the
    # four text channels (Telegram / Discord / Slack / Feishu) all build
    # their request here, so /model and /new work uniformly.
    from corlinman_channels import binding_prefs as _binding_prefs

    return SimpleNamespace(
        model=_binding_prefs.effective_model(inbound.binding, model),
        messages=[message],
        session_key=_binding_prefs.effective_session_key(
            inbound.binding, inbound.binding.session_key()
        ),
        stream=True,
        max_tokens=None,
        temperature=None,
        attachments=[
            _to_server_attachment_shape(a) for a in inbound.attachments
        ],
        binding=inbound.binding,
        # Shape-match InternalChatRequest. Persona injection overwrites this
        # with a real id when humanlike is enabled; default None keeps the
        # server-side proto builder's tolerant read happy.
        persona_id=None,
    )


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
    produced an empty reply (caller should send nothing) OR when the
    backend signalled ``Done(finish_reason="supplemented")`` — the
    running turn absorbed the user text and the caller must stay silent.
    On a backend error the body is a short ``[corlinman error] <msg>``
    string so the user knows something failed — matching
    :func:`handle_one_telegram`.
    """
    request = _build_text_channel_request(inbound, model)
    stream = chat_service.run(request, cancel)
    text_parts: list[str] = []
    error_message: str | None = None
    async for ev in stream:
        kind = _event_kind(ev)
        if kind == "token_delta":
            text_parts.append(getattr(ev, "text", "") or "")
        elif kind == "done":
            if _is_supplemented_done(ev):
                return None  # silent ack — running turn absorbed it
            break
        elif kind == "error":
            error_message = getattr(ev, "error", "") or getattr(ev, "message", "")
            break

    if error_message is not None:
        return f"[corlinman error] {error_message}"
    body = _normalize_for_channel("".join(text_parts), inbound.channel)
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
    async def _anext() -> Any:
        return await iterator.__anext__()

    next_task = asyncio.create_task(_anext())
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
            except (StopAsyncIteration, Exception):
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
        "ToolResult": "tool_result",
        "Done": "done",
        "Error": "error",
        "InternalChatEvent": "token_delta",
    }
    return mapping.get(name, name.lower())


#: Wire sentinel for a Done frame that absorbed a mid-turn user
#: supplement (see ``agent_servicer.Chat`` — when a second Chat RPC
#: arrives for an already-running session, it injects the new user
#: text into the in-flight ``ReasoningLoop`` and returns a Done with
#: this finish_reason). Channel handlers MUST NOT render a reply for
#: these — the original turn is still running and will produce the
#: actual reply on its own.
_SUPPLEMENTED_FINISH_REASON = "supplemented"


def _is_supplemented_done(ev: Any) -> bool:
    """Return ``True`` if ``ev`` is the ``Done(supplemented)`` sentinel.

    Checks the kind first to avoid mistakenly treating a non-Done
    event with a stray ``finish_reason`` attribute as supplemented.
    Tolerates both the dataclass shape (``ev.kind == "done"``) and
    the gateway_api dataclass (``finish_reason`` attribute).
    """
    if _event_kind(ev) != "done":
        return False
    fr = getattr(ev, "finish_reason", "") or ""
    return fr == _SUPPLEMENTED_FINISH_REASON


def _attr(obj: Any, name: str, default: Any) -> Any:
    """Walk attribute / mapping access uniformly. Tolerates both
    ``SimpleNamespace`` configs and TOML-loaded dicts."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


#: Default per-channel concurrent in-flight reply tasks. Conservative
#: because a single chat turn already drives the upstream provider hard.
_DEFAULT_CHANNEL_CONCURRENCY: int = 8


def _channel_max_concurrency(channel: str) -> int:
    """Resolve the per-channel concurrency cap.

    Each channel can override via ``CORLINMAN_<CHANNEL>_MAX_CONCURRENCY``
    (e.g. ``CORLINMAN_QQ_MAX_CONCURRENCY=4``). Invalid / unset values
    fall back to :data:`_DEFAULT_CHANNEL_CONCURRENCY`. Values < 1 are
    coerced to 1 so the semaphore can't deadlock the loop.

    R3 fix: the dispatch loops acquire this semaphore BEFORE spawning a
    reply task so a slow chat backend exerts backpressure on the
    inbound reader instead of fanning out unbounded asyncio tasks.
    """
    import os

    env_name = f"CORLINMAN_{channel.upper()}_MAX_CONCURRENCY"
    raw = os.environ.get(env_name)
    if not raw:
        return _DEFAULT_CHANNEL_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_CHANNEL_CONCURRENCY
    return max(1, value)


async def _bounded_spawn(
    semaphore: asyncio.Semaphore,
    pending: set[asyncio.Task[None]],
    coro_factory: Callable[[], Awaitable[None]],
) -> None:
    """Acquire ``semaphore`` then spawn the task, releasing on completion.

    Used by the four text-only channel dispatch loops (Telegram /
    Discord / Slack / Feishu) to keep R3's concurrency cap in one
    place. The semaphore is released in the task's ``finally`` block
    so a crashing handler never strands a permit.
    """
    await semaphore.acquire()

    async def _wrapped() -> None:
        try:
            await coro_factory()
        finally:
            semaphore.release()

    t = asyncio.create_task(_wrapped())
    pending.add(t)
    t.add_done_callback(pending.discard)


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
