"""Integration tests for the uniform admin channel surface.

Covers the five channels that previously had zero admin endpoints:

* Discord / Slack / Feishu — status + messages + send (traffic-bearing).
* WeChat-Official / QQ-Official — status only (config-only).

Mirrors the FastAPI TestClient pattern in ``test_admin_channels_telegram.py``.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_channels.service import (
    DISCORD_HEALTH,
    DISCORD_RECENT_MESSAGES,
    FEISHU_HEALTH,
    FEISHU_RECENT_MESSAGES,
    SLACK_HEALTH,
    SLACK_RECENT_MESSAGES,
    _channel_record_inbound,
    _channel_record_sent,
    _channel_reset_state_for_tests,
)
from corlinman_server.gateway.routes_admin_a import (
    AdminState,
    build_router,
    set_admin_state,
)
from corlinman_server.gateway.routes_admin_a._session_store import (
    AdminSessionStore,
)
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient

_HEALTH = {
    "discord": (DISCORD_HEALTH, DISCORD_RECENT_MESSAGES),
    "slack": (SLACK_HEALTH, SLACK_RECENT_MESSAGES),
    "feishu": (FEISHU_HEALTH, FEISHU_RECENT_MESSAGES),
}


def _basic_auth_header() -> str:
    token = base64.b64encode(b"admin:rootroot").decode("ascii")
    return f"Basic {token}"


class _StubSender:
    """Captures send_message(target_id, text) calls; returns a string id
    mirroring the real Discord / Slack / Feishu senders."""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.raise_exc = raise_exc
        self._next = 1000

    async def send_message(self, target_id: str, text: str) -> str:
        if self.raise_exc is not None:
            raise self.raise_exc
        self._next += 1
        self.sent.append((target_id, text))
        return f"msg-{self._next}"


def _reset_all() -> None:
    for health, recent in _HEALTH.values():
        _channel_reset_state_for_tests(health, recent)


@pytest.fixture()
def base_state(tmp_path: Path) -> Iterator[AdminState]:
    _reset_all()
    channels_config: dict[str, Any] = {
        "discord": {
            "enabled": True,
            "bot_token": "secret-discord-token",
            "allowed_channel_ids": ["111", "222"],
            "keyword_filter": ["hey bot"],
            "respond_to_all": False,
        },
        "slack": {
            "enabled": True,
            "app_token": "xapp-secret",
            "bot_token": "xoxb-secret",
            "allowed_channel_ids": ["C123"],
            "respond_to_all": True,
        },
        "feishu": {
            "enabled": True,
            "app_id": "cli_app_id_public",
            "app_secret": "secret-feishu",
            "allowed_chat_ids": ["oc_1"],
        },
        "qq_official": {
            "enabled": True,
            "app_id": "qq_official_app_id",
            "app_secret": "secret",
            "sandbox": True,
            "intents": ["GUILD_MESSAGES"],
        },
        "wechat_official": {
            "enabled": False,
            "app_id": "wx_app_id",
            "app_secret": "secret",
            "token": "verify-token-secret",
        },
    }
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config=channels_config,
    )
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)
        _reset_all()


@pytest.fixture()
def client(base_state: AdminState) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
        yield c


# ---------------------------------------------------------------------------
# Status — disabled / not configured
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["discord", "slack", "feishu"])
def test_traffic_status_not_configured(
    client: TestClient, base_state: AdminState, channel: str
) -> None:
    """No config section at all → configured=False, zeroed counters."""
    base_state.channels_config.pop(channel)
    resp = client.get(f"/admin/channels/{channel}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is False
    assert body["enabled"] is False
    assert body["online"] is False
    assert body["received"] == 0
    assert body["sent"] == 0
    assert body["errors"] == 0
    assert body["config_keys"] == {}


@pytest.mark.parametrize("channel", ["discord", "slack", "feishu"])
def test_traffic_status_disabled_section(
    client: TestClient, base_state: AdminState, channel: str
) -> None:
    """Section present but enabled=False → configured=True, enabled=False."""
    base_state.channels_config[channel]["enabled"] = False
    resp = client.get(f"/admin/channels/{channel}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True
    assert body["enabled"] is False


# ---------------------------------------------------------------------------
# Status — configured, full shape + non-secret config_keys
# ---------------------------------------------------------------------------


def test_discord_status_full_shape(client: TestClient) -> None:
    resp = client.get("/admin/channels/discord/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Full envelope keys present.
    for key in (
        "configured",
        "enabled",
        "online",
        "last_event_at_ms",
        "received",
        "sent",
        "errors",
        "error_message",
        "config_keys",
    ):
        assert key in body, key
    assert body["configured"] is True
    assert body["enabled"] is True
    ck = body["config_keys"]
    # NON-SECRET keys exposed.
    assert ck["allowed_channel_ids"] == ["111", "222"]
    assert ck["keyword_filter"] == ["hey bot"]
    assert ck["respond_to_all"] == "False"
    # Secret never leaks.
    assert "bot_token" not in ck


def test_feishu_status_exposes_app_id_not_secret(client: TestClient) -> None:
    resp = client.get("/admin/channels/feishu/status")
    assert resp.status_code == 200, resp.text
    ck = resp.json()["config_keys"]
    assert ck["app_id"] == "cli_app_id_public"
    assert ck["allowed_chat_ids"] == ["oc_1"]
    assert "app_secret" not in ck


def test_status_surfaces_live_counters(client: TestClient) -> None:
    """Drive the recorder helpers so the route reflects real traffic."""
    from corlinman_channels.common import ChannelBinding, InboundEvent

    binding = ChannelBinding(
        channel="discord", account="bot", thread="111", sender="user1"
    )
    inbound: InboundEvent[Any] = InboundEvent(
        channel="discord",
        binding=binding,
        text="hi there",
        message_id="m1",
        mentioned=True,
    )
    _channel_record_inbound(DISCORD_HEALTH, DISCORD_RECENT_MESSAGES, inbound)
    _channel_record_sent(DISCORD_HEALTH)

    resp = client.get("/admin/channels/discord/status")
    body = resp.json()
    assert body["received"] == 1
    assert body["sent"] == 1
    assert body["errors"] == 0
    assert body["online"] is True
    assert isinstance(body["last_event_at_ms"], int)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def test_messages_empty(client: TestClient) -> None:
    resp = client.get("/admin/channels/slack/messages")
    assert resp.status_code == 200
    assert resp.json() == {"messages": []}


def test_messages_returns_recent(client: TestClient) -> None:
    from corlinman_channels.common import ChannelBinding, InboundEvent

    for i in range(3):
        binding = ChannelBinding(
            channel="slack", account="bot", thread=f"C{i}", sender=f"u{i}"
        )
        inbound: InboundEvent[Any] = InboundEvent(
            channel="slack",
            binding=binding,
            text=f"msg {i}",
            message_id=str(i),
            mentioned=False,
        )
        _channel_record_inbound(
            SLACK_HEALTH, SLACK_RECENT_MESSAGES, inbound, now_ms=1_700_000_000_000 + i
        )
    resp = client.get("/admin/channels/slack/messages?limit=2")
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert len(msgs) == 2
    # Newest first.
    assert [m["id"] for m in msgs] == ["2", "1"]


def test_messages_rejects_out_of_range_limit(client: TestClient) -> None:
    assert client.get("/admin/channels/discord/messages?limit=0").status_code == 422
    assert client.get("/admin/channels/discord/messages?limit=999").status_code == 422


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "channel,attr",
    [
        ("discord", "discord_sender"),
        ("slack", "slack_sender"),
        ("feishu", "feishu_sender"),
    ],
)
def test_send_503_when_no_sender(
    client: TestClient, base_state: AdminState, channel: str, attr: str
) -> None:
    setattr(base_state, attr, None)
    resp = client.post(
        f"/admin/channels/{channel}/send",
        json={"target_id": "123", "text": "hi"},
    )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["error"] == f"{channel}_disabled"


@pytest.mark.parametrize(
    "channel,attr,field",
    [
        ("discord", "discord_sender", "channel_id"),
        ("slack", "slack_sender", "channel_id"),
        ("feishu", "feishu_sender", "chat_id"),
    ],
)
def test_send_routes_through_live_sender(
    client: TestClient,
    base_state: AdminState,
    channel: str,
    attr: str,
    field: str,
) -> None:
    sender = _StubSender()
    setattr(base_state, attr, sender)
    resp = client.post(
        f"/admin/channels/{channel}/send",
        json={field: "T1", "text": "hello"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["message_id"].startswith("msg-")
    assert sender.sent == [("T1", "hello")]


def test_send_503_when_section_disabled(
    client: TestClient, base_state: AdminState
) -> None:
    base_state.channels_config["discord"]["enabled"] = False
    base_state.discord_sender = _StubSender()
    resp = client.post(
        "/admin/channels/discord/send",
        json={"target_id": "T1", "text": "hi"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "discord_disabled"


def test_send_missing_target_is_400(
    client: TestClient, base_state: AdminState
) -> None:
    base_state.discord_sender = _StubSender()
    resp = client.post(
        "/admin/channels/discord/send",
        json={"text": "hi"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "missing_target"


def test_send_transport_error_is_502(
    client: TestClient, base_state: AdminState
) -> None:
    base_state.discord_sender = _StubSender(raise_exc=RuntimeError("boom"))
    resp = client.post(
        "/admin/channels/discord/send",
        json={"target_id": "T1", "text": "hi"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "send_failed"


# ---------------------------------------------------------------------------
# WeChat-Official / QQ-Official — config-only status
# ---------------------------------------------------------------------------


def test_qq_official_status_config_only(client: TestClient) -> None:
    resp = client.get("/admin/channels/qq_official/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True
    assert body["enabled"] is True
    assert body["online"] is False
    ck = body["config_keys"]
    assert ck["app_id"] == "qq_official_app_id"
    assert ck["sandbox"] == "True"
    assert ck["intents"] == ["GUILD_MESSAGES"]
    assert "app_secret" not in ck
    # Config-only channels expose no traffic counters / send route.
    assert "received" not in body


def test_wechat_official_status_disabled(client: TestClient) -> None:
    resp = client.get("/admin/channels/wechat_official/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True
    assert body["enabled"] is False
    ck = body["config_keys"]
    assert ck["app_id"] == "wx_app_id"
    # Verify token + secret never leak.
    assert "token" not in ck
    assert "app_secret" not in ck


def test_wechat_official_status_not_configured(
    client: TestClient, base_state: AdminState
) -> None:
    base_state.channels_config.pop("wechat_official")
    resp = client.get("/admin/channels/wechat_official/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["enabled"] is False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_status_requires_auth(base_state: AdminState) -> None:
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app) as anonymous:
        resp = anonymous.get("/admin/channels/discord/status")
    assert resp.status_code == 401
