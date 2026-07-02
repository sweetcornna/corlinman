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
    ErrorEvent,
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


def _channel_style_request() -> Any:
    """A lightweight ``SimpleNamespace`` shaped exactly like the request the
    channel adapters hand to ``chat_service.run`` — note the absence of a
    ``persona_id`` attribute, which is the default when humanlike persona
    injection is off (see ``corlinman_channels.service``)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        model="x",
        messages=[SimpleNamespace(role="user", content="hi")],
        session_key="tg:1:2",
        stream=True,
        max_tokens=None,
        temperature=None,
        attachments=[],
        binding=None,
    )


def test_build_chat_start_tolerates_request_without_persona_id() -> None:
    """Regression (commit 0622848): a channel ``SimpleNamespace`` request
    carries no ``persona_id`` attribute by default. The proto builder must
    read it tolerantly (-> "") rather than raising ``AttributeError``, which
    previously crashed every channel turn before any reply was sent."""
    start = _build_chat_start(_channel_style_request())

    assert start.persona_id == ""


def test_build_chat_start_defaults_to_empty_tools_json() -> None:
    start = _build_chat_start(InternalChatRequest(model="x", messages=[]))

    assert start.tools_json == b""


def test_build_chat_start_injects_advertised_tools_json() -> None:
    """Gateway-supplied MCP tool schemas ride into ChatStart.tools_json so the
    servicer advertises them to the model (L-003: discovered MCP tools)."""
    advertised = b'[{"type":"function","function":{"name":"echo"}}]'

    start = _build_chat_start(
        InternalChatRequest(model="x", messages=[]),
        advertised_tools_json=advertised,
    )

    assert start.tools_json == advertised


def test_advertised_tools_come_from_gateway_not_the_channel_request() -> None:
    """The duck-typed channel request is untouched: advertised MCP tools are
    threaded from gateway state, so a channel ``SimpleNamespace`` (no new field)
    still builds and gets the tools — the contract that once killed all channels
    stays safe."""
    advertised = b'[{"type":"function","function":{"name":"echo"}}]'

    start = _build_chat_start(
        _channel_style_request(), advertised_tools_json=advertised
    )

    assert start.tools_json == advertised
    assert start.persona_id == ""  # channel request needed no new attribute


@pytest.mark.asyncio
async def test_run_chat_with_channel_request_missing_persona_id_streams_to_done() -> None:
    """End-to-end guard: a channel-style request (no ``persona_id``) must
    stream to a terminal ``DoneEvent`` instead of escaping ``_run_chat`` as a
    raw ``AttributeError`` that the channel reply loop never converts into a
    user-visible reply or error."""
    backend = _ScriptedBackend(
        [agent_pb2.ServerFrame(done=agent_pb2.Done(finish_reason="stop"))]
    )
    executor = _RecordingExecutor()

    events = [
        ev
        async for ev in _run_chat(
            backend, executor, _channel_style_request(), asyncio.Event()
        )
    ]

    assert any(isinstance(ev, DoneEvent) for ev in events)
    assert not any(isinstance(ev, ErrorEvent) for ev in events)
