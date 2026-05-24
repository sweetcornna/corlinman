"""Tests for ``corlinman_channels.qq_official`` + ``qq_official_send``.

Mirrors the ``test_feishu.py`` shape: pure-function coverage for the
parsing helpers, plus integration coverage that drives the inbound
iterator and the REST sender through mocked transports.

The gateway WebSocket is *not* dialed — tests pre-seed
``adapter._inbound_q`` and mark the adapter "connected" so ``inbound()``
drains the queue without a network round-trip.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from corlinman_channels.common import ConfigError, InboundEvent, TransportError
from corlinman_channels.qq_official import (
    DEFAULT_API_BASE,
    DEFAULT_INTENTS,
    DEFAULT_SANDBOX_API_BASE,
    QqOfficialAdapter,
    QqOfficialConfig,
    binding_from_payload,
    extract_message_text,
    extract_msg_id,
)
from corlinman_channels.qq_official_send import (
    FILE_TYPE_IMAGE,
    MSG_TYPE_RICH_MEDIA,
    MSG_TYPE_TEXT,
    QqOfficialSender,
)

APP_ID = "1234567890"
APP_SECRET = "secret-abc"


# ---------------------------------------------------------------------------
# Mock REST transport
# ---------------------------------------------------------------------------


def _rest_client_for_sender(
    *,
    captured: list[httpx.Request] | None = None,
    file_info: str = "file_info_xyz",
    message_id: str = "msg_reply_1",
) -> httpx.AsyncClient:
    """An httpx client whose mock transport answers QQ Official REST."""

    def _handle(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        path = request.url.path
        if path.endswith("/files"):
            return httpx.Response(
                200,
                json={"file_info": file_info, "ttl": 600, "id": "file_id_1"},
            )
        if "/messages" in path:
            return httpx.Response(200, json={"id": message_id})
        return httpx.Response(404, json={"code": 99999, "msg": "not mocked"})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handle), timeout=httpx.Timeout(5.0)
    )


def _make_connected_adapter() -> QqOfficialAdapter:
    """Build an adapter and mark it 'connected' without dialing the WS."""
    adapter = QqOfficialAdapter(
        QqOfficialConfig(app_id=APP_ID, app_secret=APP_SECRET)
    )
    adapter._token = "tok-abc"
    adapter._token_expiry = 1e18  # never expires within the test
    adapter._reader_task = asyncio.create_task(asyncio.sleep(3600))
    return adapter


async def _drain_one(adapter: QqOfficialAdapter) -> InboundEvent[Any] | None:
    it = adapter.inbound()
    try:
        return await asyncio.wait_for(it.__anext__(), timeout=2.0)
    except (StopAsyncIteration, TimeoutError):
        return None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestExtractMessageText:
    def test_string_content(self) -> None:
        assert extract_message_text({"content": "hello bot"}) == "hello bot"

    def test_missing_content_is_empty(self) -> None:
        assert extract_message_text({}) == ""

    def test_non_string_content_is_empty(self) -> None:
        assert extract_message_text({"content": 42}) == ""


class TestExtractMsgId:
    def test_prefers_id(self) -> None:
        assert extract_msg_id({"id": "m1", "event_id": "e1"}) == "m1"

    def test_falls_back_to_event_id(self) -> None:
        assert extract_msg_id({"event_id": "e1"}) == "e1"

    def test_missing_returns_none(self) -> None:
        assert extract_msg_id({}) is None


class TestBinding:
    def test_c2c_thread_is_openid(self) -> None:
        payload = {"author": {"user_openid": "ou_user_aaa"}}
        b = binding_from_payload(
            event_type="C2C_MESSAGE_CREATE",
            payload=payload,
            bot_app_id=APP_ID,
        )
        assert b.channel == "qq_official"
        assert b.account == APP_ID
        assert b.thread == "ou_user_aaa"
        assert b.sender == "ou_user_aaa"

    def test_group_thread_is_group_openid(self) -> None:
        payload = {
            "group_openid": "og_group_1",
            "author": {"member_openid": "om_user_2"},
        }
        b = binding_from_payload(
            event_type="GROUP_AT_MESSAGE_CREATE",
            payload=payload,
            bot_app_id=APP_ID,
        )
        assert b.thread == "og_group_1"
        assert b.sender == "om_user_2"

    def test_guild_channel_thread_is_channel_id(self) -> None:
        payload = {"channel_id": "ch_42", "author": {"id": "u_99"}}
        b = binding_from_payload(
            event_type="AT_MESSAGE_CREATE",
            payload=payload,
            bot_app_id=APP_ID,
        )
        assert b.thread == "ch_42"
        assert b.sender == "u_99"


# ---------------------------------------------------------------------------
# Config / construction
# ---------------------------------------------------------------------------


class TestConfig:
    def test_empty_app_id_raises(self) -> None:
        with pytest.raises(ConfigError, match="app_id"):
            QqOfficialAdapter(QqOfficialConfig(app_id="", app_secret="s"))

    def test_empty_app_secret_raises(self) -> None:
        with pytest.raises(ConfigError, match="app_secret"):
            QqOfficialAdapter(QqOfficialConfig(app_id="a", app_secret=""))

    def test_sender_empty_app_id_raises(self) -> None:
        client = httpx.AsyncClient()

        async def _token() -> str:
            return "tok"

        with pytest.raises(ConfigError, match="app_id"):
            QqOfficialSender(client, _token, app_id="")

    def test_sender_missing_token_provider_raises(self) -> None:
        client = httpx.AsyncClient()
        with pytest.raises(ConfigError, match="token_provider"):
            QqOfficialSender(client, None, app_id=APP_ID)  # type: ignore[arg-type]

    def test_sandbox_flag_picks_sandbox_base(self) -> None:
        cfg = QqOfficialConfig(
            app_id="a", app_secret="s", sandbox=True
        )
        assert cfg.api_base == DEFAULT_SANDBOX_API_BASE

    def test_production_default_api_base(self) -> None:
        cfg = QqOfficialConfig(app_id="a", app_secret="s")
        assert cfg.api_base == DEFAULT_API_BASE

    def test_default_intents_bitmask(self) -> None:
        # GUILDS (1<<9) + DIRECT (1<<12) + C2C/GROUP (1<<25) + PUBLIC GUILD (1<<30).
        assert DEFAULT_INTENTS == (1 << 9) | (1 << 12) | (1 << 25) | (1 << 30)


# ---------------------------------------------------------------------------
# Inbound — parse and yield
# ---------------------------------------------------------------------------


class TestInbound:
    @pytest.mark.asyncio
    async def test_c2c_message_yields_event(self) -> None:
        adapter = _make_connected_adapter()
        try:
            adapter._inbound_q.put_nowait((
                "C2C_MESSAGE_CREATE",
                {
                    "id": "msg_c2c_1",
                    "content": "hello bot",
                    "author": {"user_openid": "ou_user_1"},
                },
            ))
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.channel == "qq_official"
            assert ev.text == "hello bot"
            assert ev.message_id == "msg_c2c_1"
            assert ev.binding.thread == "ou_user_1"
            assert ev.mentioned is True
            assert isinstance(ev.payload, dict)
            assert ev.payload["_qq_official_event_type"] == "C2C_MESSAGE_CREATE"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_group_at_message_yields_event(self) -> None:
        adapter = _make_connected_adapter()
        try:
            adapter._inbound_q.put_nowait((
                "GROUP_AT_MESSAGE_CREATE",
                {
                    "id": "msg_grp_1",
                    "content": " /help please",
                    "group_openid": "og_group_42",
                    "author": {"member_openid": "om_user_3"},
                },
            ))
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.binding.thread == "og_group_42"
            assert ev.binding.sender == "om_user_3"
            assert ev.message_id == "msg_grp_1"
            assert ev.payload["_qq_official_event_type"] == "GROUP_AT_MESSAGE_CREATE"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_guild_at_message_strips_mention(self) -> None:
        adapter = _make_connected_adapter()
        try:
            adapter._inbound_q.put_nowait((
                "AT_MESSAGE_CREATE",
                {
                    "id": "msg_guild_1",
                    "content": "<@!123456> ping",
                    "channel_id": "ch_abc",
                    "author": {"id": "user_77"},
                },
            ))
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.text == "ping"  # mention token stripped
            assert ev.binding.thread == "ch_abc"
            assert ev.binding.sender == "user_77"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_empty_text_dropped(self) -> None:
        adapter = _make_connected_adapter()
        try:
            adapter._inbound_q.put_nowait((
                "C2C_MESSAGE_CREATE",
                {"id": "m1", "content": "   ", "author": {"user_openid": "ou_x"}},
            ))
            adapter._inbound_q.put_nowait((
                "C2C_MESSAGE_CREATE",
                {"id": "m2", "content": "real", "author": {"user_openid": "ou_x"}},
            ))
            ev = await _drain_one(adapter)
            assert ev is not None
            assert ev.message_id == "m2"
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Drop-oldest behavior under burst
# ---------------------------------------------------------------------------


class TestInboundQueueOverflow:
    @pytest.mark.asyncio
    async def test_drop_oldest_when_queue_full(self) -> None:
        adapter = _make_connected_adapter()
        try:
            # Fill the queue past capacity (maxsize=64).
            for i in range(70):
                adapter._enqueue_event(
                    "C2C_MESSAGE_CREATE",
                    {
                        "id": f"m{i}",
                        "content": f"msg-{i}",
                        "author": {"user_openid": "ou_burst"},
                    },
                )
            assert adapter.inbound_dropped_count > 0
            # The queue should not exceed the bound (64) materially.
            assert adapter._inbound_q.qsize() <= 64
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Sender envelope tests
# ---------------------------------------------------------------------------


class TestSenderText:
    @pytest.mark.asyncio
    async def test_send_c2c_text_routes_to_users_endpoint(self) -> None:
        captured: list[httpx.Request] = []
        client = _rest_client_for_sender(
            captured=captured, message_id="msg_x"
        )

        async def _token() -> str:
            return "tok-c2c"

        sender = QqOfficialSender(client, _token, app_id=APP_ID)
        try:
            mid = await sender.send_c2c_text(
                "ou_user_a", "hi from bot", msg_id="orig_msg_1"
            )
            assert mid == "msg_x"
            assert len(captured) == 1
            req = captured[0]
            # URL routing.
            assert "/v2/users/ou_user_a/messages" in str(req.url)
            # Authorization header carries the access token.
            auth = req.headers.get("authorization", "")
            assert auth == "QQBot tok-c2c"
            # X-Union-Appid carries the app id.
            assert req.headers.get("x-union-appid") == APP_ID
            # Body shape: content + msg_type + msg_id passive-reply.
            body = json.loads(req.content.decode("utf-8"))
            assert body["content"] == "hi from bot"
            assert body["msg_type"] == MSG_TYPE_TEXT
            assert body["msg_id"] == "orig_msg_1"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_send_group_text_routes_to_groups_endpoint(self) -> None:
        captured: list[httpx.Request] = []
        client = _rest_client_for_sender(captured=captured, message_id="msg_g")

        async def _token() -> str:
            return "tok-grp"

        sender = QqOfficialSender(client, _token, app_id=APP_ID)
        try:
            mid = await sender.send_group_text(
                "og_42", "group reply", msg_id="inbound_1"
            )
            assert mid == "msg_g"
            req = captured[0]
            assert "/v2/groups/og_42/messages" in str(req.url)
            body = json.loads(req.content.decode("utf-8"))
            assert body["content"] == "group reply"
            assert body["msg_id"] == "inbound_1"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_send_guild_channel_text_routes_to_channels_endpoint(self) -> None:
        captured: list[httpx.Request] = []
        client = _rest_client_for_sender(captured=captured, message_id="msg_gh")

        async def _token() -> str:
            return "tok-gh"

        sender = QqOfficialSender(client, _token, app_id=APP_ID)
        try:
            mid = await sender.send_text(
                "channel_xyz", "guild reply", msg_id="inbound_2"
            )
            assert mid == "msg_gh"
            req = captured[0]
            assert "/channels/channel_xyz/messages" in str(req.url)
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_event_id_fallback_when_no_msg_id(self) -> None:
        captured: list[httpx.Request] = []
        client = _rest_client_for_sender(captured=captured)

        async def _token() -> str:
            return "tok"

        sender = QqOfficialSender(client, _token, app_id=APP_ID)
        try:
            await sender.send_c2c_text(
                "ou_a", "test", event_id="evt_42"
            )
            body = json.loads(captured[0].content.decode("utf-8"))
            # event_id takes the slot when msg_id is absent.
            assert body.get("event_id") == "evt_42"
            assert "msg_id" not in body
        finally:
            await client.aclose()


class TestSenderImageUpload:
    @pytest.mark.asyncio
    async def test_upload_group_image_returns_file_info(self) -> None:
        captured: list[httpx.Request] = []
        client = _rest_client_for_sender(
            captured=captured, file_info="cdn-token-123"
        )

        async def _token() -> str:
            return "tok"

        sender = QqOfficialSender(client, _token, app_id=APP_ID)
        try:
            info = await sender.upload_group_image(
                "og_g1", file_data=b"\x89PNG\r\n\x1a\nfake"
            )
            assert info == "cdn-token-123"
            req = captured[0]
            assert "/v2/groups/og_g1/files" in str(req.url)
            body = json.loads(req.content.decode("utf-8"))
            assert body["file_type"] == FILE_TYPE_IMAGE
            assert "file_data" in body  # base64 ships under file_data
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_upload_image_dispatcher_routes_to_c2c(self) -> None:
        captured: list[httpx.Request] = []
        client = _rest_client_for_sender(captured=captured)

        async def _token() -> str:
            return "tok"

        sender = QqOfficialSender(client, _token, app_id=APP_ID)
        try:
            await sender.upload_image(openid="ou_user_zz", url="https://e.x/i.png")
            req = captured[0]
            assert "/v2/users/ou_user_zz/files" in str(req.url)
            body = json.loads(req.content.decode("utf-8"))
            assert body.get("url") == "https://e.x/i.png"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_upload_image_dispatcher_rejects_both_targets(self) -> None:
        client = httpx.AsyncClient()

        async def _token() -> str:
            return "tok"

        sender = QqOfficialSender(client, _token, app_id=APP_ID)
        try:
            with pytest.raises(ValueError):
                await sender.upload_image(
                    group_openid="g1", openid="u1", url="https://x"
                )
        finally:
            await client.aclose()


class TestSenderImageSend:
    @pytest.mark.asyncio
    async def test_send_group_image_uses_file_info(self) -> None:
        captured: list[httpx.Request] = []
        client = _rest_client_for_sender(captured=captured)

        async def _token() -> str:
            return "tok"

        sender = QqOfficialSender(client, _token, app_id=APP_ID)
        try:
            await sender.send_group_image(
                "og_42", "cdn-token-xyz", msg_id="m_orig"
            )
            req = captured[0]
            assert "/v2/groups/og_42/messages" in str(req.url)
            body = json.loads(req.content.decode("utf-8"))
            assert body["msg_type"] == MSG_TYPE_RICH_MEDIA
            assert body["media"]["file_info"] == "cdn-token-xyz"
            assert body["msg_id"] == "m_orig"
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Access token single-flight refresh
# ---------------------------------------------------------------------------


class TestAccessTokenRefresh:
    @pytest.mark.asyncio
    async def test_parallel_refresh_makes_single_post(self) -> None:
        """Five parallel ``access_token()`` calls when the cache is
        empty must collapse to exactly ONE token-exchange POST."""
        token_calls: list[httpx.Request] = []

        async def _gate() -> None:
            # Slow down the token exchange enough that parallel callers
            # actually pile up on the lock.
            await asyncio.sleep(0.05)

        def _handle(request: httpx.Request) -> httpx.Response:
            token_calls.append(request)
            # Block the response slightly via a sync sleep equivalent —
            # we use the mock's "preprocessing" hook by deferring to
            # the awaitable on the actual call below.
            return httpx.Response(
                200,
                json={
                    "access_token": "first-token",
                    "expires_in": 7200,
                },
            )

        async def _async_handler(request: httpx.Request) -> httpx.Response:
            await _gate()
            return _handle(request)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_async_handler),
            timeout=httpx.Timeout(5.0),
        )
        adapter = QqOfficialAdapter(
            QqOfficialConfig(app_id=APP_ID, app_secret=APP_SECRET),
            http_client=client,
        )
        try:
            # Fire five concurrent token requests against an empty cache.
            results = await asyncio.gather(
                *(adapter.access_token() for _ in range(5))
            )
            assert all(r == "first-token" for r in results)
            assert len(token_calls) == 1, (
                f"expected single-flight, got {len(token_calls)} token POSTs"
            )
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_cached_token_short_circuits(self) -> None:
        """A second call within the expiry window returns the cached
        token without re-posting."""
        calls: list[httpx.Request] = []

        def _handle(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={"access_token": "tok-cached", "expires_in": 7200},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(_handle))
        adapter = QqOfficialAdapter(
            QqOfficialConfig(app_id=APP_ID, app_secret=APP_SECRET),
            http_client=client,
        )
        try:
            t1 = await adapter.access_token()
            t2 = await adapter.access_token()
            assert t1 == t2 == "tok-cached"
            assert len(calls) == 1
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_bad_credentials_raises_transport_error(self) -> None:
        def _handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"err": "bad creds"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(_handle))
        adapter = QqOfficialAdapter(
            QqOfficialConfig(app_id=APP_ID, app_secret=APP_SECRET),
            http_client=client,
        )
        try:
            with pytest.raises(TransportError, match="HTTP 401"):
                await adapter.access_token()
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Sender error envelope
# ---------------------------------------------------------------------------


class TestSenderErrors:
    @pytest.mark.asyncio
    async def test_http_error_raises_transport(self) -> None:
        def _handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="rate limited")

        client = httpx.AsyncClient(transport=httpx.MockTransport(_handle))

        async def _token() -> str:
            return "tok"

        sender = QqOfficialSender(client, _token, app_id=APP_ID)
        try:
            with pytest.raises(TransportError, match="HTTP 429"):
                await sender.send_c2c_text("ou_a", "x", msg_id="m1")
        finally:
            await client.aclose()
