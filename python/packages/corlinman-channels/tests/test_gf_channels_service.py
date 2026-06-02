"""Gap-fill (lane-channels) — service + router integration.

Covers the inbound attribution prefix injected into the agent-facing
request, the album merge-debounce async wrapper, and the router
unknown-command notice carried onto the routed request.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from corlinman_channels.common import (
    Attachment,
    AttachmentKind,
    ChannelBinding,
    InboundEvent,
)
from corlinman_channels.onebot import MessageEvent, MessageType
from corlinman_channels.router import ChannelRouter
from corlinman_channels.service import (
    _attribution_prefix,
    _build_text_channel_request,
    _debounce_albums,
)


def _binding() -> ChannelBinding:
    return ChannelBinding("telegram", "bot", "chat", "user")


# ---------------------------------------------------------------------------
# Attribution prefix in the agent-facing request
# ---------------------------------------------------------------------------


def test_request_prefixes_sender_and_reply() -> None:
    ev = InboundEvent(
        channel="telegram",
        binding=_binding(),
        text="+1",
        sender_name="Alice",
        reply_to_text="the original proposal",
    )
    assert _attribution_prefix(ev) == '[Alice 回复 "the original proposal"]'
    req = _build_text_channel_request(ev, "model-x")
    assert req.messages[0].content == '[Alice 回复 "the original proposal"]\n+1'


def test_request_no_attribution_is_byte_identical() -> None:
    ev = InboundEvent(channel="telegram", binding=_binding(), text="plain text")
    req = _build_text_channel_request(ev, "model-x")
    assert req.messages[0].content == "plain text"


def test_request_carries_attachments() -> None:
    ev = InboundEvent(
        channel="telegram",
        binding=_binding(),
        text="see photo",
        attachments=[Attachment(kind=AttachmentKind.IMAGE, url="https://x/y.png")],
        sender_name="Bob",
    )
    req = _build_text_channel_request(ev, "m")
    assert req.messages[0].content == "[Bob]\nsee photo"
    assert len(req.attachments) == 1


# ---------------------------------------------------------------------------
# Album merge-debounce wrapper
# ---------------------------------------------------------------------------


def _ev(mid: int, *, group: str | None = None, text: str = "", n_att: int = 0) -> InboundEvent:
    atts = [Attachment(kind=AttachmentKind.IMAGE, url=f"u{mid}-{i}") for i in range(n_att)]
    return InboundEvent(
        channel="telegram",
        binding=_binding(),
        text=text,
        message_id=str(mid),
        attachments=atts,
        media_group_id=group,
    )


async def _drive(items: list[InboundEvent], window: float = 0.05) -> list[InboundEvent]:
    async def gen() -> AsyncIterator[InboundEvent]:
        for it in items:
            yield it
            await asyncio.sleep(0)

    cancel = asyncio.Event()
    out: list[InboundEvent] = []
    async for ev in _debounce_albums(gen(), cancel, window_secs=window):
        out.append(ev)
    return out


@pytest.mark.asyncio
async def test_debounce_passes_standalone_through() -> None:
    out = await _drive([_ev(1, text="solo")])
    assert [e.message_id for e in out] == ["1"]


@pytest.mark.asyncio
async def test_debounce_merges_album_into_one_event() -> None:
    items = [
        _ev(2, group="g", text="caption", n_att=1),
        _ev(3, group="g", n_att=1),
        _ev(4, group="g", n_att=1),
    ]
    out = await _drive(items)
    assert len(out) == 1
    merged = out[0]
    assert merged.message_id == "2"
    assert len(merged.attachments) == 3
    assert merged.text == "caption"


@pytest.mark.asyncio
async def test_debounce_interleaves_standalone_and_album() -> None:
    items = [
        _ev(1, text="first"),
        _ev(2, group="g", n_att=1),
        _ev(3, group="g", n_att=1),
    ]
    out = await _drive(items)
    ids = sorted(e.message_id for e in out)
    assert ids == ["1", "2"]  # standalone + one merged album


@pytest.mark.asyncio
async def test_debounce_survives_idle_window_without_cancel() -> None:
    """Regression: an idle window must NOT end the stream.

    The debouncer used to cancel the in-flight inbound read on every
    timeout tick, raising ``CancelledError`` inside ``inbound()`` whose
    ``except CancelledError: return`` ended the generator for good. That
    silently killed the whole Telegram channel one debounce window after
    boot (``channel_exited`` ~1.5s in, no message ever dispatched). With
    ``cancel`` unset the stream must stay open across many idle windows.
    """
    async def gen() -> AsyncIterator[InboundEvent]:
        yield _ev(1, text="solo")
        await asyncio.Event().wait()  # block: more messages may arrive later

    cancel = asyncio.Event()  # never set — simulates a healthy, running gateway
    agen = _debounce_albums(gen(), cancel, window_secs=0.02)
    try:
        first = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
        assert first.message_id == "1"
        # Sit idle for ~10 windows. Pre-fix this raised StopAsyncIteration;
        # post-fix the read stays pending, so our wait_for times out — the
        # healthy outcome that proves the stream is still alive.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(agen.__anext__(), timeout=0.2)
    finally:
        cancel.set()
        await agen.aclose()


# ---------------------------------------------------------------------------
# Router unknown-command notice
# ---------------------------------------------------------------------------


def _qq_event(text: str) -> MessageEvent:
    return MessageEvent(
        self_id=100,
        message_type=MessageType.PRIVATE,
        message_id=7,
        user_id=55,
        message=[],
        time=1,
        raw_message=text,
    )


def test_router_sets_unknown_command_notice() -> None:
    router = ChannelRouter()
    req = router.dispatch(_qq_event("/definitelynotacommand"))
    assert req is not None
    assert req.unknown_command_notice is not None
    assert "/definitelynotacommand" in req.unknown_command_notice


def test_router_no_notice_for_plain_prose() -> None:
    router = ChannelRouter()
    req = router.dispatch(_qq_event("hello there"))
    assert req is not None
    assert req.unknown_command_notice is None


def test_router_no_notice_for_registered_command() -> None:
    router = ChannelRouter()
    req = router.dispatch(_qq_event("/help"))
    assert req is not None
    assert req.unknown_command_notice is None
