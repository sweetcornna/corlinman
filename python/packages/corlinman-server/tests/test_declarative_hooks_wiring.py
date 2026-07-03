"""Declarative-hooks wiring through the agent servicer (Dim 9 parity).

Pins the four production seams added with the declarative layer:

- the pre-tool gate honours a declarative deny (``hook_blocked`` envelope)
  through the REAL ``_dispatch_builtin`` path;
- post-tool hooks receive the actual result string via the dispatch
  wrapper (fire-and-forget, never affects the call);
- ``user_prompt_submit`` declarative hooks run at Chat entry and an
  awaited hook's inject verdict lands as a system note in the messages
  the provider sees;
- the servicer-constructed ``ReasoningLoop`` now carries the hook runner,
  so the Stop hook actually fires at turn end (was inert before).
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from typing import Any

import grpc
import grpc.aio
import pytest
from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
from corlinman_grpc import agent_pb2, agent_pb2_grpc, common_pb2
from corlinman_hooks import HookRunner
from corlinman_hooks.runner import HookDecision
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shell hooks are POSIX-flavored")


class _CapturingProvider:
    """Yields tokens and records the kwargs of every ``chat_stream`` call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:
        self.calls.append(kwargs)
        yield ProviderChunk(kind="token", text="ok")
        yield ProviderChunk(kind="done", finish_reason="stop")


async def _drive_chat(servicer: CorlinmanAgentServicer, *, user_text: str, session_key: str) -> None:
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def frames():
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(
                        model="claude-sonnet-4-5",
                        session_key=session_key,
                        messages=[
                            common_pb2.Message(role=common_pb2.USER, content=user_text)
                        ],
                    )
                )

            async for _ in stub.Chat(frames()):
                pass
    finally:
        await server.stop(grace=None)


def _tool_event(tool: str = "no_such_builtin_tool", args: dict | None = None) -> ToolCallEvent:
    return ToolCallEvent(
        call_id="call-1",
        plugin="builtin",
        tool=tool,
        args_json=json.dumps(args or {}).encode(),
    )


def _start(session_key: str = "sess-decl") -> ChatStart:
    return ChatStart(model="m", session_key=session_key, messages=[])


@pytest.mark.asyncio
async def test_declarative_pre_tool_deny_blocks_dispatch() -> None:
    runner = HookRunner(
        {
            "hooks": {
                "declarative": {
                    "PreToolUse": [
                        {"hooks": [{"kind": "command", "command": "echo declarative-no >&2; exit 2"}]}
                    ]
                }
            }
        }
    )
    servicer = CorlinmanAgentServicer(hook_runner=runner)
    result = await servicer._dispatch_builtin(_tool_event(), _start(), provider=None)
    assert isinstance(result, str)
    payload = json.loads(result)
    assert "blocked by hook" in payload["error"]
    assert "declarative-no" in payload["error"]


@pytest.mark.asyncio
async def test_declarative_pre_tool_mutation_rewrites_args() -> None:
    mutated = {"cmd": "safe"}
    cmd = f"""echo '{json.dumps({"decision": "allow", "mutated_args": mutated})}'"""
    runner = HookRunner(
        {"hooks": {"declarative": {"PreToolUse": [{"hooks": [{"kind": "command", "command": cmd}]}]}}}
    )
    servicer = CorlinmanAgentServicer(hook_runner=runner)
    event = _tool_event(args={"cmd": "rm -rf /"})
    await servicer._dispatch_builtin(event, _start(), provider=None)
    assert json.loads(event.args_json.decode()) == mutated


@pytest.mark.asyncio
async def test_post_tool_hooks_receive_result() -> None:
    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict, str, dict]] = []

        async def run_pre_tool_async(self, tool: str, args: dict, ctx: dict | None = None) -> HookDecision:
            return HookDecision.allow_all()

        async def run_post_tool_async(self, tool: str, args: dict, result: str, ctx: dict | None = None) -> None:
            self.calls.append((tool, args, result, ctx or {}))

    recorder = _Recorder()
    servicer = CorlinmanAgentServicer(hook_runner=recorder)
    result = await servicer._dispatch_builtin(_tool_event(), _start(), provider=None)
    assert len(recorder.calls) == 1
    tool, _args, recorded_result, ctx = recorder.calls[0]
    assert tool == "no_such_builtin_tool"
    assert recorded_result == result
    assert ctx["session_key"] == "sess-decl"


@pytest.mark.asyncio
async def test_user_prompt_submit_inject_becomes_system_note() -> None:
    inject = {"decision": "allow", "inject_message": "remember the style guide"}
    cmd = f"""echo '{json.dumps(inject)}'"""
    runner = HookRunner(
        {
            "hooks": {
                "declarative": {
                    "UserPromptSubmit": [
                        {"hooks": [{"kind": "command", "command": cmd, "async": False}]}
                    ]
                }
            }
        }
    )
    provider = _CapturingProvider()
    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _model: provider,
        hook_runner=runner,
    )
    await _drive_chat(servicer, user_text="write the docs", session_key="sess-note")
    assert provider.calls, "provider was never invoked"
    rendered = json.dumps(provider.calls[0].get("messages", []), ensure_ascii=False, default=str)
    assert "hook:user_prompt_submit" in rendered
    assert "remember the style guide" in rendered


@pytest.mark.asyncio
async def test_user_prompt_submit_note_reaches_midturn_supplement() -> None:
    """When a turn is already in flight, the hook note rides along with the
    injected supplement text instead of being dropped with the early
    return (Codex #109)."""
    inject = {"decision": "allow", "inject_message": "obey the style guide"}
    cmd = f"""echo '{json.dumps(inject)}'"""
    runner = HookRunner(
        {
            "hooks": {
                "declarative": {
                    "UserPromptSubmit": [
                        {"hooks": [{"kind": "command", "command": cmd, "async": False}]}
                    ]
                }
            }
        }
    )

    class _FakeActiveLoop:
        def __init__(self) -> None:
            self.injected: list[str] = []

        def inject_user_message(self, text: str) -> None:
            self.injected.append(text)

    fake_loop = _FakeActiveLoop()
    provider = _CapturingProvider()
    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _model: provider,
        hook_runner=runner,
    )
    servicer._active_loops["sess-supp"] = fake_loop  # a turn is "running"
    await _drive_chat(servicer, user_text="also add tests", session_key="sess-supp")
    assert len(fake_loop.injected) == 1
    assert "also add tests" in fake_loop.injected[0]
    assert "hook:user_prompt_submit" in fake_loop.injected[0]
    assert "obey the style guide" in fake_loop.injected[0]
    assert not provider.calls  # supplement path — no fresh turn started


@pytest.mark.asyncio
async def test_servicer_loop_carries_hook_runner_to_stop_hook() -> None:
    class _StopRecorder:
        def __init__(self) -> None:
            self.stop_calls: list[dict] = []

        async def run_pre_tool_async(self, tool: str, args: dict, ctx: dict | None = None) -> HookDecision:
            return HookDecision.allow_all()

        async def run_stop_async(self, ctx: dict | None = None) -> HookDecision:
            self.stop_calls.append(dict(ctx or {}))
            return HookDecision.allow_all()

        async def run_event_async(self, event: str, payload: dict | None = None, ctx: dict | None = None) -> HookDecision:
            return HookDecision.allow_all()

    recorder = _StopRecorder()
    provider = _CapturingProvider()
    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _model: provider,
        hook_runner=recorder,
    )
    await _drive_chat(servicer, user_text="hi", session_key="sess-stop")
    assert len(recorder.stop_calls) == 1
    assert recorder.stop_calls[0].get("session_key") == "sess-stop"
    assert recorder.stop_calls[0].get("finish_reason") == "stop"
