"""``_builtin_attachment:`` sentinel handling in
:mod:`gateway.services.chat_service`.

The agent servicer yields one of these frames per newly registered
media file; ``_run_chat`` must surface it as :class:`AttachmentEvent`
without invoking the executor (no round-trip — the call_id belongs to a
tool the servicer already resolved in-process). Malformed / url-less
payloads are dropped, never fatal.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_grpc._generated.corlinman.v1 import agent_pb2
from corlinman_server.gateway.services.chat_service import _run_chat
from corlinman_server.gateway_api import (
    DoneEvent,
    InternalChatRequest,
)
from corlinman_server.gateway_api.types import AttachmentEvent


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[agent_pb2.ToolCall] = []

    async def execute(self, call: agent_pb2.ToolCall) -> agent_pb2.ToolResult:
        self.calls.append(call)
        return agent_pb2.ToolResult(call_id=call.call_id, result_json=b"{}")


class _ScriptedBackend:
    """Replays a fixed list of ``ServerFrame`` then ends the stream."""

    def __init__(self, frames: list[agent_pb2.ServerFrame]) -> None:
        self._frames = frames

    async def start(
        self,
        start: agent_pb2.ChatStart,
    ) -> tuple[asyncio.Queue[Any], AsyncIterator[agent_pb2.ServerFrame]]:
        tx: asyncio.Queue[Any] = asyncio.Queue()
        frames = self._frames

        async def _gen() -> AsyncIterator[agent_pb2.ServerFrame]:
            for f in frames:
                yield f

        return tx, _gen()


def _attachment_frame(meta: dict[str, Any] | bytes) -> agent_pb2.ServerFrame:
    args = meta if isinstance(meta, bytes) else json.dumps(meta).encode("utf-8")
    return agent_pb2.ServerFrame(
        tool_call=agent_pb2.ToolCall(
            call_id="c1",
            plugin="_builtin_attachment:x",
            tool="send_attachment",
            args_json=args,
            seq=1,
        )
    )


_DONE = agent_pb2.ServerFrame(done=agent_pb2.Done(finish_reason="stop"))


@pytest.mark.asyncio
async def test_attachment_frame_maps_to_attachment_event() -> None:
    meta = {
        "kind": "file",
        "url": "/v1/files/f-123",
        "name": "report.pdf",
        "mime": "application/pdf",
    }
    backend = _ScriptedBackend([_attachment_frame(meta), _DONE])
    executor = _RecordingExecutor()
    req = InternalChatRequest(model="x", messages=[])

    events = [ev async for ev in _run_chat(backend, executor, req, asyncio.Event())]

    att = [ev for ev in events if isinstance(ev, AttachmentEvent)]
    assert len(att) == 1
    assert att[0].kind == "file"
    assert att[0].url == "/v1/files/f-123"
    assert att[0].name == "report.pdf"
    assert att[0].mime == "application/pdf"
    assert att[0].call_id == "c1"
    # No executor round-trip — the servicer already resolved the call.
    assert executor.calls == []
    assert any(isinstance(ev, DoneEvent) for ev in events)


@pytest.mark.asyncio
async def test_attachment_frame_defaults_kind_to_file() -> None:
    backend = _ScriptedBackend(
        [_attachment_frame({"url": "/v1/files/f-1", "name": "x", "mime": ""}), _DONE]
    )
    events = [
        ev
        async for ev in _run_chat(
            backend,
            _RecordingExecutor(),
            InternalChatRequest(model="x", messages=[]),
            asyncio.Event(),
        )
    ]
    att = [ev for ev in events if isinstance(ev, AttachmentEvent)]
    assert len(att) == 1
    assert att[0].kind == "file"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"not json",  # parse failure → skip
        b"[1, 2]",  # non-dict → skip
        json.dumps({"kind": "image", "name": "x", "mime": "image/png"}).encode(),
        json.dumps({"url": ""}).encode(),  # empty url → skip
    ],
)
async def test_bad_or_urlless_payload_is_dropped(payload: bytes) -> None:
    backend = _ScriptedBackend([_attachment_frame(payload), _DONE])
    executor = _RecordingExecutor()
    events = [
        ev
        async for ev in _run_chat(
            backend,
            executor,
            InternalChatRequest(model="x", messages=[]),
            asyncio.Event(),
        )
    ]
    assert not any(isinstance(ev, AttachmentEvent) for ev in events)
    assert executor.calls == []
    assert any(isinstance(ev, DoneEvent) for ev in events)
