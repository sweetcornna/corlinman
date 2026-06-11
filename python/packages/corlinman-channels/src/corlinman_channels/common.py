"""Shared types for inbound channel adapters.

Mirrors the cross-cutting pieces of ``rust/crates/corlinman-channels/src/``:

* :class:`InboundEvent` — the normalized envelope each adapter yields.
* :class:`ChannelBinding` — transport-agnostic ``(channel, account, thread,
  sender)`` tuple, matching ``corlinman_core::channel_binding::ChannelBinding``.
* :class:`Attachment` / :class:`AttachmentKind` — multimodal attachment
  metadata (mirrors ``corlinman_gateway_api::Attachment``).
* :class:`ChannelError` — typed error surface for adapter operations.

Keeping these in one module means the per-channel adapters (``onebot``,
``logstream``, ``telegram``) all consume the same envelope shape and an
``async for`` loop over the ``inbound`` async-iterator yields a uniform
object regardless of transport.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

# Re-export UserId so downstream consumers can write
# ``from corlinman_channels.common import UserId`` without a separate import.
# Soft dependency — corlinman-identity is the W1 package this one builds on.
from corlinman_identity import UserId

# ---------------------------------------------------------------------------
# Attachment metadata (mirrors corlinman_gateway_api::Attachment)
# ---------------------------------------------------------------------------


class AttachmentKind(StrEnum):
    """Coarse-grained classification of a multimodal payload.

    Matches the Rust ``AttachmentKind`` enum (``image`` / ``audio`` /
    ``video`` / ``document``). String values are the canonical wire shape;
    the gateway routes these to the provider's multimodal handler.
    """

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class Attachment:
    """One inbound multimodal attachment.

    Either ``url`` or ``data`` is populated depending on whether the transport
    pre-uploaded the asset to a CDN (OneBot's ``image`` segment carries a
    URL) or shipped raw bytes (Telegram requires a follow-up download). The
    ``mime`` is best-effort; QQ doesn't expose a precise content type so we
    fall back to ``image/*`` / ``audio/*`` glob form.
    """

    kind: AttachmentKind
    url: str | None = None
    data: bytes | None = None
    mime: str | None = None
    file_name: str | None = None


# ---------------------------------------------------------------------------
# ChannelBinding — transport-agnostic conversation locus
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelBinding:
    """Transport-agnostic conversation locus.

    Mirrors ``corlinman_core::channel_binding::ChannelBinding`` (Rust). The
    four-tuple ``(channel, account, thread, sender)`` is hashed into a
    16-hex-char ``session_key`` which downstream RAG / approval logic uses
    as the stable conversation key.

    Examples:
    - QQ group: ``ChannelBinding("qq", "<bot_qq>", "<group_id>", "<user_qq>")``
    - QQ private: ``thread == sender`` (the user's QQ id).
    - Telegram: ``ChannelBinding("telegram", "<bot_id>", "<chat_id>",
      "<user_id>")``; private chats also have ``thread == sender``.
    """

    channel: str
    account: str
    thread: str
    sender: str

    def session_key(self) -> str:
        """Deterministic 16-hex-char digest of the four-tuple.

        Truncated SHA-256; collisions are vanishingly rare across the
        identifier space we use. Stable across processes so two replicas of
        the gateway compute the same key for the same binding.
        """
        digest = hashlib.sha256(
            f"{self.channel}|{self.account}|{self.thread}|{self.sender}".encode()
        ).hexdigest()
        return digest[:16]

    @classmethod
    def qq_group(cls, self_id: int | str, group_id: int | str, user_id: int | str) -> ChannelBinding:
        """Builder mirroring ``ChannelBinding::qq_group`` in Rust."""
        return cls(
            channel="qq",
            account=str(self_id),
            thread=str(group_id),
            sender=str(user_id),
        )

    @classmethod
    def qq_private(cls, self_id: int | str, user_id: int | str) -> ChannelBinding:
        """Builder mirroring ``ChannelBinding::qq_private`` in Rust.

        Per the QQ adapter convention, private-chat threads use the peer
        user id as both ``thread`` and ``sender`` so session keys remain
        stable per-peer.
        """
        return cls(
            channel="qq",
            account=str(self_id),
            thread=str(user_id),
            sender=str(user_id),
        )

    @classmethod
    def telegram(
        cls,
        bot_id: int | str,
        chat_id: int | str,
        user_id: int | str | None = None,
    ) -> ChannelBinding:
        """Builder for Telegram messages.

        ``user_id`` defaults to ``chat_id`` when absent (anonymous channel
        posts). Matches the fallback in
        ``rust/.../telegram/message.rs::binding_from_message``.
        """
        sender = user_id if user_id is not None else chat_id
        return cls(
            channel="telegram",
            account=str(bot_id),
            thread=str(chat_id),
            sender=str(sender),
        )


# ---------------------------------------------------------------------------
# Normalized inbound event
# ---------------------------------------------------------------------------

#: Payload type variable for :class:`InboundEvent`. The per-channel adapters
#: parametrize this so callers that only care about the normalized envelope
#: can write ``AsyncIterator[InboundEvent[Any]]``, while a caller that wants
#: the raw OneBot ``MessageEvent`` can keep the precise type.
PayloadT = TypeVar("PayloadT")


@dataclass(frozen=True, slots=True)
class InboundEvent[PayloadT]:
    """Normalized inbound event yielded by every channel adapter.

    Designed so a generic consumer can write::

        async for event in adapter.inbound():
            print(event.channel, event.text, event.binding.session_key())

    without knowing whether the source is QQ, Telegram, or a log stream.

    Adapters fill ``text`` with the human-readable content (flattened from
    CQ segments / Telegram entities) and ``payload`` with the raw transport
    event so callers can downcast when they need richer details.
    """

    channel: str
    """Channel slug (``"qq"``, ``"telegram"``, ``"logstream"``)."""

    binding: ChannelBinding
    """Transport-agnostic conversation locus."""

    text: str
    """Best-effort plain-text content. May be empty (e.g. an image-only
    message). Consumers that need richer structure read ``payload``."""

    message_id: str | None = None
    """Transport-specific message id (``str`` so 64-bit QQ ids round-trip
    safely; Telegram ids fit too)."""

    timestamp: int = 0
    """Unix seconds. Falls back to 0 when the transport doesn't expose one
    (LogStream frames sometimes lack timestamps)."""

    mentioned: bool = False
    """True when the bot was @-addressed (group / supergroup); always
    ``True`` for private chats since every DM is implicitly addressed."""

    attachments: list[Attachment] = field(default_factory=list)
    """Multimodal payload metadata; empty for text-only messages."""

    payload: PayloadT | None = None
    """Raw transport event for callers that need to introspect further. The
    concrete shape is documented per adapter module."""

    user_id: UserId | None = None
    """Optional canonical :class:`UserId` if the adapter was wired with an
    identity resolver. Adapters that don't perform resolution leave this
    ``None``; the caller can do it lazily via the binding."""

    sender_name: str | None = None
    """Best-effort human-readable display name of the message author
    (``"Alice"``, ``"@bob"``, a Telegram first_name, a Feishu nickname...).
    Mirrors openclaw/hermes inbound attribution: the gateway prefixes the
    agent-facing content with this so multi-party group chats stay
    attributable. ``None`` when the transport didn't expose one."""

    reply_to_text: str | None = None
    """Best-effort plain text of the message this one is replying to
    (Telegram ``reply_to_message.text``, Discord ``referenced_message``,
    Slack thread-parent, ...). Carries quote context to the agent so a
    bare ``"+1"`` reply isn't ambiguous. ``None`` when there's no reply
    parent or the transport didn't ship its text."""

    media_group_id: str | None = None
    """Transport media-group / album id (Telegram ``media_group_id``).
    Two inbound events that share this id were sent together as one album;
    :class:`AlbumDebouncer` buffers them and merges their attachments +
    captions before dispatch. ``None`` for standalone messages."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ChannelError(Exception):
    """Base error for channel adapter operations.

    Mirrors the Rust ``ChannelError`` enum — concrete subclasses below
    cover the cases the adapters actually surface today.
    """


class ConfigError(ChannelError):
    """Adapter configuration is invalid (missing token, empty URL, ...)."""


class TransportError(ChannelError):
    """Underlying transport failed (WS disconnect we cannot recover from,
    Telegram returned 4xx, etc.)."""


class UnsupportedError(ChannelError):
    """Operation not supported by this adapter (read-only channels that
    do not implement outbound send, etc.)."""


# ---------------------------------------------------------------------------
# InboundAdapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class InboundAdapter(Protocol):
    """Structural protocol every channel adapter satisfies.

    The minimal contract is just ``inbound()`` returning an async iterator
    of :class:`InboundEvent`. Adapters typically also implement ``__aenter__``
    / ``__aexit__`` for connection lifecycle, but the protocol does not
    require it so callers can wrap pre-connected fixtures in tests.
    """

    def inbound(self) -> AsyncIterator[InboundEvent[Any]]:
        """Yield normalized inbound events until the adapter is closed."""
        ...


def split_on_msg_break(text: str) -> list[str]:
    """Split on the [MSG_BREAK] persona marker into separate message bubbles."""
    parts = [p.strip() for p in text.split("[MSG_BREAK]") if p.strip()]
    return parts if parts else [text]


# ---------------------------------------------------------------------------
# Outbound text normalization
# ---------------------------------------------------------------------------

# Asterisk emphasis (``*x*`` / ``**x**`` / ``***x***``) → inner text.
# Asterisks effectively never appear intra-word, so this stays greedy.
_EMPHASIS_STAR_RE = re.compile(r"(\*\*\*|\*\*|\*)(?P<inner>.+?)\1", re.DOTALL)
# Underscore emphasis (``_x_`` / ``__x__`` / ``___x___``) → inner text, but
# ONLY at word boundaries. Markdown's intra-word rule: a real emphasis run
# has a non-word char (or string edge) just outside each delimiter, so
# identifiers/paths/URLs like ``my_file.py`` or ``/foo_bar/baz_qux`` keep
# their underscores instead of being mangled to ``myfile.py``.
_EMPHASIS_USCORE_RE = re.compile(
    r"(?<!\w)(_{1,3})(?=\S)(?P<inner>.+?)(?<=\S)\1(?!\w)",
    re.DOTALL,
)
# A mention-like token: leaving these wrapped in backticks preserves the
# author's escaping so a render-and-parse transport (Slack/Discord) can't
# turn ``\`@everyone\``` / ``\`<@U123>\``` into a live notification.
_MENTION_RE = re.compile(r"^\s*(?:@|<[@!#&])")
# ``inline code`` → inner text (drop the backticks; chat clients show them
# raw) UNLESS the inner content is mention-like.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+?)`")


def _unwrap_inline_code(m: re.Match[str]) -> str:
    inner = m.group(1)
    # Keep the escaping for mentions; strip backticks otherwise.
    return m.group(0) if _MENTION_RE.match(inner) else inner
# A fenced code block — preserved verbatim so real code keeps its shape.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
# Leading ATX heading hashes: ``## Title`` → ``Title``.
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
# Leading blockquote markers: ``> quote`` → ``quote``.
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
# Leading list bullet (-, *, +) → a clean middle-dot bullet.
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
# AI-tell Latin punctuation → plain ASCII. Chinese full-width punctuation
# (，。、：；？！“”‘’（）) is correct typography and deliberately left intact.
_AI_PUNCT = {
    "—": "-",  # — em dash
    "–": "-",  # – en dash
    "…": "...",  # … ellipsis
    " ": " ",  # non-breaking space
}
_AI_PUNCT_RE = re.compile("|".join(re.escape(k) for k in _AI_PUNCT))


def normalize_outbound_text(text: str) -> str:
    """Flatten LLM markdown/AI-tell punctuation for plain-text channels.

    Channels send literal text (no markdown rendering), so ``**bold**``,
    ``- bullets``, ``# headings`` and ``` `code` ``` arrive as visual
    clutter. This collapses that scaffolding to clean plain text while
    PRESERVING: fenced code blocks (verbatim), Chinese full-width
    punctuation (correct typography), and URLs/paths. Idempotent.
    """
    if not text:
        return text

    # Carve out fenced code blocks so their contents are never rewritten.
    fences: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        fences.append(m.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    work = _FENCE_RE.sub(_stash, text)

    work = _HEADING_RE.sub("", work)
    work = _BLOCKQUOTE_RE.sub("", work)
    work = _BULLET_RE.sub(r"\1· ", work)
    work = _INLINE_CODE_RE.sub(_unwrap_inline_code, work)
    # Emphasis can nest (``**_x_**``); run twice to unwrap both layers.
    for _ in range(2):
        work = _EMPHASIS_STAR_RE.sub(lambda m: m.group("inner"), work)
        work = _EMPHASIS_USCORE_RE.sub(lambda m: m.group("inner"), work)
    work = _AI_PUNCT_RE.sub(lambda m: _AI_PUNCT[m.group(0)], work)
    # Collapse 3+ blank lines left behind by stripped scaffolding.
    work = re.sub(r"\n{3,}", "\n\n", work)

    for i, fence in enumerate(fences):
        work = work.replace(f"\x00FENCE{i}\x00", fence)
    return work.strip()


# ---------------------------------------------------------------------------
# Sticker → vision-description placeholder
# ---------------------------------------------------------------------------


def sticker_placeholder(emoji: str | None = None, set_name: str | None = None) -> str:
    """Build a short text description for a sticker the agent can read.

    Stickers carry no body text, so without a placeholder a sticker-only
    message looks empty and is dropped. openclaw/hermes both surface the
    sticker's associated emoji (and set name when present) as a textual
    hint so the agent has *something* to react to. The returned string is
    written into :attr:`InboundEvent.text` for sticker-only messages.

    Examples::

        sticker_placeholder("😂")            -> "[sticker 😂]"
        sticker_placeholder("😂", "Cats")    -> "[sticker 😂 from \\"Cats\\"]"
        sticker_placeholder()                -> "[sticker]"
    """
    emoji_part = f" {emoji}" if emoji else ""
    set_part = f' from "{set_name}"' if set_name else ""
    return f"[sticker{emoji_part}{set_part}]"


# ---------------------------------------------------------------------------
# Inbound attribution prefix
# ---------------------------------------------------------------------------


def format_attribution_prefix(
    *,
    sender_name: str | None,
    reply_to_text: str | None,
    max_quote_chars: int = 280,
) -> str:
    """Render the sender / reply-to attribution block prepended to the
    agent-facing content.

    Mirrors openclaw's group-chat attribution: the agent sees who said a
    message and (when it's a reply) a truncated quote of the parent so a
    bare ``"+1"`` keeps its referent. Returns ``""`` when there's nothing
    to attribute, so callers can unconditionally prepend it.

    Format::

        [sender_name 回复 "<quoted parent ...>"]\\n<content>

    The reply quote is single-line-collapsed and truncated to
    ``max_quote_chars`` with an ellipsis so a long parent message can't
    blow the prompt budget.
    """
    parts: list[str] = []
    name = (sender_name or "").strip()
    if name:
        parts.append(name)
    quote = (reply_to_text or "").strip()
    if quote:
        # Collapse to a single line so the bracketed prefix stays compact.
        quote = " ".join(quote.split())
        if len(quote) > max_quote_chars:
            quote = quote[: max_quote_chars - 1].rstrip() + "…"
        parts.append(f'回复 "{quote}"')
    if not parts:
        return ""
    return f"[{' '.join(parts)}]"


# ---------------------------------------------------------------------------
# Album / media-group debounce
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _AlbumBuffer:
    """Mutable accumulator for one in-flight album (private to
    :class:`AlbumDebouncer`)."""

    first: InboundEvent[Any]
    attachments: list[Attachment]
    texts: list[str]
    last_seen: float


class AlbumDebouncer:
    """Buffer + merge media-group (album) items before dispatch.

    Telegram delivers an album as N separate ``Message`` updates that
    share a ``media_group_id`` — each carries one photo and at most one
    caption (usually only the first). Dispatching each as its own turn
    produces N duplicate agent turns and loses the "these belong
    together" relationship. openclaw debounces them: items are buffered
    keyed by ``media_group_id`` and flushed once no new item has arrived
    for ``window_secs``, merged into a single :class:`InboundEvent`
    carrying every attachment and the concatenated captions.

    The debouncer is transport-agnostic and side-effect-free: it does no
    timers of its own. The caller drives it from its inbound loop::

        async for ev in adapter.inbound():
            for merged in debouncer.feed(ev):
                await handle(merged)        # ready (non-album or window lapsed)
        for merged in debouncer.flush_all():  # drain on shutdown
            await handle(merged)

    A standalone (non-album) event passes straight through ``feed`` as a
    single-element list. Album members accumulate silently; ``feed``
    emits a previously-buffered album only once a *different* group's
    item arrives after the window, or the caller calls
    :meth:`flush_ready` / :meth:`flush_all`.
    """

    __slots__ = ("_buffers", "_clock", "_window_secs")

    def __init__(
        self,
        window_secs: float = 1.5,
        *,
        clock: Any = None,
    ) -> None:
        self._buffers: dict[str, _AlbumBuffer] = {}
        self._window_secs = window_secs
        # Injectable clock for deterministic tests (defaults to monotonic).
        self._clock = clock or time.monotonic

    def feed(self, event: InboundEvent[Any]) -> list[InboundEvent[Any]]:
        """Ingest one inbound event; return the events ready to dispatch.

        Non-album events return ``[event]`` immediately (plus any albums
        whose window has lapsed). Album members are buffered; the list
        returned contains only albums whose debounce window already
        expired by the time this newer item arrived.
        """
        now = self._clock()
        ready = self._flush_expired(now)
        group = event.media_group_id
        if not group:
            ready.append(event)
            return ready
        buf = self._buffers.get(group)
        if buf is None:
            self._buffers[group] = _AlbumBuffer(
                first=event,
                attachments=list(event.attachments),
                texts=[event.text] if event.text.strip() else [],
                last_seen=now,
            )
        else:
            buf.attachments.extend(event.attachments)
            if event.text.strip():
                buf.texts.append(event.text)
            buf.last_seen = now
        return ready

    def flush_ready(self) -> list[InboundEvent[Any]]:
        """Flush every album whose debounce window has lapsed *now*.

        The caller polls this (e.g. on a short timer) so an album that is
        never followed by another inbound item still gets dispatched.
        """
        return self._flush_expired(self._clock())

    def flush_all(self) -> list[InboundEvent[Any]]:
        """Drain every buffered album regardless of timing (shutdown)."""
        out = [self._merge(buf) for buf in self._buffers.values()]
        self._buffers.clear()
        return out

    def _flush_expired(self, now: float) -> list[InboundEvent[Any]]:
        expired = [
            group
            for group, buf in self._buffers.items()
            if now - buf.last_seen >= self._window_secs
        ]
        out: list[InboundEvent[Any]] = []
        for group in expired:
            out.append(self._merge(self._buffers.pop(group)))
        return out

    @staticmethod
    def _merge(buf: _AlbumBuffer) -> InboundEvent[Any]:
        """Collapse a buffered album into one :class:`InboundEvent`.

        The merged event keeps the first item's binding / message_id /
        timestamp / sender attribution, concatenates the captions, and
        carries every attachment from every member.
        """
        merged_text = "\n".join(buf.texts)
        first = buf.first
        return InboundEvent(
            channel=first.channel,
            binding=first.binding,
            text=merged_text,
            message_id=first.message_id,
            timestamp=first.timestamp,
            mentioned=first.mentioned,
            attachments=buf.attachments,
            payload=first.payload,
            user_id=first.user_id,
            sender_name=first.sender_name,
            reply_to_text=first.reply_to_text,
            media_group_id=first.media_group_id,
        )


__all__ = [
    "AlbumDebouncer",
    "Attachment",
    "AttachmentKind",
    "ChannelBinding",
    "ChannelError",
    "ConfigError",
    "InboundAdapter",
    "InboundEvent",
    "TransportError",
    "UnsupportedError",
    "UserId",
    "format_attribution_prefix",
    "normalize_outbound_text",
    "split_on_msg_break",
    "sticker_placeholder",
]
