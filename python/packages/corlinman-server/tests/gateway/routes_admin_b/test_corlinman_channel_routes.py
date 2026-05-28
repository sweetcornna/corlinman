"""Tests for :mod:`corlinman_server.gateway.routes_admin_b.corlinman_channel`.

Wave 3 of ``docs/PLAN_IN_APP_CHAT.md``. Covers:

* ``POST /api/channels/corlinman/send`` happy path + 503 (no channel wired) +
  400 (bad attachment kind / bad base64).
* ``GET  /api/channels/corlinman/events`` SSE: handshake comment + one
  ``message`` frame published mid-stream.
* ``POST /api/channels/corlinman/typing`` 204.
* Wave 4 stubs (edit / delete / react) all return typed 503 with
  ``error: <op>_not_supported``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator

import pytest
from corlinman_channels.corlinman import CorlinmanChannel
from corlinman_server.gateway.routes_admin_b import corlinman_channel as web_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin_state() -> Iterator[AdminState]:
    state = AdminState()
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(admin_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(web_routes.router())
    return authenticated_test_client(app)


@pytest.fixture()
def wired_state(admin_state: AdminState) -> AdminState:
    """Variant that attaches a live :class:`CorlinmanChannel` to the admin state."""
    admin_state.corlinman_channel = CorlinmanChannel()
    return admin_state


@pytest.fixture()
def wired_client(wired_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(web_routes.router())
    return authenticated_test_client(app)


# ---------------------------------------------------------------------------
# Disabled-503 paths
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_send_503_when_channel_not_wired(self, client: TestClient) -> None:
        resp = client.post(
            "/api/channels/corlinman/send",
            json={"session_key": "s1", "text": "hi"},
        )
        assert resp.status_code == 503
        body = resp.json()
        assert body["detail"]["error"] == "corlinman_channel_disabled"

    def test_events_503_when_channel_not_wired(self, client: TestClient) -> None:
        resp = client.get("/api/channels/corlinman/events?session_key=s1")
        assert resp.status_code == 503
        body = resp.json()
        assert body["detail"]["error"] == "corlinman_channel_disabled"

    def test_typing_503_when_channel_not_wired(self, client: TestClient) -> None:
        resp = client.post(
            "/api/channels/corlinman/typing",
            json={"session_key": "s1", "typing": True},
        )
        assert resp.status_code == 503
        body = resp.json()
        assert body["detail"]["error"] == "corlinman_channel_disabled"


# ---------------------------------------------------------------------------
# Send happy path + validation
# ---------------------------------------------------------------------------


class TestSend:
    def test_send_happy_path(
        self, wired_client: TestClient, wired_state: AdminState
    ) -> None:
        resp = wired_client.post(
            "/api/channels/corlinman/send",
            json={
                "session_key": "abc-123",
                "text": "hello backend",
                "user_id": "admin",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["session_key"] == "abc-123"
        assert body["message_id"].startswith("corlinman-")
        assert body["accepted_at"] > 0

    def test_send_with_attachment(
        self, wired_client: TestClient
    ) -> None:
        raw = b"fake-bytes"
        resp = wired_client.post(
            "/api/channels/corlinman/send",
            json={
                "session_key": "abc-123",
                "text": "see file",
                "attachments": [
                    {
                        "kind": "document",
                        "data_b64": base64.b64encode(raw).decode("ascii"),
                        "mime": "application/pdf",
                        "file_name": "doc.pdf",
                    }
                ],
            },
        )
        assert resp.status_code == 200, resp.text

    def test_send_rejects_unknown_attachment_kind(
        self, wired_client: TestClient
    ) -> None:
        resp = wired_client.post(
            "/api/channels/corlinman/send",
            json={
                "session_key": "s1",
                "text": "x",
                "attachments": [
                    {"kind": "hologram", "url": "https://x/y"}
                ],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_attachment_kind"

    def test_send_rejects_attachment_without_payload(
        self, wired_client: TestClient
    ) -> None:
        resp = wired_client.post(
            "/api/channels/corlinman/send",
            json={
                "session_key": "s1",
                "text": "x",
                "attachments": [{"kind": "image"}],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "attachment_missing_payload"

    def test_send_rejects_bad_base64(
        self, wired_client: TestClient
    ) -> None:
        resp = wired_client.post(
            "/api/channels/corlinman/send",
            json={
                "session_key": "s1",
                "text": "x",
                "attachments": [{"kind": "image", "data_b64": "%%%not-base64%%%"}],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_attachment_data_b64"


# ---------------------------------------------------------------------------
# Typing
# ---------------------------------------------------------------------------


class TestTyping:
    def test_typing_204(self, wired_client: TestClient) -> None:
        resp = wired_client.post(
            "/api/channels/corlinman/typing",
            json={"session_key": "s1", "typing": True},
        )
        assert resp.status_code == 204
        assert resp.content == b""


# ---------------------------------------------------------------------------
# SSE events stream
# ---------------------------------------------------------------------------


class TestEvents:
    def test_events_streams_handshake_and_message(
        self, wired_client: TestClient, wired_state: AdminState
    ) -> None:
        """End-to-end: open SSE, then have a producer push a frame.

        We pre-seed BOTH a real ``message`` frame AND a sentinel
        ``done`` frame on the queue so the iterator terminates on its
        own — the channel's :meth:`CorlinmanChannel.subscribe` exits cleanly
        on the ``done`` event, which lets :class:`TestClient.stream`'s
        ``__exit__`` return without blocking on a server still
        producing.
        """
        channel: CorlinmanChannel = wired_state.corlinman_channel  # type: ignore[assignment]

        # Pre-seed: send the real frame, then push a sentinel ``done``
        # via the channel's private queue (the public ``send`` API
        # always uses event="message"; we reach into the bucket here
        # purely so the iterator has a deterministic exit signal).
        from corlinman_channels.corlinman import CorlinmanOutboundFrame  # noqa: PLC0415

        async def _seed() -> str:
            mid = await channel.send("evt-sess", "first delta")
            state = channel._outbound["evt-sess"]  # type: ignore[attr-defined]
            await state.queue.put(CorlinmanOutboundFrame(event="done", data="{}"))
            return mid

        mid = asyncio.run(_seed())
        assert mid.startswith("corlinman-")

        with wired_client.stream(
            "GET", "/api/channels/corlinman/events?session_key=evt-sess"
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            chunks = bytearray()
            for chunk in resp.iter_bytes(chunk_size=512):
                chunks.extend(chunk)

        text = chunks.decode("utf-8", errors="replace")
        assert ": connected" in text
        assert "event: message" in text
        assert "event: done" in text
        # Find the message data line and confirm the body shape.
        for line in text.splitlines():
            if line.startswith("data:") and "first delta" in line:
                body = json.loads(line[len("data:"):].strip())
                assert body["text"] == "first delta"
                assert body["message_id"] == mid
                break
        else:
            pytest.fail(f"no message data line found in stream: {text!r}")


# ---------------------------------------------------------------------------
# Wave 4 stubs — all three should 503 with typed error codes
# ---------------------------------------------------------------------------


class TestWave4Stubs:
    def test_edit_returns_503_not_supported(
        self, wired_client: TestClient
    ) -> None:
        resp = wired_client.post(
            "/api/channels/corlinman/edit/web-abc",
            json={"session_key": "s1", "text": "edited"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "edit_not_supported"

    def test_delete_returns_503_not_supported(
        self, wired_client: TestClient
    ) -> None:
        resp = wired_client.delete(
            "/api/channels/corlinman/delete/web-abc?session_key=s1"
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "delete_not_supported"

    def test_react_returns_503_not_supported(
        self, wired_client: TestClient
    ) -> None:
        resp = wired_client.post(
            "/api/channels/corlinman/react/web-abc",
            json={"session_key": "s1", "emoji": ":+1:"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "react_not_supported"
