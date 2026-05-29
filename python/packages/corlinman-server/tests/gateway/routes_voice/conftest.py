"""Shared fixtures + doubles for the ``/v1/voice`` route tests.

Houses the fake WebSocket doubles used by both the provider-level tests
and the route-integration tests so neither has to cross-import the
other (the ``tests`` tree isn't an importable package).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fake realtime (upstream OpenAI) WebSocket
# ---------------------------------------------------------------------------


class FakeRealtimeWebSocket:
    """In-memory stand-in for a ``websockets`` asyncio client connection.

    * :meth:`send` records every outbound JSON payload.
    * Iterating the connection yields whatever frames the test queued
      via :meth:`feed`; a ``None`` sentinel ends the iteration (mirrors
      a clean server-side close).
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, payload: str) -> None:
        if self.closed:
            raise ConnectionError("fake websocket closed")
        self.sent.append(payload)

    def feed(self, frame: dict[str, Any]) -> None:
        """Queue one server → client realtime frame."""
        self._incoming.put_nowait(json.dumps(frame))

    def end(self) -> None:
        """Queue the end-of-stream sentinel (clean server close)."""
        self._incoming.put_nowait(None)

    async def close(self) -> None:
        self.closed = True
        self._incoming.put_nowait(None)

    def __aiter__(self) -> FakeRealtimeWebSocket:
        return self

    async def __anext__(self) -> str:
        frame = await self._incoming.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    @property
    def sent_json(self) -> list[dict[str, Any]]:
        """Decode every recorded outbound payload."""
        return [json.loads(p) for p in self.sent]


def install_fake_connect(
    monkeypatch: pytest.MonkeyPatch, ws: FakeRealtimeWebSocket
) -> dict[str, Any]:
    """Patch the realtime adapter's ``connect`` indirection to return
    ``ws``. Returns a dict the caller can inspect for the captured
    connect URL + headers."""
    captured: dict[str, Any] = {}

    async def fake_connect(url: str, **kwargs: Any) -> FakeRealtimeWebSocket:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return ws

    monkeypatch.setattr(
        "corlinman_server.gateway.routes_voice.provider_openai._import_ws_connect",
        lambda: fake_connect,
    )
    return captured
