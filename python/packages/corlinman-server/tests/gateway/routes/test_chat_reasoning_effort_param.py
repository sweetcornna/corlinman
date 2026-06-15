"""Per-turn reasoning effort on the OpenAI-compatible chat route."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from corlinman_server.gateway.routes.chat import ChatState, router
from corlinman_server.gateway_api import (
    DoneEvent,
    InternalChatRequest,
    TokenDeltaEvent,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _RecordingService:
    """Captures the InternalChatRequest handed to ChatService."""

    def __init__(self) -> None:
        self.seen: InternalChatRequest | None = None

    def run(
        self,
        req: InternalChatRequest,
        cancel: asyncio.Event,
    ) -> AsyncIterator[Any]:
        self.seen = req
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[Any]:
        yield TokenDeltaEvent(text="ok")
        yield DoneEvent(finish_reason="stop", usage=None)


def test_chat_request_reasoning_effort_becomes_provider_param() -> None:
    service = _RecordingService()
    app = FastAPI()
    app.include_router(router(ChatState(service=service)))

    resp = TestClient(app).post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high",
            "stream": False,
        },
    )

    assert resp.status_code == 200, resp.text
    assert service.seen is not None
    assert service.seen.provider_params == {"reasoning_effort": "high"}
