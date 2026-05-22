"""Round-trip test: real :class:`RegistryToolExecutor` drives the loop.

Proves that wiring the *real* tool executor (instead of the M2
``PlaceholderExecutor``) lets :class:`ReasoningLoop` make genuine
multi-round progress: the loop emits a :class:`ToolCallEvent`, the
executor runs a fake plugin returning a known value, that value is fed
back as a :class:`ToolResult`, and the loop produces a final answer in a
second provider round.

Contrast: the same flow with a placeholder result short-circuits the
loop (``_is_awaiting_placeholder``) — covered here so the regression is
explicit.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_agent import (
    ChatStart,
    DoneEvent,
    ReasoningLoop,
    ToolCallEvent,
    ToolResult,
)
from corlinman_grpc._generated.corlinman.v1 import agent_pb2
from corlinman_grpc.agent_client import RegistryToolExecutor, ToolInvocation
from corlinman_providers.base import ProviderChunk


class _MultiRoundProvider:
    """Replays a list of per-round ProviderChunk scripts, recording the
    messages each round saw so the test can assert the tool turn was
    appended."""

    def __init__(self, rounds: list[list[ProviderChunk]]) -> None:
        self._rounds = rounds
        self.calls_seen: list[list[dict[str, Any]]] = []

    async def chat_stream(  # type: ignore[override]
        self, *, messages: Any, **_: Any
    ) -> AsyncIterator[ProviderChunk]:
        idx = len(self.calls_seen)
        self.calls_seen.append(list(messages))
        for chunk in self._rounds[idx]:
            yield chunk


_TOOL_ROUND = [
    ProviderChunk(kind="tool_call_start", tool_call_id="c1", tool_name="add"),
    ProviderChunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta='{"a":2,"b":3}'),
    ProviderChunk(kind="tool_call_end", tool_call_id="c1"),
    ProviderChunk(kind="done", finish_reason="tool_calls"),
]
_FINAL_ROUND = [
    ProviderChunk(kind="token", text="the sum is 5"),
    ProviderChunk(kind="done", finish_reason="stop"),
]


async def _drive_with_executor(
    loop: ReasoningLoop,
    executor: RegistryToolExecutor,
) -> list[Any]:
    """Run the loop, routing every ToolCallEvent through ``executor`` and
    feeding the genuine result back — exactly what the gateway's
    chat-service loop does, minus the gRPC frames."""
    events: list[Any] = []
    start = ChatStart(model="m", messages=[{"role": "user", "content": "2+3?"}])
    async for ev in loop.run(start):
        events.append(ev)
        if isinstance(ev, ToolCallEvent):
            call = agent_pb2.ToolCall(
                call_id=ev.call_id,
                plugin=ev.plugin,
                tool=ev.tool,
                args_json=ev.args_json,
            )
            result = await executor.execute(call)
            loop.feed_tool_result(
                ToolResult(
                    call_id=result.call_id,
                    content=result.result_json.decode("utf-8"),
                    is_error=result.is_error,
                )
            )
    return events


@pytest.mark.asyncio
async def test_real_executor_drives_second_round() -> None:
    """A real plugin result feeds back and the loop completes a second
    provider round with a final answer."""
    invoked: list[tuple[str, str]] = []

    async def fake_plugin(plugin: str, tool: str, args_json: bytes) -> ToolInvocation:
        invoked.append((plugin, tool))
        args = json.loads(args_json)
        total = args["a"] + args["b"]
        return ToolInvocation(content=json.dumps({"sum": total}), is_error=False)

    executor = RegistryToolExecutor(fake_plugin)
    provider = _MultiRoundProvider([_TOOL_ROUND, _FINAL_ROUND])
    loop = ReasoningLoop(provider, tool_result_timeout=2.0)

    events = await asyncio.wait_for(
        _drive_with_executor(loop, executor), timeout=3.0
    )

    # The fake plugin was actually invoked.
    assert invoked == [("add", "add")]
    # Two provider rounds ran — the loop made real progress.
    assert len(provider.calls_seen) == 2
    # The second round saw the tool result message appended.
    tool_msgs = [m for m in provider.calls_seen[1] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert json.loads(tool_msgs[0]["content"]) == {"sum": 5}
    # The loop terminates cleanly with the final answer's finish_reason.
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_executor_error_result_still_progresses() -> None:
    """An executor error result is a *real* (non-placeholder) result, so
    the loop still feeds it and runs a second round — it does not
    short-circuit the way the M2 placeholder did."""

    async def failing_plugin(*_: object) -> ToolInvocation:
        raise RuntimeError("plugin down")

    executor = RegistryToolExecutor(failing_plugin)
    provider = _MultiRoundProvider([_TOOL_ROUND, _FINAL_ROUND])
    loop = ReasoningLoop(provider, tool_result_timeout=2.0)

    events = await asyncio.wait_for(
        _drive_with_executor(loop, executor), timeout=3.0
    )

    # Two rounds still ran — the error result drove progress.
    assert len(provider.calls_seen) == 2
    tool_msgs = [m for m in provider.calls_seen[1] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert json.loads(tool_msgs[0]["content"])["error"] == "tool_invocation_failed"
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_unwired_executor_does_not_short_circuit_like_placeholder() -> None:
    """Regression contrast: the unwired executor's ``executor_not_wired``
    result is NOT the ``awaiting_plugin_runtime`` placeholder, so the
    loop still advances to a second round instead of bailing early."""
    executor = RegistryToolExecutor(None)
    provider = _MultiRoundProvider([_TOOL_ROUND, _FINAL_ROUND])
    loop = ReasoningLoop(provider, tool_result_timeout=2.0)

    events = await asyncio.wait_for(
        _drive_with_executor(loop, executor), timeout=3.0
    )

    assert len(provider.calls_seen) == 2
    assert isinstance(events[-1], DoneEvent)
