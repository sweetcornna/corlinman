"""Tests for ``corlinman_channels.discord`` — the Discord adapter.

corlinman has no Rust reference for Discord, so these mirror the
``test_telegram.py`` structure: pure-function coverage for the
mention / binding helpers, plus integration coverage that drives the
inbound iterator and the REST sender through mocked transports.

Mocking strategy
----------------

* The Gateway WebSocket handshake is *not* dialed — instead the tests
  pre-seed ``adapter._inbound_q`` and mark the adapter "connected" so
  ``inbound()`` drains the queue without a network round-trip. The
  gateway-loop wiring (HELLO / heartbeat / dispatch decode) is covered
  by ``_pump`` being exercised against an in-process frame list.
* The REST side (``users/@me`` discovery, ``sendMessage``) runs against
  an :class:`httpx.MockTransport`, the same double the Telegram tests
  use.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from corlinman_channels.common import ConfigError, InboundEvent
from corlinman_channels.discord import (
    DiscordAdapter,
    DiscordConfig,
    DiscordSender,
    binding_from_message,
    is_mentioning_bot,
)

BOT_ID = "900000000000000001"


# ---------------------------------------------------------------------------
# Mock REST transport
# ---------------------------------------------------------------------------


def _rest_client(message_id: str = "555") -> httpx.AsyncClient:
    """An httpx client whose mock transport answers ``users/@me`` and
    ``POST /channels/.../messages``."""

    def _handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/users/@me"):
            return httpx.Response(200, json={"id": BOT_ID, "username": "corlinman"})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"id": message_id})
        return httpx.Response(404, json={"message": "not mocked"})

    return httpx.AsyncClient(transport=httpx.MockTransport(_handle))


def _make_connected_adapter(cfg: DiscordConfig) -> DiscordAdapter:
    """Build an adapter and mark it 'connected' without dialing the WS.

    The gateway loop is what fills ``_inbound_q``; tests seed the queue
    directly so ``inbound()`` runs against in-process data.
    """
    adapter = DiscordAdapter(cfg, http_client=_rest_client())
    adapter._bot_id = BOT_ID
    adapter._reader_task = asyncio.create_task(asyncio.sleep(3600))
    return adapter


async def _drain_one(adapter: DiscordAdapter) -> InboundEvent[Any] | None:
    """Pull a single event off the inbound iterator with a short cap."""
    it = adapter.inbound()
    try:
        return await asyncio.wait_for(it.__anext__(), timeout=2.0)
    except (StopAsyncIteration, TimeoutError):
        return None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestMentionHelper:
    def test_resolved_mention_matches(self) -> None:
        msg = {"mentions": [{"id": BOT_ID}], "content": "hi"}
        assert is_mentioning_bot(msg, BOT_ID) is True

    def test_raw_token_fallback(self) -> None:
        msg = {"mentions": [], "content": f"<@{BOT_ID}> hello"}
        assert is_mentioning_bot(msg, BOT_ID) is True

    def test_nickname_token_fallback(self) -> None:
        msg = {"content": f"<@!{BOT_ID}> hello"}
        assert is_mentioning_bot(msg, BOT_ID) is True

    def test_unrelated_mention_does_not_match(self) -> None:
        msg = {"mentions": [{"id": "777"}], "content": "<@777> hi"}
        assert is_mentioning_bot(msg, BOT_ID) is False


class TestBinding:
    def test_binding_fields(self) -> None:
        msg = {"channel_id": "C1", "author": {"id": "U9"}}
        b = binding_from_message(msg, BOT_ID)
        assert b.channel == "discord"
        assert b.account == BOT_ID
        assert b.thread == "C1"
        assert b.sender == "U9"


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfig:
    def test_empty_token_raises(self) -> None:
        with pytest.raises(ConfigError, match="bot_token"):
            DiscordAdapter(DiscordConfig(bot_token=""))


# ---------------------------------------------------------------------------
# Inbound — message-in → normalized event
# ---------------------------------------------------------------------------


class TestInbound:
    @pytest.mark.asyncio
    async def test_dm_yields_event(self) -> None:
        adapter = _make_connected_adapter(DiscordConfig(bot_token="t"))
        try:
            adapter._inbound_q.put_nowait(
                {
                    "id": "M1",
                    "channel_id": "C-dm",
                    "author": {"id": "U-user"},
                    "content": "hello bot",
                    # No guild_id → DM.
                }
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.channel == "discord"
            assert ev.text == "hello bot"
            assert ev.mentioned is True  # DMs are implicitly addressed
            assert ev.binding.thread == "C-dm"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_guild_message_without_mention_is_dropped(self) -> None:
        adapter = _make_connected_adapter(DiscordConfig(bot_token="t"))
        try:
            adapter._inbound_q.put_nowait(
                {
                    "id": "M2",
                    "channel_id": "C-guild",
                    "guild_id": "G1",
                    "author": {"id": "U-user"},
                    "content": "no mention here",
                }
            )
            # Then a mentioning message that *should* surface.
            adapter._inbound_q.put_nowait(
                {
                    "id": "M3",
                    "channel_id": "C-guild",
                    "guild_id": "G1",
                    "author": {"id": "U-user"},
                    "content": f"<@{BOT_ID}> ping",
                }
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.message_id == "M3"  # the un-mentioned one was skipped
            assert ev.text == "ping"  # mention token stripped
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_guild_respond_to_all(self) -> None:
        adapter = _make_connected_adapter(
            DiscordConfig(bot_token="t", respond_to_all=True)
        )
        try:
            adapter._inbound_q.put_nowait(
                {
                    "id": "M4",
                    "channel_id": "C-guild",
                    "guild_id": "G1",
                    "author": {"id": "U-user"},
                    "content": "plain message",
                }
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.text == "plain message"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_own_and_bot_messages_skipped(self) -> None:
        adapter = _make_connected_adapter(DiscordConfig(bot_token="t"))
        try:
            # Bot's own message.
            adapter._inbound_q.put_nowait(
                {
                    "id": "M5",
                    "channel_id": "C-dm",
                    "author": {"id": BOT_ID},
                    "content": "echo",
                }
            )
            # Another bot's message.
            adapter._inbound_q.put_nowait(
                {
                    "id": "M6",
                    "channel_id": "C-dm",
                    "author": {"id": "U-otherbot", "bot": True},
                    "content": "spam",
                }
            )
            # Real user follows.
            adapter._inbound_q.put_nowait(
                {
                    "id": "M7",
                    "channel_id": "C-dm",
                    "author": {"id": "U-user"},
                    "content": "real",
                }
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.message_id == "M7"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_allowed_channel_filter(self) -> None:
        adapter = _make_connected_adapter(
            DiscordConfig(bot_token="t", allowed_channel_ids=["C-allowed"])
        )
        try:
            adapter._inbound_q.put_nowait(
                {
                    "id": "M8",
                    "channel_id": "C-denied",
                    "author": {"id": "U-user"},
                    "content": "blocked",
                }
            )
            adapter._inbound_q.put_nowait(
                {
                    "id": "M9",
                    "channel_id": "C-allowed",
                    "author": {"id": "U-user"},
                    "content": "ok",
                }
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.message_id == "M9"
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Sender — reply-out
# ---------------------------------------------------------------------------


class TestSender:
    @pytest.mark.asyncio
    async def test_send_message_returns_id(self) -> None:
        client = _rest_client(message_id="NEW1")
        sender = DiscordSender(client, "tok")
        try:
            mid = await sender.send_message("C1", "reply text")
            assert mid == "NEW1"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_send_message_http_error_raises(self) -> None:
        def _handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = httpx.AsyncClient(transport=httpx.MockTransport(_handle))
        sender = DiscordSender(client, "tok")
        try:
            with pytest.raises(Exception, match="HTTP 500"):
                await sender.send_message("C1", "x")
        finally:
            await client.aclose()
