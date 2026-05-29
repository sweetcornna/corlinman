"""Route-level tests for ``gateway/routes/wechat_webhook.py`` (TEST-007).

The R2-004 work covered the ``defusedxml`` parse *inside the adapter*;
the ROUTE itself (the per-bot lookup + GET/POST dispatch surface) had no
coverage. This file exercises the real ``build_wechat_router`` against a
real ``corlinman_channels.wechat_official.WeChatOfficialAdapter`` (not a
mock of ``handle_webhook``):

* **GET echo** — the WeChat URL-verify handshake. A correctly-signed
  ``GET ?echostr=...`` returns the echostr verbatim as ``text/plain``.
* **bad signature** — a wrong signature is rejected by the adapter
  (401 ``forbidden``), proving the route really delegates verification.
* **POST dispatch** — a correctly-signed inbound text-message XML POST
  is dispatched to the adapter and returns 200.
* **unknown bot → 404 text/plain** — an unregistered ``bot_name`` is
  pure route logic and returns a 404 ``text/plain`` body.

The route is intentionally *public* (WeChat's edge is unauthenticated;
the SHA-1 signature is the only trust anchor), so there is no API-key
gate to assert here — that is by design per the module docstring.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("corlinman_channels.wechat_official")

from corlinman_channels.wechat_official import (  # noqa: E402
    WeChatOfficialAdapter,
    WeChatOfficialConfig,
)
from corlinman_server.gateway.routes import wechat_webhook  # noqa: E402
from corlinman_server.gateway.routes.wechat_webhook import (  # noqa: E402
    build_wechat_router,
    register_bot,
    unregister_bot,
)
from fastapi.testclient import TestClient  # noqa: E402

_TOKEN = "corntoken"
_BOT_NAME = "cornbot"


def _signature(timestamp: str, nonce: str, token: str = _TOKEN) -> str:
    """WeChat's handshake signature: ``sha1(sorted(token, ts, nonce))``."""
    joined = "".join(sorted([token, timestamp, nonce]))
    return hashlib.sha1(joined.encode()).hexdigest()


@pytest.fixture
def wechat_client() -> Iterator[TestClient]:
    """A TestClient with one real adapter registered under ``cornbot``.

    The WeChat registry is a process-global module dict, so the fixture
    cleans up after itself to avoid cross-test leakage.
    """
    cfg = WeChatOfficialConfig(
        app_id="wxapp",
        app_secret="secret",
        token=_TOKEN,
        encoding_aes_key=None,
        passive_timeout_s=4.0,
    )
    adapter = WeChatOfficialAdapter(cfg)
    register_bot(_BOT_NAME, adapter)
    app = fastapi.FastAPI()
    app.include_router(build_wechat_router())
    try:
        with TestClient(app) as client:
            yield client
    finally:
        unregister_bot(_BOT_NAME)


# ---------------------------------------------------------------------------
# GET echo (URL-verify handshake)
# ---------------------------------------------------------------------------


def test_get_echo_returns_echostr_on_valid_signature(
    wechat_client: TestClient,
) -> None:
    ts, nonce, echo = "1700000000", "abc123", "verify-me"
    resp = wechat_client.get(
        f"/wechat/{_BOT_NAME}",
        params={
            "signature": _signature(ts, nonce),
            "timestamp": ts,
            "nonce": nonce,
            "echostr": echo,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.text == echo
    assert resp.headers["content-type"].startswith("text/plain")


def test_get_with_bad_signature_is_rejected(wechat_client: TestClient) -> None:
    """A wrong signature must NOT echo back — proves the route delegates
    real verification to the adapter rather than blindly echoing."""
    ts, nonce, echo = "1700000000", "abc123", "verify-me"
    resp = wechat_client.get(
        f"/wechat/{_BOT_NAME}",
        params={
            "signature": "deadbeef",
            "timestamp": ts,
            "nonce": nonce,
            "echostr": echo,
        },
    )
    assert resp.status_code != 200, resp.text
    assert resp.text != echo


# ---------------------------------------------------------------------------
# POST dispatch
# ---------------------------------------------------------------------------


def test_post_text_message_is_dispatched(wechat_client: TestClient) -> None:
    ts, nonce = "1700000000", "abc123"
    xml = (
        "<xml>"
        "<ToUserName><![CDATA[gh_official]]></ToUserName>"
        "<FromUserName><![CDATA[openid_user]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[hello]]></Content>"
        "<MsgId>1234567890</MsgId>"
        "</xml>"
    )
    resp = wechat_client.post(
        f"/wechat/{_BOT_NAME}",
        params={
            "signature": _signature(ts, nonce),
            "timestamp": ts,
            "nonce": nonce,
        },
        content=xml.encode(),
        headers={"content-type": "text/xml"},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# unknown bot → 404 text/plain (pure route logic)
# ---------------------------------------------------------------------------


def test_unknown_bot_get_returns_404_text_plain(
    wechat_client: TestClient,
) -> None:
    ts, nonce = "1700000000", "abc123"
    resp = wechat_client.get(
        "/wechat/no-such-bot",
        params={
            "signature": _signature(ts, nonce),
            "timestamp": ts,
            "nonce": nonce,
            "echostr": "x",
        },
    )
    assert resp.status_code == 404, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert "no-such-bot" in resp.text


def test_unknown_bot_post_returns_404_text_plain(
    wechat_client: TestClient,
) -> None:
    resp = wechat_client.post(
        "/wechat/no-such-bot",
        content=b"<xml></xml>",
        headers={"content-type": "text/xml"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert "no-such-bot" in resp.text


def test_registry_is_clean_after_fixture_teardown() -> None:
    """Defence-in-depth: the process-global registry must not leak the
    fixture's bot into sibling tests."""
    assert _BOT_NAME not in wechat_webhook.WECHAT_BOT_REGISTRY
