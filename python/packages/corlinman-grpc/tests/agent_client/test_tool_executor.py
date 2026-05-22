"""Unit tests for the real :class:`RegistryToolExecutor`.

Covers the executor in isolation (no plugin registry, no gateway): a
fake :data:`PluginInvoker` proves the executor delegates correctly and
that every failure mode (no invoker, invoker raises, invoker times out,
invoker returns a tool-level error) is folded into a non-crashing
``ToolResult``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from corlinman_grpc._generated.corlinman.v1 import agent_pb2
from corlinman_grpc.agent_client import (
    RegistryToolExecutor,
    ToolInvocation,
    error_result,
)


def _call(
    *, call_id: str = "c1", plugin: str = "calc", tool: str = "add", args: bytes = b"{}"
) -> agent_pb2.ToolCall:
    return agent_pb2.ToolCall(
        call_id=call_id, plugin=plugin, tool=tool, args_json=args, seq=0
    )


@pytest.mark.asyncio
async def test_no_invoker_degrades_to_error_result() -> None:
    """A wired-but-invokerless executor must not crash — it returns a
    clear ``executor_not_wired`` error result."""
    executor = RegistryToolExecutor(None)
    assert executor.is_wired is False

    result = await executor.execute(_call())
    assert result.call_id == "c1"
    assert result.is_error is True
    payload = json.loads(result.result_json.decode("utf-8"))
    assert payload["error"] == "executor_not_wired"
    # The placeholder short-circuit must NOT fire on this — it's a real
    # (terminal) error result, not the awaiting_plugin_runtime placeholder.
    assert "awaiting_plugin_runtime" not in result.result_json.decode("utf-8")


@pytest.mark.asyncio
async def test_invoker_success_is_passed_through() -> None:
    """A successful invocation maps straight onto the ToolResult."""

    async def invoker(plugin: str, tool: str, args_json: bytes) -> ToolInvocation:
        assert plugin == "calc"
        assert tool == "add"
        assert json.loads(args_json) == {"a": 2, "b": 3}
        return ToolInvocation(content='{"sum": 5}', is_error=False, duration_ms=12)

    executor = RegistryToolExecutor(invoker)
    assert executor.is_wired is True

    result = await executor.execute(_call(args=b'{"a": 2, "b": 3}'))
    assert result.call_id == "c1"
    assert result.is_error is False
    assert json.loads(result.result_json) == {"sum": 5}
    assert result.duration_ms == 12


@pytest.mark.asyncio
async def test_invoker_tool_level_error_is_preserved() -> None:
    """An ``is_error`` invocation (plugin ran, returned an error) is
    forwarded with ``is_error=True`` so the model can react."""

    async def invoker(*_: object) -> ToolInvocation:
        return ToolInvocation(
            content='{"error": "boom"}', is_error=True, duration_ms=3
        )

    result = await RegistryToolExecutor(invoker).execute(_call())
    assert result.is_error is True
    assert json.loads(result.result_json) == {"error": "boom"}


@pytest.mark.asyncio
async def test_invoker_exception_becomes_error_result() -> None:
    """A raised invoker must never crash the executor."""

    async def invoker(*_: object) -> ToolInvocation:
        raise RuntimeError("plugin exploded")

    result = await RegistryToolExecutor(invoker).execute(_call())
    assert result.is_error is True
    payload = json.loads(result.result_json)
    assert payload["error"] == "tool_invocation_failed"
    assert "plugin exploded" in payload["message"]


@pytest.mark.asyncio
async def test_invoker_timeout_becomes_error_result() -> None:
    """A slow invoker is bounded by ``timeout_s`` and yields a
    ``tool_timeout`` error result."""

    async def invoker(*_: object) -> ToolInvocation:
        await asyncio.sleep(10)
        return ToolInvocation(content="never", is_error=False)

    executor = RegistryToolExecutor(invoker, timeout_s=0.05)
    result = await executor.execute(_call())
    assert result.is_error is True
    assert json.loads(result.result_json)["error"] == "tool_timeout"


@pytest.mark.asyncio
async def test_cancellation_propagates() -> None:
    """Structural cancellation must propagate, not be swallowed as a
    tool error."""

    async def invoker(*_: object) -> ToolInvocation:
        await asyncio.sleep(10)
        return ToolInvocation(content="never")

    executor = RegistryToolExecutor(invoker, timeout_s=None)
    task = asyncio.create_task(executor.execute(_call()))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_error_result_helper_shape() -> None:
    """The ``error_result`` helper produces a stable, decodable body."""
    call = _call(plugin="p", tool="t")
    result = error_result(call, code="x", message="m", duration_ms=7)
    assert result.is_error is True
    assert result.duration_ms == 7
    payload = json.loads(result.result_json)
    assert payload == {"error": "x", "message": "m", "plugin": "p", "tool": "t"}
