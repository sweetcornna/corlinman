"""SSE wire-contract coverage for ``POST /v1/chat/completions`` (W1a).

Three regressions guarded here, all found by the 2026-06-11 chat audit
(``docs/PLAN_CHAT_PERFECT.md`` §2-§3):

1. **Heartbeat** — while a slow tool blocks the event stream the route
   must emit SSE comment frames (``: ping``) so idle-timeout proxies
   don't kill the connection mid-turn. Pre-W1a the stream went silent
   for the full tool duration (image generation: 120s+) and the browser
   surfaced a bare "network error".
2. **Error chunk shape** — a mid-stream :class:`ErrorEvent` must render
   as a *valid* OpenAI chunk (``choices[0].finish_reason == "error"``
   plus the legacy top-level ``error`` payload). The old bare
   ``{"error": {...}}`` frame had no ``choices`` so stream reducers
   folded zero events and the turn hung in "loading" forever.
3. **Stream exception containment** — if ``ChatService.run`` itself
   raises, the route must still emit a terminal error chunk and
   ``[DONE]`` rather than dying half-open.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_server.gateway.routes import chat as chat_route
from corlinman_server.gateway.routes.chat import (
    ChatState,
    ModelRedirect,
    router,
)
from corlinman_server.gateway_api import (
    DoneEvent,
    ErrorEvent,
    InternalChatRequest,
    TokenDeltaEvent,
)
from corlinman_server.gateway_api.types import InternalChatError
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app(service: Any) -> FastAPI:
    app = FastAPI()
    state = ChatState(service=service, model_redirect=ModelRedirect())
    app.include_router(router(state))
    return app


def _stream_body(app: FastAPI) -> str:
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    return resp.text


class _SilentThenDoneService:
    """Yields one token, goes silent (tool running), then finishes."""

    def __init__(self, silence_s: float) -> None:
        self._silence_s = silence_s

    def run(
        self, req: InternalChatRequest, cancel: asyncio.Event
    ) -> AsyncIterator[Any]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[Any]:
        yield TokenDeltaEvent(text="before ")
        await asyncio.sleep(self._silence_s)
        yield TokenDeltaEvent(text="after")
        yield DoneEvent(finish_reason="stop", usage=None)


class _ErrorEventService:
    """Yields a token then a mid-stream :class:`ErrorEvent`."""

    def run(
        self, req: InternalChatRequest, cancel: asyncio.Event
    ) -> AsyncIterator[Any]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[Any]:
        yield TokenDeltaEvent(text="partial ")
        yield ErrorEvent(
            error=InternalChatError(
                reason="provider_timeout", message="upstream timed out"
            )
        )


class _RaisingService:
    """The stream itself blows up after one token."""

    def run(
        self, req: InternalChatRequest, cancel: asyncio.Event
    ) -> AsyncIterator[Any]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[Any]:
        yield TokenDeltaEvent(text="partial ")
        raise RuntimeError("boom mid-stream")


def test_heartbeat_emitted_during_stream_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence longer than the heartbeat interval ⇒ ``: ping`` frames."""
    monkeypatch.setattr(chat_route, "_SSE_HEARTBEAT_SECS", 0.05)
    body = _stream_body(_make_app(_SilentThenDoneService(silence_s=0.25)))

    assert ": ping" in body, body
    # Multiple heartbeats over a 0.25s silence at 0.05s cadence.
    assert body.count(": ping") >= 2, body
    # The stream still completes normally after the silence.
    assert '"finish_reason": "stop"' in body
    assert "data: [DONE]" in body


def test_no_heartbeat_on_fast_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream with no silence must not interleave comment frames."""
    monkeypatch.setattr(chat_route, "_SSE_HEARTBEAT_SECS", 5.0)
    body = _stream_body(_make_app(_SilentThenDoneService(silence_s=0.0)))

    assert ": ping" not in body
    assert "data: [DONE]" in body


def test_error_event_renders_as_valid_chunk() -> None:
    """Mid-stream errors carry ``choices[0].finish_reason == "error"``."""
    body = _stream_body(_make_app(_ErrorEventService()))

    assert '"finish_reason": "error"' in body, body
    # Legacy payload preserved for API consumers.
    assert '"code": "upstream_error"' in body
    assert '"reason": "provider_timeout"' in body
    assert '"message": "upstream timed out"' in body
    # Every error frame is still a structurally valid chunk.
    assert '"object": "chat.completion.chunk"' in body
    assert "data: [DONE]" in body


def test_stream_exception_contained_as_error_chunk() -> None:
    """``ChatService.run`` raising ⇒ terminal error chunk, not a dead pipe."""
    body = _stream_body(_make_app(_RaisingService()))

    assert '"finish_reason": "error"' in body, body
    assert '"code": "internal_error"' in body
    assert "boom mid-stream" in body
    assert "data: [DONE]" in body


def test_oversized_messages_rejected_with_400() -> None:
    """Total content above the cap fails fast at validation time."""
    app = _make_app(_SilentThenDoneService(silence_s=0.0))
    client = TestClient(app)
    huge = "x" * (chat_route._MAX_TOTAL_CONTENT_CHARS + 1)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": huge}],
            "stream": False,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"
