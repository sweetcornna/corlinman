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
    DiscordChannelParams,
    FeishuChannelParams,
    QqChannelParams,
    SlackChannelParams,
    TelegramChannelParams,
    _build_internal_request,
    _build_reply_action,
    _event_kind,
    handle_one_discord,
    handle_one_feishu,
    handle_one_qq,
    handle_one_slack,
    handle_one_telegram,
    run_discord_channel,
    run_feishu_channel,
    run_qq_channel,
    run_slack_channel,
    run_telegram_channel,
)

# ---------------------------------------------------------------------------
# Fake chat backend
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Ev:
    kind: str
    text: str = ""
    error: str = ""


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

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> int:
        self.sent.append((chat_id, text, reply_to_message_id))
        return 1


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
        assert len(sender.sent) == 1
        chat_id, text, reply_to = sender.sent[0]
        assert chat_id == 42
        assert text == "hi there"
        assert reply_to == 7

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
        assert len(sender.sent) == 1
        _, text, _ = sender.sent[0]
        assert "[corlinman error]" in text
        assert "nope" in text


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
# text-only channels share the ``_collect_reply`` collapse, so the tests
# assert the round-trip (token deltas → concatenated reply) and the
# error-rendering path for each transport's sender shape.
# ---------------------------------------------------------------------------


class _FakeDiscordSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    async def send_message(
        self,
        channel_id: str,
        text: str,
        reply_to_message_id: str | None = None,
    ) -> str:
        self.sent.append((channel_id, text, reply_to_message_id))
        return "new-msg"


class _FakeSlackSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    async def send_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> str:
        self.sent.append((channel, text, thread_ts))
        return "1.1"


class _FakeFeishuSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to_message_id: str | None = None,
    ) -> str:
        self.sent.append((chat_id, text, reply_to_message_id))
        return "om-new"


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


class TestHandleOneDiscord:
    @pytest.mark.asyncio
    async def test_concat_and_send(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="he"),
            _Ev(kind="token_delta", text="llo"),
            _Ev(kind="done"),
        ])
        sender = _FakeDiscordSender()
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        assert sender.sent == [("T1", "hello", "M1")]

    @pytest.mark.asyncio
    async def test_error_renders_short_reply(self) -> None:
        import asyncio

        svc = _ScriptedChatService([_Ev(kind="error", error="boom")])
        sender = _FakeDiscordSender()
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        assert len(sender.sent) == 1
        assert "[corlinman error]" in sender.sent[0][1]
        assert "boom" in sender.sent[0][1]

    @pytest.mark.asyncio
    async def test_empty_reply_is_dropped(self) -> None:
        import asyncio

        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="  "),
            _Ev(kind="done"),
        ])
        sender = _FakeDiscordSender()
        await handle_one_discord(svc, _inbound("discord"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        assert sender.sent == []


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
        # message_id is threaded as thread_ts.
        assert sender.sent == [("T1", "hi there", "M1")]

    @pytest.mark.asyncio
    async def test_error_renders_short_reply(self) -> None:
        import asyncio

        svc = _ScriptedChatService([_Ev(kind="error", error="nope")])
        sender = _FakeSlackSender()
        await handle_one_slack(svc, _inbound("slack"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        assert "[corlinman error]" in sender.sent[0][1]


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
        assert sender.sent == [("T1", "ok", "M1")]

    @pytest.mark.asyncio
    async def test_error_renders_short_reply(self) -> None:
        import asyncio

        svc = _ScriptedChatService([_Ev(kind="error", error="bad")])
        sender = _FakeFeishuSender()
        await handle_one_feishu(svc, _inbound("feishu"), "m", sender, asyncio.Event())  # type: ignore[arg-type]
        assert "[corlinman error]" in sender.sent[0][1]
        assert "bad" in sender.sent[0][1]


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
