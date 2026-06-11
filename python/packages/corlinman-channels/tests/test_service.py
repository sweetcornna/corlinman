"""Tests for ``corlinman_channels.service`` — the orchestration helpers.

Mirrors the Rust unit tests in ``rust/.../service.rs`` and
``rust/.../telegram/service.rs`` (the inbound→router→reply round-trip).

We don't stand up a real WebSocket / Telegram backend here; the
adapter / sender layers already have integration coverage. These
tests focus on the wiring inside ``handle_one_*`` and the structural
behaviour of ``QqChannelParams`` / ``TelegramChannelParams``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_channels.common import ChannelBinding, InboundEvent
from corlinman_channels.onebot import (
    AtSegment,
    MessageEvent,
    MessageType,
    SendGroupMsg,
    SendPrivateMsg,
    TextSegment,
)
from corlinman_channels.router import RoutedRequest
from corlinman_channels.service import (
    TELEGRAM_HEALTH,
    TELEGRAM_RECENT_MESSAGES,
    DiscordChannelParams,
    FeishuChannelParams,
    QqChannelParams,
    QqOfficialChannelParams,
    SlackChannelParams,
    TelegramChannelParams,
    _build_internal_request,
    _build_reply_action,
    _event_kind,
    _telegram_reset_state_for_tests,
    handle_one_discord,
    handle_one_feishu,
    handle_one_qq,
    handle_one_qq_official,
    handle_one_slack,
    handle_one_telegram,
    run_discord_channel,
    run_feishu_channel,
    run_qq_channel,
    run_qq_official_channel,
    run_slack_channel,
    run_telegram_channel,
    telegram_record_inbound,
    telegram_record_reply_sent,
)

# ---------------------------------------------------------------------------
# Fake chat backend
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Ev:
    kind: str
    text: str = ""
    error: str = ""
    plugin: str = ""
    tool: str = ""
    args_json: bytes = b""
    is_reasoning: bool = False
    call_id: str = ""
    duration_ms: int = 0
    is_error: bool = False
    error_summary: str = ""
    finish_reason: str = ""


class _ScriptedChatService:
    """Streams a scripted list of events. Mirrors the Rust
    ``InternalChatEvent::TokenDelta`` / ``Done`` flow."""

    def __init__(self, events: list[_Ev]) -> None:
        self.events = events
        self.calls: list[Any] = []

    def run(self, request: Any, cancel: Any) -> Any:
        self.calls.append(request)
        async def _gen():
            for ev in self.events:
                yield ev
        return _gen()


# ---------------------------------------------------------------------------
# Adapter fakes — capture sent actions / messages
# ---------------------------------------------------------------------------


class _FakeOneBotAdapter:
    """Just enough surface for ``handle_one_qq`` — only ``send_action``
    is exercised."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_action(self, action: Any) -> None:
        self.sent.append(action)


class _FakeTelegramSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, int | None]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.chat_actions: list[tuple[int, str]] = []
        #: Inline-keyboard payloads observed on ``send_message`` calls,
        #: parallel-indexed against ``sent``. ``None`` when the call
        #: didn't include a keyboard. Tests for the ``ask_user`` path
        #: assert on this to confirm the buttons threaded through.
        self.sent_keyboards: list[list[list[dict[str, str]]] | None] = []
        self._next_message_id = 0
        # Test knobs: flip to make the corresponding sender call raise.
        self.send_message_should_raise = False
        self.edit_message_text_should_raise = False

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        if self.send_message_should_raise:
            raise RuntimeError("simulated send_message failure")
        self._next_message_id += 1
        self.sent.append((chat_id, text, reply_to_message_id))
        self.sent_keyboards.append(inline_keyboard)
        return self._next_message_id

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str
    ) -> None:
        if self.edit_message_text_should_raise:
            raise RuntimeError("simulated edit_message_text failure")
        self.edits.append((chat_id, message_id, text))

    async def send_chat_action(
        self, chat_id: int, action: str = "typing"
    ) -> None:
        self.chat_actions.append((chat_id, action))

    async def send_document(
        self,
        chat_id: int,
        path: Any,
        caption: str | None = None,
        filename: str | None = None,
        mime: str = "application/octet-stream",
    ) -> int:
        self._next_message_id += 1
        self.sent.append((chat_id, f"[doc {filename or path}]", caption))  # type: ignore[arg-type]
        return self._next_message_id

    async def send_photo(self, *args: Any, **kw: Any) -> int:
        self._next_message_id += 1
        self.sent.append((args[0], "[photo]", kw.get("caption")))  # type: ignore[arg-type]
        return self._next_message_id

    async def send_voice(self, *args: Any, **kw: Any) -> int:
        self._next_message_id += 1
        self.sent.append((args[0], "[voice]", kw.get("caption")))  # type: ignore[arg-type]
        return self._next_message_id


# ---------------------------------------------------------------------------
# QQ reply assembly
# ---------------------------------------------------------------------------


def _sample_group_event() -> MessageEvent:
    return MessageEvent(
        self_id=100,
        message_type=MessageType.GROUP,
        sub_type="normal",
        group_id=12345,
        user_id=555,
        message_id=42,
        message=[TextSegment(text="格兰早")],
        raw_message="格兰早",
        time=1_700_000_000,
        sender=None,
    )


def _qq_private_event(user_id: int = 555) -> MessageEvent:
    return MessageEvent(
        self_id=100,
        message_type=MessageType.PRIVATE,
        sub_type="friend",
        group_id=None,
        user_id=user_id,
        message_id=42,
        message=[TextSegment(text="hi")],
        raw_message="hi",
        time=1_700_000_000,
        sender=None,
    )


class TestQqReplyAction:
    def test_group_reply_addresses_sender(self) -> None:
        ev = _sample_group_event()
        a = _build_reply_action(ev, "hello")
        assert isinstance(a, SendGroupMsg)
        assert a.group_id == 12345
        assert len(a.message) == 2
        assert isinstance(a.message[0], AtSegment)
        assert a.message[0].qq == "555"
        assert isinstance(a.message[1], TextSegment)
        assert "hello" in a.message[1].text

    def test_private_reply_omits_at(self) -> None:
        ev = _sample_group_event()
        ev.message_type = MessageType.PRIVATE
        ev.group_id = None
        a = _build_reply_action(ev, "hi")
        assert isinstance(a, SendPrivateMsg)
        assert a.user_id == 555
        assert len(a.message) == 1
        assert isinstance(a.message[0], TextSegment)
        assert a.message[0].text == "hi"


# ---------------------------------------------------------------------------
# _build_internal_request
# ---------------------------------------------------------------------------


class TestInternalRequest:
    def test_dispatch_empty_attachments_when_text_only(self) -> None:
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content=ev.raw_message)
        internal = _build_internal_request(req, ev, "claude-sonnet-4-5")
        # _build_internal_request returns a SimpleNamespace (attribute
        # access — chat_service.run expects .model/.messages/...) since
        # the earlier QQ fix; assert via getattr.
        assert internal.attachments == []
        assert internal.model == "claude-sonnet-4-5"
        assert internal.messages[0].content == "格兰早"


# ---------------------------------------------------------------------------
# _event_kind discriminator
# ---------------------------------------------------------------------------


class TestEventKind:
    def test_kind_attr_wins(self) -> None:
        assert _event_kind(_Ev(kind="Token_Delta", text="a")) == "token_delta"
        assert _event_kind(_Ev(kind="done")) == "done"

    def test_class_name_fallback(self) -> None:
        class TokenDelta:
            pass

        class Done:
            pass

        assert _event_kind(TokenDelta()) == "token_delta"
        assert _event_kind(Done()) == "done"

    def test_unknown_class_lowercased(self) -> None:
        class Whatever:
            pass

        assert _event_kind(Whatever()) == "whatever"


# ---------------------------------------------------------------------------
# handle_one_qq — end-to-end with the scripted service + fake adapter
# ---------------------------------------------------------------------------


class TestQqPersonaInjection:
    """Verify the per-channel humanlike persona toggle prepends the
    persona system_prompt to the chat request when on, and stays out of
    the way when off."""

    class _FakePersonaStore:
        def __init__(self, persona_id: str, system_prompt: str) -> None:
            from types import SimpleNamespace

            self._row = SimpleNamespace(
                id=persona_id,
                display_name="Test Persona",
                short_summary="",
                system_prompt=system_prompt,
                is_builtin=False,
            )

        async def get(self, persona_id: str):
            if persona_id == self._row.id:
                return self._row
            return None

    @pytest.mark.asyncio
    async def test_persona_prepended_when_humanlike_on(self) -> None:
        """With humanlike_enabled=True + persona_id + persona_store, the
        chat_service should see a leading role=system message carrying
        the persona body."""
        import asyncio

        from corlinman_channels.service import QqChannelParams, handle_one_qq

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()
        store = self._FakePersonaStore("grantley", "PERSONA-BODY-MARK\nYou are Grantley.")
        params = QqChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=True,
            persona_id="grantley",
            persona_store=store,
        )

        await handle_one_qq(
            svc, req, ev, "m", adapter, asyncio.Event(), params=params,  # type: ignore[arg-type]
        )
        assert svc.calls, "chat_service.run was never invoked"
        request = svc.calls[0]
        # Should have at least 2 messages: leading system + user
        assert len(request.messages) >= 2
        sys_msg = request.messages[0]
        assert sys_msg.role == "system"
        assert "PERSONA-BODY-MARK" in sys_msg.content

    @pytest.mark.asyncio
    async def test_no_injection_when_humanlike_off(self) -> None:
        import asyncio

        from corlinman_channels.service import QqChannelParams, handle_one_qq

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()
        store = self._FakePersonaStore("grantley", "PERSONA-BODY-MARK")
        params = QqChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=False,   # off
            persona_id="grantley",
            persona_store=store,
        )

        await handle_one_qq(
            svc, req, ev, "m", adapter, asyncio.Event(), params=params,  # type: ignore[arg-type]
        )
        request = svc.calls[0]
        # No system message when humanlike is off.
        roles = [m.role for m in request.messages]
        assert "system" not in roles

    @pytest.mark.asyncio
    async def test_resolver_overrides_static_fields(self) -> None:
        """When humanlike_resolver is callable, it wins. Live admin PUT
        flips the toggle via the resolver without restarting the channel."""
        import asyncio

        from corlinman_channels.service import QqChannelParams, handle_one_qq

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()
        store = self._FakePersonaStore("kitty", "MEOW-PERSONA")
        params = QqChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=False,      # static says off
            persona_id=None,
            persona_store=store,
            humanlike_resolver=lambda: (True, "kitty"),  # live says on
        )

        await handle_one_qq(
            svc, req, ev, "m", adapter, asyncio.Event(), params=params,  # type: ignore[arg-type]
        )
        request = svc.calls[0]
        sys_msg = request.messages[0]
        assert sys_msg.role == "system"
        assert "MEOW-PERSONA" in sys_msg.content


class TestHandleOneQq:
    @pytest.mark.asyncio
    async def test_concatenates_token_deltas_and_sends_action(self) -> None:
        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="hel"),
            _Ev(kind="token_delta", text="lo"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        import asyncio

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        assert len(adapter.sent) == 1
        action = adapter.sent[0]
        assert isinstance(action, SendGroupMsg)
        # text segment after the at-mention carries "hello".
        text_seg = action.message[1]
        assert isinstance(text_seg, TextSegment)
        assert "hello" in text_seg.text

    @pytest.mark.asyncio
    async def test_error_event_renders_to_short_reply(self) -> None:
        svc = _ScriptedChatService([_Ev(kind="error", error="boom")])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        import asyncio

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        assert len(adapter.sent) == 1
        action = adapter.sent[0]
        assert isinstance(action, SendGroupMsg)
        text_seg = action.message[1]
        assert isinstance(text_seg, TextSegment)
        assert "[corlinman error]" in text_seg.text
        assert "boom" in text_seg.text

    @pytest.mark.asyncio
    async def test_empty_response_is_silently_dropped(self) -> None:
        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="   "),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        import asyncio

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        assert adapter.sent == []

    @pytest.mark.asyncio
    async def test_multichunk_group_reply_ats_only_first_chunk(self) -> None:
        """Regression: when a long body splits into multiple chunks the
        @sender mention MUST appear only on chunk[0]. Pre-fix every
        chunk prepended ``AtSegment``, spamming the user with N pings
        in the group.
        """
        # Build a body that exceeds the 3800-char QQ cap so chunk_reply
        # splits it into 2+ segments. Two paragraphs separated by a
        # blank line so chunking lands on a paragraph boundary.
        big = "A" * 2500
        body = big + "\n\n" + big
        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text=body),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="long please")
        adapter = _FakeOneBotAdapter()

        import asyncio

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        sends = [a for a in adapter.sent if isinstance(a, SendGroupMsg)]
        assert len(sends) >= 2, f"expected multi-chunk send; got {len(sends)}"

        # Chunk[0]: AtSegment + TextSegment, in that order.
        first = sends[0]
        assert isinstance(first.message[0], AtSegment)
        assert first.message[0].qq == str(ev.user_id)
        assert isinstance(first.message[1], TextSegment)

        # Chunks[1:]: no AtSegment, only a single TextSegment.
        for follow in sends[1:]:
            assert not any(
                isinstance(seg, AtSegment) for seg in follow.message
            ), (
                "follow-up chunk must NOT @-mention again — Tencent "
                "anti-spam treats repeat pings as abuse"
            )
            # And the text payload is non-empty.
            text_segs = [
                seg for seg in follow.message if isinstance(seg, TextSegment)
            ]
            assert text_segs
            assert text_segs[0].text.strip()

    @pytest.mark.asyncio
    async def test_send_attachment_private_uploads_via_napcat(
        self, tmp_path: Any
    ) -> None:
        """send_attachment tool_call must dispatch an UploadPrivateFile
        action for QQ private chats."""
        import json

        from corlinman_channels.onebot import UploadPrivateFile

        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-fake")
        args = json.dumps({"path": str(f), "filename": "doc.pdf"})
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="send_attachment",
                tool="send_attachment",
                args_json=args.encode("utf-8"),
            ),
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        ev = _qq_private_event(user_id=10001)
        binding = ChannelBinding.qq_private(999, 10001)
        req = RoutedRequest(binding=binding, content="give me the pdf")
        adapter = _FakeOneBotAdapter()

        import asyncio

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        uploads = [a for a in adapter.sent if isinstance(a, UploadPrivateFile)]
        assert uploads, f"expected UploadPrivateFile; got {[type(a).__name__ for a in adapter.sent]}"
        assert uploads[0].user_id == 10001
        assert uploads[0].name == "doc.pdf"
        assert uploads[0].file.endswith("doc.pdf")

    @pytest.mark.asyncio
    async def test_send_attachment_image_sends_inline_segment(
        self, tmp_path: Any
    ) -> None:
        """WS-1 task 1 — an ``image/*`` send_attachment must ship an inline
        ImageSegment (SendPrivateMsg with an image), NOT an UploadPrivateFile,
        so the picture lands in the chat instead of the file panel. The
        outbound image uses OneBot's ``base64://`` form so NapCat does not
        need to read corlinman's local filesystem path."""
        import asyncio
        import base64
        import json

        from corlinman_channels.onebot import (
            ImageSegment,
            UploadPrivateFile,
        )

        f = tmp_path / "sticker.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        args = json.dumps({"path": str(f), "filename": "sticker.png"})
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="send_attachment",
                tool="send_attachment",
                args_json=args.encode("utf-8"),
            ),
            _Ev(kind="token_delta", text="here you go"),
            _Ev(kind="done"),
        ])
        ev = _qq_private_event(user_id=20002)
        binding = ChannelBinding.qq_private(999, 20002)
        req = RoutedRequest(binding=binding, content="send the sticker")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]

        # No file-share upload for an image.
        assert not [a for a in adapter.sent if isinstance(a, UploadPrivateFile)]
        # An inline image segment landed (the dedicated image send PLUS
        # the final text reply are both SendPrivateMsg).
        image_msgs = [
            a
            for a in adapter.sent
            if isinstance(a, SendPrivateMsg)
            and any(isinstance(s, ImageSegment) for s in a.message)
        ]
        assert image_msgs, (
            "expected a SendPrivateMsg carrying an ImageSegment; got "
            f"{[type(a).__name__ for a in adapter.sent]}"
        )
        seg = next(s for s in image_msgs[0].message if isinstance(s, ImageSegment))
        assert seg.url == ""
        assert seg.file == (
            "base64://" + base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")
        )

    @pytest.mark.asyncio
    async def test_send_attachment_image_group_inline_segment(
        self, tmp_path: Any
    ) -> None:
        """Group path mirrors private: image → inline ImageSegment in a
        SendGroupMsg, no UploadGroupFile."""
        import asyncio
        import json

        from corlinman_channels.onebot import (
            ImageSegment,
            UploadGroupFile,
        )

        f = tmp_path / "pic.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
        args = json.dumps({"path": str(f)})
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="send_attachment",
                tool="send_attachment",
                args_json=args.encode("utf-8"),
            ),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="show the pic")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]

        assert not [a for a in adapter.sent if isinstance(a, UploadGroupFile)]
        image_msgs = [
            a
            for a in adapter.sent
            if isinstance(a, SendGroupMsg)
            and any(isinstance(s, ImageSegment) for s in a.message)
        ]
        assert image_msgs, (
            "expected a SendGroupMsg carrying an ImageSegment; got "
            f"{[type(a).__name__ for a in adapter.sent]}"
        )
        seg = next(s for s in image_msgs[0].message if isinstance(s, ImageSegment))
        assert seg.url == ""
        assert seg.file is not None
        assert seg.file.startswith("base64://")

    @pytest.mark.asyncio
    async def test_send_attachment_audio_sends_private_record_segment(
        self, tmp_path: Any
    ) -> None:
        """QQ audio must be sent as an inline record segment, not a file upload.

        NapCat runs outside the corlinman container in Docker, so it cannot
        read generated paths like /data/workspace/generated/*.mp3 from
        UploadPrivateFile. A base64 record payload keeps the voice message
        self-contained.
        """
        import asyncio
        import base64
        import json

        from corlinman_channels.onebot import (
            RecordSegment,
            UploadPrivateFile,
        )

        f = tmp_path / "voice.mp3"
        f.write_bytes(b"fake-mp3")
        args = json.dumps({"path": str(f), "filename": "voice.mp3"})
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="send_attachment",
                tool="send_attachment",
                args_json=args.encode("utf-8"),
            ),
            _Ev(kind="done"),
        ])
        ev = _qq_private_event(user_id=20003)
        binding = ChannelBinding.qq_private(999, 20003)
        req = RoutedRequest(binding=binding, content="send voice")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]

        assert not [a for a in adapter.sent if isinstance(a, UploadPrivateFile)]
        record_msgs = [
            a
            for a in adapter.sent
            if isinstance(a, SendPrivateMsg)
            and any(isinstance(s, RecordSegment) for s in a.message)
        ]
        assert record_msgs, (
            "expected a SendPrivateMsg carrying a RecordSegment; got "
            f"{[type(a).__name__ for a in adapter.sent]}"
        )
        seg = next(s for s in record_msgs[0].message if isinstance(s, RecordSegment))
        assert seg.url == ""
        assert seg.file == (
            "base64://" + base64.b64encode(b"fake-mp3").decode("ascii")
        )

    @pytest.mark.asyncio
    async def test_send_attachment_audio_sends_group_record_segment(
        self, tmp_path: Any
    ) -> None:
        """Group audio mirrors private: record segment, no UploadGroupFile."""
        import asyncio
        import json

        from corlinman_channels.onebot import RecordSegment, UploadGroupFile

        f = tmp_path / "voice.ogg"
        f.write_bytes(b"fake-ogg")
        args = json.dumps({"path": str(f)})
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="send_attachment",
                tool="send_attachment",
                args_json=args.encode("utf-8"),
            ),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="send voice")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]

        assert not [a for a in adapter.sent if isinstance(a, UploadGroupFile)]
        record_msgs = [
            a
            for a in adapter.sent
            if isinstance(a, SendGroupMsg)
            and any(isinstance(s, RecordSegment) for s in a.message)
        ]
        assert record_msgs, (
            "expected a SendGroupMsg carrying a RecordSegment; got "
            f"{[type(a).__name__ for a in adapter.sent]}"
        )
        seg = next(s for s in record_msgs[0].message if isinstance(s, RecordSegment))
        assert seg.url == ""
        assert seg.file is not None
        assert seg.file.startswith("base64://")

    @pytest.mark.asyncio
    async def test_input_status_pulse_fires_until_cancelled(self) -> None:
        """NapCat-only ``set_input_status`` pulse must re-fire on its
        interval and stop on cancel.set(). Private chats only."""
        import asyncio

        from corlinman_channels.onebot import SetInputStatus
        from corlinman_channels.service import _qq_input_status_pulse

        adapter = _FakeOneBotAdapter()
        cancel = asyncio.Event()

        async def stop() -> None:
            await asyncio.sleep(0.15)
            cancel.set()

        await asyncio.gather(
            _qq_input_status_pulse(
                adapter,  # type: ignore[arg-type]
                user_id=12345,
                cancel=cancel,
                interval_s=0.05,
            ),
            stop(),
        )
        assert adapter.sent, "expected at least one set_input_status action"
        for act in adapter.sent:
            assert isinstance(act, SetInputStatus)
            assert act.user_id == 12345
            assert act.event_type == 1
        # Pulse must not keep firing past cancel.
        count_at_stop = len(adapter.sent)
        await asyncio.sleep(0.1)
        assert len(adapter.sent) == count_at_stop

    # -- tool-activity summary prelude (QQ-only; groups + private) ----------

    @pytest.mark.asyncio
    async def test_summary_prepends_when_tool_used(self) -> None:
        """A turn that used a tool must prepend the 📋 本次操作 block
        with one resolved tool line before the assistant body."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="web_search",
                tool="web_search",
                args_json=b'{"query":"gpt-5.5 news"}',
            ),
            _Ev(
                kind="tool_result",
                plugin="web_search",
                tool="web_search",
                duration_ms=302,
                is_error=False,
            ),
            _Ev(kind="token_delta", text="answer"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        assert len(adapter.sent) == 1
        action = adapter.sent[0]
        assert isinstance(action, SendGroupMsg)
        text_seg = action.message[1]
        assert isinstance(text_seg, TextSegment)
        text = text_seg.text
        # Summary block heading comes first (groups prepend a leading
        # space because the @-mention precedes the text segment).
        assert text.lstrip().startswith("📋 本次操作:"), text
        # Tool line carries the per-tool preview + success duration.
        assert "web_search" in text
        assert "'gpt-5.5 news'" in text or "gpt-5.5 news" in text
        assert "302ms" in text
        assert "✅" in text
        # Divider + assistant body trail the block.
        assert "─────" in text
        assert text.rstrip().endswith("answer")

    @pytest.mark.asyncio
    async def test_summary_omitted_when_no_tools(self) -> None:
        """No tool events → body is exactly the assistant text, no
        summary prefix. Preserves the legacy single-line reply shape."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="just talk"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        assert len(adapter.sent) == 1
        text_seg = adapter.sent[0].message[1]
        assert isinstance(text_seg, TextSegment)
        # No 📋 prefix anywhere — body is only the answer (with whatever
        # newline shape the reply builder added around it).
        assert "📋" not in text_seg.text
        assert "just talk" in text_seg.text

    @pytest.mark.asyncio
    async def test_summary_includes_send_attachment_line(
        self, tmp_path: Any
    ) -> None:
        """A send_attachment tool_call must surface as a 📎 已发送文件
        line in the summary block."""
        import asyncio
        import json

        f = tmp_path / "hello.html"
        f.write_text("<h1>hi</h1>", encoding="utf-8")
        args = json.dumps({"path": str(f), "filename": "hello.html"})
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="send_attachment",
                tool="send_attachment",
                args_json=args.encode("utf-8"),
            ),
            _Ev(kind="token_delta", text="done"),
            _Ev(kind="done"),
        ])
        ev = _qq_private_event(user_id=42)
        binding = ChannelBinding.qq_private(999, 42)
        req = RoutedRequest(binding=binding, content="give me the html")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        # Locate the text reply (an UploadPrivateFile action also lands).
        text_actions = [a for a in adapter.sent if isinstance(a, SendPrivateMsg)]
        assert text_actions, f"expected SendPrivateMsg; got {[type(a).__name__ for a in adapter.sent]}"
        text = text_actions[-1].message[0].text
        assert "📋 本次操作:" in text
        assert "📎 已发送文件: hello.html" in text
        assert "done" in text

    @pytest.mark.asyncio
    async def test_summary_env_disable_suppresses_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CORLINMAN_QQ_TOOL_SUMMARY=0 must hide the prelude even when
        tools were used — body is just the assistant text."""
        import asyncio

        monkeypatch.setenv("CORLINMAN_QQ_TOOL_SUMMARY", "0")
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="web_search", tool="web_search",
                args_json=b'{"query":"x"}'),
            _Ev(kind="tool_result", plugin="web_search", tool="web_search",
                duration_ms=10, is_error=False),
            _Ev(kind="token_delta", text="answer"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        text = adapter.sent[0].message[1].text
        assert "📋" not in text
        assert "web_search" not in text
        assert "answer" in text

    @pytest.mark.asyncio
    async def test_summary_includes_error_lines(self) -> None:
        """tool_result with is_error=True must render ❌ + summary text
        in the prelude block."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="run_shell", tool="run_shell",
                args_json=b'{"command":"rm -rf /"}'),
            _Ev(kind="tool_result", plugin="run_shell", tool="run_shell",
                duration_ms=42, is_error=True,
                error_summary="permission denied"),
            _Ev(kind="token_delta", text="failed"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        text = adapter.sent[0].message[1].text
        assert "📋 本次操作:" in text
        assert "❌" in text
        assert "run_shell" in text
        assert "permission denied" in text
        assert "42ms" in text

    @pytest.mark.asyncio
    async def test_supplemented_done_skips_reply_send(self) -> None:
        """A ``Done(finish_reason="supplemented")`` on the QQ handler must
        skip the reply ``send_action`` entirely — the running turn for
        the same session_key already has the user's text and will
        produce the actual reply when it finishes.
        """
        svc = _ScriptedChatService([
            _Ev(kind="done", finish_reason="supplemented"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="再问一句")
        adapter = _FakeOneBotAdapter()

        import asyncio

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        # No reply action sent — the handler stayed silent.
        assert adapter.sent == []

    # -- todo_write rendering (QQ summary block) -----------------------------

    @pytest.mark.asyncio
    async def test_qq_summary_no_longer_prepends_todo_list(self) -> None:
        """``todo_write`` calls must NOT prepend the checkbox view on
        the QQ summary block. Pending ``☐`` rows are forward-looking
        noise on a non-editable channel — the operation log alone is
        the user-visible "what just happened" signal. The legacy
        ``📋 本次操作:`` header style applies whenever the activity log
        is non-empty (no "🔧 操作:" lifted-header variant)."""
        import asyncio
        import json

        first_todos = json.dumps({"todos": [
            {"content": "Search market data",
             "activeForm": "Searching market data",
             "status": "in_progress"},
            {"content": "Draft memo",
             "activeForm": "Drafting memo",
             "status": "pending"},
        ]}).encode("utf-8")
        final_todos = json.dumps({"todos": [
            {"content": "Search market data",
             "activeForm": "Searching market data",
             "status": "completed"},
            {"content": "Draft memo",
             "activeForm": "Drafting memo",
             "status": "in_progress"},
        ]}).encode("utf-8")

        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="builtin", tool="todo_write",
                args_json=first_todos),
            _Ev(kind="tool_result", plugin="builtin", tool="todo_write",
                duration_ms=3, is_error=False),
            _Ev(kind="tool_call", plugin="web_search", tool="web_search",
                args_json=b'{"query":"gpt-5.5 news"}'),
            _Ev(kind="tool_result", plugin="web_search", tool="web_search",
                duration_ms=302, is_error=False),
            _Ev(kind="tool_call", plugin="builtin", tool="todo_write",
                args_json=final_todos),
            _Ev(kind="tool_result", plugin="builtin", tool="todo_write",
                duration_ms=2, is_error=False),
            _Ev(kind="token_delta", text="answer"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        assert len(adapter.sent) == 1
        text = adapter.sent[0].message[1].text
        # No todo block anywhere — no header, no checkbox glyphs.
        assert "📋 任务清单 (" not in text
        assert "☑" not in text
        assert "▣" not in text
        assert "☐" not in text
        # No reference to the todo_write tool itself either.
        assert "todo_write" not in text
        # Lifted-header variant is gone too — back to the legacy shape.
        assert "🔧 操作:" not in text

    @pytest.mark.asyncio
    async def test_qq_summary_keeps_operation_log(self) -> None:
        """The activity log still surfaces with the legacy
        ``📋 本次操作:`` header. Each non-todo tool call resolves to
        ``✅ <tool> (<duration>)`` and the divider sits between the
        log and the body."""
        import asyncio
        import json

        todos = json.dumps({"todos": [
            {"content": "Plan the work",
             "activeForm": "Planning the work",
             "status": "in_progress"},
        ]}).encode("utf-8")
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="builtin", tool="todo_write",
                args_json=todos),
            _Ev(kind="tool_result", plugin="builtin", tool="todo_write",
                duration_ms=3, is_error=False),
            _Ev(kind="tool_call", plugin="web_search", tool="web_search",
                args_json=b'{"query":"gpt-5.5 news"}'),
            _Ev(kind="tool_result", plugin="web_search", tool="web_search",
                duration_ms=302, is_error=False),
            _Ev(kind="token_delta", text="answer"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        text = adapter.sent[0].message[1].text
        # Legacy header style — ordering: header < tool line < body.
        assert "📋 本次操作:" in text
        hdr_idx = text.find("📋 本次操作:")
        tool_idx = text.find("web_search")
        body_idx = text.find("answer")
        assert hdr_idx != -1 and tool_idx != -1 and body_idx != -1
        assert hdr_idx < tool_idx < body_idx, text
        # Tool resolved with ✅ + duration.
        assert "✅ web_search" in text
        assert "302ms" in text
        # Divider between the log and the body.
        assert "─────" in text
        # No todo artefacts.
        assert "📋 任务清单" not in text
        assert "todo_write" not in text

    @pytest.mark.asyncio
    async def test_qq_summary_todo_only_no_other_tools_is_silent(self) -> None:
        """When the ONLY tool activity is ``todo_write``, the QQ summary
        builds to the empty string (no activity to log) — so the reply
        body ships alone, with NO prelude block at all."""
        import asyncio
        import json

        todos = json.dumps({"todos": [
            {"content": "Verify the patch",
             "activeForm": "Verifying the patch",
             "status": "in_progress"},
        ]}).encode("utf-8")
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="builtin", tool="todo_write",
                args_json=todos),
            _Ev(kind="tool_result", plugin="builtin", tool="todo_write",
                duration_ms=1, is_error=False),
            _Ev(kind="token_delta", text="working on it"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        text = adapter.sent[0].message[1].text
        assert "📋 任务清单" not in text
        assert "📋 本次操作:" not in text
        assert "─────" not in text
        # Just the assistant body, nothing else.
        assert text.strip() == "working on it"

    @pytest.mark.asyncio
    async def test_summary_env_disable_hides_todo_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``CORLINMAN_QQ_TOOL_SUMMARY=0`` still suppresses the prelude
        in full — even though the todo block was already dropped at the
        builder layer, the env knob remains the user-facing escape
        hatch for the operation log."""
        import asyncio
        import json

        monkeypatch.setenv("CORLINMAN_QQ_TOOL_SUMMARY", "0")
        todos = json.dumps({"todos": [
            {"content": "x", "activeForm": "doing x", "status": "pending"},
        ]}).encode("utf-8")
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="builtin", tool="todo_write",
                args_json=todos),
            _Ev(kind="tool_call", plugin="web_search", tool="web_search",
                args_json=b'{"query":"x"}'),
            _Ev(kind="token_delta", text="just text"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        await handle_one_qq(svc, req, ev, "m", adapter, asyncio.Event())  # type: ignore[arg-type]
        text = adapter.sent[0].message[1].text
        assert "📋 任务清单" not in text
        assert "📋 本次操作:" not in text
        assert "web_search" not in text
        assert "just text" in text


# ---------------------------------------------------------------------------
# handle_one_telegram
# ---------------------------------------------------------------------------


class TestHandleOneTelegram:
    @pytest.mark.asyncio
    async def test_concat_and_send(self) -> None:
        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="hi "),
            _Ev(kind="token_delta", text="there"),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=42, user_id=42)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram",
            binding=binding,
            text="ping",
            message_id="7",
            timestamp=0,
            mentioned=True,
            attachments=[],
            payload=None,
        )
        sender = _FakeTelegramSender()

        import asyncio

        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]
        # New behavior: placeholder sent up-front, edited as events land,
        # final edit overwrites it with the assistant reply.
        assert len(sender.sent) == 1
        ph_chat, ph_text, ph_reply_to = sender.sent[0]
        assert ph_chat == 42
        assert "思考中" in ph_text or "Thinking" in ph_text
        assert ph_reply_to == 7
        # The final edited text must be the joined reply.
        assert sender.edits, "expected at least one edit_message_text call"
        last_edit = sender.edits[-1]
        assert last_edit[0] == 42
        assert last_edit[2] == "hi there"
        # The status spinner must have switched to the "generating" state
        # at some point during the run (we emitted token_delta events).
        edit_texts = [e[2] for e in sender.edits]
        assert any("生成回复中" in t or "Generating" in t for t in edit_texts)
        # Regression: handle_one_telegram used to pass a plain dict to
        # chat_service.run, which crashed downstream with
        # ``AttributeError: 'dict' object has no attribute 'model'``.
        assert len(svc.calls) == 1
        req = svc.calls[0]
        assert not isinstance(req, dict)
        assert req.model == "m"
        assert req.messages[0].role == "user"
        assert req.messages[0].content == "ping"
        assert req.stream is True

    @pytest.mark.asyncio
    async def test_error_renders_short_reply(self) -> None:
        svc = _ScriptedChatService([_Ev(kind="error", error="nope")])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=42, user_id=42)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram",
            binding=binding,
            text="ping",
            message_id="1",
            timestamp=0,
            mentioned=True,
        )
        sender = _FakeTelegramSender()

        import asyncio

        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]
        # Placeholder went out as the single sent message.
        assert len(sender.sent) == 1
        # The final edited text is the error rendering.
        assert sender.edits, "expected an edit with the error reply"
        _, _, final_text = sender.edits[-1]
        assert "[corlinman error]" in final_text
        assert "nope" in final_text

    @pytest.mark.asyncio
    async def test_send_attachment_uploads_via_sender(
        self, tmp_path: Any
    ) -> None:
        """A tool_call event with tool=send_attachment must trigger a
        real upload via the sender (not just a status edit)."""
        import json

        html = tmp_path / "page.html"
        html.write_text("<!DOCTYPE html><h1>hi</h1>", encoding="utf-8")
        args = json.dumps({"path": str(html), "filename": "page.html"})
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="send_attachment",
                tool="send_attachment",
                args_json=args.encode("utf-8"),
            ),
            _Ev(kind="token_delta", text="文件已发送"),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=7, user_id=7)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram",
            binding=binding,
            text="give me the html",
            message_id="1",
            timestamp=0,
            mentioned=True,
        )
        sender = _FakeTelegramSender()

        import asyncio

        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]
        # The sender must have been called via send_document.
        doc_calls = [s for s in sender.sent if isinstance(s[1], str) and s[1].startswith("[doc")]
        assert doc_calls, f"expected send_document; got sent={sender.sent}"
        # The placeholder must show the upload status.
        edit_texts = [e[2] for e in sender.edits]
        assert any("已发送文件: page.html" in t for t in edit_texts)

    @pytest.mark.asyncio
    async def test_tool_call_event_updates_status(self) -> None:
        """ToolCallEvent must be rendered as a mutable-spinner line that
        edits the placeholder message in place. Mirrors hermes-agent's
        ``_last_activity_desc``."""
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="builtin", tool="read_file"),
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=7, user_id=7)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram",
            binding=binding,
            text="hi",
            message_id="1",
            timestamp=0,
            mentioned=True,
        )
        sender = _FakeTelegramSender()

        import asyncio

        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [e[2] for e in sender.edits]
        assert any("read_file" in t for t in edit_texts)
        # Final edit is the assistant reply, not a status line.
        assert sender.edits[-1][2] == "ok"

    @pytest.mark.asyncio
    async def test_tool_call_renders_arg_preview(self) -> None:
        """tool_call event with args_json must show a per-tool preview
        next to the tool name on the spinner line."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="web_search",
                tool="web_search",
                args_json=b'{"query":"latest gpt-5.5 news"}',
            ),
            _Ev(kind="token_delta", text="done"),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=7, user_id=7)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram", binding=binding, text="hi",
            message_id="1", timestamp=0, mentioned=True,
        )
        sender = _FakeTelegramSender()
        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]
        # Some edit must surface the query string as the preview.
        edits = [e[2] for e in sender.edits]
        assert any("latest gpt-5.5 news" in t for t in edits), edits

    @pytest.mark.asyncio
    async def test_tool_result_renders_duration_success(self) -> None:
        """tool_result event must render ✅ + human duration."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="web_search", tool="web_search",
                args_json=b'{"query":"x"}'),
            _Ev(kind="tool_result", plugin="web_search", tool="web_search",
                duration_ms=1234, is_error=False),
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=7, user_id=7)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram", binding=binding, text="hi",
            message_id="1", timestamp=0, mentioned=True,
        )
        sender = _FakeTelegramSender()
        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edits = [e[2] for e in sender.edits]
        assert any(("✅" in t and "1.2s" in t) for t in edits), edits

    @pytest.mark.asyncio
    async def test_tool_result_renders_error(self) -> None:
        """tool_result with is_error=True must render ❌ + summary."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="run_shell", tool="run_shell",
                args_json=b'{"command":"rm -rf /"}'),
            _Ev(kind="tool_result", plugin="run_shell", tool="run_shell",
                duration_ms=42, is_error=True,
                error_summary="permission denied"),
            _Ev(kind="token_delta", text="failed"),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=7, user_id=7)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram", binding=binding, text="hi",
            message_id="1", timestamp=0, mentioned=True,
        )
        sender = _FakeTelegramSender()
        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edits = [e[2] for e in sender.edits]
        assert any(("❌" in t and "permission denied" in t) for t in edits), edits

    @pytest.mark.asyncio
    async def test_reasoning_delta_shows_thinking_line(self) -> None:
        """token_delta with is_reasoning=True must render as 💭 推理: …
        and NOT be accumulated into the final reply."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="let me think about this",
                is_reasoning=True),
            _Ev(kind="token_delta", text="the answer is 42"),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=7, user_id=7)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram", binding=binding, text="hi",
            message_id="1", timestamp=0, mentioned=True,
        )
        sender = _FakeTelegramSender()
        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edits = [e[2] for e in sender.edits]
        # The reasoning text must appear on a 💭 line.
        assert any(("💭" in t and "let me think" in t) for t in edits), edits
        # The final reply must NOT contain the reasoning text.
        assert sender.edits[-1][2] == "the answer is 42"

    @pytest.mark.asyncio
    async def test_handle_one_telegram_sends_buttons_when_ask_user_called(
        self,
    ) -> None:
        """An ``ask_user`` tool call with canned options must surface a
        final ``send_message`` with an ``inline_keyboard`` payload — the
        button labels are the option strings the agent supplied."""
        import asyncio
        import json

        args = json.dumps(
            {
                "question": "Overwrite README.md?",
                "options": ["yes", "no", "let me think"],
            }
        ).encode()
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="builtin",
                tool="ask_user",
                args_json=args,
            ),
            _Ev(
                kind="tool_result",
                plugin="builtin",
                tool="ask_user",
                duration_ms=1,
            ),
            _Ev(kind="token_delta", text="Overwrite README.md?"),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=42, user_id=42)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram",
            binding=binding,
            text="please rewrite the readme",
            message_id="7",
            timestamp=0,
            mentioned=True,
        )
        sender = _FakeTelegramSender()
        await handle_one_telegram(
            svc, inbound, "m", sender, asyncio.Event()  # type: ignore[arg-type]
        )

        # At least one send_message call must carry an inline_keyboard.
        keyboards = [kb for kb in sender.sent_keyboards if kb is not None]
        assert keyboards, (
            "expected a send_message with inline_keyboard; sent_keyboards="
            f"{sender.sent_keyboards!r}"
        )
        kb = keyboards[-1]
        # Each option becomes one row with a single button.
        assert len(kb) == 3
        labels = [row[0]["text"] for row in kb]
        assert labels == ["yes", "no", "let me think"]
        # callback_data echoes the label (so the inbound flow gets a
        # meaningful synthesized text on press).
        for row, label in zip(kb, labels, strict=False):
            assert row[0]["callback_data"] == label

    @pytest.mark.asyncio
    async def test_handle_one_telegram_no_buttons_when_ask_user_omits_options(
        self,
    ) -> None:
        """``ask_user`` without options is a plain question — no
        inline_keyboard should attach to the final reply (otherwise the
        Telegram client renders an empty button bar)."""
        import asyncio
        import json

        args = json.dumps({"question": "What's the deadline?"}).encode()
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="builtin",
                tool="ask_user",
                args_json=args,
            ),
            _Ev(kind="token_delta", text="What's the deadline?"),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=42, user_id=42)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram", binding=binding, text="hi",
            message_id="1", timestamp=0, mentioned=True,
        )
        sender = _FakeTelegramSender()
        await handle_one_telegram(
            svc, inbound, "m", sender, asyncio.Event()  # type: ignore[arg-type]
        )
        # No send_message call may carry a keyboard.
        assert all(kb is None for kb in sender.sent_keyboards)

    @pytest.mark.asyncio
    async def test_placeholder_send_raises_cancels_typing_pulse(self) -> None:
        """If the initial placeholder send raises, the background
        typing-pulse task must still be cancelled — otherwise it would
        keep firing ``sendChatAction`` forever after the turn ended."""
        import asyncio

        svc = _ScriptedChatService([_Ev(kind="done")])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=42, user_id=42)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram",
            binding=binding,
            text="ping",
            message_id="7",
            timestamp=0,
            mentioned=True,
        )
        sender = _FakeTelegramSender()
        sender.send_message_should_raise = True

        all_tasks_before = set(asyncio.all_tasks())
        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]
        # Give any leaked pulse one tick to misbehave.
        await asyncio.sleep(0.05)
        new_tasks = set(asyncio.all_tasks()) - all_tasks_before
        # The typing-pulse task must NOT be among the live tasks.
        live = [t for t in new_tasks if not t.done()]
        assert not live, f"typing pulse leaked: {live}"

    @pytest.mark.asyncio
    async def test_final_edit_failure_does_not_crash(self) -> None:
        """If the final ``edit_message_text`` raises (rate limit /
        blocked user), the function must still return cleanly instead
        of propagating the exception out of the channel loop."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="hi"),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=42, user_id=42)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram",
            binding=binding,
            text="ping",
            message_id="7",
            timestamp=0,
            mentioned=True,
        )
        sender = _FakeTelegramSender()

        # Let in-stream edits succeed; flip the flag just before the
        # final emit by wrapping ``edit_message_text`` and triggering
        # the failure on the last call (text == reply body "hi").
        original_edit = sender.edit_message_text

        async def _edit(chat_id: int, message_id: int, text: str) -> None:
            if text == "hi":
                raise RuntimeError("simulated final edit failure")
            await original_edit(chat_id, message_id, text)

        sender.edit_message_text = _edit  # type: ignore[assignment]
        # Must NOT raise — the channel loop must keep going.
        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_typing_pulse_fires_until_cancelled(self) -> None:
        """The sendChatAction pulse helper must fire at least once per
        interval and stop on cancel.set()."""
        import asyncio

        from corlinman_channels.service import _telegram_typing_pulse

        sender = _FakeTelegramSender()
        cancel = asyncio.Event()

        async def stop_after_two_pulses() -> None:
            await asyncio.sleep(0.15)
            cancel.set()

        await asyncio.gather(
            _telegram_typing_pulse(
                sender,  # type: ignore[arg-type]
                chat_id=42,
                cancel=cancel,
                interval_s=0.05,
            ),
            stop_after_two_pulses(),
        )
        assert sender.chat_actions, "expected at least one sendChatAction"
        assert all(a == "typing" for _, a in sender.chat_actions)
        # Pulse must stop cleanly after cancel — no runaway loop.
        actions_at_stop = len(sender.chat_actions)
        await asyncio.sleep(0.1)
        assert len(sender.chat_actions) == actions_at_stop

    @pytest.mark.asyncio
    async def test_long_reply_splits_into_multiple_messages(self) -> None:
        """Replies longer than Telegram's 4000-char cap MUST be split
        into chunks and sent as separate messages — never truncated with
        a "[已截断]" marker. The placeholder edit gets chunk 1; chunks
        2..N are follow-up sendMessage calls keyed to the same reply
        target. Together the chunks must reconstruct the full body."""
        import asyncio
        import re

        # Use varied content so chunk_reply has real boundaries to find.
        # Pure "xxxxx" lacks structure so it'd hard-cut at the limit —
        # interleave paragraphs to exercise the natural-boundary path.
        big = ("Paragraph " + "x" * 50 + ".\n\n") * 200
        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text=big),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=42, user_id=42)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram",
            binding=binding,
            text="ping",
            message_id="7",
            timestamp=0,
            mentioned=True,
        )
        sender = _FakeTelegramSender()

        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]

        # The placeholder edit holds chunk 1.
        assert sender.edits, "expected a final edit with chunk 1"
        _, _, chunk1 = sender.edits[-1]
        assert len(chunk1) <= 4096
        # Chunk 1 carries the (1/N) prefix.
        assert re.match(r"^\(1/\d+\)\n", chunk1), (
            f"chunk 1 missing prefix: {chunk1[:30]!r}"
        )
        # Follow-up sends carry chunks 2..N. _FakeTelegramSender.sent
        # records (chat_id, text, reply_to_message_id) tuples. The very
        # first send is the placeholder ("🧠 思考中..."); the rest are
        # our chunk follow-ups.
        followups = [m for m in sender.sent if "🧠" not in m[1]]
        assert len(followups) >= 1, "expected ≥1 follow-up send for long reply"
        for i, (_, body, _) in enumerate(followups, start=2):
            assert re.match(rf"^\({i}/\d+\)\n", body), (
                f"chunk {i} missing prefix: {body[:30]!r}"
            )
            assert len(body) <= 4096
        # No chunk should contain the truncation marker — the whole
        # body is preserved across the split.
        assert "回复过长" not in chunk1
        for _, body, _ in followups:
            assert "回复过长" not in body

    @pytest.mark.asyncio
    async def test_supplemented_done_skips_final_emit(self) -> None:
        """A ``Done(finish_reason="supplemented")`` must NOT trigger a final
        edit / send — the agent absorbed our user text into a running turn
        and the original turn will produce the actual reply.

        Regression: without the supplemented short-circuit the handler
        would overwrite the placeholder with ``"（无回复）"`` (the empty-reply
        cleanup branch), making the user think the bot dropped their
        supplemental message.
        """
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="done", finish_reason="supplemented"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=42, user_id=42)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram",
            binding=binding,
            text="第二条消息",
            message_id="9",
            timestamp=0,
            mentioned=True,
        )
        sender = _FakeTelegramSender()

        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]

        # Placeholder was sent (decorative spinner) — but no final edit
        # or "(no reply)" overwrite happened. The first turn's
        # placeholder remains in place until the running turn yields
        # its own reply.
        assert len(sender.sent) == 1, (
            "expected only the initial placeholder; no extra send"
        )
        # No edits with the empty-reply cleanup text — the handler
        # must not have touched the placeholder.
        for _, _, edit_text in sender.edits:
            assert "（无回复）" not in edit_text, (
                "supplemented Done must not trigger the empty-reply cleanup"
            )

    # -- todo_write rendering (Telegram spinner) -----------------------------

    @pytest.mark.asyncio
    async def test_telegram_spinner_renders_todo_list(self) -> None:
        """A ``tool_call(todo_write)`` event must edit the placeholder
        with the rendered checkbox list. Subsequent ``token_delta`` +
        ``done`` overwrite it with the assistant reply; we only assert
        that at least one mid-turn edit carried the list."""
        import asyncio
        import json

        todos = json.dumps({"todos": [
            {"content": "Search market data",
             "activeForm": "Searching market data",
             "status": "completed"},
            {"content": "Collate vendor list",
             "activeForm": "Collating vendor list",
             "status": "in_progress"},
            {"content": "Send the report",
             "activeForm": "Sending the report",
             "status": "pending"},
        ]}).encode("utf-8")
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="builtin", tool="todo_write",
                args_json=todos),
            _Ev(kind="tool_result", plugin="builtin", tool="todo_write",
                duration_ms=3, is_error=False),
            _Ev(kind="token_delta", text="done"),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=7, user_id=7)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram", binding=binding, text="hi",
            message_id="1", timestamp=0, mentioned=True,
        )
        sender = _FakeTelegramSender()
        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]
        # At least one mid-turn edit must carry the rendered list.
        edits = [e[2] for e in sender.edits]
        assert any("📋 任务清单 (1/3)" in t for t in edits), edits
        # The list edit must carry all three checkbox glyphs.
        list_edits = [t for t in edits if "📋 任务清单" in t]
        assert list_edits, edits
        sample = list_edits[0]
        assert "☑" in sample
        assert "▣" in sample
        assert "☐" in sample
        # The in_progress row uses the activeForm (present-continuous).
        assert "Collating vendor list" in sample
        # Final edit overwrites everything with the assistant reply.
        assert sender.edits[-1][2] == "done"

    @pytest.mark.asyncio
    async def test_telegram_spinner_suppresses_todo_write_completion(
        self,
    ) -> None:
        """``tool_result(todo_write)`` must NOT fire a ``✅ todo_write``
        edit — the checkbox list rendered on the call side is the
        signal; a trailing completion line would just clutter it."""
        import asyncio
        import json

        todos = json.dumps({"todos": [
            {"content": "a", "activeForm": "doing a", "status": "pending"},
        ]}).encode("utf-8")
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="builtin", tool="todo_write",
                args_json=todos),
            _Ev(kind="tool_result", plugin="builtin", tool="todo_write",
                duration_ms=3, is_error=False),
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        binding = ChannelBinding.telegram(bot_id=999, chat_id=7, user_id=7)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram", binding=binding, text="hi",
            message_id="1", timestamp=0, mentioned=True,
        )
        sender = _FakeTelegramSender()
        await handle_one_telegram(svc, inbound, "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edits = [e[2] for e in sender.edits]
        # No ✅ todo_write completion line anywhere — the suppression
        # is unconditional, same shape as send_attachment.
        for t in edits:
            assert "✅ todo_write" not in t, t
        # And no "🔧 todo_write" generic fallback either — we rendered
        # the list directly.
        for t in edits:
            assert "🔧 todo_write" not in t, t


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestRunChannelConfig:
    @pytest.mark.asyncio
    async def test_run_qq_channel_requires_ws_url(self) -> None:
        import asyncio

        params = QqChannelParams(
            config=SimpleNamespace(ws_url="", self_ids=[100]),
        )
        with pytest.raises(ValueError, match="ws_url"):
            await run_qq_channel(params, asyncio.Event())

    @pytest.mark.asyncio
    async def test_run_qq_channel_allows_empty_self_ids(self) -> None:
        """``self_ids`` is optional now — the bot id is auto-detected
        from the OneBot event stream, so an empty config value must not
        raise. (cancel is pre-set so the loop exits immediately.)"""
        import asyncio

        cancel = asyncio.Event()
        cancel.set()
        params = QqChannelParams(
            config=SimpleNamespace(ws_url="ws://x", self_ids=[]),
        )
        # Must not raise ValueError about self_ids.
        await run_qq_channel(params, cancel)

    @pytest.mark.asyncio
    async def test_run_telegram_channel_requires_bot_token(self) -> None:
        import asyncio

        params = TelegramChannelParams(config=SimpleNamespace(bot_token=""))
        with pytest.raises(ValueError, match="bot_token"):
            await run_telegram_channel(params, asyncio.Event())

    @pytest.mark.asyncio
    async def test_run_discord_channel_requires_bot_token(self) -> None:
        import asyncio

        params = DiscordChannelParams(config=SimpleNamespace(bot_token=""))
        with pytest.raises(ValueError, match="bot_token"):
            await run_discord_channel(params, asyncio.Event())

    @pytest.mark.asyncio
    async def test_run_slack_channel_requires_app_token(self) -> None:
        import asyncio

        params = SlackChannelParams(
            config=SimpleNamespace(app_token="", bot_token="xoxb")
        )
        with pytest.raises(ValueError, match="app_token"):
            await run_slack_channel(params, asyncio.Event())

    @pytest.mark.asyncio
    async def test_run_slack_channel_requires_bot_token(self) -> None:
        import asyncio

        params = SlackChannelParams(
            config=SimpleNamespace(app_token="xapp", bot_token="")
        )
        with pytest.raises(ValueError, match="bot_token"):
            await run_slack_channel(params, asyncio.Event())

    @pytest.mark.asyncio
    async def test_run_feishu_channel_requires_app_id(self) -> None:
        import asyncio

        params = FeishuChannelParams(
            config=SimpleNamespace(app_id="", app_secret="s")
        )
        with pytest.raises(ValueError, match="app_id"):
            await run_feishu_channel(params, asyncio.Event())

    @pytest.mark.asyncio
    async def test_run_feishu_channel_requires_app_secret(self) -> None:
        import asyncio

        params = FeishuChannelParams(
            config=SimpleNamespace(app_id="a", app_secret="")
        )
        with pytest.raises(ValueError, match="app_secret"):
            await run_feishu_channel(params, asyncio.Event())


# ---------------------------------------------------------------------------
# handle_one_discord / handle_one_slack / handle_one_feishu — these three
# now share the Telegram-style mutable-spinner UX. Each section asserts
# the four canonical scenarios:
#   1. tool_call renders the arg preview on the spinner
#   2. tool_result renders ✅ + human duration
#   3. tool_result with is_error renders ❌ + summary
#   4. reasoning delta shows 💭 line + is excluded from the final reply
# plus the placeholder / final-edit / error round-trip the prior tests
# already covered.
# ---------------------------------------------------------------------------


class _FakeDiscordSender:
    """Fake :class:`DiscordSender` — captures every outbound call.

    Discord's mutable-spinner UX uses ``send_message`` to drop the
    initial placeholder, ``edit_message`` for every spinner edit, and
    ``trigger_typing`` for the typing-pulse helper. ``send_file`` is the
    multipart upload triggered by the ``send_attachment`` tool intercept.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.edits: list[tuple[str, str, str]] = []
        self.typings: list[str] = []
        self.files: list[tuple[str, str, str | None, str | None]] = []
        self._next_id = 0
        # Test knobs.
        self.send_message_should_raise = False
        self.edit_message_should_raise = False

    async def send_message(
        self,
        channel_id: str,
        text: str,
        reply_to_message_id: str | None = None,
    ) -> str:
        if self.send_message_should_raise:
            raise RuntimeError("simulated send_message failure")
        self._next_id += 1
        self.sent.append((channel_id, text, reply_to_message_id))
        return f"msg-{self._next_id}"

    async def edit_message(
        self, channel_id: str, message_id: str, content: str
    ) -> None:
        if self.edit_message_should_raise:
            raise RuntimeError("simulated edit_message failure")
        self.edits.append((channel_id, message_id, content))

    async def trigger_typing(self, channel_id: str) -> None:
        self.typings.append(channel_id)

    async def send_file(
        self,
        channel_id: str,
        path: Any,
        *,
        filename: str | None = None,
        content: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> str:
        self._next_id += 1
        self.files.append(
            (channel_id, str(path), filename, content)
        )
        return f"file-{self._next_id}"


class _FakeSlackSender:
    """Fake :class:`SlackSender`. ``post_typing`` is a stub for parity."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.updates: list[tuple[str, str, str]] = []
        self.typings: list[tuple[str, str | None]] = []
        self.uploads: list[tuple[str, str, str | None, str | None, str | None]] = []
        self._next_id = 0
        self.send_message_should_raise = False
        self.update_message_should_raise = False

    async def send_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> str:
        if self.send_message_should_raise:
            raise RuntimeError("simulated send_message failure")
        self._next_id += 1
        self.sent.append((channel, text, thread_ts))
        return f"1.{self._next_id}"

    async def update_message(
        self, channel: str, ts: str, text: str
    ) -> None:
        if self.update_message_should_raise:
            raise RuntimeError("simulated update_message failure")
        self.updates.append((channel, ts, text))

    async def post_typing(
        self, channel: str, thread_ts: str | None = None
    ) -> None:
        self.typings.append((channel, thread_ts))

    async def upload_file(
        self,
        channels: str,
        path: Any,
        *,
        filename: str | None = None,
        initial_comment: str | None = None,
        thread_ts: str | None = None,
    ) -> str:
        self._next_id += 1
        self.uploads.append(
            (channels, str(path), filename, initial_comment, thread_ts)
        )
        return f"F-{self._next_id}"


class _FakeFeishuSender:
    """Fake :class:`FeishuSender`."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.updates: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, str | None]] = []
        self.file_messages: list[tuple[str, str, str | None]] = []
        self._next_id = 0
        self.send_message_should_raise = False
        self.update_message_should_raise = False

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to_message_id: str | None = None,
    ) -> str:
        if self.send_message_should_raise:
            raise RuntimeError("simulated send_message failure")
        self._next_id += 1
        self.sent.append((chat_id, text, reply_to_message_id))
        return f"om-{self._next_id}"

    async def update_message(self, message_id: str, text: str) -> None:
        if self.update_message_should_raise:
            raise RuntimeError("simulated update_message failure")
        self.updates.append((message_id, text))

    async def upload_file(
        self, path: Any, *, filename: str | None = None, file_type: str = "stream",
    ) -> str:
        self._next_id += 1
        self.uploads.append((str(path), filename))
        return f"fk-{self._next_id}"

    async def send_file_message(
        self,
        chat_id: str,
        file_key: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> str:
        self._next_id += 1
        self.file_messages.append((chat_id, file_key, reply_to_message_id))
        return f"omf-{self._next_id}"


def _inbound(channel: str) -> InboundEvent[Any]:
    binding = ChannelBinding(
        channel=channel, account="bot", thread="T1", sender="U1"
    )
    return InboundEvent(
        channel=channel,
        binding=binding,
        text="ping",
        message_id="M1",
        timestamp=0,
        mentioned=True,
    )


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


class TestHandleOneDiscord:
    @pytest.mark.asyncio
    async def test_concat_and_send(self) -> None:
        """Token deltas accumulate and land as the final edit on the
        placeholder — same UX shape as Telegram."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="he"),
            _Ev(kind="token_delta", text="llo"),
            _Ev(kind="done"),
        ])
        sender = _FakeDiscordSender()
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        # Placeholder went out as a normal sendMessage.
        assert len(sender.sent) == 1
        chan, ph_text, reply_to = sender.sent[0]
        assert chan == "T1"
        assert "思考中" in ph_text
        assert reply_to == "M1"
        # Final edit text is the joined reply.
        assert sender.edits, "expected at least one edit_message call"
        last_edit = sender.edits[-1]
        assert last_edit[2] == "hello"
        # Generating spinner appeared at some point.
        edit_texts = [e[2] for e in sender.edits]
        assert any("生成回复中" in t for t in edit_texts)
        # Regression: the handler must pass a SimpleNamespace, not a dict,
        # to chat_service.run (downstream attribute access).
        req = svc.calls[0]
        assert not isinstance(req, dict)
        assert req.model == "m"
        assert req.messages[0].content == "ping"

    @pytest.mark.asyncio
    async def test_error_renders_short_reply(self) -> None:
        """error event renders as a final [corlinman error] edit."""
        import asyncio

        svc = _ScriptedChatService([_Ev(kind="error", error="boom")])
        sender = _FakeDiscordSender()
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        assert sender.edits, "expected error reply edit"
        _, _, final_text = sender.edits[-1]
        assert "[corlinman error]" in final_text
        assert "boom" in final_text

    @pytest.mark.asyncio
    async def test_empty_reply_edits_placeholder(self) -> None:
        """An empty assistant reply must tidy the placeholder rather
        than leave it stuck at the generating spinner."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="  "),
            _Ev(kind="done"),
        ])
        sender = _FakeDiscordSender()
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        # No new send_message after the placeholder.
        assert len(sender.sent) == 1
        # The placeholder was edited to a tidy "no reply" marker.
        assert sender.edits, "expected the placeholder to be tidied"
        assert sender.edits[-1][2] == "（无回复）"

    @pytest.mark.asyncio
    async def test_tool_call_renders_arg_preview(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="web_search",
                tool="web_search",
                args_json=b'{"query":"latest gpt-5.5 news"}',
            ),
            _Ev(kind="token_delta", text="done"),
            _Ev(kind="done"),
        ])
        sender = _FakeDiscordSender()
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [e[2] for e in sender.edits]
        assert any("latest gpt-5.5 news" in t for t in edit_texts), edit_texts

    @pytest.mark.asyncio
    async def test_tool_result_renders_duration_success(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="web_search", tool="web_search",
                args_json=b'{"query":"x"}'),
            _Ev(kind="tool_result", plugin="web_search", tool="web_search",
                duration_ms=1234, is_error=False),
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        sender = _FakeDiscordSender()
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [e[2] for e in sender.edits]
        assert any(("✅" in t and "1.2s" in t) for t in edit_texts), edit_texts

    @pytest.mark.asyncio
    async def test_tool_result_renders_error(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="run_shell", tool="run_shell",
                args_json=b'{"command":"rm -rf /"}'),
            _Ev(kind="tool_result", plugin="run_shell", tool="run_shell",
                duration_ms=42, is_error=True,
                error_summary="permission denied"),
            _Ev(kind="token_delta", text="failed"),
            _Ev(kind="done"),
        ])
        sender = _FakeDiscordSender()
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [e[2] for e in sender.edits]
        assert any(("❌" in t and "permission denied" in t) for t in edit_texts), edit_texts

    @pytest.mark.asyncio
    async def test_reasoning_delta_shows_thinking_line(self) -> None:
        """token_delta with is_reasoning=True must render as 💭 推理: …
        and NOT be accumulated into the final reply."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="let me think about this",
                is_reasoning=True),
            _Ev(kind="token_delta", text="the answer is 42"),
            _Ev(kind="done"),
        ])
        sender = _FakeDiscordSender()
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [e[2] for e in sender.edits]
        assert any(("💭" in t and "let me think" in t) for t in edit_texts), edit_texts
        # Final reply must NOT contain the reasoning text.
        assert sender.edits[-1][2] == "the answer is 42"

    @pytest.mark.asyncio
    async def test_send_attachment_uploads_via_sender(self, tmp_path: Any) -> None:
        """A tool_call event with tool=send_attachment must trigger a
        real multipart upload via sender.send_file (not just a status edit)."""
        import asyncio
        import json

        html = tmp_path / "page.html"
        html.write_text("<!DOCTYPE html><h1>hi</h1>", encoding="utf-8")
        args = json.dumps({"path": str(html), "filename": "page.html"})
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="send_attachment",
                tool="send_attachment",
                args_json=args.encode("utf-8"),
            ),
            _Ev(kind="token_delta", text="文件已发送"),
            _Ev(kind="done"),
        ])
        sender = _FakeDiscordSender()
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        assert sender.files, f"expected send_file call; got files={sender.files}"
        assert sender.files[0][2] == "page.html"
        # The placeholder must show the upload status.
        edit_texts = [e[2] for e in sender.edits]
        assert any("已发送文件: page.html" in t for t in edit_texts)

    @pytest.mark.asyncio
    async def test_typing_pulse_fires_until_cancelled(self) -> None:
        import asyncio

        from corlinman_channels.service import _discord_typing_pulse

        sender = _FakeDiscordSender()
        cancel = asyncio.Event()

        async def stop() -> None:
            await asyncio.sleep(0.15)
            cancel.set()

        await asyncio.gather(
            _discord_typing_pulse(
                sender,  # type: ignore[arg-type]
                channel_id="T1",
                cancel=cancel,
                interval_s=0.05,
            ),
            stop(),
        )
        assert sender.typings, "expected at least one trigger_typing call"
        assert all(c == "T1" for c in sender.typings)
        count_at_stop = len(sender.typings)
        await asyncio.sleep(0.1)
        assert len(sender.typings) == count_at_stop

    @pytest.mark.asyncio
    async def test_placeholder_send_raises_cancels_typing_pulse(self) -> None:
        """If the placeholder send raises, the typing-pulse task must
        still be cancelled."""
        import asyncio

        svc = _ScriptedChatService([_Ev(kind="done")])
        sender = _FakeDiscordSender()
        sender.send_message_should_raise = True

        all_tasks_before = set(asyncio.all_tasks())
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        await asyncio.sleep(0.05)
        new_tasks = set(asyncio.all_tasks()) - all_tasks_before
        live = [t for t in new_tasks if not t.done()]
        assert not live, f"typing pulse leaked: {live}"

    @pytest.mark.asyncio
    async def test_final_edit_failure_does_not_crash(self) -> None:
        """If the final edit_message raises, the function must still
        return cleanly."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="hi"),
            _Ev(kind="done"),
        ])
        sender = _FakeDiscordSender()
        original_edit = sender.edit_message

        async def _edit(channel_id: str, message_id: str, content: str) -> None:
            if content == "hi":
                raise RuntimeError("simulated final edit failure")
            await original_edit(channel_id, message_id, content)

        sender.edit_message = _edit  # type: ignore[assignment]
        # Must NOT raise.
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


class TestHandleOneSlack:
    @pytest.mark.asyncio
    async def test_concat_and_send_threaded(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="hi "),
            _Ev(kind="token_delta", text="there"),
            _Ev(kind="done"),
        ])
        sender = _FakeSlackSender()
        await handle_one_slack(svc, _inbound("slack"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        # Placeholder sent into the thread.
        assert len(sender.sent) == 1
        chan, ph_text, thread_ts = sender.sent[0]
        assert chan == "T1"
        assert "思考中" in ph_text
        assert thread_ts == "M1"
        # Final update is the joined reply.
        assert sender.updates, "expected at least one update_message call"
        assert sender.updates[-1][2] == "hi there"
        edit_texts = [u[2] for u in sender.updates]
        assert any("生成回复中" in t for t in edit_texts)
        req = svc.calls[0]
        assert not isinstance(req, dict)
        assert req.model == "m"

    @pytest.mark.asyncio
    async def test_error_renders_short_reply(self) -> None:
        import asyncio

        svc = _ScriptedChatService([_Ev(kind="error", error="nope")])
        sender = _FakeSlackSender()
        await handle_one_slack(svc, _inbound("slack"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        assert sender.updates, "expected error reply update"
        _, _, final_text = sender.updates[-1]
        assert "[corlinman error]" in final_text
        assert "nope" in final_text

    @pytest.mark.asyncio
    async def test_empty_reply_edits_placeholder(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="  "),
            _Ev(kind="done"),
        ])
        sender = _FakeSlackSender()
        await handle_one_slack(svc, _inbound("slack"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        assert len(sender.sent) == 1
        assert sender.updates, "expected tidy placeholder edit"
        assert sender.updates[-1][2] == "（无回复）"

    @pytest.mark.asyncio
    async def test_tool_call_renders_arg_preview(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="web_search",
                tool="web_search",
                args_json=b'{"query":"slack mutable spinner"}',
            ),
            _Ev(kind="token_delta", text="done"),
            _Ev(kind="done"),
        ])
        sender = _FakeSlackSender()
        await handle_one_slack(svc, _inbound("slack"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [u[2] for u in sender.updates]
        assert any("slack mutable spinner" in t for t in edit_texts), edit_texts

    @pytest.mark.asyncio
    async def test_tool_result_renders_duration_success(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="web_search", tool="web_search",
                args_json=b'{"query":"x"}'),
            _Ev(kind="tool_result", plugin="web_search", tool="web_search",
                duration_ms=2500, is_error=False),
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        sender = _FakeSlackSender()
        await handle_one_slack(svc, _inbound("slack"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [u[2] for u in sender.updates]
        assert any(("✅" in t and "2.5s" in t) for t in edit_texts), edit_texts

    @pytest.mark.asyncio
    async def test_tool_result_renders_error(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="run_shell", tool="run_shell",
                args_json=b'{"command":"x"}'),
            _Ev(kind="tool_result", plugin="run_shell", tool="run_shell",
                duration_ms=42, is_error=True,
                error_summary="permission denied"),
            _Ev(kind="token_delta", text="failed"),
            _Ev(kind="done"),
        ])
        sender = _FakeSlackSender()
        await handle_one_slack(svc, _inbound("slack"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [u[2] for u in sender.updates]
        assert any(("❌" in t and "permission denied" in t) for t in edit_texts), edit_texts

    @pytest.mark.asyncio
    async def test_reasoning_delta_shows_thinking_line(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="hmm",
                is_reasoning=True),
            _Ev(kind="token_delta", text="answer"),
            _Ev(kind="done"),
        ])
        sender = _FakeSlackSender()
        await handle_one_slack(svc, _inbound("slack"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [u[2] for u in sender.updates]
        assert any(("💭" in t and "hmm" in t) for t in edit_texts), edit_texts
        # Final reply must NOT contain the reasoning text.
        assert sender.updates[-1][2] == "answer"

    @pytest.mark.asyncio
    async def test_send_attachment_uploads_via_sender(self, tmp_path: Any) -> None:
        import asyncio
        import json

        html = tmp_path / "doc.pdf"
        html.write_bytes(b"%PDF-1.4 fake")
        args = json.dumps({"path": str(html), "filename": "doc.pdf"})
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="send_attachment",
                tool="send_attachment",
                args_json=args.encode("utf-8"),
            ),
            _Ev(kind="token_delta", text="文件已发送"),
            _Ev(kind="done"),
        ])
        sender = _FakeSlackSender()
        await handle_one_slack(svc, _inbound("slack"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        assert sender.uploads, f"expected upload_file call; got uploads={sender.uploads}"
        assert sender.uploads[0][2] == "doc.pdf"
        # Thread_ts threaded into the upload to keep replies grouped.
        assert sender.uploads[0][4] == "M1"
        edit_texts = [u[2] for u in sender.updates]
        assert any("已发送文件: doc.pdf" in t for t in edit_texts)

    @pytest.mark.asyncio
    async def test_post_typing_is_noop_stub(self) -> None:
        """Slack has no per-thread typing indicator — post_typing must
        return without raising and without mutating state visibly."""

        sender = _FakeSlackSender()
        # Direct call must not raise.
        await sender.post_typing("T1", "M1")
        # The handle_one path itself doesn't use post_typing (Slack lacks
        # a typing pulse), so updates / sent must remain empty.
        assert sender.sent == []
        assert sender.updates == []


# ---------------------------------------------------------------------------
# Feishu
# ---------------------------------------------------------------------------


class TestHandleOneFeishu:
    @pytest.mark.asyncio
    async def test_concat_and_send_reply(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        sender = _FakeFeishuSender()
        await handle_one_feishu(svc, _inbound("feishu"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        # Placeholder went out as a sendMessage; final edit landed via update_message.
        assert len(sender.sent) == 1
        chat_id, ph_text, reply_to = sender.sent[0]
        assert chat_id == "T1"
        assert "思考中" in ph_text
        assert reply_to == "M1"
        assert sender.updates, "expected at least one update_message call"
        # The last update is the reply text.
        assert sender.updates[-1][1] == "ok"
        req = svc.calls[0]
        assert not isinstance(req, dict)
        assert req.model == "m"

    @pytest.mark.asyncio
    async def test_error_renders_short_reply(self) -> None:
        import asyncio

        svc = _ScriptedChatService([_Ev(kind="error", error="bad")])
        sender = _FakeFeishuSender()
        await handle_one_feishu(svc, _inbound("feishu"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        assert sender.updates, "expected error reply update"
        _, final_text = sender.updates[-1]
        assert "[corlinman error]" in final_text
        assert "bad" in final_text

    @pytest.mark.asyncio
    async def test_empty_reply_edits_placeholder(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="  "),
            _Ev(kind="done"),
        ])
        sender = _FakeFeishuSender()
        await handle_one_feishu(svc, _inbound("feishu"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        assert len(sender.sent) == 1
        assert sender.updates, "expected tidy placeholder edit"
        assert sender.updates[-1][1] == "（无回复）"

    @pytest.mark.asyncio
    async def test_tool_call_renders_arg_preview(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="web_search",
                tool="web_search",
                args_json=b'{"query":"feishu spinner port"}',
            ),
            _Ev(kind="token_delta", text="done"),
            _Ev(kind="done"),
        ])
        sender = _FakeFeishuSender()
        await handle_one_feishu(svc, _inbound("feishu"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [u[1] for u in sender.updates]
        assert any("feishu spinner port" in t for t in edit_texts), edit_texts

    @pytest.mark.asyncio
    async def test_tool_result_renders_duration_success(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="web_search", tool="web_search",
                args_json=b'{"query":"x"}'),
            _Ev(kind="tool_result", plugin="web_search", tool="web_search",
                duration_ms=500, is_error=False),
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        sender = _FakeFeishuSender()
        await handle_one_feishu(svc, _inbound("feishu"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [u[1] for u in sender.updates]
        # < 1000ms renders as 500ms (no decimal)
        assert any(("✅" in t and "500ms" in t) for t in edit_texts), edit_texts

    @pytest.mark.asyncio
    async def test_tool_result_renders_error(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="run_shell", tool="run_shell",
                args_json=b'{"command":"x"}'),
            _Ev(kind="tool_result", plugin="run_shell", tool="run_shell",
                duration_ms=42, is_error=True,
                error_summary="permission denied"),
            _Ev(kind="token_delta", text="failed"),
            _Ev(kind="done"),
        ])
        sender = _FakeFeishuSender()
        await handle_one_feishu(svc, _inbound("feishu"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [u[1] for u in sender.updates]
        assert any(("❌" in t and "permission denied" in t) for t in edit_texts), edit_texts

    @pytest.mark.asyncio
    async def test_reasoning_delta_shows_thinking_line(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="thinking out loud",
                is_reasoning=True),
            _Ev(kind="token_delta", text="42"),
            _Ev(kind="done"),
        ])
        sender = _FakeFeishuSender()
        await handle_one_feishu(svc, _inbound("feishu"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        edit_texts = [u[1] for u in sender.updates]
        assert any(("💭" in t and "thinking out loud" in t) for t in edit_texts), edit_texts
        assert sender.updates[-1][1] == "42"

    @pytest.mark.asyncio
    async def test_send_attachment_uploads_via_sender(self, tmp_path: Any) -> None:
        import asyncio
        import json

        html = tmp_path / "report.csv"
        html.write_text("col1,col2\n1,2\n", encoding="utf-8")
        args = json.dumps({"path": str(html), "filename": "report.csv"})
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="send_attachment",
                tool="send_attachment",
                args_json=args.encode("utf-8"),
            ),
            _Ev(kind="token_delta", text="文件已发送"),
            _Ev(kind="done"),
        ])
        sender = _FakeFeishuSender()
        await handle_one_feishu(svc, _inbound("feishu"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        # Two-step: upload_file mints a file_key, send_file_message posts it.
        assert sender.uploads, f"expected upload_file call; got uploads={sender.uploads}"
        assert sender.uploads[0][1] == "report.csv"
        assert sender.file_messages, "expected send_file_message call"
        # send_file_message must reply to the original message id.
        assert sender.file_messages[0][2] == "M1"
        edit_texts = [u[1] for u in sender.updates]
        assert any("已发送文件: report.csv" in t for t in edit_texts)


class TestQqHealthWatcher:
    """Heartbeat watcher message rendering — regression for the
    ``no NapCat event in Nones`` formatting bug when ``last_event_at_ms``
    is ``None`` (NapCat never sent an event yet)."""

    @pytest.mark.asyncio
    async def test_warns_with_ws_url_when_no_event_ever_received(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import asyncio
        import logging

        from corlinman_channels.service import _qq_health_watcher

        adapter = SimpleNamespace(
            last_event_at_ms=None, url="ws://napcat.example:3001"
        )
        cancel = asyncio.Event()

        async def cancel_soon() -> None:
            # Fire well after the first probe tick (probe_s=1) so the
            # warning has a chance to land before we exit.
            await asyncio.sleep(1.3)
            cancel.set()

        import os

        os.environ["CORLINMAN_QQ_HEALTH_PROBE_S"] = "1"
        os.environ["CORLINMAN_QQ_HEALTH_LOST_S"] = "1"
        try:
            with caplog.at_level(
                logging.WARNING, logger="corlinman_channels.service"
            ):
                await asyncio.wait_for(
                    asyncio.gather(
                        _qq_health_watcher(adapter, cancel),  # type: ignore[arg-type]
                        cancel_soon(),
                    ),
                    timeout=3.0,
                )
        finally:
            os.environ.pop("CORLINMAN_QQ_HEALTH_PROBE_S", None)
            os.environ.pop("CORLINMAN_QQ_HEALTH_LOST_S", None)

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("heartbeat_lost" in m for m in warnings)
        # The buggy version rendered "no NapCat event in Nones"; the fix
        # routes the None case to a different branch that names the ws url.
        joined = "\n".join(warnings)
        assert "Nones" not in joined
        assert "ws://napcat.example:3001" in joined
        assert "received yet" in joined

    @pytest.mark.asyncio
    async def test_warns_with_seconds_when_events_then_silence(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import asyncio
        import logging
        import os
        import time

        from corlinman_channels.service import _qq_health_watcher

        # last event was 200s ago — over the 1s test threshold.
        last = int(time.time() * 1000) - 200_000
        adapter = SimpleNamespace(last_event_at_ms=last, url="ws://x")
        cancel = asyncio.Event()

        async def cancel_soon() -> None:
            await asyncio.sleep(1.3)
            cancel.set()

        os.environ["CORLINMAN_QQ_HEALTH_PROBE_S"] = "1"
        os.environ["CORLINMAN_QQ_HEALTH_LOST_S"] = "1"
        try:
            with caplog.at_level(
                logging.WARNING, logger="corlinman_channels.service"
            ):
                await asyncio.wait_for(
                    asyncio.gather(
                        _qq_health_watcher(adapter, cancel),  # type: ignore[arg-type]
                        cancel_soon(),
                    ),
                    timeout=3.0,
                )
        finally:
            os.environ.pop("CORLINMAN_QQ_HEALTH_PROBE_S", None)
            os.environ.pop("CORLINMAN_QQ_HEALTH_LOST_S", None)

        msgs = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "heartbeat_lost" in msgs
        assert "Nones" not in msgs
        assert "scan a fresh QR" in msgs


# ---------------------------------------------------------------------------
# R3 — per-channel dispatch concurrency cap (semaphore-bounded fan-out).
# ---------------------------------------------------------------------------


class TestQqDispatchConcurrencyCap:
    """Regression for R3 — ``_qq_dispatch_loop`` used to ``create_task``
    per inbound message with no cap. A burst of 20 messages under a
    slow chat backend would fan out 20 tasks instantly. The fix bounds
    fan-out via an ``asyncio.Semaphore`` configurable via
    ``CORLINMAN_QQ_MAX_CONCURRENCY``."""

    @pytest.mark.asyncio
    async def test_qq_inbound_burst_caps_concurrent_handlers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fire 20 inbound messages with max_concurrency=2 and assert
        the chat service NEVER sees more than 2 concurrent ``run`` calls."""
        import asyncio

        from corlinman_channels.common import ChannelBinding, InboundEvent
        from corlinman_channels.onebot import (
            MessageEvent as _ME,
        )
        from corlinman_channels.onebot import (
            MessageType as _MT,
        )
        from corlinman_channels.onebot import (
            TextSegment as _TS,
        )
        from corlinman_channels.router import ChannelRouter
        from corlinman_channels.service import (
            QqChannelParams,
            _qq_dispatch_loop,
        )

        monkeypatch.setenv("CORLINMAN_QQ_MAX_CONCURRENCY", "2")

        # Build 20 inbound MessageEvent payloads wrapped as InboundEvent.
        events: list[InboundEvent[_ME]] = []
        for i in range(20):
            msg = _ME(
                self_id=100,
                message_type=_MT.PRIVATE,
                sub_type="friend",
                group_id=None,
                user_id=1000 + i,
                message_id=i,
                message=[_TS(text="hi")],
                raw_message="hi",
                time=0,
                sender=None,
            )
            events.append(
                InboundEvent(
                    channel="qq",
                    binding=ChannelBinding.qq_private(100, msg.user_id),
                    text="hi",
                    message_id=str(msg.message_id),
                    timestamp=0,
                    mentioned=True,
                    payload=msg,
                )
            )

        class _FakeAdapter:
            """Just enough surface for the dispatch loop. Holds the
            inbound stream open after the 20 events have flowed so the
            dispatch loop doesn't tear down in-flight tasks via its
            ``finally: t.cancel()`` clause — we want every handler to
            actually run to completion to count it."""

            def __init__(self) -> None:
                self.sent: list[Any] = []
                self.drained = asyncio.Event()

            async def inbound(self):  # type: ignore[no-untyped-def]
                for ev in events:
                    yield ev
                # Pause indefinitely so the loop stays alive until the
                # test sets ``cancel`` explicitly.
                self.drained.set()
                await asyncio.Event().wait()

            async def send_action(self, action: Any) -> None:
                self.sent.append(action)

        concurrent: int = 0
        peak: int = 0
        finished: int = 0
        gate = asyncio.Event()

        class _ConcurrencyRecorder:
            """Returns an async generator that records concurrent entry."""

            def run(self, request: Any, cancel: Any):  # type: ignore[no-untyped-def]
                async def _gen():
                    nonlocal concurrent, peak, finished
                    concurrent += 1
                    if concurrent > peak:
                        peak = concurrent
                    try:
                        # Pause so the scheduler has a chance to try (and
                        # fail) to fan out more — proves the semaphore is
                        # actually parking the dispatch loop.
                        try:
                            await asyncio.wait_for(gate.wait(), timeout=0.25)
                        except TimeoutError:
                            pass
                        from types import SimpleNamespace
                        yield SimpleNamespace(kind="token_delta", text="ok")
                        yield SimpleNamespace(kind="done")
                    finally:
                        concurrent -= 1
                        finished += 1

                return _gen()

        # The router needs ``self_ids`` to match the event ``self_id`` for
        # @mention; PRIVATE chats always route, so any value is fine.
        router = ChannelRouter(self_ids=[100])
        params = QqChannelParams(
            config=SimpleNamespace(ws_url="ws://x", self_ids=[100]),
            model="m",
            chat_service=_ConcurrencyRecorder(),  # type: ignore[arg-type]
        )
        adapter = _FakeAdapter()
        cancel = asyncio.Event()

        async def release_gate() -> None:
            # Give the dispatch loop time to fan out under the cap.
            await asyncio.sleep(0.15)
            gate.set()

        async def stop_when_done() -> None:
            # Wait until every handler has run, then cancel so the
            # dispatch loop exits — don't fire cancel on a timer because
            # that would cancel an in-flight final task and skew the
            # ``finished`` counter.
            for _ in range(500):
                if finished == 20:
                    break
                await asyncio.sleep(0.01)
            cancel.set()

        await asyncio.gather(
            _qq_dispatch_loop(adapter, router, params, cancel),  # type: ignore[arg-type]
            release_gate(),
            stop_when_done(),
        )

        # CRITICAL: the recorder never saw more than the cap concurrently.
        assert peak <= 2, (
            f"semaphore cap=2 was violated; observed peak={peak} concurrent runs"
        )
        # Every accepted inbound must have been dispatched.
        assert finished == 20, f"expected all 20 to finish, got {finished}"


class TestQqDispatchInboxLocalScope:
    """Regression for L4 — ``_qq_dispatch_loop`` used to mutate
    ``params.inbox = await _try_open_inbox()`` which made the lazily-
    opened inbox handle leak across channel restarts. The fix keeps the
    lazy fallback as a LOCAL variable inside the loop. ``params.inbox``
    stays untouched so callers can re-run the channel without their
    config drifting from underneath them."""

    @pytest.mark.asyncio
    async def test_lazy_inbox_open_does_not_mutate_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from corlinman_channels import service as _service_mod
        from corlinman_channels.common import ChannelBinding, InboundEvent
        from corlinman_channels.onebot import (
            MessageEvent as _ME,
        )
        from corlinman_channels.onebot import (
            MessageType as _MT,
        )
        from corlinman_channels.onebot import (
            TextSegment as _TS,
        )
        from corlinman_channels.router import ChannelRouter
        from corlinman_channels.service import (
            QqChannelParams,
            _qq_dispatch_loop,
        )

        # Substitute a sentinel inbox the lazy open would return, so we
        # can assert it does NOT leak into ``params.inbox``. Stubs avoid
        # the sqlite thread that ``corlinman_server.inbox.Inbox.open``
        # would otherwise spawn under tests.
        class _StubInbox:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def enqueue(self, **_kw: Any) -> int:
                self.calls.append("enqueue")
                return 1

            async def mark_dispatched(self, *_a: Any) -> None:
                self.calls.append("mark_dispatched")

            async def mark_done(self, *_a: Any) -> None:
                self.calls.append("mark_done")

            async def mark_dead(self, *_a: Any, **_kw: Any) -> None:
                self.calls.append("mark_dead")

        sentinel_inbox = _StubInbox()

        async def _fake_open() -> Any:
            return sentinel_inbox

        monkeypatch.setattr(_service_mod, "_try_open_inbox", _fake_open)

        class _FakeAdapter:
            def __init__(self) -> None:
                self.sent: list[Any] = []

            async def inbound(self):  # type: ignore[no-untyped-def]
                ev = _ME(
                    self_id=100,
                    message_type=_MT.PRIVATE,
                    sub_type="friend",
                    group_id=None,
                    user_id=200,
                    message_id=1,
                    message=[_TS(text="hi")],
                    raw_message="hi",
                    time=0,
                    sender=None,
                )
                yield InboundEvent(
                    channel="qq",
                    binding=ChannelBinding.qq_private(100, 200),
                    text="hi",
                    message_id="1",
                    timestamp=0,
                    mentioned=True,
                    payload=ev,
                )
                # Keep the stream open so the dispatch loop doesn't tear
                # down the in-flight handler before its inbox calls fire.
                await asyncio.Event().wait()

            async def send_action(self, action: Any) -> None:
                self.sent.append(action)

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        router = ChannelRouter(self_ids=[100])
        params = QqChannelParams(
            config=SimpleNamespace(ws_url="ws://x", self_ids=[100]),
            model="m",
            chat_service=svc,  # type: ignore[arg-type]
            inbox=None,  # explicit — the lazy-open path is what's tested
        )
        adapter = _FakeAdapter()
        cancel = asyncio.Event()

        async def stop_when_done() -> None:
            for _ in range(200):
                if "mark_done" in sentinel_inbox.calls:
                    break
                await asyncio.sleep(0.01)
            cancel.set()

        await asyncio.gather(
            _qq_dispatch_loop(adapter, router, params, cancel),  # type: ignore[arg-type]
            stop_when_done(),
        )

        # The loop must NOT have mutated params.inbox — the lazy open
        # lives in a local. L4 regression: the prior code did
        # ``params.inbox = await _try_open_inbox()`` which leaked the
        # sentinel into the shared params dataclass.
        assert params.inbox is None, (
            "params.inbox was mutated by the dispatch loop — L4 regression"
        )
        # And the local *did* get used during the handler — proves the
        # lazy open ran and that handle_one_qq actually saw the stub.
        assert "enqueue" in sentinel_inbox.calls
        assert "mark_dispatched" in sentinel_inbox.calls
        assert "mark_done" in sentinel_inbox.calls


# ---------------------------------------------------------------------------
# QQ Official (api.sgroup.qq.com) — summary-prepend handler
# ---------------------------------------------------------------------------


class _FakeQqOfficialSender:
    """Records every text / image send call. Mirrors the surface that
    :func:`handle_one_qq_official` exercises."""

    def __init__(self) -> None:
        self.text_sends: list[tuple[str, str, str | None]] = []
        self.image_sends: list[tuple[str, str, str | None]] = []
        self.uploads: list[tuple[str, bytes | str | None]] = []
        self._next_id = 0

    def _id(self) -> str:
        self._next_id += 1
        return f"msg_{self._next_id}"

    async def send_c2c_text(
        self,
        openid: str,
        content: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        self.text_sends.append((openid, content, msg_id))
        return self._id()

    async def send_group_text(
        self,
        group_openid: str,
        content: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        self.text_sends.append((group_openid, content, msg_id))
        return self._id()

    async def send_text(
        self,
        channel_id: str,
        content: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        self.text_sends.append((channel_id, content, msg_id))
        return self._id()

    async def upload_group_image(
        self,
        group_openid: str,
        *,
        url: str | None = None,
        file_data: bytes | None = None,
    ) -> str:
        self.uploads.append((group_openid, file_data or url))
        return "file_info_grp"

    async def upload_c2c_image(
        self,
        openid: str,
        *,
        url: str | None = None,
        file_data: bytes | None = None,
    ) -> str:
        self.uploads.append((openid, file_data or url))
        return "file_info_c2c"

    async def send_group_image(
        self,
        group_openid: str,
        file_info: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
        content: str = "",
    ) -> str:
        self.image_sends.append((group_openid, file_info, msg_id))
        return self._id()

    async def send_c2c_image(
        self,
        openid: str,
        file_info: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
        content: str = "",
    ) -> str:
        self.image_sends.append((openid, file_info, msg_id))
        return self._id()


def _qq_official_inbound(
    *,
    event_type: str,
    thread: str,
    sender: str,
    message_id: str = "msg_inbound_1",
    text: str = "hi",
) -> InboundEvent[Any]:
    """Build an :class:`InboundEvent` shaped like the qq_official adapter."""
    binding = ChannelBinding(
        channel="qq_official",
        account="app_xyz",
        thread=thread,
        sender=sender,
    )
    payload = {
        "id": message_id,
        "content": text,
        "_qq_official_event_type": event_type,
    }
    return InboundEvent(
        channel="qq_official",
        binding=binding,
        text=text,
        message_id=message_id,
        timestamp=0,
        mentioned=True,
        attachments=[],
        payload=payload,
    )


class TestHandleOneQqOfficial:
    @pytest.mark.asyncio
    async def test_text_reply_routes_to_c2c_endpoint(self) -> None:
        """A plain text reply for a C2C inbound must hit
        ``send_c2c_text`` carrying the inbound ``msg_id``."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="hello "),
            _Ev(kind="token_delta", text="world"),
            _Ev(kind="done"),
        ])
        sender = _FakeQqOfficialSender()
        inbound = _qq_official_inbound(
            event_type="C2C_MESSAGE_CREATE",
            thread="ou_user_1",
            sender="ou_user_1",
            message_id="msg_inbound_42",
        )
        await handle_one_qq_official(
            svc, inbound, "m", sender, asyncio.Event()  # type: ignore[arg-type]
        )
        assert len(sender.text_sends) == 1
        openid, body, msg_id = sender.text_sends[0]
        assert openid == "ou_user_1"
        assert "hello world" in body
        assert msg_id == "msg_inbound_42"

    @pytest.mark.asyncio
    async def test_tool_calls_prepend_summary_block(self) -> None:
        """Tool-call events must collect into a summary block that
        prepends the final reply (no mutable spinner available)."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="web_search",
                tool="web_search",
                args_json=b'{"query":"tencent earnings"}',
            ),
            _Ev(
                kind="tool_call",
                plugin="builtin",
                tool="read_file",
                args_json=b'{"path":"/tmp/notes.md"}',
            ),
            _Ev(kind="token_delta", text="here is the answer"),
            _Ev(kind="done"),
        ])
        sender = _FakeQqOfficialSender()
        inbound = _qq_official_inbound(
            event_type="GROUP_AT_MESSAGE_CREATE",
            thread="og_group_99",
            sender="om_user_5",
            message_id="msg_grp_1",
        )
        await handle_one_qq_official(
            svc, inbound, "m", sender, asyncio.Event()  # type: ignore[arg-type]
        )
        assert len(sender.text_sends) == 1
        body = sender.text_sends[0][1]
        # The summary header must appear BEFORE the reply.
        assert body.index("🔧 工具调用记录") < body.index("here is the answer")
        # Each tool call must be listed.
        assert "web_search" in body
        assert "read_file" in body
        # And the args preview must surface for each tool.
        assert "tencent earnings" in body
        assert "/tmp/notes.md" in body
        # The separator line must appear.
        assert "─" in body

    @pytest.mark.asyncio
    async def test_send_attachment_uploads_and_sends_image(
        self, tmp_path: Any
    ) -> None:
        """``send_attachment`` for an image must pre-upload via
        ``upload_*_image`` then dispatch via ``send_*_image``."""
        import asyncio
        import json as _json

        # Real PNG header so mimetypes guesses correctly.
        img = tmp_path / "chart.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        args = _json.dumps({"path": str(img), "filename": "chart.png"})
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="send_attachment",
                tool="send_attachment",
                args_json=args.encode("utf-8"),
            ),
            _Ev(kind="token_delta", text="see attached"),
            _Ev(kind="done"),
        ])
        sender = _FakeQqOfficialSender()
        inbound = _qq_official_inbound(
            event_type="C2C_MESSAGE_CREATE",
            thread="ou_user_x",
            sender="ou_user_x",
            message_id="msg_c2c_99",
        )
        await handle_one_qq_official(
            svc, inbound, "m", sender, asyncio.Event()  # type: ignore[arg-type]
        )
        # The upload + the image send must both have fired.
        assert sender.uploads, "expected upload_c2c_image to be called"
        assert sender.uploads[0][0] == "ou_user_x"
        assert sender.image_sends, "expected send_c2c_image to be called"
        assert sender.image_sends[0][1] == "file_info_c2c"
        assert sender.image_sends[0][2] == "msg_c2c_99"
        # The text reply must mention the attachment status.
        assert len(sender.text_sends) == 1
        body = sender.text_sends[0][1]
        assert "已发送图片: chart.png" in body
        assert "see attached" in body

    @pytest.mark.asyncio
    async def test_non_image_attachment_renders_unsupported_status(
        self, tmp_path: Any
    ) -> None:
        """Non-image files cannot be sent via QQ Official; the handler
        must surface a friendly status text instead of crashing."""
        import asyncio
        import json as _json

        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        args = _json.dumps({"path": str(f), "filename": "doc.pdf"})
        svc = _ScriptedChatService([
            _Ev(
                kind="tool_call",
                plugin="send_attachment",
                tool="send_attachment",
                args_json=args.encode("utf-8"),
            ),
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        sender = _FakeQqOfficialSender()
        inbound = _qq_official_inbound(
            event_type="C2C_MESSAGE_CREATE",
            thread="ou_user_pdf",
            sender="ou_user_pdf",
            message_id="msg_pdf_1",
        )
        await handle_one_qq_official(
            svc, inbound, "m", sender, asyncio.Event()  # type: ignore[arg-type]
        )
        # No upload should have happened for the non-image file.
        assert sender.uploads == []
        assert sender.image_sends == []
        # The summary block must include the unsupported notice.
        assert len(sender.text_sends) == 1
        body = sender.text_sends[0][1]
        assert "QQ官方机器人暂不支持文件直发" in body
        assert "doc.pdf" in body

    @pytest.mark.asyncio
    async def test_error_event_renders_corlinman_error_reply(self) -> None:
        """A backend error must surface as a short ``[corlinman error]``
        reply so the user knows the turn failed."""
        import asyncio

        svc = _ScriptedChatService([_Ev(kind="error", error="boom")])
        sender = _FakeQqOfficialSender()
        inbound = _qq_official_inbound(
            event_type="C2C_MESSAGE_CREATE",
            thread="ou_err",
            sender="ou_err",
        )
        await handle_one_qq_official(
            svc, inbound, "m", sender, asyncio.Event()  # type: ignore[arg-type]
        )
        assert len(sender.text_sends) == 1
        assert "[corlinman error]" in sender.text_sends[0][1]
        assert "boom" in sender.text_sends[0][1]

    @pytest.mark.asyncio
    async def test_empty_reply_with_no_tool_activity_is_silent(self) -> None:
        """If the assistant says nothing AND no tool ran, the handler
        must not ship an empty message."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="   "),
            _Ev(kind="done"),
        ])
        sender = _FakeQqOfficialSender()
        inbound = _qq_official_inbound(
            event_type="C2C_MESSAGE_CREATE",
            thread="ou_empty",
            sender="ou_empty",
        )
        await handle_one_qq_official(
            svc, inbound, "m", sender, asyncio.Event()  # type: ignore[arg-type]
        )
        assert sender.text_sends == []

    @pytest.mark.asyncio
    async def test_todo_write_dropped_keeps_tool_log(self) -> None:
        """``todo_write`` on the QQ-official channel must NOT render the
        checkbox view in the summary block. Pending ``☐`` rows are
        forward-looking noise on a non-editable transport — the
        ``🔧 工具调用记录`` block alone is the user-visible "what just
        happened" signal."""
        import asyncio
        import json as _json

        todos = _json.dumps({"todos": [
            {"content": "Fetch earnings page",
             "activeForm": "Fetching earnings page",
             "status": "completed"},
            {"content": "Draft summary",
             "activeForm": "Drafting summary",
             "status": "in_progress"},
            {"content": "Email customer",
             "activeForm": "Emailing customer",
             "status": "pending"},
        ]}).encode("utf-8")
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", plugin="builtin", tool="todo_write",
                args_json=todos),
            _Ev(kind="tool_call", plugin="web_search", tool="web_search",
                args_json=b'{"query":"tencent earnings"}'),
            _Ev(kind="token_delta", text="here is the answer"),
            _Ev(kind="done"),
        ])
        sender = _FakeQqOfficialSender()
        inbound = _qq_official_inbound(
            event_type="C2C_MESSAGE_CREATE",
            thread="ou_todo",
            sender="ou_todo",
        )
        await handle_one_qq_official(
            svc, inbound, "m", sender, asyncio.Event()  # type: ignore[arg-type]
        )
        assert len(sender.text_sends) == 1
        body = sender.text_sends[0][1]
        # The tool log still appears, in order: header < bullet < body.
        tools_idx = body.find("🔧 工具调用记录")
        bullet_idx = body.find("web_search")
        body_idx = body.find("here is the answer")
        assert tools_idx != -1 and bullet_idx != -1 and body_idx != -1
        assert tools_idx < bullet_idx < body_idx, body
        assert "tencent earnings" in body
        # No todo artefacts anywhere.
        assert "📋 任务清单" not in body
        assert "☑" not in body
        assert "▣" not in body
        assert "☐" not in body
        assert "todo_write" not in body

    @pytest.mark.asyncio
    async def test_run_qq_official_channel_requires_app_id(self) -> None:
        import asyncio

        params = QqOfficialChannelParams(
            config=SimpleNamespace(app_id="", app_secret="s")
        )
        with pytest.raises(ValueError, match="app_id"):
            await run_qq_official_channel(params, asyncio.Event())

    @pytest.mark.asyncio
    async def test_run_qq_official_channel_requires_app_secret(self) -> None:
        import asyncio

        params = QqOfficialChannelParams(
            config=SimpleNamespace(app_id="a", app_secret="")
        )
        with pytest.raises(ValueError, match="app_secret"):
            await run_qq_official_channel(params, asyncio.Event())


# ---------------------------------------------------------------------------
# Persona injection for the four new channels (W7 Persona Studio)
# ---------------------------------------------------------------------------


class _FakePersonaStoreW7:
    """Minimal :class:`PersonaStore` stand-in — returns a single canned
    row. Stays here (not in conftest) so the W7-specific test
    surface is co-located with its assertions."""

    def __init__(self, persona_id: str, system_prompt: str) -> None:
        self._row = SimpleNamespace(
            id=persona_id,
            display_name="Test",
            short_summary="",
            system_prompt=system_prompt,
            is_builtin=False,
        )

    async def get(self, persona_id: str):  # type: ignore[no-untyped-def]
        if persona_id == self._row.id:
            return self._row
        return None


class _FakeAssetRecordW7:
    """Tiny duck-typed asset record — only ``label`` + ``path``."""

    def __init__(self, label: str, path: str) -> None:
        self.label = label
        self.path = path


class _FakeAssetStoreW7:
    """Minimal :class:`PersonaAssetStore` stand-in for W7 emoji tests."""

    def __init__(self, records: list[_FakeAssetRecordW7]) -> None:
        self._records = records

    async def list(self, persona_id: str, *, kind: str | None = None):  # type: ignore[no-untyped-def]
        if kind == "emoji":
            return list(self._records)
        return []

    def path_for(self, record: _FakeAssetRecordW7) -> str:
        return record.path


class TestPersonaInjectionMultiChannel:
    """Verify the W7 humanlike injector fires inside each text-channel
    handle_one_* — Telegram / Discord / Slack / Feishu. Each test:

    * builds a chat service that records the request,
    * spins up the channel handler with a populated ``*ChannelParams``
      carrying ``humanlike_enabled=True`` + a fake persona store + a
      fake asset store with one emoji slot,
    * asserts the chat backend saw a leading ``role="system"`` message
      whose content carries both the persona body marker and the
      ``## Available emoji`` block listing the emoji path.
    """

    @staticmethod
    def _assert_persona_injected_with_emoji(req: Any) -> None:
        sys_msg = req.messages[0]
        assert sys_msg.role == "system"
        assert "PERSONA-BODY-MARK" in sys_msg.content
        assert "## Available emoji" in sys_msg.content
        assert "- happy: /abs/happy.png" in sys_msg.content
        assert req.persona_id == "grantley"

    @pytest.mark.asyncio
    async def test_telegram_injects_persona_and_emoji_block(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        sender = _FakeTelegramSender()
        # Telegram's handle_one_* does ``int(binding.thread)`` for the
        # chat id; use a numeric binding here to avoid the cast failing.
        binding = ChannelBinding.telegram(
            bot_id=999, chat_id=42, user_id=42
        )
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram",
            binding=binding,
            text="ping",
            message_id="7",
            timestamp=0,
            mentioned=True,
            attachments=[],
            payload=None,
        )
        params = TelegramChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=True,
            persona_id="grantley",
            persona_store=_FakePersonaStoreW7(
                "grantley", "PERSONA-BODY-MARK\nYou are Grantley."
            ),
            asset_store=_FakeAssetStoreW7(
                [_FakeAssetRecordW7("happy", "/abs/happy.png")]
            ),
        )
        await handle_one_telegram(
            svc,
            inbound,
            "m",
            sender,
            asyncio.Event(),  # type: ignore[arg-type]
            params=params,
        )
        assert svc.calls, "chat_service.run was never invoked"
        self._assert_persona_injected_with_emoji(svc.calls[0])

    @pytest.mark.asyncio
    async def test_discord_injects_persona_and_emoji_block(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        sender = _FakeDiscordSender()
        params = DiscordChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=True,
            persona_id="grantley",
            persona_store=_FakePersonaStoreW7(
                "grantley", "PERSONA-BODY-MARK"
            ),
            asset_store=_FakeAssetStoreW7(
                [_FakeAssetRecordW7("happy", "/abs/happy.png")]
            ),
        )
        await handle_one_discord(
            svc,
            _inbound("discord"),
            "m",
            sender,
            asyncio.Event(),  # type: ignore[arg-type]
            params=params,
        )
        assert svc.calls
        self._assert_persona_injected_with_emoji(svc.calls[0])

    @pytest.mark.asyncio
    async def test_slack_injects_persona_and_emoji_block(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        sender = _FakeSlackSender()
        params = SlackChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=True,
            persona_id="grantley",
            persona_store=_FakePersonaStoreW7(
                "grantley", "PERSONA-BODY-MARK"
            ),
            asset_store=_FakeAssetStoreW7(
                [_FakeAssetRecordW7("happy", "/abs/happy.png")]
            ),
        )
        await handle_one_slack(
            svc,
            _inbound("slack"),
            "m",
            sender,
            asyncio.Event(),  # type: ignore[arg-type]
            params=params,
        )
        assert svc.calls
        self._assert_persona_injected_with_emoji(svc.calls[0])

    @pytest.mark.asyncio
    async def test_feishu_injects_persona_and_emoji_block(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        sender = _FakeFeishuSender()
        params = FeishuChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=True,
            persona_id="grantley",
            persona_store=_FakePersonaStoreW7(
                "grantley", "PERSONA-BODY-MARK"
            ),
            asset_store=_FakeAssetStoreW7(
                [_FakeAssetRecordW7("happy", "/abs/happy.png")]
            ),
        )
        await handle_one_feishu(
            svc,
            _inbound("feishu"),
            "m",
            sender,
            asyncio.Event(),  # type: ignore[arg-type]
            params=params,
        )
        assert svc.calls
        self._assert_persona_injected_with_emoji(svc.calls[0])

    @pytest.mark.asyncio
    async def test_qq_emoji_block_listed_in_system_prompt(self) -> None:
        """QQ already had the persona injector; W7 adds the emoji block
        when the new ``asset_store`` is wired. Mirror the QQ test in
        :class:`TestQqPersonaInjection` with an asset store present."""
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="ok"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(
            ev.self_id, ev.group_id or 0, ev.user_id
        )
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()
        params = QqChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=True,
            persona_id="grantley",
            persona_store=_FakePersonaStoreW7(
                "grantley", "PERSONA-BODY-MARK"
            ),
            asset_store=_FakeAssetStoreW7(
                [
                    _FakeAssetRecordW7("happy", "/abs/happy.png"),
                    _FakeAssetRecordW7("sad", "/abs/sad.png"),
                ]
            ),
        )
        await handle_one_qq(
            svc, req, ev, "m", adapter, asyncio.Event(),  # type: ignore[arg-type]
            params=params,
        )
        sys_msg = svc.calls[0].messages[0]
        assert sys_msg.role == "system"
        assert "PERSONA-BODY-MARK" in sys_msg.content
        assert "## Available emoji" in sys_msg.content
        assert "- happy: /abs/happy.png" in sys_msg.content
        assert "- sad: /abs/sad.png" in sys_msg.content


# ---------------------------------------------------------------------------
# Telegram health tracker (F2)
# ---------------------------------------------------------------------------


class TestTelegramHealth:
    """Pins the W4-FE F2 counter / latency / recent-messages plumbing.

    The recorders ride alongside ``handle_one_telegram`` so the admin
    page sees real numbers instead of a hardcoded mock. These tests
    exercise the public recorder helpers directly so we don't need to
    drive a full Telegram round-trip per assertion.
    """

    def setup_method(self) -> None:
        _telegram_reset_state_for_tests()

    def teardown_method(self) -> None:
        _telegram_reset_state_for_tests()

    def _tg_inbound(
        self,
        *,
        chat_id: int = 42,
        message_id: str = "1",
        text: str = "hi",
        payload: dict[str, Any] | None = None,
    ) -> InboundEvent[Any]:
        binding = ChannelBinding.telegram(
            bot_id=999, chat_id=chat_id, user_id=chat_id
        )
        return InboundEvent(
            channel="telegram",
            binding=binding,
            text=text,
            message_id=message_id,
            timestamp=0,
            mentioned=True,
            payload=payload,
        )

    def test_record_inbound_bumps_day_counter(self) -> None:
        inbound = self._tg_inbound()
        now = 1_700_000_000_000  # arbitrary UTC ms
        telegram_record_inbound(inbound, now_ms=now)
        assert TELEGRAM_HEALTH["messages_today"] == 1
        assert TELEGRAM_HEALTH["messages_week"] == 1
        assert TELEGRAM_HEALTH["active_chats"] == 1
        assert TELEGRAM_HEALTH["online"] is True
        assert TELEGRAM_HEALTH["last_event_at_ms"] == now

        # Second message from the same chat: counter ticks, distinct
        # chats stays at 1.
        telegram_record_inbound(self._tg_inbound(message_id="2"), now_ms=now + 1_000)
        assert TELEGRAM_HEALTH["messages_today"] == 2
        assert TELEGRAM_HEALTH["active_chats"] == 1

    def test_active_chats_counts_distinct_threads(self) -> None:
        now = 1_700_000_000_000
        telegram_record_inbound(self._tg_inbound(chat_id=1), now_ms=now)
        telegram_record_inbound(self._tg_inbound(chat_id=2), now_ms=now)
        telegram_record_inbound(self._tg_inbound(chat_id=3), now_ms=now)
        assert TELEGRAM_HEALTH["active_chats"] == 3

    def test_active_chats_prunes_after_24h(self) -> None:
        now = 1_700_000_000_000
        telegram_record_inbound(self._tg_inbound(chat_id=1), now_ms=now)
        # 25h later — the chat falls off the 24h rolling window.
        later = now + (25 * 60 * 60 * 1000)
        telegram_record_inbound(self._tg_inbound(chat_id=2), now_ms=later)
        assert TELEGRAM_HEALTH["active_chats"] == 1

    def test_latency_percentiles_from_round_trips(self) -> None:
        inbound = self._tg_inbound()
        # Simulate 5 turns with known round-trip latencies.
        base = 1_700_000_000_000
        deltas_ms = [120, 200, 300, 400, 800]
        for i, d in enumerate(deltas_ms):
            telegram_record_inbound(inbound, now_ms=base + i * 10_000)
            telegram_record_reply_sent(
                inbound,
                inbound_ts_ms=base + i * 10_000,
                now_ms=base + i * 10_000 + d,
            )
        assert TELEGRAM_HEALTH["latency_p50_ms"] == 300
        assert TELEGRAM_HEALTH["latency_p95_ms"] == 800

    def test_recent_messages_appended_and_capped(self) -> None:
        # Recent messages buffer is capped at 500 — drive a few past
        # that to confirm the deque truncates the oldest.
        for i in range(600):
            telegram_record_inbound(
                self._tg_inbound(chat_id=i, message_id=str(i)),
                now_ms=1_700_000_000_000 + i,
            )
        assert len(TELEGRAM_RECENT_MESSAGES) == 500
        # The newest message id wins.
        assert TELEGRAM_RECENT_MESSAGES[-1]["id"] == "599"

    def test_reply_flips_routing_to_responded(self) -> None:
        inbound = self._tg_inbound(chat_id=7, message_id="abc")
        now = 1_700_000_000_000
        telegram_record_inbound(inbound, now_ms=now)
        entry = TELEGRAM_RECENT_MESSAGES[-1]
        assert entry["routing"] == "queued"
        assert entry["mention_reason"] == "dm"
        telegram_record_reply_sent(inbound, inbound_ts_ms=now, now_ms=now + 50)
        assert TELEGRAM_RECENT_MESSAGES[-1]["routing"] == "responded"

    def test_inbound_payload_with_chat_metadata(self) -> None:
        inbound = self._tg_inbound(
            chat_id=99,
            payload={
                "chat": {"type": "supergroup", "title": "Bot Lab"},
                "from": {"username": "alice"},
            },
        )
        telegram_record_inbound(inbound, now_ms=1_700_000_000_000)
        entry = TELEGRAM_RECENT_MESSAGES[-1]
        assert entry["kind"] == "group"
        assert entry["chat_title"] == "Bot Lab"
        assert entry["from_username"] == "alice"
        # group + mentioned=True → mention_reason="mention"
        assert entry["mention_reason"] == "mention"

    def test_online_flips_false_after_window(self) -> None:
        # Record an event then recompute aggregates far in the future:
        # the channel must be reported offline.
        from corlinman_channels.service import _telegram_recompute_aggregates

        inbound = self._tg_inbound()
        now = 1_700_000_000_000
        telegram_record_inbound(inbound, now_ms=now)
        assert TELEGRAM_HEALTH["online"] is True

        future = now + 10 * 60 * 1000  # 10 minutes later (> 5 min window)
        _telegram_recompute_aggregates(future)
        assert TELEGRAM_HEALTH["online"] is False
        assert TELEGRAM_HEALTH["seconds_since_event"] is not None
        assert TELEGRAM_HEALTH["seconds_since_event"] >= 600

    def test_messages_week_resets_after_seven_days(self) -> None:
        from corlinman_channels.service import _telegram_recompute_aggregates

        inbound = self._tg_inbound()
        now = 1_700_000_000_000
        telegram_record_inbound(inbound, now_ms=now)
        assert TELEGRAM_HEALTH["messages_week"] == 1
        # 8 days later — the old bucket falls out of the 7-day window.
        future = now + 8 * 24 * 60 * 60 * 1000
        _telegram_recompute_aggregates(future)
        assert TELEGRAM_HEALTH["messages_week"] == 0
