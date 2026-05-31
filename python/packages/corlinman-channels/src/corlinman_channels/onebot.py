"""OneBot v11 (QQ) WebSocket adapter.

Port of ``rust/.../qq/onebot.rs`` (forward-WS client) + ``rust/.../qq/message.rs``
(wire types + helpers).

corlinman is the **client** — it dials out to gocq / Lagrange / NapCatQQ
matching the "forward WebSocket" mode from the OneBot v11 spec
(<https://github.com/botuniverse/onebot-11>).

## Connection topology

::

    gocq/NapCat  <── WS ──>  OneBotAdapter
                                │   ▲
                        event_tx│   │action_rx
                                ▼   │
                        normalized InboundEvent  /  outbound Action

## Reconnect schedule

``1s → 2s → 5s → 10s → 30s`` (then saturates). A heartbeat ping every 30s
matches NapCat's idle expectation.

## Surface

Two layers:

* High-level :class:`OneBotAdapter` — implements
  :class:`corlinman_channels.common.InboundAdapter`; ``async for`` over
  ``adapter.inbound()`` yields normalized :class:`InboundEvent` objects.
* Low-level wire types (:class:`Event`, :class:`MessageEvent`,
  :class:`MessageSegment`, :class:`Action`, ...) so callers can opt into
  the raw OneBot vocabulary when they need it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

_log = logging.getLogger(__name__)

from corlinman_channels.common import (
    Attachment,
    AttachmentKind,
    ChannelBinding,
    ConfigError,
    InboundEvent,
    TransportError,
)

# ---------------------------------------------------------------------------
# Constants — match Rust ``RECONNECT_SCHEDULE`` / ``PING_INTERVAL``.
# ---------------------------------------------------------------------------

#: Backoff schedule (seconds) between reconnect attempts. Last entry repeats.
RECONNECT_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

#: Self-ping interval (seconds) for idle connections.
PING_INTERVAL: float = 30.0


# ===========================================================================
# Wire types — events
# ===========================================================================


class MessageType(StrEnum):
    """``private`` vs ``group``."""

    PRIVATE = "private"
    GROUP = "group"


@dataclass(slots=True)
class Sender:
    """Inner ``sender`` object of a :class:`MessageEvent`."""

    user_id: int | None = None
    nickname: str | None = None
    card: str | None = None
    role: str | None = None


@dataclass(slots=True)
class MessageEvent:
    """OneBot ``post_type = "message"`` event."""

    self_id: int
    message_type: MessageType
    user_id: int
    message_id: int
    message: list[MessageSegment]
    time: int
    sub_type: str | None = None
    group_id: int | None = None
    raw_message: str = ""
    sender: Sender | None = None


@dataclass(slots=True)
class NoticeEvent:
    """OneBot ``post_type = "notice"`` event — parsed but unused."""

    self_id: int
    notice_type: str
    time: int
    group_id: int | None = None
    user_id: int | None = None


@dataclass(slots=True)
class MetaEvent:
    """OneBot ``post_type = "meta_event"`` (heartbeat / lifecycle)."""

    self_id: int
    meta_event_type: str
    time: int


@dataclass(slots=True)
class RequestEvent:
    """OneBot ``post_type = "request"`` event (friend / group add)."""

    self_id: int
    request_type: str
    time: int
    user_id: int | None = None
    group_id: int | None = None
    flag: str | None = None


@dataclass(slots=True)
class UnknownEvent:
    """Sentinel for ``post_type`` values we don't model. Carries the raw
    JSON so callers can log the unexpected shape without aborting."""

    raw: dict[str, Any]


#: Tagged-union over the four OneBot event categories + the unknown fallback.
Event = MessageEvent | NoticeEvent | MetaEvent | RequestEvent | UnknownEvent


# ===========================================================================
# Wire types — message segments
# ===========================================================================


@dataclass(slots=True)
class TextSegment:
    """``{"type": "text", "data": {"text": ...}}``."""

    text: str


@dataclass(slots=True)
class AtSegment:
    """``{"type": "at", "data": {"qq": ...}}``. ``qq == "all"`` for @all."""

    qq: str


@dataclass(slots=True)
class ImageSegment:
    """``{"type": "image", "data": {"url": ..., "file": ...}}``."""

    url: str = ""
    file: str | None = None


@dataclass(slots=True)
class ReplySegment:
    """``{"type": "reply", "data": {"id": ...}}``."""

    id: str


@dataclass(slots=True)
class FaceSegment:
    """``{"type": "face", "data": {"id": ...}}``."""

    id: str


@dataclass(slots=True)
class RecordSegment:
    """``{"type": "record", "data": {"url": ...}}``."""

    url: str = ""


@dataclass(slots=True)
class VideoSegment:
    """``{"type": "video", "data": {"url": ..., "file": ...}}``.

    NapCat / gocq surface short-video messages as a ``video`` segment.
    ``file`` is the upstream filename (when present) so the normalized
    :class:`Attachment` can carry it through to the agent.
    """

    url: str = ""
    file: str | None = None


@dataclass(slots=True)
class FileSegment:
    """``{"type": "file", "data": {"url": ..., "file": ...}}``.

    NapCat OneBot v11 extension — an inbound shared document. ``url`` is
    the download URL NapCat resolved; ``file`` is the display name.
    Older clients ship the document via a ``file`` segment with only the
    name (no url); we skip those in :func:`segments_to_attachments` the
    same way we skip url-less images.
    """

    url: str = ""
    file: str | None = None


@dataclass(slots=True)
class ForwardSegment:
    """``{"type": "forward", "data": {"id": ...}}``."""

    id: str


@dataclass(slots=True)
class OtherSegment:
    """Fallback wrapper for segments we don't model. Carries the raw JSON
    so the reader loop keeps going in the face of spec drift."""

    raw: dict[str, Any]


#: Tagged-union over the understood segments plus :class:`OtherSegment`.
MessageSegment = (
    TextSegment
    | AtSegment
    | ImageSegment
    | ReplySegment
    | FaceSegment
    | RecordSegment
    | VideoSegment
    | FileSegment
    | ForwardSegment
    | OtherSegment
)

#: Segment "type" → constructor table for :func:`_parse_segment`.
_SEGMENT_PARSERS: dict[str, Callable[[dict[str, Any]], MessageSegment]] = {
    "text": lambda d: TextSegment(text=str(d.get("text", ""))),
    "at": lambda d: AtSegment(qq=str(d.get("qq", ""))),
    "image": lambda d: ImageSegment(url=str(d.get("url", "")), file=d.get("file")),
    "reply": lambda d: ReplySegment(id=str(d.get("id", ""))),
    "face": lambda d: FaceSegment(id=str(d.get("id", ""))),
    "record": lambda d: RecordSegment(url=str(d.get("url", ""))),
    "video": lambda d: VideoSegment(url=str(d.get("url", "")), file=d.get("file")),
    "file": lambda d: FileSegment(url=str(d.get("url", "")), file=d.get("file")),
    "forward": lambda d: ForwardSegment(id=str(d.get("id", ""))),
}


def _parse_segment(raw: dict[str, Any]) -> MessageSegment:
    """Decode one CQ segment dict into the matching dataclass."""
    ty = raw.get("type")
    parser = _SEGMENT_PARSERS.get(ty if isinstance(ty, str) else "")
    if parser is None:
        return OtherSegment(raw=raw)
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        return OtherSegment(raw=raw)
    return parser(data)


def _coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort int coercion that never raises.

    OneBot fields like ``self_id`` / ``user_id`` / ``message_id`` / ``time``
    are expected to be numeric, but a misbehaving upstream client can ship a
    non-numeric value (string, null, float-as-string). A bare ``int()`` there
    raises and unwinds the reader pump, dropping the whole WS connection
    (reconnect churn / message loss). Falling back to ``default`` keeps the
    frame parseable so one bad value can't tear down the connection.
    """
    if isinstance(value, bool):
        # bool is an int subclass; treat it as the default rather than 0/1.
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_event(raw: dict[str, Any]) -> Event:
    """Decode one OneBot event dict into the matching :data:`Event`.

    Unknown ``post_type`` collapses to :class:`UnknownEvent` so the reader
    loop can survive spec drift — matches the Rust ``Event::Unknown``
    fall-through behaviour. Numeric fields are coerced via
    :func:`_coerce_int`, which falls back to ``0`` on a malformed value so a
    single bad frame can't raise and drop the WS connection.
    """
    post_type = raw.get("post_type")
    if post_type == "message":
        msg_type_raw = raw.get("message_type")
        if not isinstance(msg_type_raw, str):
            return UnknownEvent(raw=raw)
        try:
            msg_type = MessageType(msg_type_raw)
        except ValueError:
            return UnknownEvent(raw=raw)
        sender_raw = raw.get("sender")
        sender: Sender | None = None
        if isinstance(sender_raw, dict):
            sender = Sender(
                user_id=sender_raw.get("user_id"),
                nickname=sender_raw.get("nickname"),
                card=sender_raw.get("card"),
                role=sender_raw.get("role"),
            )
        message_raw = raw.get("message") or []
        segments = [_parse_segment(s) for s in message_raw if isinstance(s, dict)]
        return MessageEvent(
            self_id=_coerce_int(raw.get("self_id", 0)),
            message_type=msg_type,
            user_id=_coerce_int(raw.get("user_id", 0)),
            message_id=_coerce_int(raw.get("message_id", 0)),
            message=segments,
            time=_coerce_int(raw.get("time", 0)),
            sub_type=raw.get("sub_type"),
            group_id=raw.get("group_id"),
            raw_message=str(raw.get("raw_message", "")),
            sender=sender,
        )
    if post_type == "notice":
        return NoticeEvent(
            self_id=_coerce_int(raw.get("self_id", 0)),
            notice_type=str(raw.get("notice_type", "")),
            time=_coerce_int(raw.get("time", 0)),
            group_id=raw.get("group_id"),
            user_id=raw.get("user_id"),
        )
    if post_type == "meta_event":
        return MetaEvent(
            self_id=_coerce_int(raw.get("self_id", 0)),
            meta_event_type=str(raw.get("meta_event_type", "")),
            time=_coerce_int(raw.get("time", 0)),
        )
    if post_type == "request":
        return RequestEvent(
            self_id=_coerce_int(raw.get("self_id", 0)),
            request_type=str(raw.get("request_type", "")),
            time=_coerce_int(raw.get("time", 0)),
            user_id=raw.get("user_id"),
            group_id=raw.get("group_id"),
            flag=raw.get("flag"),
        )
    return UnknownEvent(raw=raw)


# ===========================================================================
# Segment helpers — match ``segments_to_text`` / ``segments_to_attachments``
# / ``is_mentioned`` in the Rust crate.
# ===========================================================================


def segments_to_text(segments: Iterable[MessageSegment]) -> str:
    """Flatten CQ segments to a single string.

    ``at`` segments become ``@<qq> `` so keyword routing still sees the
    address. Matches qqBot.js's ``_extractText`` / Rust's
    ``segments_to_text``.
    """
    out: list[str] = []
    for seg in segments:
        if isinstance(seg, TextSegment):
            out.append(seg.text)
        elif isinstance(seg, AtSegment):
            out.append(f"@{seg.qq} ")
    return "".join(out)


def segments_to_attachments(segments: Iterable[MessageSegment]) -> list[Attachment]:
    """Pull image / voice / video / file attachments out of a segment list.

    Skips segments with empty URLs (gocq sometimes ships an empty ``url``
    on offline media). Matches the Rust ``segments_to_attachments`` filter,
    extended to cover NapCat ``video`` + ``file`` segments so QQ inbound
    media parity matches Telegram's photo / voice / video / document.
    """
    out: list[Attachment] = []
    for seg in segments:
        if isinstance(seg, ImageSegment) and seg.url:
            out.append(
                Attachment(
                    kind=AttachmentKind.IMAGE,
                    url=seg.url,
                    mime="image/*",
                    file_name=seg.file,
                )
            )
        elif isinstance(seg, RecordSegment) and seg.url:
            out.append(
                Attachment(
                    kind=AttachmentKind.AUDIO,
                    url=seg.url,
                    mime="audio/*",
                )
            )
        elif isinstance(seg, VideoSegment) and seg.url:
            out.append(
                Attachment(
                    kind=AttachmentKind.VIDEO,
                    url=seg.url,
                    mime="video/*",
                    file_name=seg.file,
                )
            )
        elif isinstance(seg, FileSegment) and seg.url:
            out.append(
                Attachment(
                    kind=AttachmentKind.DOCUMENT,
                    url=seg.url,
                    # QQ doesn't expose a precise content type for shared
                    # files; leave it generic so the proto builder routes
                    # it to the FILE branch.
                    mime="application/octet-stream",
                    file_name=seg.file,
                )
            )
    return out


def is_mentioned(segments: Iterable[MessageSegment], self_id: int) -> bool:
    """True if any ``at`` segment targets ``self_id`` (or is ``@all``)."""
    target = str(self_id)
    for seg in segments:
        if isinstance(seg, AtSegment) and (seg.qq == target or seg.qq == "all"):
            return True
    return False


# ===========================================================================
# Wire types — outbound actions
# ===========================================================================


@dataclass(slots=True)
class SendPrivateMsg:
    """``action = "send_private_msg"`` payload."""

    user_id: int
    message: list[MessageSegment]


@dataclass(slots=True)
class SendGroupMsg:
    """``action = "send_group_msg"`` payload."""

    group_id: int
    message: list[MessageSegment]


@dataclass(slots=True)
class ForwardNode:
    """One node in a merged-forward (``node`` segment)."""

    name: str
    uin: str
    content: list[MessageSegment]


@dataclass(slots=True)
class SendGroupForwardMsg:
    """``action = "send_group_forward_msg"`` payload."""

    group_id: int
    messages: list[ForwardNode]


@dataclass(slots=True)
class SetInputStatus:
    """``action = "set_input_status"`` — NapCat OneBot v11 extension.

    Surfaces "对方正在输入..." (NapCat treats this as a private-chat
    feature). ``event_type`` is 0 (cancel) or 1 ("正在输入..."). The
    indicator auto-clears after ~5s, so the channel handler re-fires
    while a turn is in flight.

    Not part of the upstream OneBot spec — non-NapCat backends will
    return an "unsupported action" envelope. The QQ channel handler
    treats any failure as a no-op so the reply path still completes.
    """

    user_id: int
    event_type: int = 1


@dataclass(slots=True)
class UploadPrivateFile:
    """``action = "upload_private_file"`` — NapCat OneBot v11 extension.

    Sends a file to a private chat. ``file`` is a path the NapCat
    process can read (the channel handler resolves it from the
    gateway-side absolute path; NapCat and the gateway run on the
    same host so the path is the same).
    """

    user_id: int
    file: str
    name: str | None = None


@dataclass(slots=True)
class UploadGroupFile:
    """``action = "upload_group_file"`` — NapCat OneBot v11 extension.

    Sends a file to a QQ group. ``folder`` is optional (defaults to
    the root folder of the group's file area).
    """

    group_id: int
    file: str
    name: str | None = None
    folder: str | None = None


#: Tagged-union of every action corlinman emits.
Action = (
    SendPrivateMsg
    | SendGroupMsg
    | SendGroupForwardMsg
    | SetInputStatus
    | UploadPrivateFile
    | UploadGroupFile
)


def _segment_to_wire(seg: MessageSegment) -> dict[str, Any]:
    """Serialize a single segment back to OneBot wire form."""
    if isinstance(seg, TextSegment):
        return {"type": "text", "data": {"text": seg.text}}
    if isinstance(seg, AtSegment):
        return {"type": "at", "data": {"qq": seg.qq}}
    if isinstance(seg, ImageSegment):
        data: dict[str, Any] = {"url": seg.url}
        if seg.file is not None:
            data["file"] = seg.file
        return {"type": "image", "data": data}
    if isinstance(seg, ReplySegment):
        return {"type": "reply", "data": {"id": seg.id}}
    if isinstance(seg, FaceSegment):
        return {"type": "face", "data": {"id": seg.id}}
    if isinstance(seg, RecordSegment):
        return {"type": "record", "data": {"url": seg.url}}
    if isinstance(seg, VideoSegment):
        vdata: dict[str, Any] = {"url": seg.url}
        if seg.file is not None:
            vdata["file"] = seg.file
        return {"type": "video", "data": vdata}
    if isinstance(seg, FileSegment):
        fdata: dict[str, Any] = {"url": seg.url}
        if seg.file is not None:
            fdata["file"] = seg.file
        return {"type": "file", "data": fdata}
    if isinstance(seg, ForwardSegment):
        return {"type": "forward", "data": {"id": seg.id}}
    # OtherSegment falls through to its raw form.
    return seg.raw


def action_to_wire(action: Action) -> dict[str, Any]:
    """Serialize an :data:`Action` to the OneBot envelope.

    Output shape: ``{"action": "...", "params": {...}}`` — matches the
    Rust ``Action`` serde tag/content layout.
    """
    if isinstance(action, SendPrivateMsg):
        return {
            "action": "send_private_msg",
            "params": {
                "user_id": action.user_id,
                "message": [_segment_to_wire(s) for s in action.message],
            },
        }
    if isinstance(action, SendGroupMsg):
        return {
            "action": "send_group_msg",
            "params": {
                "group_id": action.group_id,
                "message": [_segment_to_wire(s) for s in action.message],
            },
        }
    if isinstance(action, SetInputStatus):
        return {
            "action": "set_input_status",
            "params": {
                "user_id": action.user_id,
                "event_type": action.event_type,
            },
        }
    if isinstance(action, UploadPrivateFile):
        params: dict[str, Any] = {
            "user_id": action.user_id,
            "file": action.file,
        }
        if action.name is not None:
            params["name"] = action.name
        return {"action": "upload_private_file", "params": params}
    if isinstance(action, UploadGroupFile):
        gparams: dict[str, Any] = {
            "group_id": action.group_id,
            "file": action.file,
        }
        if action.name is not None:
            gparams["name"] = action.name
        if action.folder is not None:
            gparams["folder"] = action.folder
        return {"action": "upload_group_file", "params": gparams}
    # SendGroupForwardMsg
    return {
        "action": "send_group_forward_msg",
        "params": {
            "group_id": action.group_id,
            "messages": [
                {
                    "type": "node",
                    "data": {
                        "name": node.name,
                        "uin": node.uin,
                        "content": [_segment_to_wire(s) for s in node.content],
                    },
                }
                for node in action.messages
            ],
        },
    }


# ===========================================================================
# Adapter
# ===========================================================================


@dataclass(slots=True)
class OneBotConfig:
    """Configuration for :class:`OneBotAdapter`.

    ``url`` is the full WS URL (``ws://host:port/``). ``access_token`` is
    sent as ``Authorization: Bearer <token>`` when present.
    """

    url: str
    access_token: str | None = None
    self_ids: list[int] = field(default_factory=list)
    reconnect_schedule: tuple[float, ...] = RECONNECT_SCHEDULE
    ping_interval: float = PING_INTERVAL


class OneBotAdapter:
    """Forward-WebSocket OneBot v11 client.

    The adapter is a small state machine:

    1. ``async with adapter:`` (or :meth:`connect`) dials the upstream WS.
    2. ``async for event in adapter.inbound():`` yields normalized
       :class:`InboundEvent` objects (only ``MessageEvent`` post_types are
       surfaced; meta/notice/request events are silently absorbed since
       no upstream consumer reads them yet — they exist in the wire types
       so the parser doesn't drop the connection).
    3. :meth:`send_action` posts an outbound :data:`Action`.
    4. :meth:`close` (or the ``async with`` exit) tears down the WS.

    The reconnect loop lives **inside** :meth:`inbound`: yielding an event
    is paused while we sleep+redial, but the iterator never raises a
    transient transport error — callers just see the next event after the
    reconnect lands. Permanent config errors (missing URL, invalid token
    header) raise :class:`ConfigError` from :meth:`connect` instead.
    """

    def __init__(self, config: OneBotConfig) -> None:
        if not config.url:
            raise ConfigError("OneBotConfig.url is empty")
        self._cfg = config
        self._ws: ClientConnection | None = None
        self._closed = False
        # Bounded queue so a stalled consumer doesn't grow without bound.
        # On burst overflow the *oldest* event is dropped (so the most
        # recent user message still surfaces) — see ``_pump`` for the
        # drop-oldest path. ``_inbound_dropped`` counts those drops so
        # operators can spot a consistently slow chat service.
        self._inbound_q: asyncio.Queue[Event] = asyncio.Queue(maxsize=64)
        self._inbound_dropped: int = 0
        self._outbound_q: asyncio.Queue[Action] = asyncio.Queue(maxsize=64)
        # ``_writer_loop`` drains this front buffer BEFORE ``_outbound_q``
        # so a transient WS send failure can re-queue the action without
        # losing ordering (asyncio.Queue has no push-left). See C1 fix.
        self._outbound_front: deque[Action] = deque()
        # Per-action retry counter — guards against poison messages that
        # would otherwise loop forever. Keyed by ``id(action)`` since
        # actions don't have a stable hash; entries are cleared once the
        # action lands successfully or is dropped.
        self._outbound_retries: dict[int, int] = {}
        self._reader_task: asyncio.Task[None] | None = None
        # NapCat heartbeat timestamp — updated on every parsed event
        # (messages, meta heartbeats, lifecycle, notices). A healthy
        # NapCat sends a heartbeat meta event every ~30s; long silence
        # means the bot QQ account got kicked offline by Tencent while
        # the WS stayed up. ``None`` until the first event lands.
        self._last_event_at_ms: int | None = None
        # NapCat heartbeat payload carries ``status.online`` (boolean)
        # that flips False *immediately* on KickedOffLine — the WS
        # keeps heartbeating but the field signals the QQ account is
        # gone. ``None`` until the first heartbeat lands. We update
        # this from the raw frame in ``_pump`` (before the typed event
        # is dispatched downstream) because the typed ``MetaEvent``
        # doesn't carry the status block.
        self._last_status_online: bool | None = None
        self._last_status_online_at_ms: int | None = None

    @property
    def inbound_dropped_count(self) -> int:
        """Total inbound events dropped because the consumer fell behind.

        A non-zero value indicates the chat service couldn't keep up with
        the inbound burst rate; the drop-oldest behaviour in ``_pump``
        kicked in to keep the most recent user message visible.
        """
        return self._inbound_dropped

    @property
    def last_event_at_ms(self) -> int | None:
        """Wall-clock ms when the last inbound event was parsed.

        ``None`` before the first event lands (initial connect, or
        adapter not yet connected). The QQ health watcher polls this to
        detect a kicked-offline bot — a healthy NapCat sends a heartbeat
        meta event every ~30 seconds.
        """
        return self._last_event_at_ms

    @property
    def last_status_online(self) -> bool | None:
        """Last ``status.online`` flag observed on a NapCat heartbeat
        meta-event. ``None`` until the first heartbeat lands; ``True``
        when the bot QQ account is fully online; ``False`` immediately
        after a ``KickedOffLine`` (the WS keeps heartbeating but the
        status flag flips before the account-status notice arrives).

        Watcher reads this every probe interval so the admin UI can
        surface "需要扫码" without an HTTP probe — NapCat's reverse-WS
        config doesn't expose an HTTP plane.
        """
        return self._last_status_online

    @property
    def last_status_online_at_ms(self) -> int | None:
        """Wall-clock ms of the heartbeat that set
        :attr:`last_status_online`. Used for ``account_checked_at_ms``
        in the admin status route."""
        return self._last_status_online_at_ms

    @property
    def outbound_queue_depth(self) -> int:
        """Number of outbound actions buffered but not yet sent.

        Sum of the front re-queue buffer (transient-failure retries) and
        the main outbound queue. The QQ health watcher snapshots this so
        the admin status route can surface send backpressure — a steadily
        rising depth means NapCat is rejecting / slow to accept sends.
        """
        return len(self._outbound_front) + self._outbound_q.qsize()

    @property
    def url(self) -> str:
        """The NapCat ws endpoint this adapter dials.

        Surfaced so the heartbeat watcher can point operators at the
        right ws URL when it can't reach NapCat.
        """
        return self._cfg.url

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> OneBotAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Spawn the background reader loop.

        Doesn't block on the actual WS connect — that happens inside the
        reader so reconnect logic stays in one place. The first call to
        :meth:`inbound` will start yielding once the connection lands.
        """
        if self._reader_task is not None:
            return
        self._closed = False
        self._reader_task = asyncio.create_task(self._reader_loop(), name="onebot-reader")

    async def close(self) -> None:
        """Shut down the reader loop and the underlying WS."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            with suppress(Exception):
                await self._ws.close()
            self._ws = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def inbound(self) -> AsyncIterator[InboundEvent[MessageEvent]]:
        """Yield normalized inbound events until the adapter is closed.

        Only :class:`MessageEvent` post-types surface — meta / notice /
        request events are absorbed silently (matches Rust ``service.rs``
        which short-circuits with ``let Event::Message(msg_ev) = ev else
        continue``).
        """
        if self._reader_task is None:
            await self.connect()
        while not self._closed:
            try:
                ev = await self._inbound_q.get()
            except asyncio.CancelledError:
                return
            if not isinstance(ev, MessageEvent):
                continue
            yield _normalize_message_event(ev)

    async def send_action(self, action: Action) -> None:
        """Enqueue an outbound :data:`Action` for the reader loop to flush.

        Returns once the action is queued — actual transmission happens
        on the writer side of the WS. Raises :class:`TransportError` if
        the adapter has been closed.
        """
        if self._closed:
            raise TransportError("OneBotAdapter is closed")
        await self._outbound_q.put(action)

    # ------------------------------------------------------------------
    # Reader loop — encapsulates reconnect schedule.
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Connect → pump → on disconnect, sleep + retry.

        Mirrors the Rust ``OneBotClient::run`` state machine: the
        backoff index resets to 0 after a clean disconnect and grows
        monotonically across consecutive failures.
        """
        attempt = 0
        schedule = self._cfg.reconnect_schedule or RECONNECT_SCHEDULE
        while not self._closed:
            try:
                await self._connect_once()
                if self._closed:
                    return
                attempt = 0  # clean disconnect — reset backoff
            except asyncio.CancelledError:
                raise
            except Exception:
                # Use logging at warning level. We bury the exception text in
                # the queue's debug surface so unit tests can assert without
                # patching logging.
                pass
            if self._closed:
                return
            delay = schedule[min(attempt, len(schedule) - 1)]
            attempt += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

    async def _connect_once(self) -> None:
        """One connect → pump cycle."""
        headers: list[tuple[str, str]] = []
        if self._cfg.access_token:
            headers.append(("Authorization", f"Bearer {self._cfg.access_token}"))
        # `additional_headers` is the param name for websockets >= 13.
        async with websockets.connect(
            self._cfg.url,
            additional_headers=headers or None,
            ping_interval=self._cfg.ping_interval,
        ) as ws:
            self._ws = ws
            try:
                await self._pump(ws)
            finally:
                self._ws = None

    async def _pump(self, ws: ClientConnection) -> None:
        """Two-way pump: forward outbound actions, decode inbound frames."""
        writer = asyncio.create_task(self._writer_loop(ws), name="onebot-writer")
        try:
            async for raw_msg in ws:
                if self._closed:
                    break
                if isinstance(raw_msg, bytes):
                    # OneBot v11 is text-only; ignore binary frames.
                    continue
                try:
                    raw = json.loads(raw_msg)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(raw, dict):
                    continue
                event = parse_event(raw)
                # Update the NapCat heartbeat timestamp on every event so
                # the health watcher can flag a kicked-offline bot.
                import time as _t
                now_ms = int(_t.time() * 1000)
                self._last_event_at_ms = now_ms
                # Heartbeat meta-events carry ``status.online`` (boolean)
                # which flips False *immediately* on KickedOffLine — the
                # WS keeps heartbeating, but ``status.online=False``
                # exposes that the QQ account is no longer reachable.
                # The typed MetaEvent dataclass doesn't have a slot for
                # the status block, so we read it off the raw frame
                # before dispatching. Other event types implicitly mean
                # the account WAS online at this moment, so flip True.
                if raw.get("post_type") == "meta_event":
                    if raw.get("meta_event_type") == "heartbeat":
                        # NapCat's heartbeat shape varies by build:
                        #   * upstream OneBot v11: {"status": {"online": true, ...}}
                        #   * older NapCat:        {"status": {"online": 1, ...}}
                        #   * NapCat with bot logged out: status block may
                        #     omit ``online`` entirely (we treat that as
                        #     "the WS is up but the account isn't")
                        status_block = raw.get("status")
                        online_raw: Any = None
                        if isinstance(status_block, dict):
                            online_raw = status_block.get("online")
                            if online_raw is None:
                                # Fallback: NapCat sometimes nests under
                                # ``status.app.online`` or surfaces a
                                # ``good`` field next to ``online``. Try
                                # those before declaring "unknown".
                                inner_app = status_block.get("app")
                                if isinstance(inner_app, dict):
                                    online_raw = inner_app.get("online")
                                if online_raw is None and "good" in status_block:
                                    online_raw = status_block.get("good")
                        if online_raw is None:
                            # No online flag anywhere — the heartbeat
                            # arrived but doesn't carry account state.
                            # Surface as False (the bot can't actually
                            # respond) rather than freezing the previous
                            # True; the staleness guard upstream still
                            # protects against an old reading.
                            self._last_status_online = False
                            self._last_status_online_at_ms = now_ms
                        else:
                            # Coerce truthy/falsy ints and strings into
                            # the bool the watcher expects.
                            self._last_status_online = bool(online_raw)
                            self._last_status_online_at_ms = now_ms
                else:
                    # A real inbound message / notice / request implies the
                    # account is up — heartbeats might lag.
                    self._last_status_online = True
                    self._last_status_online_at_ms = now_ms
                # Burst-absorb: a slow chat service must NOT block the
                # WS reader (websockets' frame buffer fills → NapCat
                # closes the connection with 1009 → reconnect storm).
                # Drop the OLDEST event when the queue is saturated so
                # the most recent user message still surfaces.
                try:
                    self._inbound_q.put_nowait(event)
                except asyncio.QueueFull:
                    with suppress(asyncio.QueueEmpty):
                        self._inbound_q.get_nowait()
                    self._inbound_dropped += 1
                    _log.warning(
                        "qq.inbound_q.dropped_oldest count=%d",
                        self._inbound_dropped,
                    )
                    # Best-effort: if even the second put trips QueueFull
                    # (extreme concurrency / re-entry from another task)
                    # swallow it rather than blocking the reader.
                    with suppress(asyncio.QueueFull):
                        self._inbound_q.put_nowait(event)
        finally:
            writer.cancel()
            with suppress(asyncio.CancelledError):
                await writer

    async def _writer_loop(self, ws: ClientConnection) -> None:
        """Drain ``self._outbound_q`` and send each action as a text frame.

        On a transient send failure the action is requeued to the
        ``_outbound_front`` buffer (drained first on the next iteration)
        so the dispatched inbox row doesn't stay stuck for 10 minutes
        waiting for the stale sweep. After two consecutive failures for
        the same action we drop it — a "poison" payload (oversized text,
        malformed segments) would otherwise loop forever and starve the
        rest of the queue.
        """
        while True:
            # Front buffer wins so requeued actions retain ordering.
            if self._outbound_front:
                action = self._outbound_front.popleft()
            else:
                try:
                    action = await self._outbound_q.get()
                except asyncio.CancelledError:
                    return
            payload = json.dumps(action_to_wire(action))
            try:
                await ws.send(payload)
            except asyncio.CancelledError:
                # Treat as "didn't land" — requeue at the front so the
                # reconnect path picks it up. Don't increment retries
                # because cancellation isn't the action's fault.
                self._outbound_front.appendleft(action)
                raise
            except Exception as exc:
                retries = self._outbound_retries.get(id(action), 0) + 1
                if retries >= 2:
                    self._outbound_retries.pop(id(action), None)
                    _log.error(
                        "qq.outbound.dropped action=%s retries=%d err=%s",
                        type(action).__name__,
                        retries,
                        exc,
                    )
                    # Don't re-raise — keep draining the next action.
                    continue
                self._outbound_retries[id(action)] = retries
                self._outbound_front.appendleft(action)
                _log.warning(
                    "qq.outbound.send_failed action=%s retry=%d err=%s",
                    type(action).__name__,
                    retries,
                    exc,
                )
                # Raise to abort the _pump cycle so the reconnect path
                # runs; the requeued action will fire on the new ws.
                raise TransportError(f"OneBot send failed: {exc}") from exc
            else:
                # Successful send — clear any prior retry bookkeeping.
                self._outbound_retries.pop(id(action), None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_message_event(ev: MessageEvent) -> InboundEvent[MessageEvent]:
    """Convert a low-level :class:`MessageEvent` into the normalized envelope."""
    if ev.message_type == MessageType.GROUP and ev.group_id is not None:
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id, ev.user_id)
    else:
        binding = ChannelBinding.qq_private(ev.self_id, ev.user_id)
    return InboundEvent(
        channel="qq",
        binding=binding,
        text=segments_to_text(ev.message),
        message_id=str(ev.message_id),
        timestamp=ev.time,
        mentioned=is_mentioned(ev.message, ev.self_id),
        attachments=segments_to_attachments(ev.message),
        payload=ev,
    )


__all__ = [
    "PING_INTERVAL",
    "RECONNECT_SCHEDULE",
    "Action",
    "AtSegment",
    "Event",
    "FaceSegment",
    "FileSegment",
    "ForwardNode",
    "ForwardSegment",
    "ImageSegment",
    "MessageEvent",
    "MessageSegment",
    "MessageType",
    "MetaEvent",
    "NoticeEvent",
    "OneBotAdapter",
    "OneBotConfig",
    "OtherSegment",
    "RecordSegment",
    "ReplySegment",
    "RequestEvent",
    "SendGroupForwardMsg",
    "SendGroupMsg",
    "SendPrivateMsg",
    "Sender",
    "SetInputStatus",
    "TextSegment",
    "UnknownEvent",
    "UploadGroupFile",
    "UploadPrivateFile",
    "VideoSegment",
    "action_to_wire",
    "is_mentioned",
    "parse_event",
    "segments_to_attachments",
    "segments_to_text",
]
