"""Sentinel-prefix handling in :mod:`gateway.services.chat_service`.

Verifies that an observation-only ``_builtin:`` tool_call frame is
surfaced as :class:`ToolCallEvent` with the prefix stripped and the
injected executor is NOT invoked (which would otherwise double-feed the
call_id the agent already resolved in-process).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_grpc._generated.corlinman.v1 import agent_pb2
from corlinman_server.gateway.services.chat_service import _build_chat_start, _run_chat
from corlinman_server.gateway_api import (
    DoneEvent,
    InternalChatRequest,
    ToolCallEvent,
)


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


@pytest.mark.asyncio
async def test_builtin_prefix_strips_and_skips_executor() -> None:
    backend = _ScriptedBackend(
        [
            agent_pb2.ServerFrame(
                tool_call=agent_pb2.ToolCall(
                    call_id="c1",
                    plugin="_builtin:web_search",
                    tool="web_search",
                    args_json=b"{}",
                    seq=1,
                )
            ),
            agent_pb2.ServerFrame(done=agent_pb2.Done(finish_reason="stop")),
        ]
    )
    executor = _RecordingExecutor()
    req = InternalChatRequest(model="x", messages=[])
    events = [ev async for ev in _run_chat(backend, executor, req, asyncio.Event())]

    tool_events = [ev for ev in events if isinstance(ev, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].plugin == "web_search"
    assert tool_events[0].tool == "web_search"
    assert executor.calls == []
    assert any(isinstance(ev, DoneEvent) for ev in events)


def test_build_chat_start_carries_persona_id_metadata() -> None:
    req = InternalChatRequest(model="x", messages=[], persona_id="grantley")

    start = _build_chat_start(req)

    assert start.persona_id == "grantley"
