"""Reasoning tokens must never render as visible reply content.

The provider plane tags chain-of-thought deltas with
``TokenDeltaEvent.is_reasoning=True`` end-to-end, but the HTTP rendering
layer used to ignore the flag: the streaming branch emitted reasoning as
``delta.content`` and the non-streaming branch concatenated it into
``message.content`` — the web chat then displayed the model's thinking
as the reply (user-reported: reasoning-summary headers smashed into the
visible text). Contract now:

* streaming  — reasoning rides ``delta.reasoning_content`` (the
  DeepSeek/vLLM OpenAI extension the web chat already folds into its
  collapsible thinking block); ``delta.content`` carries ONLY reply text.
* non-stream — ``message.content`` is reply-only; reasoning (when any)
  is surfaced as ``message.reasoning_content``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from corlinman_server.gateway.routes.chat import ChatState, router
from corlinman_server.gateway_api import (
    DoneEvent,
    InternalChatRequest,
    TokenDeltaEvent,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _ScriptedService:
    """Yields a fixed reasoning + reply token script, then Done."""

    async def run(
        self,
        req: InternalChatRequest,
        cancel: asyncio.Event,
    ) -> AsyncIterator[object]:
        yield TokenDeltaEvent(text="thinking hard… ", is_reasoning=True)
        yield TokenDeltaEvent(text="more thoughts. ", is_reasoning=True)
        yield TokenDeltaEvent(text="hello ")
        yield TokenDeltaEvent(text="world")
        yield DoneEvent(finish_reason="stop", usage=None)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router(ChatState(service=_ScriptedService())))
    return TestClient(app)


def _sse_chunks(body: str) -> list[dict]:
    out: list[dict] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        out.append(json.loads(data))
    return out


@pytest.mark.parametrize("stream", [True, False])
def test_reasoning_never_lands_in_content(stream: bool) -> None:
    client = _client()
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "m1",
            "stream": stream,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200

    if stream:
        chunks = _sse_chunks(resp.text)
        content = "".join(
            c["choices"][0]["delta"].get("content") or ""
            for c in chunks
            if c.get("choices")
        )
        reasoning = "".join(
            c["choices"][0]["delta"].get("reasoning_content") or ""
            for c in chunks
            if c.get("choices")
        )
        assert content == "hello world"
        assert reasoning == "thinking hard… more thoughts. "
        # A delta carries one or the other, never both.
        for c in chunks:
            for choice in c.get("choices", []):
                delta = choice.get("delta", {})
                assert not (
                    delta.get("content") and delta.get("reasoning_content")
                )
    else:
        msg = resp.json()["choices"][0]["message"]
        assert msg["content"] == "hello world"
        assert msg["reasoning_content"] == "thinking hard… more thoughts. "


def test_nonstream_omits_reasoning_key_when_none() -> None:
    class _PlainService:
        async def run(
            self, req: InternalChatRequest, cancel: asyncio.Event
        ) -> AsyncIterator[object]:
            yield TokenDeltaEvent(text="plain")
            yield DoneEvent(finish_reason="stop", usage=None)

    app = FastAPI()
    app.include_router(router(ChatState(service=_PlainService())))
    resp = TestClient(app).post(
        "/v1/chat/completions",
        json={
            "model": "m1",
            "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    msg = resp.json()["choices"][0]["message"]
    assert msg["content"] == "plain"
    assert "reasoning_content" not in msg
