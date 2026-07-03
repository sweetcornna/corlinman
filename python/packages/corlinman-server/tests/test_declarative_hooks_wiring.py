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
async def test_supplement_hook_note_is_journaled() -> None:
    """The hook note injected into a mid-turn supplement is persisted to
    the journal too, so a resume replays the same guidance (Codex #109
    round 6 — the journal previously stored only the bare user text)."""
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
        def inject_user_message(self, text: str) -> None:
            pass

    class _FakeTurn:
        turn_id = "turn-1"

    class _FakeJournal:
        def __init__(self) -> None:
            self.appended: list[tuple[str, str, str]] = []

        async def find_resumable_turn(self, session_key, user_text, user_id=None):
            return _FakeTurn()

        async def append_message(self, turn_id, role, content, **_kw):
            self.appended.append((turn_id, role, content))

    journal = _FakeJournal()
    provider = _CapturingProvider()
    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _model: provider,
        hook_runner=runner,
    )
    servicer._active_loops["sess-jrnl"] = _FakeActiveLoop()
    servicer._journal = journal  # type: ignore[assignment]
    servicer._journal_init_done = True
    await _drive_chat(servicer, user_text="also add tests", session_key="sess-jrnl")
    assert journal.appended, "supplement was never journaled"
    _tid, role, content = journal.appended[0]
    assert role == "user"
    assert "also add tests" in content
    assert "hook:user_prompt_submit" in content
    assert "obey the style guide" in content


@pytest.mark.asyncio
async def test_external_block_fires_post_hooks() -> None:
    """A PreToolUse block on an external tool still fires PostToolUse with
    the block result — matching the builtin path (Codex #109 round 6)."""
    calls: list[tuple[str, str]] = []

    class _Runner:
        async def run_pre_tool_async(self, tool, args, ctx=None) -> HookDecision:
            return HookDecision.deny("external no")

        async def run_post_tool_async(self, tool, args, result, ctx=None) -> None:
            calls.append((tool, result))

    servicer = CorlinmanAgentServicer(hook_runner=_Runner())
    _allow, reason, _mutated = await servicer._run_pre_tool_hook_gate(
        _tool_event(tool="mcp__srv__fetch"), _start()
    )
    assert _allow is False
    # Simulate the external-block branch calling the post hook with the
    # block result (the same call the loop makes inline).
    blocked = json.dumps({"error": f"blocked by hook: {reason}", "tool": "mcp__srv__fetch"})
    await servicer._run_post_tool_hooks("mcp__srv__fetch", {}, _start(), blocked)
    assert len(calls) == 1
    assert calls[0][0] == "mcp__srv__fetch"
    assert "blocked by hook" in calls[0][1]


@pytest.mark.asyncio
async def test_post_hooks_receive_full_result_by_default() -> None:
    """Large tool results reach post hooks untruncated by default — audit
    hooks see the whole output (Codex #109 round 6)."""
    seen: list[str] = []

    class _Runner:
        async def run_pre_tool_async(self, tool, args, ctx=None) -> HookDecision:
            return HookDecision.allow_all()

        async def run_post_tool_async(self, tool, args, result, ctx=None) -> None:
            seen.append(result)

    servicer = CorlinmanAgentServicer(hook_runner=_Runner())
    big = "x" * 50_000
    await servicer._run_post_tool_hooks("read_file", {}, _start(), big)
    assert len(seen) == 1
    assert seen[0] == big  # no truncation, no sentinel
    assert "truncated for hook" not in seen[0]


@pytest.mark.asyncio
async def test_post_hooks_respect_explicit_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator-set CORLINMAN_HOOK_RESULT_MAX_BYTES caps the payload —
    explicit, opt-in truncation."""
    monkeypatch.setenv("CORLINMAN_HOOK_RESULT_MAX_BYTES", "1000")
    seen: list[str] = []

    class _Runner:
        async def run_pre_tool_async(self, tool, args, ctx=None) -> HookDecision:
            return HookDecision.allow_all()

        async def run_post_tool_async(self, tool, args, result, ctx=None) -> None:
            seen.append(result)

    servicer = CorlinmanAgentServicer(hook_runner=_Runner())
    await servicer._run_post_tool_hooks("read_file", {}, _start(), "y" * 50_000)
    assert len(seen) == 1
    assert seen[0].endswith("…[truncated for hook]")
    assert len(seen[0]) < 1100


@pytest.mark.asyncio
async def test_pre_tool_gate_blocks_external_tool() -> None:
    """A PreToolUse hook with matcher="*" blocks an external plugin/MCP
    tool — the frame is never yielded and the model gets a blocked result
    (Codex #109 round 5). Exercised through the real gate helper."""
    runner = HookRunner(
        {
            "hooks": {
                "declarative": {
                    "PreToolUse": [
                        {"hooks": [{"kind": "command", "command": "echo ext-no >&2; exit 2"}]}
                    ]
                }
            }
        }
    )
    servicer = CorlinmanAgentServicer(hook_runner=runner)
    allow, reason, mutated = await servicer._run_pre_tool_hook_gate(
        _tool_event(tool="mcp__server__fetch", args={"url": "http://x"}), _start()
    )
    assert allow is False
    assert "ext-no" in reason
    assert mutated is None


@pytest.mark.asyncio
async def test_pre_tool_gate_mutates_external_tool_args() -> None:
    mutated_args = {"url": "http://safe"}
    cmd = f"""echo '{json.dumps({"decision": "allow", "mutated_args": mutated_args})}'"""
    runner = HookRunner(
        {"hooks": {"declarative": {"PreToolUse": [{"hooks": [{"kind": "command", "command": cmd}]}]}}}
    )
    servicer = CorlinmanAgentServicer(hook_runner=runner)
    allow, _reason, mutated = await servicer._run_pre_tool_hook_gate(
        _tool_event(tool="mcp__server__fetch", args={"url": "http://x"}), _start()
    )
    assert allow is True
    assert mutated == mutated_args


@pytest.mark.asyncio
async def test_pre_tool_gate_allows_without_runner() -> None:
    servicer = CorlinmanAgentServicer()  # no hook runner
    allow, reason, mutated = await servicer._run_pre_tool_hook_gate(
        _tool_event(), _start()
    )
    assert allow is True and reason == "" and mutated is None


@pytest.mark.asyncio
async def test_pump_inbound_fires_post_tool_callback() -> None:
    """External tool_result frames trigger the post-hook callback after
    feeding the loop; a raising callback is swallowed (Codex #109 r4)."""
    from corlinman_grpc import agent_pb2 as pb
    from corlinman_server.agent_servicer import _pump_inbound

    class _LoopStub:
        def __init__(self) -> None:
            self.fed: list[Any] = []

        def feed_tool_result(self, tr: Any) -> None:
            self.fed.append(tr)

    async def frames():
        yield pb.ClientFrame(
            tool_result=pb.ToolResult(
                call_id="ext-1", result_json=b'{"ok": true}', is_error=False
            )
        )

    seen: list[tuple[str, str, bool]] = []

    async def on_result(call_id: str, content: str, is_error: bool) -> None:
        seen.append((call_id, content, is_error))
        raise RuntimeError("hook blew up — must be swallowed")

    loop_stub = _LoopStub()
    await _pump_inbound(frames(), loop_stub, on_tool_result=on_result)  # type: ignore[arg-type]
    assert [tr.call_id for tr in loop_stub.fed] == ["ext-1"]
    assert seen == [("ext-1", '{"ok": true}', False)]


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
