"""Tests for ``corlinman_channels.feishu`` — the Feishu / Lark adapter.

corlinman has no Rust reference for Feishu, so these mirror the
``test_slack.py`` structure: pure-function coverage for the
text-extraction / mention / binding helpers, plus integration coverage
that drives the inbound iterator and the IM REST sender through mocked
transports.

The long-connection WebSocket is *not* dialed — tests pre-seed
``adapter._inbound_q`` and mark the adapter "connected" so ``inbound()``
drains the queue without a network round-trip. The REST side (token
exchange, ``im/v1/messages``) runs against an :class:`httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from corlinman_channels.common import ConfigError, InboundEvent
from corlinman_channels.feishu import (
    FeishuAdapter,
    FeishuConfig,
    FeishuSender,
    binding_from_event,
    extract_text,
    is_mentioning_bot,
)

BOT_OPEN_ID = "ou_bot_0001"


# ---------------------------------------------------------------------------
# Mock REST transport
# ---------------------------------------------------------------------------


def _rest_client(message_id: str = "om_new_0001") -> httpx.AsyncClient:
    """An httpx client whose mock transport answers the Feishu REST API."""

    def _handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-abc", "expire": 7200},
            )
        if path.endswith("/callback/ws/endpoint"):
            return httpx.Response(
                200, json={"code": 0, "data": {"URL": "wss://feishu.example/link"}}
            )
        if "/im/v1/messages" in path:
            return httpx.Response(
                200, json={"code": 0, "data": {"message_id": message_id}}
            )
        return httpx.Response(200, json={"code": 99999, "msg": "not mocked"})

    return httpx.AsyncClient(transport=httpx.MockTransport(_handle))


def _make_connected_adapter(cfg: FeishuConfig) -> FeishuAdapter:
    """Build an adapter and mark it 'connected' without dialing the WS."""
    adapter = FeishuAdapter(cfg, http_client=_rest_client())
    adapter._token = "t-abc"
    adapter._token_expiry = 1e18  # never expires within the test
    adapter._bot_open_id = BOT_OPEN_ID
    adapter._reader_task = asyncio.create_task(asyncio.sleep(3600))
    return adapter


async def _drain_one(adapter: FeishuAdapter) -> InboundEvent[Any] | None:
    it = adapter.inbound()
    try:
        return await asyncio.wait_for(it.__anext__(), timeout=2.0)
    except (StopAsyncIteration, TimeoutError):
        return None


def _text_message(
    *,
    chat_id: str,
    chat_type: str,
    text: str,
    message_id: str = "om_1",
    mentions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a Feishu ``im.message.receive_v1`` ``event`` object."""
    return {
        "sender": {
            "sender_id": {"open_id": "ou_user_1"},
            "sender_type": "user",
        },
        "message": {
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "message_type": "text",
            "content": json.dumps({"text": text}),
            "create_time": "1700000000000",
            "mentions": mentions or [],
        },
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestExtractText:
    def test_plain_text(self) -> None:
        msg = {"message_type": "text", "content": json.dumps({"text": "hello"})}
        assert extract_text(msg) == "hello"

    def test_rich_post(self) -> None:
        content = json.dumps(
            {
                "zh_cn": {
                    "title": "t",
                    "content": [[{"tag": "text", "text": "rich body"}]],
                }
            }
        )
        msg = {"message_type": "post", "content": content}
        assert "rich body" in extract_text(msg)

    def test_unknown_type_is_empty(self) -> None:
        msg = {"message_type": "image", "content": json.dumps({"image_key": "x"})}
        assert extract_text(msg) == ""


class TestMentionHelper:
    def test_mention_matches_open_id(self) -> None:
        msg = {"mentions": [{"id": {"open_id": BOT_OPEN_ID}}]}
        assert is_mentioning_bot(msg, BOT_OPEN_ID) is True

    def test_unrelated_mention_does_not_match(self) -> None:
        msg = {"mentions": [{"id": {"open_id": "ou_other"}}]}
        assert is_mentioning_bot(msg, BOT_OPEN_ID) is False


class TestBinding:
    def test_binding_fields(self) -> None:
        msg = {"chat_id": "oc_1", "_sender_open_id": "ou_user_1"}
        b = binding_from_event(msg, BOT_OPEN_ID)
        assert b.channel == "feishu"
        assert b.account == BOT_OPEN_ID
        assert b.thread == "oc_1"
        assert b.sender == "ou_user_1"


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfig:
    def test_empty_app_id_raises(self) -> None:
        with pytest.raises(ConfigError, match="app_id"):
            FeishuAdapter(FeishuConfig(app_id="", app_secret="s"))

    def test_empty_app_secret_raises(self) -> None:
        with pytest.raises(ConfigError, match="app_secret"):
            FeishuAdapter(FeishuConfig(app_id="a", app_secret=""))


# ---------------------------------------------------------------------------
# Inbound — message-in → normalized event
# ---------------------------------------------------------------------------


class TestInbound:
    @pytest.mark.asyncio
    async def test_p2p_yields_event(self) -> None:
        adapter = _make_connected_adapter(FeishuConfig(app_id="a", app_secret="s"))
        try:
            adapter._inbound_q.put_nowait(
                _text_message(chat_id="oc_dm", chat_type="p2p", text="hello bot")
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.channel == "feishu"
            assert ev.text == "hello bot"
            assert ev.mentioned is True  # p2p implicitly addressed
            assert ev.binding.thread == "oc_dm"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_group_without_mention_dropped(self) -> None:
        adapter = _make_connected_adapter(FeishuConfig(app_id="a", app_secret="s"))
        try:
            adapter._inbound_q.put_nowait(
                _text_message(
                    chat_id="oc_grp",
                    chat_type="group",
                    text="no mention",
                    message_id="om_skip",
                )
            )
            adapter._inbound_q.put_nowait(
                _text_message(
                    chat_id="oc_grp",
                    chat_type="group",
                    text="@_user_1 ping",
                    message_id="om_keep",
                    mentions=[{"id": {"open_id": BOT_OPEN_ID}}],
                )
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.message_id == "om_keep"
            assert ev.text == "ping"  # @_user_1 placeholder stripped
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_app_sender_skipped(self) -> None:
        adapter = _make_connected_adapter(FeishuConfig(app_id="a", app_secret="s"))
        try:
            self_msg = _text_message(
                chat_id="oc_dm", chat_type="p2p", text="echo", message_id="om_self"
            )
            self_msg["sender"]["sender_type"] = "app"
            adapter._inbound_q.put_nowait(self_msg)
            adapter._inbound_q.put_nowait(
                _text_message(
                    chat_id="oc_dm",
                    chat_type="p2p",
                    text="real",
                    message_id="om_real",
                )
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.message_id == "om_real"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_allowed_chat_filter(self) -> None:
        adapter = _make_connected_adapter(
            FeishuConfig(app_id="a", app_secret="s", allowed_chat_ids=["oc_ok"])
        )
        try:
            adapter._inbound_q.put_nowait(
                _text_message(
                    chat_id="oc_denied",
                    chat_type="p2p",
                    text="blocked",
                    message_id="om_x",
                )
            )
            adapter._inbound_q.put_nowait(
                _text_message(
                    chat_id="oc_ok", chat_type="p2p", text="ok", message_id="om_y"
                )
            )
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.message_id == "om_y"
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Sender — reply-out
# ---------------------------------------------------------------------------


class TestSender:
    @pytest.mark.asyncio
    async def test_send_reply_returns_id(self) -> None:
        client = _rest_client(message_id="om_reply_99")

        async def _token() -> str:
            return "t-abc"

        sender = FeishuSender(client, _token)
        try:
            mid = await sender.send_message(
                "oc_1", "reply text", reply_to_message_id="om_orig"
            )
            assert mid == "om_reply_99"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_send_fresh_message_returns_id(self) -> None:
        client = _rest_client(message_id="om_fresh_1")

        async def _token() -> str:
            return "t-abc"

        sender = FeishuSender(client, _token)
        try:
            mid = await sender.send_message("oc_1", "new message")
            assert mid == "om_fresh_1"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_send_error_code_raises(self) -> None:
        def _handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 230001, "msg": "bad chat"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(_handle))

        async def _token() -> str:
            return "t-abc"

        sender = FeishuSender(client, _token)
        try:
            with pytest.raises(Exception, match="error code 230001"):
                await sender.send_message("oc_1", "x")
        finally:
            await client.aclose()
