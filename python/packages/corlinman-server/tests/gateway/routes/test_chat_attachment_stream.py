"""``AttachmentEvent`` rendering in the ``/v1/chat/completions`` route.

Streaming: each event becomes an OpenAI-shaped chunk with an empty
``delta`` and the metadata under the ``corlinman.attachment`` vendor
extension (the frontend renders the file card from this mid-turn).
Non-streaming: events are collected into an ``attachments`` list on the
assistant message.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_server.gateway.routes.chat import _run_nonstream, _sse_iter
from corlinman_server.gateway_api import (
    DoneEvent,
    InternalChatRequest,
    TokenDeltaEvent,
)
from corlinman_server.gateway_api.types import AttachmentEvent

_ATTACHMENT = AttachmentEvent(
    kind="file",
    url="/v1/files/f-123",
    name="report.pdf",
    mime="application/pdf",
    call_id="c1",
)


class _ScriptedChatService:
    """Replays a fixed event list — enough surface for the SSE writer."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def run(
        self, req: InternalChatRequest, cancel: asyncio.Event
    ) -> AsyncIterator[Any]:
        events = self._events

        async def _gen() -> AsyncIterator[Any]:
            for ev in events:
                yield ev

        return _gen()


def _data_chunks(frames: list[bytes]) -> list[dict[str, Any]]:
    """Decode ``data: {...}`` SSE frames, dropping comments + [DONE]."""
    out: list[dict[str, Any]] = []
    for raw in frames:
        for line in raw.decode("utf-8").splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            out.append(json.loads(line[len("data: "):]))
    return out


@pytest.mark.asyncio
async def test_sse_renders_attachment_chunk() -> None:
    service = _ScriptedChatService(
        [
            TokenDeltaEvent(text="here you go "),
            _ATTACHMENT,
            DoneEvent(finish_reason="stop", usage=None),
        ]
    )
    req = InternalChatRequest(model="m", messages=[])

    frames = [b async for b in _sse_iter(service, req, "m", asyncio.Event())]
    chunks = _data_chunks(frames)

    att_chunks = [c for c in chunks if "corlinman" in c]
    assert len(att_chunks) == 1
    chunk = att_chunks[0]
    assert chunk["object"] == "chat.completion.chunk"
    assert chunk["choices"] == [
        {"index": 0, "delta": {}, "finish_reason": None}
    ]
    assert chunk["corlinman"]["attachment"] == {
        "kind": "file",
        "url": "/v1/files/f-123",
        "name": "report.pdf",
        "mime": "application/pdf",
    }
    # Terminal chunk still arrives after the attachment.
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_nonstream_collects_attachments_on_message() -> None:
    service = _ScriptedChatService(
        [
            TokenDeltaEvent(text="done"),
            _ATTACHMENT,
            DoneEvent(finish_reason="stop", usage=None),
        ]
    )
    req = InternalChatRequest(model="m", messages=[])

    resp = await _run_nonstream(service, req, "m", asyncio.Event())
    body = json.loads(resp.body)

    message = body["choices"][0]["message"]
    assert message["content"] == "done"
    assert message["attachments"] == [
        {
            "kind": "file",
            "url": "/v1/files/f-123",
            "name": "report.pdf",
            "mime": "application/pdf",
        }
    ]


@pytest.mark.asyncio
async def test_nonstream_omits_attachments_key_when_none() -> None:
    service = _ScriptedChatService(
        [TokenDeltaEvent(text="hi"), DoneEvent(finish_reason="stop", usage=None)]
    )
    req = InternalChatRequest(model="m", messages=[])

    resp = await _run_nonstream(service, req, "m", asyncio.Event())
    body = json.loads(resp.body)
    assert "attachments" not in body["choices"][0]["message"]
