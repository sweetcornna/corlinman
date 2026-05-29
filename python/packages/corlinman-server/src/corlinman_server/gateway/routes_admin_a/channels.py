"""``/admin/channels/qq*`` + ``/admin/channels/telegram*`` — channel admin.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/channels.rs``.

QQ routes (all behind :func:`require_admin_dependency`):

* ``GET  /admin/channels/qq/status``     — configuration snapshot. Reads
  ``state.channels_config`` which the bootstrapper hands in as a dict
  with the keys ``enabled``, ``ws_url``, ``self_ids``, ``group_keywords``.
* ``POST /admin/channels/qq/reconnect``  — placeholder; returns 501
  ``reconnect_unsupported`` matching the Rust contract.
* ``POST /admin/channels/qq/keywords``   — updates the
  ``group_keywords`` map and persists via ``state.channels_writer``.

Telegram routes (W4-FE F2 — gives the admin page real numbers):

* ``GET  /admin/channels/telegram/status``    — config + traffic snapshot.
  Reads :data:`corlinman_channels.TELEGRAM_HEALTH` for live counters,
  latency percentiles, and online flag. Shape matches the frontend's
  ``TelegramStatusResponse`` so the page renders without UI edits.
* ``GET  /admin/channels/telegram/messages``  — last N recent messages
  from :data:`corlinman_channels.TELEGRAM_RECENT_MESSAGES`.
* ``POST /admin/channels/telegram/send``      — admin-only test send via
  the live :class:`~corlinman_channels.TelegramSender` instance parked
  on :class:`AdminState`. Returns 503 ``telegram_disabled`` when the
  channel isn't wired.

NapCat-flavoured sub-routes (``/admin/channels/qq/{qrcode,accounts,
quick-login,qrcode/status}``) are part of the ``napcat`` Rust module
in the Rust tree — assigned to the parallel ``routes_admin_b`` agent.
This module deliberately does **not** mount them.
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)

# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class StatusOut(BaseModel):
    configured: bool
    enabled: bool
    ws_url: str | None
    self_ids: list[int] = Field(default_factory=list)
    group_keywords: dict[str, list[str]] = Field(default_factory=dict)
    runtime: str = "unknown"
    recent_messages: list[Any] = Field(default_factory=list)
    # NapCat health (T4 follow-up): updated by the QQ health watcher
    # every CORLINMAN_QQ_HEALTH_PROBE_S (default 30s).
    health_online: bool | None = None
    health_last_event_at_ms: int | None = None
    health_seconds_since_event: int | None = None
    health_checked_at_ms: int | None = None
    # Bot account state (separate from NapCat WS health; the WS can
    # stay alive while the QQ account is kicked offline by Tencent).
    # ``account_online=False`` is the operator-action signal: re-scan
    # the QR via the NapCat WebUI.
    account_online: bool | None = None
    account_qq: int | None = None
    account_nickname: str | None = None
    account_checked_at_ms: int | None = None
    account_last_error: str | None = None


class KeywordsBody(BaseModel):
    """Full replacement map: ``group_id → [keyword, …]``."""

    group_keywords: dict[str, list[str]] = Field(default_factory=dict)


class KeywordsOut(BaseModel):
    status: str
    group_keywords: dict[str, list[str]]


# ---------------------------------------------------------------------------
# Telegram wire shapes — match ui/lib/api/telegram.ts field-for-field so the
# admin page renders real numbers without any frontend edits.
# ---------------------------------------------------------------------------


class TelegramConfigOut(BaseModel):
    bot_token: str = ""
    webhook_url: str = ""
    secret_token: str = ""
    drop_pending_updates: bool = False


class TelegramStatsOut(BaseModel):
    messages_today: int = 0
    messages_week: int = 0
    latency_p50_ms: int = 0
    latency_p95_ms: int = 0
    active_chats: int = 0


class TelegramStatusOut(BaseModel):
    config: TelegramConfigOut = Field(default_factory=TelegramConfigOut)
    stats: TelegramStatsOut = Field(default_factory=TelegramStatsOut)
    connected: bool = False
    runtime: str = "unknown"
    last_error: str | None = None
    last_webhook_payload: dict[str, Any] | None = None


class TelegramSendBody(BaseModel):
    chat_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)


class TelegramSendOut(BaseModel):
    status: str
    message_id: int | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Discord / Slack / Feishu + WeChat-Official / QQ-Official wire shapes
# ---------------------------------------------------------------------------
#
# Uniform admin surface for the five channels that previously had zero
# admin endpoints. Discord / Slack / Feishu expose status + messages +
# send; the webhook-/credential-only channels (wechat_official /
# qq_official) expose status only.


class ChannelStatusOut(BaseModel):
    """Status envelope for Discord / Slack / Feishu (traffic-bearing)."""

    configured: bool = False
    enabled: bool = False
    online: bool = False
    last_event_at_ms: int | None = None
    received: int = 0
    sent: int = 0
    errors: int = 0
    error_message: str | None = None
    # NON-SECRET config only — bot/app id, allowed targets, keyword
    # filter. Never tokens / secrets. Values are str or list[str].
    config_keys: dict[str, str | list[str]] = Field(default_factory=dict)


class ChannelConfigStatusOut(BaseModel):
    """Status envelope for WeChat-Official / QQ-Official (config-only)."""

    configured: bool = False
    enabled: bool = False
    online: bool = False
    last_event_at_ms: int | None = None
    error_message: str | None = None
    config_keys: dict[str, str | list[str]] = Field(default_factory=dict)


class ChannelMessagesOut(BaseModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)


class ChannelSendBody(BaseModel):
    """Send body — accepts ``target_id`` / ``chat_id`` / ``channel_id``
    interchangeably so the frontend can use whichever name reads best per
    channel. At least one must be non-empty (validated in the handler)."""

    target_id: str | None = None
    chat_id: str | None = None
    channel_id: str | None = None
    text: str = Field(..., min_length=1)

    def resolve_target(self) -> str | None:
        for v in (self.target_id, self.chat_id, self.channel_id):
            if v:
                return v
        return None


class ChannelSendOut(BaseModel):
    ok: bool
    message_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _qq_config(state: AdminState) -> dict[str, Any] | None:
    """Borrow the QQ subsection of the channels config dict. Returns
    ``None`` when the bootstrapper didn't pre-populate it (the Rust
    ``cfg.channels.qq.is_none()`` path)."""
    if state.channels_config is None:
        return None
    qq = state.channels_config.get("qq")
    if not isinstance(qq, dict):
        return None
    return qq


def _telegram_config(state: AdminState) -> dict[str, Any] | None:
    """Borrow the Telegram subsection of the channels config dict.
    Returns ``None`` when no ``[channels.telegram]`` section was
    configured. The status route surfaces an "empty config" envelope
    in that case so the admin page still renders gracefully."""
    if state.channels_config is None:
        return None
    tg = state.channels_config.get("telegram")
    if not isinstance(tg, dict):
        return None
    return tg


def _telegram_is_enabled(state: AdminState) -> bool:
    """``True`` when the Telegram channel section is present AND has
    ``enabled=True``. Used to gate the send route — disabled channels
    return 503 even if a stale sender handle is still around."""
    tg = _telegram_config(state)
    if tg is None:
        return False
    return bool(tg.get("enabled", False))


# ---------------------------------------------------------------------------
# Generic channel helpers (Discord / Slack / Feishu / *-official)
# ---------------------------------------------------------------------------


def _channel_config(state: AdminState, name: str) -> dict[str, Any] | None:
    """Borrow the ``[channels.<name>]`` subsection of the config dict.
    ``None`` when the section is missing — the status route then surfaces
    a ``configured=False`` envelope."""
    if state.channels_config is None:
        return None
    section = state.channels_config.get(name)
    if not isinstance(section, dict):
        return None
    return section


# Per-channel NON-SECRET config keys to surface. NEVER includes tokens /
# secrets (``bot_token`` / ``app_token`` / ``app_secret`` / ``token``).
# ``id_keys`` are plain string ids; ``list_keys`` are list[str].
_CHANNEL_CONFIG_KEYS: dict[str, dict[str, list[str]]] = {
    "discord": {
        "id_keys": [],
        "list_keys": ["allowed_channel_ids", "keyword_filter"],
        "bool_keys": ["respond_to_all"],
    },
    "slack": {
        "id_keys": [],
        "list_keys": ["allowed_channel_ids", "keyword_filter"],
        "bool_keys": ["respond_to_all"],
    },
    "feishu": {
        "id_keys": ["app_id"],
        "list_keys": ["allowed_chat_ids", "keyword_filter"],
        "bool_keys": ["respond_to_all"],
    },
    "wechat_official": {
        "id_keys": ["app_id"],
        "list_keys": [],
        "bool_keys": [],
    },
    "qq_official": {
        "id_keys": ["app_id"],
        "list_keys": ["intents"],
        "bool_keys": ["sandbox"],
    },
}


def _non_secret_config_keys(
    name: str, section: dict[str, Any]
) -> dict[str, str | list[str]]:
    """Project a channel config section down to the NON-SECRET keys for
    the named channel. Tokens / secrets are never read here."""
    spec = _CHANNEL_CONFIG_KEYS.get(name, {})
    out: dict[str, str | list[str]] = {}
    for key in spec.get("id_keys", []):
        val = section.get(key)
        if val is not None:
            out[key] = str(val)
    for key in spec.get("list_keys", []):
        val = section.get(key)
        if isinstance(val, list):
            out[key] = [str(v) for v in val]
    for key in spec.get("bool_keys", []):
        if key in section:
            out[key] = str(bool(section.get(key)))
    return out


def _channel_health(name: str) -> dict[str, Any]:
    """Best-effort import of the live health snapshot for a channel.
    Empty dict when corlinman-channels isn't installed."""
    try:
        from corlinman_channels import service as _svc

        attr = {
            "discord": "DISCORD_HEALTH",
            "slack": "SLACK_HEALTH",
            "feishu": "FEISHU_HEALTH",
        }.get(name)
        if attr is None:
            return {}
        _svc._channel_refresh_online(getattr(_svc, attr))
        return dict(getattr(_svc, attr))
    except Exception:  # noqa: BLE001 — degrade silently
        return {}


def _channel_recent(name: str, limit: int) -> list[dict[str, Any]]:
    """Best-effort import of the recent-message buffer for a channel,
    newest first, capped at ``limit``."""
    try:
        from corlinman_channels import service as _svc

        attr = {
            "discord": "DISCORD_RECENT_MESSAGES",
            "slack": "SLACK_RECENT_MESSAGES",
            "feishu": "FEISHU_RECENT_MESSAGES",
        }.get(name)
        if attr is None:
            return []
        snapshot = list(getattr(_svc, attr))
        snapshot.sort(key=lambda m: m.get("timestamp_ms", 0), reverse=True)
        return [dict(m) for m in snapshot[:limit]]
    except Exception:  # noqa: BLE001 — degrade gracefully
        return []


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/channels/qq*``."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/channels/qq/status",
        response_model=StatusOut,
        summary="Snapshot of the QQ channel configuration",
    )
    async def status_handler(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> StatusOut:
        qq = _qq_config(state)
        if qq is None:
            return StatusOut(
                configured=False,
                enabled=False,
                ws_url=None,
            )
        # Pull the latest NapCat health snapshot (best-effort; the
        # channels package may not be installed for non-QQ deploys).
        health: dict[str, Any] = {}
        try:
            from corlinman_channels.service import QQ_HEALTH

            health = dict(QQ_HEALTH)
        except Exception:  # noqa: BLE001
            health = {}

        return StatusOut(
            configured=True,
            enabled=bool(qq.get("enabled", False)),
            ws_url=qq.get("ws_url"),
            self_ids=list(qq.get("self_ids", [])),
            group_keywords=dict(qq.get("group_keywords", {})),
            health_online=health.get("online"),
            health_last_event_at_ms=health.get("last_event_at_ms"),
            health_seconds_since_event=health.get("seconds_since_event"),
            health_checked_at_ms=health.get("checked_at_ms"),
            account_online=health.get("account_online"),
            account_qq=health.get("account_qq"),
            account_nickname=health.get("account_nickname"),
            account_checked_at_ms=health.get("account_checked_at_ms"),
            account_last_error=health.get("account_last_error"),
        )

    @r.post(
        "/admin/channels/qq/reconnect",
        summary="Placeholder — force a QQ ws reconnect (not implemented)",
    )
    async def reconnect(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> None:
        qq = _qq_config(state)
        if qq is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "channel_not_configured",
                    "message": "no [channels.qq] section in config",
                },
            )
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "error": "reconnect_unsupported",
                "message": (
                    "force-reconnect control is not yet implemented; "
                    "the OneBot client handles reconnect internally"
                ),
            },
        )

    @r.post(
        "/admin/channels/qq/keywords",
        response_model=KeywordsOut,
        summary="Replace the QQ per-group keyword overrides",
    )
    async def update_keywords(
        body: KeywordsBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> KeywordsOut:
        # Validate up front so an empty group / keyword is rejected
        # before we touch the writer.
        for group, kws in body.group_keywords.items():
            if not group:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "invalid_group",
                        "message": "group id must be non-empty",
                    },
                )
            if any(not k for k in kws):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "invalid_keyword",
                        "message": "keyword must be non-empty",
                    },
                )

        qq = _qq_config(state)
        if qq is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "channel_not_configured",
                    "message": (
                        "[channels.qq] missing; add a stub in config.toml "
                        "before editing keywords"
                    ),
                },
            )

        qq["group_keywords"] = dict(body.group_keywords)

        writer = state.channels_writer
        if writer is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "config_path_unset",
                    "message": "gateway booted without a config writer",
                },
            )
        try:
            ret = writer(state.channels_config)
            if inspect.isawaitable(ret):
                await ret
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "write_failed", "message": str(exc)},
            ) from exc

        return KeywordsOut(
            status="ok",
            group_keywords=dict(qq.get("group_keywords", {})),
        )

    # -- Telegram ----------------------------------------------------

    @r.get(
        "/admin/channels/telegram/status",
        response_model=TelegramStatusOut,
        summary="Snapshot of the Telegram channel + traffic counters",
    )
    async def telegram_status(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> TelegramStatusOut:
        # Pull the live in-process snapshot — the channels package
        # populates :data:`TELEGRAM_HEALTH` on every accepted event +
        # successful send. The import is best-effort so a deploy that
        # didn't install corlinman-channels still gets an empty envelope
        # rather than a 500.
        health: dict[str, Any] = {}
        try:
            from corlinman_channels.service import (
                TELEGRAM_HEALTH,
                _telegram_recompute_aggregates,
            )

            # Refresh "seconds_since_event" / online flag against now()
            # so the page never shows a stale "checked_at" timestamp.
            _telegram_recompute_aggregates()
            health = dict(TELEGRAM_HEALTH)
        except Exception:  # noqa: BLE001 — degrade silently
            health = {}

        tg = _telegram_config(state) or {}
        # Bot token is shown masked client-side; we surface the full
        # value here so the page's eye-toggle reveals real bytes when
        # the operator presses it. Webhook URL / secret token mirror
        # the TOML keys verbatim.
        config = TelegramConfigOut(
            bot_token=str(tg.get("bot_token", "") or ""),
            webhook_url=str(tg.get("webhook_url", "") or ""),
            secret_token=str(tg.get("secret_token", "") or ""),
            drop_pending_updates=bool(tg.get("drop_pending_updates", False)),
        )
        stats = TelegramStatsOut(
            messages_today=int(health.get("messages_today", 0) or 0),
            messages_week=int(health.get("messages_week", 0) or 0),
            latency_p50_ms=int(health.get("latency_p50_ms") or 0),
            latency_p95_ms=int(health.get("latency_p95_ms") or 0),
            active_chats=int(health.get("active_chats", 0) or 0),
        )

        # Runtime: online when the health watcher saw an event recently
        # AND the channel is gated on in config. A configured-but-
        # not-yet-started bot reports "disconnected" so the page banner
        # stays consistent with the QQ status semantics.
        configured_and_enabled = bool(tg.get("enabled", False))
        online = bool(health.get("online", False))
        if not configured_and_enabled:
            runtime = "disconnected"
        elif online:
            runtime = "connected"
        else:
            runtime = "disconnected"

        return TelegramStatusOut(
            config=config,
            stats=stats,
            connected=configured_and_enabled and online,
            runtime=runtime,
            last_error=None,
            last_webhook_payload=None,
        )

    @r.get(
        "/admin/channels/telegram/messages",
        summary="Recent inbound + outbound Telegram messages",
    )
    async def telegram_messages(
        state: Annotated[AdminState, Depends(get_admin_state)],
        limit: Annotated[int, Query(ge=1, le=500)] = 20,
    ) -> list[dict[str, Any]]:
        try:
            from corlinman_channels.service import TELEGRAM_RECENT_MESSAGES
        except Exception:  # noqa: BLE001 — degrade gracefully
            return []
        # Most recent first; the ring buffer is append-only so the
        # newest entry sits at the right end.
        snapshot = list(TELEGRAM_RECENT_MESSAGES)
        snapshot.sort(key=lambda m: m.get("timestamp_ms", 0), reverse=True)
        # Defensive copy: the deque entries are mutated in-place by the
        # ``routing="responded"`` flip in
        # :func:`telegram_record_reply_sent`. Returning shallow copies
        # protects the admin response payload from concurrent mutation.
        return [dict(m) for m in snapshot[:limit]]

    @r.post(
        "/admin/channels/telegram/send",
        response_model=TelegramSendOut,
        summary="Send a test message via the live Telegram channel",
    )
    async def telegram_send(
        body: TelegramSendBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> TelegramSendOut:
        # Two-step gate: (1) the [channels.telegram] section must be
        # enabled, (2) the live sender must have been wired by
        # channels_runtime.bootstrap. Either gap is reported as
        # "telegram_disabled" — the admin page renders the same offline
        # banner for both, and the operator's fix is the same: enable
        # the channel + restart the gateway.
        if not _telegram_is_enabled(state):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "telegram_disabled",
                    "message": (
                        "[channels.telegram] is missing or disabled — "
                        "enable it in config.toml and restart the gateway"
                    ),
                },
            )
        sender = getattr(state, "telegram_sender", None)
        if sender is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "telegram_disabled",
                    "message": (
                        "Telegram channel is enabled but the live sender "
                        "is not wired; restart the gateway to start the "
                        "channel task"
                    ),
                },
            )
        # ``chat_id`` is a string on the wire (Telegram chat ids fit
        # 64-bit but the UI never has to care about overflow) — coerce
        # to int because :meth:`TelegramSender.send_message` expects the
        # numeric form.
        try:
            chat_id_int = int(body.chat_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_chat_id",
                    "message": "chat_id must be a numeric Telegram chat id",
                },
            ) from None
        try:
            message_id = await sender.send_message(chat_id_int, body.text)
        except Exception as exc:  # noqa: BLE001 — surface the error inline
            return TelegramSendOut(status="error", error=str(exc))
        return TelegramSendOut(status="ok", message_id=int(message_id))

    # -- Discord / Slack / Feishu (status + messages + send) --------
    #
    # Registered via a factory loop so the three traffic-bearing channels
    # share one implementation. The send route reaches the live sender
    # parked on ``AdminState.<name>_sender`` by channels_runtime.bootstrap.

    def _register_traffic_channel(name: str, sender_attr: str) -> None:
        # ``name`` / ``sender_attr`` are captured by closure (NOT default
        # args) so they never leak into the public OpenAPI schema as query
        # params.
        @r.get(
            f"/admin/channels/{name}/status",
            response_model=ChannelStatusOut,
            summary=f"Snapshot of the {name} channel + traffic counters",
            name=f"{name}_status",
        )
        async def _status(
            state: Annotated[AdminState, Depends(get_admin_state)],
        ) -> ChannelStatusOut:
            section = _channel_config(state, name)
            if section is None:
                return ChannelStatusOut(configured=False, enabled=False)
            health = _channel_health(name)
            return ChannelStatusOut(
                configured=True,
                enabled=bool(section.get("enabled", False)),
                online=bool(health.get("online", False)),
                last_event_at_ms=health.get("last_event_at_ms"),
                received=int(health.get("received", 0) or 0),
                sent=int(health.get("sent", 0) or 0),
                errors=int(health.get("errors", 0) or 0),
                error_message=None,
                config_keys=_non_secret_config_keys(name, section),
            )

        @r.get(
            f"/admin/channels/{name}/messages",
            response_model=ChannelMessagesOut,
            summary=f"Recent inbound + outbound {name} messages",
            name=f"{name}_messages",
        )
        async def _messages(
            state: Annotated[AdminState, Depends(get_admin_state)],
            limit: Annotated[int, Query(ge=1, le=200)] = 20,
        ) -> ChannelMessagesOut:
            return ChannelMessagesOut(messages=_channel_recent(name, limit))

        @r.post(
            f"/admin/channels/{name}/send",
            response_model=ChannelSendOut,
            summary=f"Send a test message via the live {name} channel",
            name=f"{name}_send",
        )
        async def _send(
            body: ChannelSendBody,
            state: Annotated[AdminState, Depends(get_admin_state)],
        ) -> ChannelSendOut:
            target = body.resolve_target()
            if not target:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "missing_target",
                        "message": (
                            "one of target_id / chat_id / channel_id is required"
                        ),
                    },
                )
            section = _channel_config(state, name)
            enabled = bool(section.get("enabled", False)) if section else False
            sender = getattr(state, sender_attr, None)
            if not enabled or sender is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "error": f"{name}_disabled",
                        "message": (
                            f"[channels.{name}] is missing/disabled or the "
                            "live sender is not wired; enable it in config.toml "
                            "and restart the gateway"
                        ),
                    },
                )
            try:
                message_id = await sender.send_message(target, body.text)
            except Exception as exc:  # noqa: BLE001 — surface inline
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail={"error": "send_failed", "message": str(exc)},
                ) from exc
            return ChannelSendOut(ok=True, message_id=str(message_id))

    for _ch, _attr in (
        ("discord", "discord_sender"),
        ("slack", "slack_sender"),
        ("feishu", "feishu_sender"),
    ):
        _register_traffic_channel(_ch, _attr)

    # -- WeChat-Official / QQ-Official (config-only status) ---------
    #
    # No live in-process traffic counters / sender for these webhook-/
    # credential-only channels — the status route surfaces config presence
    # + the enabled flag only. ``online`` stays ``False`` (no health
    # watcher) so the admin page renders a neutral "configured but not
    # observed" state.

    def _register_config_channel(name: str) -> None:
        # ``name`` captured by closure — see _register_traffic_channel.
        @r.get(
            f"/admin/channels/{name}/status",
            response_model=ChannelConfigStatusOut,
            summary=f"Snapshot of the {name} channel configuration",
            name=f"{name}_status",
        )
        async def _status(
            state: Annotated[AdminState, Depends(get_admin_state)],
        ) -> ChannelConfigStatusOut:
            section = _channel_config(state, name)
            if section is None:
                return ChannelConfigStatusOut(configured=False, enabled=False)
            return ChannelConfigStatusOut(
                configured=True,
                enabled=bool(section.get("enabled", False)),
                online=False,
                last_event_at_ms=None,
                error_message=None,
                config_keys=_non_secret_config_keys(name, section),
            )

    for _ch in ("wechat_official", "qq_official"):
        _register_config_channel(_ch)

    return r


__all__ = [
    "ChannelConfigStatusOut",
    "ChannelMessagesOut",
    "ChannelSendBody",
    "ChannelSendOut",
    "ChannelStatusOut",
    "KeywordsBody",
    "KeywordsOut",
    "StatusOut",
    "TelegramConfigOut",
    "TelegramSendBody",
    "TelegramSendOut",
    "TelegramStatsOut",
    "TelegramStatusOut",
    "router",
]
