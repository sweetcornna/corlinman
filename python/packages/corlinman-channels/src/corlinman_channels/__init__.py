"""corlinman-channels — inbound channel adapters (Python plane).

Python port of the Rust ``corlinman-channels`` crate. Three inbound
transports share one uniform shape:

* :class:`OneBotAdapter` — QQ via gocq / NapCat over a forward-WebSocket
  using the OneBot v11 protocol.
* :class:`LogStreamAdapter` — WebSocket subscriber for structured log
  frames (newline-delimited JSON, one frame per WS text frame).
* :class:`TelegramAdapter` — Telegram Bot API HTTPS ``getUpdates``
  long-poll.

Each adapter exposes ``async for event in adapter.inbound(): ...`` and
yields the same :class:`InboundEvent` envelope so consumers don't need
to special-case the transport.

Plus the cross-cutting machinery the gateway wires on top:

* :class:`ChannelRegistry` / :class:`ChannelContext` / :func:`spawn_all`
  — the uniform Channel Protocol the gateway iterates over.
* :class:`ChannelRouter` — keyword / @mention gate + rate-limit hooks
  for the OneBot dispatcher.
* :class:`TokenBucket` — per-key token-bucket rate limiter.
* :class:`TelegramSender` / :class:`TelegramHttp` / :func:`process_update`
  — the Telegram outbound + webhook surface.
* :func:`run_qq_channel` / :func:`run_telegram_channel` — orchestration
  helpers wiring an adapter to a chat backend (parallel to Rust
  ``service.rs``).

The W1 :class:`UserId` is re-exported here for convenience; an adapter
that has access to an identity store can populate
``InboundEvent.user_id`` to bridge per-channel ids to a canonical
opaque handle.
"""

from corlinman_channels.channel import (
    ApnsChannel,
    Channel,
    ChannelContext,
    ChannelError,
    ChannelRegistry,
    QqChannel,
    TelegramChannel,
    spawn_all,
)
from corlinman_channels.commands import (
    COMMAND_REGISTRY,
    CommandSpec,
    apply_command_prelude,
    match_command,
)
from corlinman_channels.common import (
    Attachment,
    AttachmentKind,
    ChannelBinding,
    ConfigError,
    InboundAdapter,
    InboundEvent,
    TransportError,
    UnsupportedError,
    UserId,
)
from corlinman_channels.common import ChannelError as _CommonChannelError
from corlinman_channels.corlinman import (
    CORLINMAN_CHANNEL_ENV_FLAG,
    CorlinmanChannel,
    CorlinmanOutboundFrame,
    corlinman_channel_enabled,
)
from corlinman_channels.corlinman import (
    DEFAULT_ACCOUNT as WEB_DEFAULT_ACCOUNT,
)
from corlinman_channels.discord import (
    DiscordAdapter,
    DiscordConfig,
    DiscordSender,
)
from corlinman_channels.feishu import (
    FeishuAdapter,
    FeishuConfig,
    FeishuSender,
)
from corlinman_channels.logstream import (
    LogFrame,
    LogStreamAdapter,
    LogStreamConfig,
)
from corlinman_channels.onebot import (
    Action,
    AtSegment,
    Event,
    FaceSegment,
    ForwardNode,
    ForwardSegment,
    ImageSegment,
    MessageEvent,
    MessageSegment,
    MessageType,
    MetaEvent,
    NoticeEvent,
    OneBotAdapter,
    OneBotConfig,
    OtherSegment,
    RecordSegment,
    ReplySegment,
    RequestEvent,
    Sender,
    SendGroupForwardMsg,
    SendGroupMsg,
    SendPrivateMsg,
    TextSegment,
    UnknownEvent,
    action_to_wire,
    is_mentioned,
    parse_event,
    segments_to_attachments,
    segments_to_text,
)
from corlinman_channels.persona_inject import (
    compose_persona_emoji_block,
    inject_persona_if_enabled,
)
from corlinman_channels.qq_official import (
    QqOfficialAdapter,
    QqOfficialConfig,
)
from corlinman_channels.qq_official_send import QqOfficialSender
from corlinman_channels.rate_limit import (
    GC_INTERVAL,
    GC_STALE_AFTER,
    TokenBucket,
)
from corlinman_channels.router import (
    ChannelRouter,
    GroupKeywords,
    RateLimitHook,
    RoutedRequest,
    parse_group_keywords,
)
from corlinman_channels.service import (
    QQ_HEALTH,
    TELEGRAM_HEALTH,
    TELEGRAM_RECENT_MESSAGES,
    ChatEventLike,
    ChatServiceLike,
    DiscordChannelParams,
    FeishuChannelParams,
    QqChannelParams,
    QqOfficialChannelParams,
    SlackChannelParams,
    TelegramChannelParams,
    WeChatOfficialChannelParams,
    handle_one_discord,
    handle_one_feishu,
    handle_one_qq,
    handle_one_qq_official,
    handle_one_slack,
    handle_one_telegram,
    handle_one_wechat_official,
    run_discord_channel,
    run_feishu_channel,
    run_qq_channel,
    run_qq_official_channel,
    run_slack_channel,
    run_telegram_channel,
    run_wechat_official_channel,
    telegram_record_inbound,
    telegram_record_reply_sent,
)
from corlinman_channels.slack import (
    SlackAdapter,
    SlackConfig,
    SlackSender,
)
from corlinman_channels.telegram import (
    Chat,
    Document,
    File,
    Message,
    MessageEntity,
    MessageRoute,
    PhotoSize,
    TelegramAdapter,
    TelegramConfig,
    Update,
    User,
    Voice,
    binding_from_message,
    classify,
    is_mentioning_bot,
    session_key_for,
)
from corlinman_channels.telegram_media import (
    DownloadedMedia,
    HttpxTelegramHttp,
    MediaError,
    TelegramHttp,
    download_to_media_dir,
)
from corlinman_channels.telegram_send import (
    PhotoSource,
    SendError,
    TelegramSender,
)
from corlinman_channels.telegram_webhook import (
    ProcessedUpdate,
    WebhookContext,
    WebhookCtx,
    WebhookError,
    default_media_dir,
    process_update,
    verify_secret,
)
from corlinman_channels.wechat_official import (
    WeChatOfficialAdapter,
    WeChatOfficialConfig,
    build_passive_xml,
    parse_wechat_xml,
    verify_signature,
)
from corlinman_channels.wechat_official_send import (
    WeChatOfficialSender,
    split_for_send,
)

# ``ChannelError`` is defined in *both* ``common`` (the base error for
# adapter operations) and ``channel`` (the trait-surface error factory).
# Keep the channel-side name as the public ``ChannelError`` because the
# Rust crate's external API surface matches that one; the common base
# remains accessible via ``corlinman_channels.common.ChannelError`` for
# subclassing.
_ = _CommonChannelError

__all__ = [
    # Slash-command registry (W8 Persona Studio)
    "COMMAND_REGISTRY",
    "CORLINMAN_CHANNEL_ENV_FLAG",
    # Rate limit
    "GC_INTERVAL",
    "GC_STALE_AFTER",
    "QQ_HEALTH",
    "TELEGRAM_HEALTH",
    "TELEGRAM_RECENT_MESSAGES",
    "WEB_DEFAULT_ACCOUNT",
    # OneBot
    "Action",
    # Channel registry surface
    "ApnsChannel",
    "AtSegment",
    # Common / shared
    "Attachment",
    "AttachmentKind",
    "Channel",
    "ChannelBinding",
    "ChannelContext",
    "ChannelError",
    "ChannelRegistry",
    # Router
    "ChannelRouter",
    # Telegram inbound + parsing
    "Chat",
    # Service orchestration
    "ChatEventLike",
    "ChatServiceLike",
    "CommandSpec",
    "ConfigError",
    "CorlinmanChannel",
    "CorlinmanOutboundFrame",
    # Discord
    "DiscordAdapter",
    "DiscordChannelParams",
    "DiscordConfig",
    "DiscordSender",
    "Document",
    # Telegram outbound (media + send + webhook)
    "DownloadedMedia",
    "Event",
    "FaceSegment",
    # Feishu / Lark
    "FeishuAdapter",
    "FeishuChannelParams",
    "FeishuConfig",
    "FeishuSender",
    "File",
    "ForwardNode",
    "ForwardSegment",
    "GroupKeywords",
    "HttpxTelegramHttp",
    "ImageSegment",
    "InboundAdapter",
    "InboundEvent",
    # LogStream
    "LogFrame",
    "LogStreamAdapter",
    "LogStreamConfig",
    "MediaError",
    "Message",
    "MessageEntity",
    "MessageEvent",
    "MessageRoute",
    "MessageSegment",
    "MessageType",
    "MetaEvent",
    "NoticeEvent",
    "OneBotAdapter",
    "OneBotConfig",
    "OtherSegment",
    "PhotoSize",
    "PhotoSource",
    "ProcessedUpdate",
    "QqChannel",
    "QqChannelParams",
    # QQ Official (api.sgroup.qq.com)
    "QqOfficialAdapter",
    "QqOfficialChannelParams",
    "QqOfficialConfig",
    "QqOfficialSender",
    "RateLimitHook",
    "RecordSegment",
    "ReplySegment",
    "RequestEvent",
    "RoutedRequest",
    "SendError",
    "SendGroupForwardMsg",
    "SendGroupMsg",
    "SendPrivateMsg",
    "Sender",
    # Slack
    "SlackAdapter",
    "SlackChannelParams",
    "SlackConfig",
    "SlackSender",
    "TelegramAdapter",
    "TelegramChannel",
    "TelegramChannelParams",
    "TelegramConfig",
    "TelegramHttp",
    "TelegramSender",
    "TextSegment",
    "TokenBucket",
    "TransportError",
    "UnknownEvent",
    "UnsupportedError",
    "Update",
    "User",
    "UserId",
    "Voice",
    # WeChat Official Account (webhook-only)
    "WeChatOfficialAdapter",
    "WeChatOfficialChannelParams",
    "WeChatOfficialConfig",
    "WeChatOfficialSender",
    "WebhookContext",
    "WebhookCtx",
    "WebhookError",
    "action_to_wire",
    "apply_command_prelude",
    "binding_from_message",
    "build_passive_xml",
    "classify",
    # Persona injection (W7 Persona Studio)
    "compose_persona_emoji_block",
    "corlinman_channel_enabled",
    "default_media_dir",
    "download_to_media_dir",
    "handle_one_discord",
    "handle_one_feishu",
    "handle_one_qq",
    "handle_one_qq_official",
    "handle_one_slack",
    "handle_one_telegram",
    "handle_one_wechat_official",
    "inject_persona_if_enabled",
    "is_mentioned",
    "is_mentioning_bot",
    "match_command",
    "parse_event",
    "parse_group_keywords",
    "parse_wechat_xml",
    "process_update",
    "run_discord_channel",
    "run_feishu_channel",
    "run_qq_channel",
    "run_qq_official_channel",
    "run_slack_channel",
    "run_telegram_channel",
    "run_wechat_official_channel",
    "segments_to_attachments",
    "segments_to_text",
    "session_key_for",
    "spawn_all",
    "split_for_send",
    "telegram_record_inbound",
    "telegram_record_reply_sent",
    "verify_secret",
    "verify_signature",
]
