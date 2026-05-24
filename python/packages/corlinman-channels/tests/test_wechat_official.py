"""Tests for ``corlinman_channels.wechat_official`` + companion modules.

Covers:

* signature verification (``sha1(token, ts, nonce)`` shape);
* XML parsing for text / image / voice / event(subscribe/unsubscribe);
* access-token single-flight refresh under concurrent callers;
* customer-service ``msgtype=text`` envelope shape via httpx mock;
* passive-reply timeout fall-back to customer-service push;
* ``handle_one_wechat_official`` summary-prepend pattern (short head
  resolves the passive future, long tail goes to customer-service).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import httpx
import pytest
from corlinman_channels.common import InboundEvent
from corlinman_channels.service import (
    WeChatOfficialChannelParams,
    _split_passive_and_rest,
    handle_one_wechat_official,
    run_wechat_official_channel,
)
from corlinman_channels.wechat_official import (
    DEFAULT_PASSIVE_TIMEOUT_S,
    WeChatOfficialAdapter,
    WeChatOfficialConfig,
    _build_inbound_event,
    build_passive_xml,
    parse_wechat_xml,
    verify_signature,
)
from corlinman_channels.wechat_official_send import (
    MAX_TEXT_CHUNK,
    WeChatOfficialSender,
    split_for_send,
)

# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


TOKEN = "tencent-shared-secret"


def _good_sig(token: str, ts: str, nonce: str) -> str:
    parts = sorted([token, ts, nonce])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


class TestVerifySignature:
    def test_accepts_canonical_signature(self) -> None:
        sig = _good_sig(TOKEN, "1700000000", "abc")
        assert verify_signature(TOKEN, "1700000000", "abc", sig) is True

    def test_accepts_uppercase_signature(self) -> None:
        sig = _good_sig(TOKEN, "1700000000", "abc")
        assert verify_signature(TOKEN, "1700000000", "abc", sig.upper()) is True

    def test_rejects_wrong_signature(self) -> None:
        assert verify_signature(TOKEN, "1700000000", "abc", "0" * 40) is False

    def test_rejects_wrong_token(self) -> None:
        sig = _good_sig(TOKEN, "1700000000", "abc")
        assert verify_signature("other-token", "1700000000", "abc", sig) is False

    def test_rejects_missing_fields(self) -> None:
        sig = _good_sig(TOKEN, "1700000000", "abc")
        assert verify_signature("", "1700000000", "abc", sig) is False
        assert verify_signature(TOKEN, "", "abc", sig) is False
        assert verify_signature(TOKEN, "1700000000", "", sig) is False
        assert verify_signature(TOKEN, "1700000000", "abc", "") is False

    def test_signature_must_match_exact_length(self) -> None:
        # 40-char hex sha1 — shorter / longer strings must fail.
        sig = _good_sig(TOKEN, "1700000000", "abc")
        assert verify_signature(TOKEN, "1700000000", "abc", sig[:39]) is False
        assert verify_signature(TOKEN, "1700000000", "abc", sig + "0") is False


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------


def _xml(**fields: str) -> bytes:
    """Build a WeChat inbound XML envelope from kwargs."""
    body = "<xml>"
    for k, v in fields.items():
        # Numbers stay raw, everything else gets CDATA wrap.
        if v.isdigit():
            body += f"<{k}>{v}</{k}>"
        else:
            body += f"<{k}><![CDATA[{v}]]></{k}>"
    body += "</xml>"
    return body.encode("utf-8")


class TestParseXml:
    def test_text_message(self) -> None:
        body = _xml(
            ToUserName="gh_official",
            FromUserName="o_user_1",
            CreateTime="1700000000",
            MsgType="text",
            Content="hello",
            MsgId="1234",
        )
        fields = parse_wechat_xml(body)
        assert fields["MsgType"] == "text"
        assert fields["Content"] == "hello"
        assert fields["FromUserName"] == "o_user_1"
        assert fields["MsgId"] == "1234"

    def test_image_message(self) -> None:
        body = _xml(
            ToUserName="gh_official",
            FromUserName="o_user_1",
            CreateTime="1700000000",
            MsgType="image",
            PicUrl="https://mmbiz.qpic.cn/example.jpg",
            MediaId="media-123",
            MsgId="1235",
        )
        fields = parse_wechat_xml(body)
        assert fields["MsgType"] == "image"
        assert fields["PicUrl"] == "https://mmbiz.qpic.cn/example.jpg"
        assert fields["MediaId"] == "media-123"

    def test_voice_message(self) -> None:
        body = _xml(
            ToUserName="gh_official",
            FromUserName="o_user_1",
            CreateTime="1700000000",
            MsgType="voice",
            MediaId="voice-456",
            Format="amr",
            MsgId="1236",
        )
        fields = parse_wechat_xml(body)
        assert fields["MsgType"] == "voice"
        assert fields["Format"] == "amr"

    def test_subscribe_event(self) -> None:
        body = _xml(
            ToUserName="gh_official",
            FromUserName="o_user_new",
            CreateTime="1700000000",
            MsgType="event",
            Event="subscribe",
        )
        fields = parse_wechat_xml(body)
        assert fields["MsgType"] == "event"
        assert fields["Event"] == "subscribe"

    def test_unsubscribe_event(self) -> None:
        body = _xml(
            ToUserName="gh_official",
            FromUserName="o_user_bye",
            CreateTime="1700000000",
            MsgType="event",
            Event="unsubscribe",
        )
        fields = parse_wechat_xml(body)
        assert fields["Event"] == "unsubscribe"

    def test_empty_body(self) -> None:
        assert parse_wechat_xml(b"") == {}

    def test_malformed_xml_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_wechat_xml(b"<xml><Content>oops")

    def test_wrong_root_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_wechat_xml(b"<other></other>")


class TestBuildInboundEvent:
    def test_text(self) -> None:
        ev = _build_inbound_event(
            {
                "ToUserName": "gh_a",
                "FromUserName": "o_user",
                "CreateTime": "1700",
                "MsgType": "text",
                "Content": "hi",
                "MsgId": "id1",
            }
        )
        assert ev is not None
        assert ev.channel == "wechat_official"
        assert ev.text == "hi"
        assert ev.message_id == "id1"
        assert ev.binding.sender == "o_user"
        assert ev.binding.account == "gh_a"
        assert ev.mentioned is True
        assert ev.attachments == []

    def test_image(self) -> None:
        ev = _build_inbound_event(
            {
                "ToUserName": "gh_a",
                "FromUserName": "o_user",
                "CreateTime": "1700",
                "MsgType": "image",
                "PicUrl": "https://example.com/p.jpg",
                "MediaId": "m1",
                "MsgId": "id2",
            }
        )
        assert ev is not None
        assert len(ev.attachments) == 1
        assert ev.attachments[0].url == "https://example.com/p.jpg"

    def test_subscribe_event_yields_inbound(self) -> None:
        ev = _build_inbound_event(
            {
                "ToUserName": "gh_a",
                "FromUserName": "o_new",
                "CreateTime": "1700",
                "MsgType": "event",
                "Event": "subscribe",
            }
        )
        assert ev is not None
        assert ev.text == "[subscribe]"

    def test_unsubscribe_event_yields_inbound(self) -> None:
        ev = _build_inbound_event(
            {
                "ToUserName": "gh_a",
                "FromUserName": "o_gone",
                "CreateTime": "1700",
                "MsgType": "event",
                "Event": "unsubscribe",
            }
        )
        assert ev is not None
        assert ev.text == "[unsubscribe]"

    def test_unknown_event_dropped(self) -> None:
        ev = _build_inbound_event(
            {
                "ToUserName": "gh_a",
                "FromUserName": "o",
                "CreateTime": "1700",
                "MsgType": "event",
                "Event": "scancode_push",
            }
        )
        assert ev is None

    def test_missing_users_dropped(self) -> None:
        assert _build_inbound_event({}) is None


# ---------------------------------------------------------------------------
# Passive XML builder
# ---------------------------------------------------------------------------


class TestBuildPassiveXml:
    def test_round_trip(self) -> None:
        xml = build_passive_xml(
            to_user="o_user",
            from_user="gh_official",
            content="hello back",
            create_time=1700,
        )
        root = ET.fromstring(xml)
        assert root.find("ToUserName").text == "o_user"
        assert root.find("FromUserName").text == "gh_official"
        assert root.find("MsgType").text == "text"
        assert root.find("Content").text == "hello back"
        assert root.find("CreateTime").text == "1700"

    def test_escapes_cdata_terminator(self) -> None:
        # ]]> inside content must NOT terminate the CDATA block early.
        xml = build_passive_xml(
            to_user="u", from_user="g", content="malicious ]]> end", create_time=1
        )
        # If escaping failed the XML would be malformed.
        root = ET.fromstring(xml)
        assert "malicious" in (root.find("Content").text or "")


# ---------------------------------------------------------------------------
# Adapter — AES guard, signature on webhook handler, passive timeout
# ---------------------------------------------------------------------------


def _cfg(**overrides: Any) -> WeChatOfficialConfig:
    base: dict[str, Any] = {
        "app_id": "wx-app-id",
        "app_secret": "wx-app-secret",
        "token": TOKEN,
        "encoding_aes_key": "",
        "passive_timeout_s": 0.0,
    }
    base.update(overrides)
    return WeChatOfficialConfig(**base)


class TestAdapterConstruction:
    def test_aes_key_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            WeChatOfficialAdapter(_cfg(encoding_aes_key="A" * 43))

    def test_missing_token_raises(self) -> None:
        from corlinman_channels.common import ConfigError

        with pytest.raises(ConfigError):
            WeChatOfficialAdapter(_cfg(token=""))


class TestPassiveTimeoutResolution:
    def test_explicit_value_wins(self) -> None:
        a = WeChatOfficialAdapter(_cfg(passive_timeout_s=2.5))
        assert a.passive_timeout_s == 2.5

    def test_default_when_unset(self) -> None:
        a = WeChatOfficialAdapter(_cfg())
        assert a.passive_timeout_s == DEFAULT_PASSIVE_TIMEOUT_S

    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORLINMAN_WECHAT_PASSIVE_TIMEOUT_S", "1.5")
        a = WeChatOfficialAdapter(_cfg())
        assert a.passive_timeout_s == 1.5

    def test_clamped_floor(self) -> None:
        a = WeChatOfficialAdapter(_cfg(passive_timeout_s=0.1))
        assert a.passive_timeout_s == 0.5


# ---------------------------------------------------------------------------
# handle_webhook — uses a starlette TestClient-style ASGI scope. We
# bypass FastAPI and synthesise a Request directly so the adapter test
# stays self-contained.
# ---------------------------------------------------------------------------


def _make_request(
    method: str,
    query_params: dict[str, str],
    body: bytes = b"",
) -> Any:
    """Build a starlette.requests.Request with the supplied method + body."""
    from starlette.requests import Request

    query_string = "&".join(f"{k}={v}" for k, v in query_params.items()).encode()
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": "/wechat/test",
        "raw_path": b"/wechat/test",
        "query_string": query_string,
        "headers": [(b"content-type", b"application/xml")],
        "scheme": "https",
        "server": ("example.com", 443),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive=receive)


@pytest.mark.asyncio
async def test_handle_webhook_rejects_bad_signature() -> None:
    adapter = WeChatOfficialAdapter(_cfg())
    req = _make_request(
        "POST",
        {"signature": "0" * 40, "timestamp": "1700", "nonce": "n"},
        body=_xml(
            ToUserName="gh", FromUserName="o", CreateTime="1",
            MsgType="text", Content="hi",
        ),
    )
    resp = await adapter.handle_webhook(req)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_handle_webhook_get_echostr() -> None:
    adapter = WeChatOfficialAdapter(_cfg())
    sig = _good_sig(TOKEN, "1700", "n")
    req = _make_request(
        "GET",
        {"signature": sig, "timestamp": "1700", "nonce": "n", "echostr": "verifyme"},
    )
    resp = await adapter.handle_webhook(req)
    assert resp.status_code == 200
    assert resp.body == b"verifyme"


@pytest.mark.asyncio
async def test_handle_webhook_passive_reply_when_agent_resolves_in_time() -> None:
    adapter = WeChatOfficialAdapter(_cfg(passive_timeout_s=1.0))

    async def fake_agent(
        inbound: InboundEvent[Any], fut: asyncio.Future[str]
    ) -> None:
        # Resolve quickly — under the 1.0s deadline.
        await asyncio.sleep(0.05)
        if not fut.done():
            fut.set_result("instant reply")

    adapter.set_on_event(fake_agent)
    sig = _good_sig(TOKEN, "1700", "n")
    req = _make_request(
        "POST",
        {"signature": sig, "timestamp": "1700", "nonce": "n"},
        body=_xml(
            ToUserName="gh", FromUserName="o_user", CreateTime="1",
            MsgType="text", Content="hi", MsgId="m1",
        ),
    )
    resp = await adapter.handle_webhook(req)
    assert resp.status_code == 200
    assert b"instant reply" in resp.body
    # Verify XML envelope swapped the user names.
    root = ET.fromstring(resp.body)
    assert root.find("ToUserName").text == "o_user"
    assert root.find("FromUserName").text == "gh"


@pytest.mark.asyncio
async def test_handle_webhook_falls_back_to_empty_200_on_timeout() -> None:
    adapter = WeChatOfficialAdapter(_cfg(passive_timeout_s=0.5))
    sink_called = asyncio.Event()

    async def slow_agent(
        inbound: InboundEvent[Any], fut: asyncio.Future[str]
    ) -> None:
        sink_called.set()
        # Sleep past the deadline — webhook should time out.
        await asyncio.sleep(2.0)

    adapter.set_on_event(slow_agent)
    sig = _good_sig(TOKEN, "1700", "n")
    req = _make_request(
        "POST",
        {"signature": sig, "timestamp": "1700", "nonce": "n"},
        body=_xml(
            ToUserName="gh", FromUserName="o_user", CreateTime="1",
            MsgType="text", Content="hi", MsgId="m2",
        ),
    )
    resp = await adapter.handle_webhook(req)
    assert resp.status_code == 200
    assert resp.body == b""
    assert sink_called.is_set()


# ---------------------------------------------------------------------------
# Sender — single-flight token, customer-service envelope, text split
# ---------------------------------------------------------------------------


def _mock_sender(
    responses: list[Any] | None = None,
) -> tuple[WeChatOfficialSender, list[httpx.Request]]:
    """Build a sender + the list it appends each handled request to."""
    seen_requests: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.url.path.endswith("/cgi-bin/token"):
            return httpx.Response(
                200, json={"access_token": "token-fresh", "expires_in": 7200}
            )
        if request.url.path.endswith("/cgi-bin/message/custom/send"):
            return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        if request.url.path.endswith("/cgi-bin/media/upload"):
            return httpx.Response(
                200, json={"type": "image", "media_id": "mid-1", "created_at": 1}
            )
        return httpx.Response(404, json={"errcode": -1, "errmsg": "not mocked"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handle))
    sender = WeChatOfficialSender(
        app_id="wx", app_secret="secret", client=client
    )
    return sender, seen_requests


class TestSplitForSend:
    def test_short_text_is_one_chunk(self) -> None:
        assert split_for_send("hello") == ["hello"]

    def test_long_text_splits(self) -> None:
        # 3x the chunk → at least 3 pieces.
        text = "a" * (MAX_TEXT_CHUNK * 3)
        parts = split_for_send(text)
        assert len(parts) >= 3
        assert all(len(p) <= MAX_TEXT_CHUNK for p in parts)
        assert "".join(parts) == text

    def test_empty_returns_empty_list(self) -> None:
        assert split_for_send("") == []

    def test_break_at_paragraph_when_possible(self) -> None:
        # Build content with a paragraph break near the chunk limit.
        head = "x" * (MAX_TEXT_CHUNK - 50)
        body = head + "\n\n" + ("y" * 500)
        parts = split_for_send(body)
        # First chunk should end at the paragraph break, not mid-y.
        assert parts[0].endswith("x" * (MAX_TEXT_CHUNK - 50))


@pytest.mark.asyncio
async def test_access_token_single_flight() -> None:
    # Custom counter to assert ONE actual fetch despite N callers.
    fetches = 0

    def _handle(request: httpx.Request) -> httpx.Response:
        nonlocal fetches
        if request.url.path.endswith("/cgi-bin/token"):
            fetches += 1
            return httpx.Response(
                200, json={"access_token": "t-1", "expires_in": 7200}
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handle))
    sender = WeChatOfficialSender(app_id="wx", app_secret="s", client=client)
    try:
        # Fire 8 callers in parallel — only ONE should reach the network.
        tokens = await asyncio.gather(*[sender.access_token() for _ in range(8)])
        assert all(t == "t-1" for t in tokens)
        assert fetches == 1
        # A second fetch within the TTL should hit the cache.
        again = await sender.access_token()
        assert again == "t-1"
        assert fetches == 1
    finally:
        await sender.aclose()


async def test_send_text_customer_envelope() -> None:
    sender, calls = _mock_sender()
    try:
        await sender.send_text_customer("o_user", "ping")
        # First call: token fetch. Second: customer/send.
        token_calls = [c for c in calls if c.url.path.endswith("/cgi-bin/token")]
        send_calls = [
            c
            for c in calls
            if c.url.path.endswith("/cgi-bin/message/custom/send")
        ]
        assert len(token_calls) == 1
        assert len(send_calls) == 1
        payload = json.loads(send_calls[0].content)
        assert payload == {
            "touser": "o_user",
            "msgtype": "text",
            "text": {"content": "ping"},
        }
        # access_token should be on the query string.
        assert "access_token=token-fresh" in str(send_calls[0].url)
    finally:
        await sender.aclose()


async def test_send_text_customer_splits_long_body() -> None:
    sender, calls = _mock_sender()
    try:
        long = "x" * (MAX_TEXT_CHUNK * 2 + 100)
        await sender.send_text_customer("o_user", long)
        send_calls = [
            c
            for c in calls
            if c.url.path.endswith("/cgi-bin/message/custom/send")
        ]
        assert len(send_calls) >= 3
        # Reassembling the chunks should yield the original message.
        bodies = [
            json.loads(c.content)["text"]["content"] for c in send_calls
        ]
        assert "".join(bodies) == long
    finally:
        await sender.aclose()


async def test_send_text_customer_skips_empty() -> None:
    sender, calls = _mock_sender()
    try:
        await sender.send_text_customer("o_user", "")
        # Token fetch should not even happen for an empty body.
        assert all(
            not c.url.path.endswith("/cgi-bin/message/custom/send") for c in calls
        )
    finally:
        await sender.aclose()


async def test_send_image_customer_envelope() -> None:
    sender, calls = _mock_sender()
    try:
        await sender.send_image_customer("o_user", "media-xyz")
        send_calls = [
            c
            for c in calls
            if c.url.path.endswith("/cgi-bin/message/custom/send")
        ]
        assert len(send_calls) == 1
        payload = json.loads(send_calls[0].content)
        assert payload == {
            "touser": "o_user",
            "msgtype": "image",
            "image": {"media_id": "media-xyz"},
        }
    finally:
        await sender.aclose()


@pytest.mark.asyncio
async def test_customer_send_refreshes_token_on_40001() -> None:
    """40001 (invalid credential) triggers ONE retry with a fresh token."""
    fetches = 0
    sends = 0

    def _handle(request: httpx.Request) -> httpx.Response:
        nonlocal fetches, sends
        if request.url.path.endswith("/cgi-bin/token"):
            fetches += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{fetches}",
                    "expires_in": 7200,
                },
            )
        if request.url.path.endswith("/cgi-bin/message/custom/send"):
            sends += 1
            if sends == 1:
                return httpx.Response(200, json={"errcode": 40001, "errmsg": "bad"})
            return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handle))
    sender = WeChatOfficialSender(app_id="wx", app_secret="s", client=client)
    try:
        await sender.send_text_customer("o", "hello")
        assert fetches == 2  # original + refresh
        assert sends == 2  # initial 40001 + retry
    finally:
        await sender.aclose()


# ---------------------------------------------------------------------------
# handle_one_wechat_official — summary-prepend + customer-service push
# ---------------------------------------------------------------------------


class _FakeEvent:
    def __init__(self, kind: str, text: str = "", error: str = "") -> None:
        self.kind = kind
        self.text = text
        self.error = error


class _FakeChatService:
    """Streams a scripted reply token by token, then a ``done`` frame."""

    def __init__(self, body: str) -> None:
        self._body = body

    def run(self, request: Any, cancel: asyncio.Event) -> Any:
        body = self._body

        async def _gen() -> Any:
            for chunk in [body[i : i + 16] for i in range(0, len(body), 16)]:
                yield _FakeEvent("token_delta", text=chunk)
            yield _FakeEvent("done")

        return _gen()


def _inbound(sender: str = "o_user", account: str = "gh_a") -> InboundEvent[Any]:
    from corlinman_channels.common import ChannelBinding

    return InboundEvent(
        channel="wechat_official",
        binding=ChannelBinding(
            channel="wechat_official",
            account=account,
            thread=sender,
            sender=sender,
        ),
        text="ping",
        message_id="m1",
    )


class TestSplitPassiveAndRest:
    def test_short_body_all_passive(self) -> None:
        passive, rest = _split_passive_and_rest("hello world")
        assert passive == "hello world"
        assert rest == ""

    def test_long_body_splits_at_sentence(self) -> None:
        # Put a sentence boundary near the end of the cap so the splitter
        # has a real punctuation marker to break on.
        head = "x" * 400 + ". This is the rest of the head sentence.\n"
        long_tail = "Second body block. " + "y" * 2000
        body = head + long_tail
        passive, rest = _split_passive_and_rest(body)
        # passive should be ≤ 600 chars and start with the head.
        assert len(passive) <= 600
        assert passive.startswith("x" * 100)
        assert rest  # non-empty remainder
        # The head sentence should end in the passive payload.
        assert passive.rstrip().endswith(("。", ".", "\n")) or "head sentence" in passive

    def test_no_punctuation_falls_back_to_slice(self) -> None:
        body = "a" * 1000
        passive, rest = _split_passive_and_rest(body)
        assert passive.endswith("…")
        assert len(rest) > 0


@pytest.mark.asyncio
async def test_handle_one_wechat_official_short_reply_passive_only() -> None:
    """Short reply: whole body lands in passive XML, no customer-service push."""
    chat = _FakeChatService("hi there")
    sender = AsyncMock(spec=WeChatOfficialSender)
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    inbound = _inbound()
    cancel = asyncio.Event()

    await handle_one_wechat_official(
        chat, inbound, "model-x", sender, cancel, passive_future=fut
    )

    assert fut.done()
    assert fut.result() == "hi there"
    sender.send_text_customer.assert_not_called()


@pytest.mark.asyncio
async def test_handle_one_wechat_official_long_reply_splits() -> None:
    """Long reply: head goes passive, tail flows through customer/send."""
    body = "Hello there. " + ("more body text " * 200)  # > 600 chars
    chat = _FakeChatService(body)
    sender = AsyncMock(spec=WeChatOfficialSender)
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    inbound = _inbound()
    cancel = asyncio.Event()

    await handle_one_wechat_official(
        chat, inbound, "model-x", sender, cancel, passive_future=fut
    )

    assert fut.done()
    head = fut.result()
    assert "Hello there" in head
    assert len(head) <= 600
    sender.send_text_customer.assert_called_once()
    args, _ = sender.send_text_customer.call_args
    assert args[0] == "o_user"  # openid
    # Combined head + tail should yield (approximately) the original body
    assert len(args[1]) > 100


@pytest.mark.asyncio
async def test_handle_one_wechat_official_no_future_pushes_all() -> None:
    """When passive_future is None, the WHOLE body goes via customer/send."""
    chat = _FakeChatService("complete answer")
    sender = AsyncMock(spec=WeChatOfficialSender)
    inbound = _inbound()
    cancel = asyncio.Event()

    await handle_one_wechat_official(
        chat, inbound, "model-x", sender, cancel, passive_future=None
    )

    sender.send_text_customer.assert_called_once_with("o_user", "complete answer")


@pytest.mark.asyncio
async def test_handle_one_wechat_official_empty_reply_releases_future() -> None:
    """Empty stream resolves the future with '' so the webhook doesn't hang."""
    chat = _FakeChatService("")
    sender = AsyncMock(spec=WeChatOfficialSender)
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    inbound = _inbound()
    cancel = asyncio.Event()

    await handle_one_wechat_official(
        chat, inbound, "model-x", sender, cancel, passive_future=fut
    )

    assert fut.done()
    assert fut.result() == ""
    sender.send_text_customer.assert_not_called()


class _TodoFakeEvent:
    """Extended fake event that also exposes ``tool`` + ``args_json`` so
    the wechat handler can pick up todo_write calls."""

    def __init__(
        self,
        kind: str,
        *,
        text: str = "",
        tool: str = "",
        args_json: bytes = b"",
    ) -> None:
        self.kind = kind
        self.text = text
        self.tool = tool
        self.args_json = args_json
        self.error = ""


class _TodoScriptedChatService:
    """Streams a fixed list of fake events — needed for the WeChat
    todo_write coverage, which the plain ``_FakeChatService`` (built
    around a body string) can't express."""

    def __init__(self, events: list[_TodoFakeEvent]) -> None:
        self._events = events

    def run(self, request: Any, cancel: asyncio.Event) -> Any:
        events = self._events

        async def _gen() -> Any:
            for ev in events:
                yield ev

        return _gen()


@pytest.mark.asyncio
async def test_handle_one_wechat_official_prepends_todo_list() -> None:
    """A ``todo_write`` call must prepend the rendered checkbox list
    above the assistant reply on the WeChat Official channel. WeChat
    has no edit / typing surface so the list is the user's only signal
    that the agent planned the work."""
    todos = json.dumps({"todos": [
        {"content": "Look up the answer",
         "activeForm": "Looking up the answer",
         "status": "completed"},
        {"content": "Compose reply",
         "activeForm": "Composing reply",
         "status": "in_progress"},
    ]}).encode("utf-8")
    chat = _TodoScriptedChatService([
        _TodoFakeEvent("tool_call", tool="todo_write", args_json=todos),
        _TodoFakeEvent("token_delta", text="here is the reply"),
        _TodoFakeEvent("done"),
    ])
    sender = AsyncMock(spec=WeChatOfficialSender)
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    inbound = _inbound()
    cancel = asyncio.Event()

    await handle_one_wechat_official(
        chat, inbound, "model-x", sender, cancel, passive_future=fut
    )

    assert fut.done()
    passive = fut.result()
    # The passive payload carries the todo list header AND the reply,
    # since the combined body fits under the 600-char passive cap.
    assert "📋 任务清单 (1/2)" in passive
    assert "☑ Look up the answer" in passive
    assert "▣ Composing reply" in passive
    assert "here is the reply" in passive
    # Ordering: todo block above body.
    assert passive.index("📋 任务清单") < passive.index("here is the reply")
    sender.send_text_customer.assert_not_called()


@pytest.mark.asyncio
async def test_handle_one_wechat_official_todo_only_empty_reply_ships_list() -> None:
    """If the agent ONLY writes a todo list and produces no token text,
    the passive payload must still carry the rendered list — otherwise
    the user sees nothing despite the agent doing work."""
    todos = json.dumps({"todos": [
        {"content": "Check inbox",
         "activeForm": "Checking inbox",
         "status": "in_progress"},
    ]}).encode("utf-8")
    chat = _TodoScriptedChatService([
        _TodoFakeEvent("tool_call", tool="todo_write", args_json=todos),
        _TodoFakeEvent("done"),
    ])
    sender = AsyncMock(spec=WeChatOfficialSender)
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    inbound = _inbound()
    cancel = asyncio.Event()

    await handle_one_wechat_official(
        chat, inbound, "model-x", sender, cancel, passive_future=fut
    )

    assert fut.done()
    passive = fut.result()
    assert "📋 任务清单 (0/1)" in passive
    assert "▣ Checking inbox" in passive
    sender.send_text_customer.assert_not_called()


# ---------------------------------------------------------------------------
# Channel runner — exercise the register_route callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_wechat_official_channel_registers_route_and_cancels() -> None:
    registered: list[tuple[str, Any]] = []

    def _register(bot_name: str, adapter: Any) -> None:
        registered.append((bot_name, adapter))

    params = WeChatOfficialChannelParams(
        config={
            "app_id": "wx",
            "app_secret": "s",
            "token": TOKEN,
            "bot_name": "test-bot",
        },
        model="model-x",
        chat_service=None,
        register_route=_register,
    )
    cancel = asyncio.Event()
    runner = asyncio.create_task(run_wechat_official_channel(params, cancel))
    # Give the runner a tick to register.
    await asyncio.sleep(0.05)
    assert registered and registered[0][0] == "test-bot"
    cancel.set()
    await asyncio.wait_for(runner, timeout=2.0)


@pytest.mark.asyncio
async def test_run_wechat_official_channel_raises_on_missing_token() -> None:
    params = WeChatOfficialChannelParams(
        config={"app_id": "wx", "app_secret": "s", "token": ""},
        chat_service=None,
    )
    with pytest.raises(ValueError):
        await run_wechat_official_channel(params, asyncio.Event())
