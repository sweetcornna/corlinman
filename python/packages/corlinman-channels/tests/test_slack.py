"""Tests for ``corlinman_channels.slack`` — the Slack Socket Mode adapter.

corlinman has no Rust reference for Slack, so these mirror the
``test_telegram.py`` / ``test_discord.py`` structure: pure-function
coverage for the mention / binding helpers, plus integration coverage
that drives the inbound iterator and the Web API sender through mocked
transports.

The Socket Mode WebSocket handshake is *not* dialed — tests pre-seed
``adapter._inbound_q`` and mark the adapter "connected" so ``inbound()``
drains the queue without a network round-trip. The Web API side
(``auth.test`` discovery, ``chat.postMessage``) runs against an
:class:`httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from corlinman_channels.common import ConfigError, InboundEvent
from corlinman_channels.slack import (
    SlackAdapter,
    SlackConfig,
    SlackSender,
    binding_from_event,
    is_mentioning_bot,
)

BOT_USER = "U0BOT0001"


# ---------------------------------------------------------------------------
# Mock Web API transport
# ---------------------------------------------------------------------------


def _api_client(post_ts: str = "1700000000.000100") -> httpx.AsyncClient:
    """An httpx client whose mock transport answers the Slack Web API."""

    def _handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/auth.test"):
            return httpx.Response(200, json={"ok": True, "user_id": BOT_USER})
        if path.endswith("/apps.connections.open"):
            return httpx.Response(
                200, json={"ok": True, "url": "wss://wss-primary.slack.com/link"}
            )
        if path.endswith("/chat.postMessage"):
            return httpx.Response(200, json={"ok": True, "ts": post_ts})
        return httpx.Response(200, json={"ok": False, "error": "not_mocked"})

    return httpx.AsyncClient(transport=httpx.MockTransport(_handle))


def _make_connected_adapter(cfg: SlackConfig) -> SlackAdapter:
    """Build an adapter and mark it 'connected' without dialing the WS."""
    adapter = SlackAdapter(cfg, http_client=_api_client())
    adapter._bot_user_id = BOT_USER
    adapter._reader_task = asyncio.create_task(asyncio.sleep(3600))
    return adapter


async def _drain_one(adapter: SlackAdapter) -> InboundEvent[Any] | None:
    it = adapter.inbound()
    try:
        return await asyncio.wait_for(it.__anext__(), timeout=2.0)
    except (StopAsyncIteration, TimeoutError):
        return None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestMentionHelper:
    def test_text_mention_matches(self) -> None:
        event = {"type": "message", "text": f"<@{BOT_USER}> hi"}
        assert is_mentioning_bot(event, BOT_USER) is True

    def test_app_mention_event_type_implies_mention(self) -> None:
        event = {"type": "app_mention", "text": "hi"}
        assert is_mentioning_bot(event, BOT_USER) is True

    def test_unrelated_mention_does_not_match(self) -> None:
        event = {"type": "message", "text": "<@U0OTHER> hi"}
        assert is_mentioning_bot(event, BOT_USER) is False


class TestBinding:
    def test_binding_fields(self) -> None:
        event = {"channel": "C1", "user": "U9"}
        b = binding_from_event(event, BOT_USER)
        assert b.channel == "slack"
        assert b.account == BOT_USER
        assert b.thread == "C1"
        assert b.sender == "U9"


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfig:
    def test_empty_app_token_raises(self) -> None:
        with pytest.raises(ConfigError, match="app_token"):
            SlackAdapter(SlackConfig(app_token="", bot_token="xoxb-1"))

    def test_empty_bot_token_raises(self) -> None:
        with pytest.raises(ConfigError, match="bot_token"):
            SlackAdapter(SlackConfig(app_token="xapp-1", bot_token=""))


# ---------------------------------------------------------------------------
# Inbound — message-in → normalized event
# ---------------------------------------------------------------------------


class TestInbound:
    @pytest.mark.asyncio
    async def test_im_yields_event(self) -> None:
        adapter = _make_connected_adapter(
            SlackConfig(app_token="xapp", bot_token="xoxb")
        )
        try:
            adapter._inbound_q.put_nowait(
                {
                    "type": "message",
                    "channel": "D-dm",
                    "channel_type": "im",
                    "user": "U-user",
                    "text": "hello bot",
                    "ts": "1700000000.000001",
                }
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.channel == "slack"
            assert ev.text == "hello bot"
            assert ev.mentioned is True  # DMs implicitly addressed
            assert ev.binding.thread == "D-dm"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_channel_without_mention_dropped(self) -> None:
        adapter = _make_connected_adapter(
            SlackConfig(app_token="xapp", bot_token="xoxb")
        )
        try:
            adapter._inbound_q.put_nowait(
                {
                    "type": "message",
                    "channel": "C-pub",
                    "channel_type": "channel",
                    "user": "U-user",
                    "text": "no mention",
                    "ts": "1.1",
                }
            )
            adapter._inbound_q.put_nowait(
                {
                    "type": "message",
                    "channel": "C-pub",
                    "channel_type": "channel",
                    "user": "U-user",
                    "text": f"<@{BOT_USER}> ping",
                    "ts": "2.2",
                }
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.message_id == "2.2"
            assert ev.text == "ping"  # mention token stripped
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_subtype_and_bot_messages_skipped(self) -> None:
        adapter = _make_connected_adapter(
            SlackConfig(app_token="xapp", bot_token="xoxb")
        )
        try:
            # channel_join subtype — skipped.
            adapter._inbound_q.put_nowait(
                {
                    "type": "message",
                    "subtype": "channel_join",
                    "channel": "D-dm",
                    "channel_type": "im",
                    "user": "U-user",
                    "text": "joined",
                    "ts": "1.1",
                }
            )
            # bot_id present — skipped.
            adapter._inbound_q.put_nowait(
                {
                    "type": "message",
                    "channel": "D-dm",
                    "channel_type": "im",
                    "user": "U-user",
                    "bot_id": "B999",
                    "text": "from a bot",
                    "ts": "2.2",
                }
            )
            # Real user message.
            adapter._inbound_q.put_nowait(
                {
                    "type": "message",
                    "channel": "D-dm",
                    "channel_type": "im",
                    "user": "U-user",
                    "text": "real",
                    "ts": "3.3",
                }
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.message_id == "3.3"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_keyword_filter(self) -> None:
        adapter = _make_connected_adapter(
            SlackConfig(
                app_token="xapp",
                bot_token="xoxb",
                respond_to_all=True,
                keyword_filter=["deploy"],
            )
        )
        try:
            adapter._inbound_q.put_nowait(
                {
                    "type": "message",
                    "channel": "C-pub",
                    "channel_type": "channel",
                    "user": "U-user",
                    "text": "chit chat",
                    "ts": "1.1",
                }
            )
            adapter._inbound_q.put_nowait(
                {
                    "type": "message",
                    "channel": "C-pub",
                    "channel_type": "channel",
                    "user": "U-user",
                    "text": "please deploy now",
                    "ts": "2.2",
                }
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.message_id == "2.2"
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Sender — reply-out
# ---------------------------------------------------------------------------


class TestSender:
    @pytest.mark.asyncio
    async def test_send_message_returns_ts(self) -> None:
        client = _api_client(post_ts="1700000111.000200")
        sender = SlackSender(client, "xoxb-tok")
        try:
            ts = await sender.send_message("C1", "reply text")
            assert ts == "1700000111.000200"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_send_message_api_error_raises(self) -> None:
        def _handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(_handle))
        sender = SlackSender(client, "xoxb-tok")
        try:
            with pytest.raises(Exception, match="channel_not_found"):
                await sender.send_message("C1", "x")
        finally:
            await client.aclose()
