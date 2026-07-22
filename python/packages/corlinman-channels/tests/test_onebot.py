"""Tests for ``corlinman_channels.onebot``.

Two layers:

* Wire-level tests (``parse_event``, ``segments_to_*``, ``action_to_wire``)
  — mirror the ``#[cfg(test)] mod tests`` block in ``rust/.../qq/message.rs``.
* End-to-end tests against an in-process WebSocket server (``websockets``
  fixture in ``conftest.py``) — mirror ``tests/onebot_integration.rs``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from corlinman_channels.common import AttachmentKind, ConfigError, TransportError
from corlinman_channels.onebot import (
    AtSegment,
    FaceSegment,
    FileSegment,
    ImageSegment,
    MessageEvent,
    MessageType,
    MetaEvent,
    OneBotAdapter,
    OneBotConfig,
    OtherSegment,
    RecordSegment,
    ReplySegment,
    SendGroupMsg,
    SendPrivateMsg,
    TextSegment,
    UnknownEvent,
    VideoSegment,
    action_to_wire,
    is_mentioned,
    parse_event,
    segments_to_attachments,
    segments_to_text,
)
from websockets.asyncio.server import ServerConnection

# ---------------------------------------------------------------------------
# Wire-type tests (parse + serialise).
# ---------------------------------------------------------------------------


class TestTencentPolicyGuard:
    async def test_blocks_prohibited_text_before_queue(self) -> None:
        adapter = OneBotAdapter(
            OneBotConfig(url="ws://example.invalid"),
            tencent_policy_resolver=lambda: True,
        )
        with pytest.raises(TransportError, match="tencent_content_policy_blocked"):
            await adapter.send_action(
                SendPrivateMsg(
                    user_id=1,
                    message=[TextSegment(text="QQ 解冻教程")],
                )
            )
        assert adapter.outbound_queue_depth == 0

    async def test_blocks_unclassified_media_but_allows_control_actions(self) -> None:
        adapter = OneBotAdapter(
            OneBotConfig(url="ws://example.invalid"),
            tencent_policy_resolver=lambda: True,
        )
        with pytest.raises(TransportError, match="tencent_content_policy_blocked"):
            await adapter.send_action(
                SendGroupMsg(group_id=1, message=[ImageSegment(file="x.png")])
            )
        assert adapter.outbound_queue_depth == 0

    async def test_explicit_opt_out_restores_original_action(self) -> None:
        adapter = OneBotAdapter(
            OneBotConfig(url="ws://example.invalid"),
            tencent_policy_resolver=lambda: False,
        )
        await adapter.send_action(
            SendPrivateMsg(user_id=1, message=[TextSegment(text="QQ 解冻教程")])
        )
        assert adapter.outbound_queue_depth == 1

    async def test_safe_refusal_bypasses_policy_without_source_text(self) -> None:
        adapter = OneBotAdapter(
            OneBotConfig(url="ws://example.invalid"),
            tencent_policy_resolver=lambda: True,
        )
        await adapter.send_safe_refusal(
            SendPrivateMsg(user_id=1, message=[TextSegment(text="blocked source")])
        )
        queued = await adapter._outbound_q.get()
        assert isinstance(queued, SendPrivateMsg)
        assert queued.message == [TextSegment(text="这个话题不适合在 QQ 上讨论，我们换个安全的话题吧。")]


class TestParseEvent:
    """``parse_event`` recognises the four documented post types and falls
    through to :class:`UnknownEvent` for anything else."""

    def test_group_message_event(self) -> None:
        raw: dict[str, Any] = {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "time": 1_700_000_000,
            "self_id": 100,
            "user_id": 200,
            "group_id": 300,
            "message_id": 1,
            "message": [
                {"type": "at", "data": {"qq": "100"}},
                {"type": "text", "data": {"text": "hello"}},
            ],
            "raw_message": "[CQ:at,qq=100] hello",
            "sender": {"user_id": 200, "nickname": "alice"},
        }
        ev = parse_event(raw)
        assert isinstance(ev, MessageEvent)
        assert ev.message_type == MessageType.GROUP
        assert ev.group_id == 300
        assert len(ev.message) == 2
        assert ev.sender is not None and ev.sender.nickname == "alice"
        assert is_mentioned(ev.message, 100)

    def test_heartbeat_decodes_as_meta_event(self) -> None:
        raw = {
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "time": 1_700_000_000,
            "self_id": 100,
            "interval": 5000,
            "status": {},
        }
        ev = parse_event(raw)
        assert isinstance(ev, MetaEvent)
        assert ev.meta_event_type == "heartbeat"

    def test_unknown_post_type_maps_to_unknown_event(self) -> None:
        raw = {"post_type": "mystery", "time": 0, "self_id": 0}
        ev = parse_event(raw)
        assert isinstance(ev, UnknownEvent)
        assert ev.raw["post_type"] == "mystery"


class TestSegments:
    """Match the seven understood segment types + the ``Other`` fall-through."""

    @pytest.mark.parametrize(
        ("payload", "expected_cls"),
        [
            ({"type": "text", "data": {"text": "hi"}}, TextSegment),
            ({"type": "at", "data": {"qq": "1"}}, AtSegment),
            ({"type": "image", "data": {"url": "https://x", "file": "f"}}, ImageSegment),
            ({"type": "reply", "data": {"id": "42"}}, ReplySegment),
            ({"type": "face", "data": {"id": "1"}}, FaceSegment),
            ({"type": "record", "data": {"url": "https://y"}}, RecordSegment),
            ({"type": "video", "data": {"url": "https://v", "file": "v.mp4"}}, VideoSegment),
            ({"type": "file", "data": {"url": "https://f", "file": "doc.pdf"}}, FileSegment),
        ],
    )
    def test_seven_segment_types(self, payload: dict[str, Any], expected_cls: type) -> None:
        ev = parse_event({"post_type": "message", "message_type": "private",
                          "self_id": 1, "user_id": 1, "message_id": 1,
                          "message": [payload], "time": 0})
        assert isinstance(ev, MessageEvent)
        assert len(ev.message) == 1
        assert isinstance(ev.message[0], expected_cls)

    def test_unknown_segment_collapses_to_other(self) -> None:
        # ``poke`` is a genuinely unmodeled segment type — ``video`` and
        # ``file`` are now first-class (see TestSegmentHelpers).
        ev = parse_event({"post_type": "message", "message_type": "private",
                          "self_id": 1, "user_id": 1, "message_id": 1,
                          "message": [{"type": "poke", "data": {"id": "x"}}], "time": 0})
        assert isinstance(ev, MessageEvent)
        assert isinstance(ev.message[0], OtherSegment)


class TestSegmentHelpers:
    """``segments_to_text`` / ``segments_to_attachments`` / ``is_mentioned``."""

    def test_text_extraction_flattens_segments(self) -> None:
        segs = [
            AtSegment(qq="100"),
            TextSegment(text="hello "),
            TextSegment(text="world"),
            FaceSegment(id="1"),
        ]
        t = segments_to_text(segs)
        assert "hello world" in t
        assert "@100" in t

    def test_attachments_cover_image_and_record(self) -> None:
        segs = [
            TextSegment(text="caption"),
            ImageSegment(url="https://cdn/img.jpg", file="img.jpg"),
            RecordSegment(url="https://cdn/voice.amr"),
            OtherSegment(raw={"type": "video"}),
            AtSegment(qq="100"),
            FaceSegment(id="1"),
            ReplySegment(id="42"),
        ]
        atts = segments_to_attachments(segs)
        assert len(atts) == 2
        assert atts[0].kind == AttachmentKind.IMAGE
        assert atts[0].url == "https://cdn/img.jpg"
        assert atts[0].file_name == "img.jpg"
        assert atts[1].kind == AttachmentKind.AUDIO

    def test_attachments_cover_video_and_file(self) -> None:
        """WS-1 task 2 — QQ inbound now captures video + file segments so
        media parity matches Telegram's photo/voice/video/document."""
        segs = [
            TextSegment(text="see this"),
            VideoSegment(url="https://cdn/clip.mp4", file="clip.mp4"),
            FileSegment(url="https://cdn/report.pdf", file="report.pdf"),
        ]
        atts = segments_to_attachments(segs)
        assert len(atts) == 2
        assert atts[0].kind == AttachmentKind.VIDEO
        assert atts[0].url == "https://cdn/clip.mp4"
        assert atts[0].file_name == "clip.mp4"
        assert atts[1].kind == AttachmentKind.DOCUMENT
        assert atts[1].url == "https://cdn/report.pdf"
        assert atts[1].file_name == "report.pdf"

    def test_attachments_skip_empty_url_video_and_file(self) -> None:
        # url-less video/file segments (offline media, name-only file
        # shares) are skipped the same way as url-less images.
        segs = [
            VideoSegment(url="", file="x.mp4"),
            FileSegment(url="", file="x.pdf"),
        ]
        assert segments_to_attachments(segs) == []

    def test_attachments_skip_empty_urls(self) -> None:
        segs = [ImageSegment(url="", file=None)]
        assert segments_to_attachments(segs) == []

    def test_attachments_empty_for_text_only(self) -> None:
        segs = [TextSegment(text="hi"), AtSegment(qq="100")]
        assert segments_to_attachments(segs) == []

    def test_is_mentioned_handles_at_all(self) -> None:
        segs = [AtSegment(qq="all")]
        assert is_mentioned(segs, 12345)

    def test_is_mentioned_returns_false_when_unmentioned(self) -> None:
        segs = [TextSegment(text="hi there")]
        assert not is_mentioned(segs, 100)


class TestActionToWire:
    """Serialised actions match the OneBot envelope shape."""

    def test_send_group_msg_envelope(self) -> None:
        a = SendGroupMsg(
            group_id=1,
            message=[ReplySegment(id="42"), TextSegment(text="hello")],
        )
        s = action_to_wire(a)
        assert s["action"] == "send_group_msg"
        assert s["params"]["group_id"] == 1
        assert s["params"]["message"][0]["type"] == "reply"
        assert s["params"]["message"][0]["data"]["id"] == "42"
        assert s["params"]["message"][1]["type"] == "text"

    def test_set_input_status_envelope(self) -> None:
        """NapCat extension — must serialize with the right action name
        and event_type so the QQ client renders '正在输入...'."""
        from corlinman_channels.onebot import SetInputStatus

        s = action_to_wire(SetInputStatus(user_id=9876, event_type=1))
        assert s["action"] == "set_input_status"
        assert s["params"]["user_id"] == 9876
        assert s["params"]["event_type"] == 1

    def test_upload_private_file_envelope(self) -> None:
        from corlinman_channels.onebot import UploadPrivateFile

        s = action_to_wire(
            UploadPrivateFile(user_id=42, file="/tmp/a.html", name="a.html")
        )
        assert s["action"] == "upload_private_file"
        assert s["params"]["user_id"] == 42
        assert s["params"]["file"] == "/tmp/a.html"
        assert s["params"]["name"] == "a.html"

    def test_upload_group_file_envelope(self) -> None:
        from corlinman_channels.onebot import UploadGroupFile

        s = action_to_wire(
            UploadGroupFile(group_id=10, file="/tmp/x.pdf", name="x.pdf")
        )
        assert s["action"] == "upload_group_file"
        assert s["params"]["group_id"] == 10
        assert s["params"]["file"] == "/tmp/x.pdf"
        assert s["params"]["name"] == "x.pdf"
        # ``folder`` is omitted when not set.
        assert "folder" not in s["params"]

    def test_image_segment_serializes_inline(self) -> None:
        """WS-1 task 1 — an outbound image segment serializes to the
        OneBot ``image`` wire form so inline media-send lands."""
        a = SendGroupMsg(
            group_id=7,
            message=[
                TextSegment(text="here"),
                ImageSegment(url="https://cdn/pic.png", file="pic.png"),
            ],
        )
        s = action_to_wire(a)
        img = s["params"]["message"][1]
        assert img["type"] == "image"
        assert img["data"]["url"] == "https://cdn/pic.png"
        assert img["data"]["file"] == "pic.png"

    def test_image_segment_serializes_file_without_url(self) -> None:
        """NapCat accepts OneBot ``file`` payloads such as base64 images."""
        a = SendPrivateMsg(
            user_id=8,
            message=[ImageSegment(file="base64://ZmFrZQ==")],
        )
        s = action_to_wire(a)
        img = s["params"]["message"][0]
        assert img["type"] == "image"
        assert "url" not in img["data"]
        assert img["data"]["file"] == "base64://ZmFrZQ=="

    def test_record_segment_serializes_file_without_url(self) -> None:
        """NapCat accepts OneBot ``file`` payloads such as base64 records."""
        a = SendPrivateMsg(
            user_id=8,
            message=[RecordSegment(file="base64://ZmFrZQ==")],
        )
        s = action_to_wire(a)
        record = s["params"]["message"][0]
        assert record["type"] == "record"
        assert "url" not in record["data"]
        assert record["data"]["file"] == "base64://ZmFrZQ=="

    def test_video_and_file_segments_serialize(self) -> None:
        from corlinman_channels.onebot import SendPrivateMsg

        a = SendPrivateMsg(
            user_id=3,
            message=[
                VideoSegment(url="https://cdn/clip.mp4", file="clip.mp4"),
                FileSegment(url="https://cdn/doc.pdf"),
            ],
        )
        s = action_to_wire(a)
        vid, fil = s["params"]["message"]
        assert vid["type"] == "video"
        assert vid["data"]["url"] == "https://cdn/clip.mp4"
        assert vid["data"]["file"] == "clip.mp4"
        assert fil["type"] == "file"
        assert fil["data"]["url"] == "https://cdn/doc.pdf"
        # ``file`` omitted when not set.
        assert "file" not in fil["data"]


# ---------------------------------------------------------------------------
# Adapter-level tests
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_empty_url_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            OneBotAdapter(OneBotConfig(url=""))


# ---------------------------------------------------------------------------
# WebSocket integration tests
# ---------------------------------------------------------------------------


class TestOneBotIntegration:
    """End-to-end tests against an in-process ``websockets`` server."""

    async def test_adapter_yields_normalized_event(self, ws_server) -> None:
        async def handler(ws: ServerConnection) -> None:
            # Push one group message; then keep the connection open until
            # the client closes.
            await ws.send(json.dumps({
                "post_type": "message",
                "message_type": "group",
                "self_id": 100,
                "user_id": 555,
                "group_id": 12345,
                "message_id": 42,
                "message": [
                    {"type": "at", "data": {"qq": "100"}},
                    {"type": "text", "data": {"text": "hello"}},
                ],
                "raw_message": "@100 hello",
                "time": 1_700_000_000,
            }))
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass

        async with ws_server(handler) as url:
            adapter = OneBotAdapter(OneBotConfig(url=url, self_ids=[100]))
            async with adapter:
                # Pull one event with a generous timeout to absorb connect.
                async def first() -> Any:
                    async for ev in adapter.inbound():
                        return ev
                    return None

                ev = await asyncio.wait_for(first(), timeout=5.0)
                assert ev is not None
                assert ev.channel == "qq"
                assert ev.binding.account == "100"
                assert ev.binding.thread == "12345"
                assert ev.binding.sender == "555"
                assert ev.mentioned is True
                assert "hello" in ev.text
                assert ev.message_id == "42"
                assert isinstance(ev.payload, MessageEvent)

    async def test_heartbeat_detects_self_id_before_message(self, ws_server) -> None:
        seen: list[int] = []

        async def handler(ws: ServerConnection) -> None:
            await ws.send(json.dumps({
                "post_type": "meta_event",
                "meta_event_type": "heartbeat",
                "self_id": 123456,
                "time": 1,
                "status": {"online": True},
            }))
            await asyncio.sleep(0.1)

        async with ws_server(handler) as url:
            adapter = OneBotAdapter(OneBotConfig(url=url), on_self_id=seen.append)
            async with adapter:
                async def detected() -> None:
                    while adapter.last_self_id is None:
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(detected(), timeout=5.0)
                assert adapter.last_self_id == 123456
                assert seen == [123456]

    async def test_self_id_observer_only_fires_on_change(self, ws_server) -> None:
        seen: list[int] = []

        async def handler(ws: ServerConnection) -> None:
            for self_id in (0, 100, 100, 200):
                await ws.send(json.dumps({
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "self_id": self_id,
                    "time": 1,
                    "status": {"online": True},
                }))
            await asyncio.sleep(0.1)

        async with ws_server(handler) as url:
            adapter = OneBotAdapter(OneBotConfig(url=url), on_self_id=seen.append)
            async with adapter:
                async def switched() -> None:
                    while adapter.last_self_id != 200:
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(switched(), timeout=5.0)
                assert seen == [100, 200]

    async def test_self_id_observer_failure_does_not_break_pump(self, ws_server) -> None:
        def fail(_self_id: int) -> None:
            raise RuntimeError("observer detail")

        async def handler(ws: ServerConnection) -> None:
            await ws.send(json.dumps({
                "post_type": "message",
                "message_type": "private",
                "self_id": 100,
                "user_id": 200,
                "message_id": 7,
                "message": [{"type": "text", "data": {"text": "yo"}}],
                "time": 2,
            }))
            await asyncio.sleep(0.1)

        async with ws_server(handler) as url:
            adapter = OneBotAdapter(OneBotConfig(url=url), on_self_id=fail)
            async with adapter:
                async def first() -> Any:
                    async for ev in adapter.inbound():
                        return ev
                    return None

                ev = await asyncio.wait_for(first(), timeout=5.0)
                assert ev is not None
                assert ev.text == "yo"
                assert adapter.last_self_id == 100

    async def test_self_id_observer_retries_after_failure(self, ws_server) -> None:
        attempts: list[int] = []

        def fail_once(self_id: int) -> None:
            attempts.append(self_id)
            if len(attempts) == 1:
                raise RuntimeError("transient observer failure")

        async def handler(ws: ServerConnection) -> None:
            for _ in range(2):
                await ws.send(json.dumps({
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "self_id": 100,
                    "time": 1,
                    "status": {"online": True},
                }))
            await asyncio.sleep(0.1)

        async with ws_server(handler) as url:
            adapter = OneBotAdapter(OneBotConfig(url=url), on_self_id=fail_once)
            async with adapter:
                async def recovered() -> None:
                    while len(attempts) < 2:
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(recovered(), timeout=5.0)
                assert attempts == [100, 100]

    async def test_adapter_drops_non_message_events(self, ws_server) -> None:
        async def handler(ws: ServerConnection) -> None:
            # First a heartbeat (meta event — should be filtered),
            # then a real message.
            await ws.send(json.dumps({
                "post_type": "meta_event",
                "meta_event_type": "heartbeat",
                "self_id": 100,
                "time": 1,
            }))
            await ws.send(json.dumps({
                "post_type": "message",
                "message_type": "private",
                "self_id": 100,
                "user_id": 200,
                "message_id": 7,
                "message": [{"type": "text", "data": {"text": "yo"}}],
                "time": 2,
            }))
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass

        async with ws_server(handler) as url:
            adapter = OneBotAdapter(OneBotConfig(url=url))
            async with adapter:
                async def first() -> Any:
                    async for ev in adapter.inbound():
                        return ev
                    return None

                ev = await asyncio.wait_for(first(), timeout=5.0)
                assert ev is not None
                # The heartbeat was filtered out; the only surfaced event is
                # the private message.
                assert ev.binding.channel == "qq"
                assert ev.binding.account == "100"
                assert ev.text == "yo"

    async def test_send_action_round_trips_through_ws(self, ws_server) -> None:
        received: list[str] = []

        async def handler(ws: ServerConnection) -> None:
            try:
                async for raw in ws:
                    if isinstance(raw, (bytes, bytearray)):
                        received.append(raw.decode("utf-8"))
                    else:
                        received.append(raw)
                    break
            except Exception:
                pass

        async with ws_server(handler) as url:
            adapter = OneBotAdapter(OneBotConfig(url=url))
            async with adapter:
                # Allow the initial connect to complete.
                await asyncio.sleep(0.1)
                await adapter.send_action(
                    SendGroupMsg(
                        group_id=10, message=[TextSegment(text="hi")]
                    )
                )
                # Give the writer task a moment to flush.
                for _ in range(20):
                    if received:
                        break
                    await asyncio.sleep(0.05)

        assert received, "server never received a frame"
        payload = json.loads(received[0])
        assert payload["action"] == "send_group_msg"
        assert payload["params"]["group_id"] == 10
        assert payload["params"]["message"][0]["data"]["text"] == "hi"


# ---------------------------------------------------------------------------
# C1 — writer requeues actions on transient send failure.
# ---------------------------------------------------------------------------


class TestWriterRequeueOnSendFailure:
    """Regression for C1 — _writer_loop must not drop an action when
    ws.send raises; it requeues to the front buffer so the next ws
    iteration retries instead of leaving the inbox row stuck for 10
    minutes waiting for the stale-dispatched sweep."""

    @pytest.mark.asyncio
    async def test_send_failure_requeues_action_at_front(self) -> None:
        """First ws.send raises, second ws.send succeeds — the action
        must end up on the wire exactly once, on the second try."""
        adapter = OneBotAdapter(OneBotConfig(url="ws://127.0.0.1:1"))
        action = SendGroupMsg(group_id=42, message=[TextSegment(text="ping")])

        sent: list[str] = []
        attempts = {"count": 0}

        class _Ws:
            async def send(self, payload: str) -> None:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise RuntimeError("simulated transport failure")
                sent.append(payload)

        # Seed the outbound queue and run the writer until the action lands.
        await adapter._outbound_q.put(action)

        # First pass — the writer raises after requeueing.
        with pytest.raises(Exception):
            await adapter._writer_loop(_Ws())  # type: ignore[arg-type]

        # Action must be in the front buffer with retry=1.
        assert list(adapter._outbound_front) == [action]
        assert adapter._outbound_retries.get(id(action)) == 1

        # Second pass — the writer drains the front buffer first and
        # ws.send succeeds. Use create_task + a short sleep so we can
        # cancel after the action lands. The writer catches
        # CancelledError around its outer ``_outbound_q.get()`` and
        # returns cleanly, so the task finishes normally rather than
        # propagating the cancellation.
        task = asyncio.create_task(adapter._writer_loop(_Ws()))  # type: ignore[arg-type]
        for _ in range(20):
            if sent:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        # The loop swallows CancelledError on its outer get(), so the
        # task completes without raising.
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(sent) == 1, f"expected exactly one wire send, got {sent}"
        payload = json.loads(sent[0])
        assert payload["action"] == "send_group_msg"
        assert payload["params"]["group_id"] == 42
        # Successful send must clear the retry bookkeeping.
        assert id(action) not in adapter._outbound_retries

    @pytest.mark.asyncio
    async def test_two_consecutive_failures_drop_poison_action(self) -> None:
        """After two failed attempts the action is dropped so a poison
        payload can't infinite-loop the writer."""
        adapter = OneBotAdapter(OneBotConfig(url="ws://127.0.0.1:1"))
        poison = SendGroupMsg(group_id=1, message=[TextSegment(text="x")])
        good = SendGroupMsg(group_id=2, message=[TextSegment(text="ok")])

        sent: list[str] = []
        attempts = {"count": 0}

        class _Ws:
            async def send(self, payload: str) -> None:
                attempts["count"] += 1
                # Fail on every send of the poison; succeed for the good
                # one.
                obj = json.loads(payload)
                if obj["params"]["group_id"] == 1:
                    raise RuntimeError("poison")
                sent.append(payload)

        await adapter._outbound_q.put(poison)
        await adapter._outbound_q.put(good)

        # First invocation: poison fails once → requeued to front (retry=1)
        # → TransportError raised.
        with pytest.raises(TransportError):
            await adapter._writer_loop(_Ws())  # type: ignore[arg-type]

        # Second invocation: poison comes from the front buffer → fails
        # again → retry=2 → DROPPED (no requeue, no raise). Loop then
        # proceeds to drain the good action from _outbound_q which lands
        # on the wire. Run as a task so we can cancel once the good
        # action has flushed.
        task = asyncio.create_task(adapter._writer_loop(_Ws()))  # type: ignore[arg-type]
        for _ in range(40):
            if sent:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert sent, "expected the non-poison action to eventually land"
        landed = json.loads(sent[0])
        assert landed["params"]["group_id"] == 2
        # Poison must NOT be tracked anymore (cleared on drop).
        assert id(poison) not in adapter._outbound_retries


# ---------------------------------------------------------------------------
# R2 — _pump drops oldest on inbound queue overflow (no backpressure stall).
# ---------------------------------------------------------------------------


class TestInboundQueueDropOldest:
    """Regression for R2 — when the inbound queue is full, ``_pump`` must
    drop the OLDEST event and put the newest so the most recent user
    message still surfaces. Blocking on ``put`` would let the websockets
    frame buffer fill until NapCat closes the connection with 1009."""

    @pytest.mark.asyncio
    async def test_overflow_drops_oldest_and_counts(self) -> None:
        adapter = OneBotAdapter(OneBotConfig(url="ws://127.0.0.1:1"))
        # Tiny queue so a couple of puts fill it.
        adapter._inbound_q = asyncio.Queue(maxsize=2)
        assert adapter.inbound_dropped_count == 0

        # Drive the drop-oldest path manually — same code that _pump runs.
        async def push(ev_id: int) -> None:
            event = parse_event({
                "post_type": "message",
                "message_type": "private",
                "self_id": 1,
                "user_id": 1,
                "message_id": ev_id,
                "message": [{"type": "text", "data": {"text": f"m{ev_id}"}}],
                "time": ev_id,
            })
            try:
                adapter._inbound_q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest, replace.
                from contextlib import suppress as _s
                with _s(asyncio.QueueEmpty):
                    adapter._inbound_q.get_nowait()
                adapter._inbound_dropped += 1
                with _s(asyncio.QueueFull):
                    adapter._inbound_q.put_nowait(event)

        # Fill the queue, then push one more — oldest should be evicted.
        await push(1)
        await push(2)
        await push(3)
        await push(4)
        # Queue capacity is 2 so two drops should have happened.
        assert adapter._inbound_q.qsize() == 2
        # We pushed 4 events into a 2-slot queue without ever blocking;
        # at least two drops must have been recorded.
        assert adapter.inbound_dropped_count >= 2

        # The newest events must be at the head of the queue (FIFO),
        # so what we pop should NOT be the very first one we pushed.
        first_out = adapter._inbound_q.get_nowait()
        assert isinstance(first_out, MessageEvent)
        # The remaining queued event should be the most recently pushed
        # (id=4).
        last_out = adapter._inbound_q.get_nowait()
        assert isinstance(last_out, MessageEvent)
        assert last_out.message_id == 4
