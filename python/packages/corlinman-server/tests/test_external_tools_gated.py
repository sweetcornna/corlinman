"""EP2 end-to-end (audit W3-2): the unified gate covers EXTERNAL tools.

Drives the real gRPC servicer with a provider that calls a non-builtin
(MCP-style) tool and asserts on the frames the gateway would execute:

* ``{"tool": "*", "action": "deny"}`` → NO external ToolCall frame is
  yielded (acceptance 1) and the model reads a permission_denied result;
* ``mode = "plan"`` → same (acceptance 2);
* ``mode = "bypass"`` → the frame IS yielded despite a deny rule;
* ``external_tools_enforced = false`` → pre-W3-2 bypass (risk R4);
* a PreToolUse hook mutating the args re-faces the gate (hook-order fix,
  external flavour of SEC-09).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import grpc
import grpc.aio
import pytest
from corlinman_agent.authz.defaults import apply_permissions_config
from corlinman_grpc import agent_pb2, agent_pb2_grpc
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer

_EXTERNAL_TOOL = "github_create_issue"


def _tool_call_chunks(tool: str, args: dict[str, Any]) -> list[ProviderChunk]:
    return [
        ProviderChunk(kind="tool_call_start", tool_call_id="call-ext-1", tool_name=tool),
        ProviderChunk(
            kind="tool_call_delta",
            tool_call_id="call-ext-1",
            arguments_delta=json.dumps(args),
        ),
        ProviderChunk(kind="tool_call_end", tool_call_id="call-ext-1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]


class _ToolThenDoneProvider:
    """Round 1: emit the external tool call. Round 2: record what the
    model read back and finish."""

    def __init__(self, tool: str = _EXTERNAL_TOOL, args: dict[str, Any] | None = None):
        self.calls = 0
        self.round2_messages: list[dict[str, Any]] = []
        self._chunks = _tool_call_chunks(tool, args or {"title": "hi"})

    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:
        self.calls += 1
        if self.calls == 1:
            for chunk in self._chunks:
                yield chunk
            return
        self.round2_messages = list(kwargs.get("messages") or [])
        yield ProviderChunk(kind="done", finish_reason="stop")


async def _drive(servicer: CorlinmanAgentServicer) -> list[agent_pb2.ToolCall]:
    """Run one turn; return every yielded ToolCall frame."""
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    frames: list[agent_pb2.ToolCall] = []
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def client_frames():
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(model="m", session_key="t::s1")
                )

            async for frame in stub.Chat(client_frames()):
                if frame.WhichOneof("kind") == "tool_call":
                    frames.append(frame.tool_call)
    finally:
        await servicer.aclose()
        await server.stop(grace=None)
    return frames


def _external_frames(frames: list[agent_pb2.ToolCall]) -> list[agent_pb2.ToolCall]:
    """Frames chat_service would EXECUTE (no ``_builtin*`` sentinel)."""
    return [f for f in frames if not f.plugin.startswith("_builtin")]


@pytest.mark.asyncio
async def test_wildcard_deny_blocks_external_tool_frame() -> None:
    apply_permissions_config({"rules": [{"tool": "*", "action": "deny"}]})
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)

    frames = await _drive(servicer)

    assert _external_frames(frames) == []
    # The model read the denial and got its second round.
    assert provider.calls == 2
    fed = json.dumps(provider.round2_messages)
    assert "permission_denied" in fed


@pytest.mark.asyncio
async def test_mcp_namespaced_rule_blocks_external_tool_frame() -> None:
    apply_permissions_config(
        {"rules": [{"tool": "mcp:github/*", "action": "deny"}]}
    )
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)

    assert _external_frames(await _drive(servicer)) == []


@pytest.mark.asyncio
async def test_plan_mode_blocks_external_tool_frame() -> None:
    apply_permissions_config({"mode": "plan"})
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)

    assert _external_frames(await _drive(servicer)) == []
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_bypass_mode_lets_external_tool_frame_through() -> None:
    apply_permissions_config(
        {"mode": "bypass", "rules": [{"tool": "*", "action": "deny"}]}
    )
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)

    external = _external_frames(await _drive(servicer))
    assert [f.tool for f in external] == [_EXTERNAL_TOOL]


@pytest.mark.asyncio
async def test_escape_hatch_restores_pre_w32_bypass() -> None:
    apply_permissions_config(
        {
            "external_tools_enforced": False,
            "rules": [{"tool": "*", "action": "deny"}],
        }
    )
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)

    external = _external_frames(await _drive(servicer))
    assert [f.tool for f in external] == [_EXTERNAL_TOOL]


@pytest.mark.asyncio
async def test_external_ask_rule_fail_closes_without_resolver() -> None:
    """The ``ask`` escalation must re-resolve under the EXTERNAL keys:
    with no resolver wired the call fail-closes to a denial instead of
    silently degrading to default-allow (W3-2 ApprovalGate fix)."""
    apply_permissions_config(
        {"rules": [{"tool": "mcp:github/*", "action": "ask"}]}
    )
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)

    frames = await _drive(servicer)
    assert _external_frames(frames) == []
    assert "approval_denied" in json.dumps(provider.round2_messages)


@pytest.mark.asyncio
async def test_hook_mutation_on_external_tool_refaces_gate() -> None:
    """External flavour of SEC-09: a hook rewriting the args must not
    smuggle a denied call past an arg-scoped rule."""
    apply_permissions_config(
        {"rules": [{"tool": "mcp:github/*(evil*)", "action": "deny"}]}
    )

    class _MutatingRunner:
        async def run_pre_tool_async(self, tool: str, args: dict, ctx: Any = None):
            return SimpleNamespace(
                allow=True, reason="", mutated_args={"title": "evil payload"}
            )

    provider = _ToolThenDoneProvider(args={"title": "benign"})
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)
    servicer.set_hook_runner(_MutatingRunner())

    frames = await _drive(servicer)
    assert _external_frames(frames) == []
    assert "permission_denied" in json.dumps(provider.round2_messages)
