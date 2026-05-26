"""Integration tests for ``/admin/channels/telegram*`` routes.

Pins the W4-FE F2 admin surface: status snapshot, recent messages, and
the admin-only test-send action. Uses the same FastAPI TestClient pattern
as ``test_admin_persona_assets.py``.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_channels.service import (
    TELEGRAM_HEALTH,
    TELEGRAM_RECENT_MESSAGES,
    _telegram_reset_state_for_tests,
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


def _basic_auth_header() -> str:
    token = base64.b64encode(b"admin:rootroot").decode("ascii")
    return f"Basic {token}"


class _StubTelegramSender:
    """Captures send_message calls so we can pin the admin send route
    without poking a real Telegram bot."""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.raise_exc = raise_exc
        self._next_id = 100

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        inline_keyboard: Any = None,
    ) -> int:
        if self.raise_exc is not None:
            raise self.raise_exc
        self._next_id += 1
        self.sent.append((chat_id, text))
        return self._next_id


@pytest.fixture()
def base_state(tmp_path: Path) -> Iterator[AdminState]:
    _telegram_reset_state_for_tests()
    channels_config: dict[str, Any] = {
        "telegram": {
            "enabled": True,
            "bot_token": "12345:ABCDEF",
            "webhook_url": "https://example.com/tg/webhook",
            "secret_token": "shhh",
            "drop_pending_updates": True,
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
        _telegram_reset_state_for_tests()


@pytest.fixture()
def client(base_state: AdminState) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /admin/channels/telegram/status
# ---------------------------------------------------------------------------


def test_status_empty_returns_zeroed_stats(client: TestClient) -> None:
    """Fresh boot — no events recorded yet — must return a zeroed
    envelope rather than the legacy mock."""
    resp = client.get("/admin/channels/telegram/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["config"]["bot_token"] == "12345:ABCDEF"
    assert body["config"]["webhook_url"] == "https://example.com/tg/webhook"
    assert body["config"]["secret_token"] == "shhh"
    assert body["config"]["drop_pending_updates"] is True
    stats = body["stats"]
    assert stats["messages_today"] == 0
    assert stats["messages_week"] == 0
    assert stats["latency_p50_ms"] == 0
    assert stats["latency_p95_ms"] == 0
    assert stats["active_chats"] == 0
    # No events seen yet → not connected, runtime=disconnected.
    assert body["connected"] is False
    assert body["runtime"] == "disconnected"


def test_status_surfaces_live_health_counters(client: TestClient) -> None:
    """Drive the public recorder helpers so the route's recompute pass
    finds real samples in the underlying buffers (rather than a
    pre-populated TELEGRAM_HEALTH dict, which the recompute resets)."""
    import time

    from corlinman_channels.common import ChannelBinding, InboundEvent
    from corlinman_channels.service import (
        telegram_record_inbound,
        telegram_record_reply_sent,
    )

    now_ms = int(time.time() * 1000)
    # Three turns from three distinct chats with different latencies.
    deltas_ms = [120, 300, 800]
    for i, d in enumerate(deltas_ms):
        binding = ChannelBinding.telegram(bot_id=999, chat_id=i + 1)
        inbound: InboundEvent[Any] = InboundEvent(
            channel="telegram",
            binding=binding,
            text="hi",
            message_id=str(i),
            timestamp=0,
            mentioned=True,
        )
        telegram_record_inbound(inbound, now_ms=now_ms + i)
        telegram_record_reply_sent(
            inbound, inbound_ts_ms=now_ms + i, now_ms=now_ms + i + d
        )

    resp = client.get("/admin/channels/telegram/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    stats = body["stats"]
    assert stats["messages_today"] == 3
    assert stats["messages_week"] == 3
    assert stats["active_chats"] == 3
    assert stats["latency_p50_ms"] == 300
    assert stats["latency_p95_ms"] == 800
    # The route's recompute pass marks the channel "online" because the
    # last event was just now.
    assert body["connected"] is True
    assert body["runtime"] == "connected"


def test_status_disconnected_when_section_disabled(client: TestClient, base_state: AdminState) -> None:
    base_state.channels_config["telegram"]["enabled"] = False
    resp = client.get("/admin/channels/telegram/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is False
    assert body["runtime"] == "disconnected"


def test_status_requires_auth(base_state: AdminState) -> None:
    app = FastAPI()
    app.include_router(build_router())
    # No Authorization header → 401.
    with TestClient(app) as anonymous:
        resp = anonymous.get("/admin/channels/telegram/status")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /admin/channels/telegram/messages
# ---------------------------------------------------------------------------


def test_messages_empty_buffer(client: TestClient) -> None:
    resp = client.get("/admin/channels/telegram/messages")
    assert resp.status_code == 200
    assert resp.json() == []


def test_messages_returns_recent_buffer_sorted_desc(client: TestClient) -> None:
    # Seed the ring buffer directly with three entries spaced in time.
    TELEGRAM_RECENT_MESSAGES.extend(
        [
            {
                "id": "10",
                "kind": "private",
                "chat_id": "1",
                "chat_title": None,
                "from_username": "alice",
                "content": "first",
                "timestamp_ms": 1_700_000_001_000,
                "routing": "responded",
                "mention_reason": "dm",
            },
            {
                "id": "20",
                "kind": "group",
                "chat_id": "100",
                "chat_title": "Bot Lab",
                "from_username": "bob",
                "content": "second",
                "timestamp_ms": 1_700_000_002_000,
                "routing": "queued",
                "mention_reason": "mention",
            },
            {
                "id": "30",
                "kind": "private",
                "chat_id": "2",
                "chat_title": None,
                "from_username": "carol",
                "content": "third",
                "timestamp_ms": 1_700_000_003_000,
                "routing": "responded",
                "mention_reason": "dm",
            },
        ]
    )
    resp = client.get("/admin/channels/telegram/messages")
    assert resp.status_code == 200
    body = resp.json()
    # Newest first.
    assert [m["id"] for m in body] == ["30", "20", "10"]


def test_messages_respects_limit(client: TestClient) -> None:
    for i in range(5):
        TELEGRAM_RECENT_MESSAGES.append(
            {
                "id": str(i),
                "kind": "private",
                "chat_id": str(i),
                "chat_title": None,
                "from_username": None,
                "content": "",
                "timestamp_ms": 1_700_000_000_000 + i,
                "routing": "queued",
                "mention_reason": "dm",
            }
        )
    resp = client.get("/admin/channels/telegram/messages?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert [m["id"] for m in body] == ["4", "3"]


def test_messages_rejects_out_of_range_limit(client: TestClient) -> None:
    resp = client.get("/admin/channels/telegram/messages?limit=0")
    assert resp.status_code == 422
    resp = client.get("/admin/channels/telegram/messages?limit=9999")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /admin/channels/telegram/send
# ---------------------------------------------------------------------------


def test_send_routes_through_live_sender(client: TestClient, base_state: AdminState) -> None:
    sender = _StubTelegramSender()
    base_state.telegram_sender = sender
    resp = client.post(
        "/admin/channels/telegram/send",
        json={"chat_id": "42", "text": "hello world"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["message_id"] == 101
    assert sender.sent == [(42, "hello world")]


def test_send_503_when_sender_not_wired(client: TestClient, base_state: AdminState) -> None:
    # Section enabled, but no sender ever landed — bootstrap hasn't
    # finished, or the channel task failed to start.
    base_state.telegram_sender = None
    resp = client.post(
        "/admin/channels/telegram/send",
        json={"chat_id": "42", "text": "hi"},
    )
    assert resp.status_code == 503
    body = resp.json()["detail"]
    assert body["error"] == "telegram_disabled"


def test_send_503_when_section_disabled(client: TestClient, base_state: AdminState) -> None:
    base_state.channels_config["telegram"]["enabled"] = False
    base_state.telegram_sender = _StubTelegramSender()
    resp = client.post(
        "/admin/channels/telegram/send",
        json={"chat_id": "42", "text": "hi"},
    )
    assert resp.status_code == 503
    body = resp.json()["detail"]
    assert body["error"] == "telegram_disabled"


def test_send_invalid_chat_id_is_400(client: TestClient, base_state: AdminState) -> None:
    base_state.telegram_sender = _StubTelegramSender()
    resp = client.post(
        "/admin/channels/telegram/send",
        json={"chat_id": "not-a-number", "text": "hi"},
    )
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["error"] == "invalid_chat_id"


def test_send_surfaces_transport_error_as_error_envelope(
    client: TestClient, base_state: AdminState
) -> None:
    base_state.telegram_sender = _StubTelegramSender(
        raise_exc=RuntimeError("boom")
    )
    resp = client.post(
        "/admin/channels/telegram/send",
        json={"chat_id": "42", "text": "hi"},
    )
    # The route returns 200 + status="error" so the UI can render a
    # toast without parsing HTTP errors.
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "boom" in (body["error"] or "")


def test_send_requires_auth(base_state: AdminState) -> None:
    base_state.telegram_sender = _StubTelegramSender()
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app) as anonymous:
        resp = anonymous.post(
            "/admin/channels/telegram/send",
            json={"chat_id": "42", "text": "hi"},
        )
    assert resp.status_code == 401
