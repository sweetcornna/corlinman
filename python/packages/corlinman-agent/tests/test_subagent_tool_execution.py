"""v1.12.3 — subagent children EXECUTE their tools and synthesize a final
answer (fix for the empty-``output_text`` prod incident).

Before this fix the child loop *recorded* tool calls but never ran them or fed
results back, so the model never received results and produced no final
answer (``output_text==""`` while ``tool_calls_made`` was populated). These
tests pin the fixed behaviour:

* tool execution + ``feed_tool_result`` drives a real synthesis round;
* a guaranteed-synthesis fallback covers the bare-tool-call case;
* ``max_tool_calls`` caps real tool execution (cost guard);
* the finish-reason mapping for ``"tool_calls"`` is the truthful ``LENGTH``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from corlinman_agent.agents.card import AgentCard
from corlinman_agent.reasoning_loop import ToolCallEvent
from corlinman_agent.subagent import FinishReason, ParentContext, TaskSpec, run_child
from corlinman_agent.subagent.runner import _map_finish_reason
from corlinman_providers.base import ProviderChunk


class _ScriptedProvider:
    """Replays a list of per-round ProviderChunk scripts. Each ``chat_stream``
    call pops the next round; extra calls replay the last round (so the
    synthesis-fallback loop, which makes its own provider call, gets the
    final scripted round)."""

    def __init__(self, rounds: list[list[ProviderChunk]]) -> None:
        self._rounds = rounds
        self.calls = 0
        self.tools_seen: list[Any] = []

    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.tools_seen.append(kwargs.get("tools"))
        idx = min(self.calls, len(self._rounds) - 1)
        self.calls += 1
        for chunk in self._rounds[idx]:
            yield chunk


def _card(name: str = "general-purpose") -> AgentCard:
    return AgentCard(
        name=name,
        description="",
        system_prompt="You are a test agent.",
        tools_allowed=["*"],
    )


def _parent_ctx() -> ParentContext:
    return ParentContext(
        tenant_id="t",
        parent_agent_id="main",
        parent_session_key="root",
        depth=0,
        trace_id="tr",
    )


def _tool_schema(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "t",
            "parameters": {"type": "object", "properties": {}},
        },
    }


_TOOL_ROUND = [
    ProviderChunk(kind="tool_call_start", tool_call_id="c1", tool_name="web_search"),
    ProviderChunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta='{"query":"x"}'),
    ProviderChunk(kind="tool_call_end", tool_call_id="c1"),
    ProviderChunk(kind="done", finish_reason="tool_calls"),
]
_FINAL_ROUND = [
    ProviderChunk(kind="token", text="最终答案：找到了 3 个值得关注的项目。"),
    ProviderChunk(kind="done", finish_reason="stop"),
]


async def test_child_executes_tool_then_synthesizes() -> None:
    """The core fix: the child runs web_search, the result is fed back, and
    the model produces a real final answer in the next round."""
    executed: list[str] = []

    async def executor(ev: ToolCallEvent) -> str:
        executed.append(ev.tool)
        return json.dumps({"results": ["proj-a", "proj-b", "proj-c"]})

    provider = _ScriptedProvider([_TOOL_ROUND, _FINAL_ROUND])
    result = await run_child(
        _parent_ctx(),
        _card(),
        TaskSpec(goal="find ai github projects"),
        provider=provider,
        parent_tools=[_tool_schema("web_search")],
        tool_executor=executor,
    )

    assert executed == ["web_search"], "the child must actually run its tool"
    assert "最终答案" in result.output_text
    assert result.output_text.strip()  # NOT empty — the bug
    assert len(result.tool_calls_made) == 1
    assert result.finish_reason is FinishReason.STOP


async def test_guaranteed_synthesis_fallback_on_empty_answer() -> None:
    """If tools ran but the model emitted no answer text (bare tool round then
    an empty stop), the tools-disabled fallback round still returns text."""
    empty_stop = [ProviderChunk(kind="done", finish_reason="stop")]
    fallback_text = [
        ProviderChunk(kind="token", text="根据搜索结果整理：A、B、C 三个项目。"),
        ProviderChunk(kind="done", finish_reason="stop"),
    ]
    # round1 tool call -> feed result -> round2 empty stop (no text) ->
    # fallback loop makes a 3rd provider call -> fallback_text.
    provider = _ScriptedProvider([_TOOL_ROUND, empty_stop, fallback_text])

    async def executor(ev: ToolCallEvent) -> str:
        return json.dumps({"results": ["a", "b", "c"]})

    result = await run_child(
        _parent_ctx(),
        _card(),
        TaskSpec(goal="x"),
        provider=provider,
        parent_tools=[_tool_schema("web_search")],
        tool_executor=executor,
    )

    assert result.output_text.strip(), "fallback must produce a non-empty answer"
    assert "整理" in result.output_text
    # The fallback round runs with tools disabled (loop normalizes [] -> None).
    assert not provider.tools_seen[-1], "fallback round must carry no tools"


async def test_max_tool_calls_caps_real_execution() -> None:
    """The child stops EXECUTING real tools past max_tool_calls (cost guard)
    while still reaching a final answer."""
    executed: list[str] = []

    async def executor(ev: ToolCallEvent) -> str:
        executed.append(ev.tool)
        return json.dumps({"results": ["x"]})

    # 3 tool rounds then a final text round. With max_tool_calls=2, only the
    # first two tool calls actually execute; the 3rd gets a budget envelope.
    provider = _ScriptedProvider([_TOOL_ROUND, _TOOL_ROUND, _TOOL_ROUND, _FINAL_ROUND])
    result = await run_child(
        _parent_ctx(),
        _card(),
        TaskSpec(goal="x", max_tool_calls=2),
        provider=provider,
        parent_tools=[_tool_schema("web_search")],
        tool_executor=executor,
    )

    assert len(executed) == 2, "real tool execution must be capped at max_tool_calls"
    assert result.output_text.strip()


async def test_finish_reason_tool_calls_maps_to_length() -> None:
    """v1.12.3 — a child that ends ON a tool-call round (no synthesis) is
    truncated, not cleanly done: map to LENGTH so the parent gets a truthful
    signal instead of a silent STOP."""
    assert _map_finish_reason("tool_calls") is FinishReason.LENGTH
    assert _map_finish_reason("length") is FinishReason.LENGTH
    assert _map_finish_reason("stop") is FinishReason.STOP


async def test_no_executor_keeps_legacy_behaviour() -> None:
    """Without a wired executor (pure-LLM child / legacy callers), a tool call
    is recorded but not run — the pre-v1.12.3 contract, so existing no-tool
    children and tests are unaffected."""
    executed: list[str] = []

    async def _never(ev: ToolCallEvent) -> str:  # pragma: no cover - must not run
        executed.append(ev.tool)
        return "{}"

    # A child that just answers directly (no tool call) still works.
    provider = _ScriptedProvider([_FINAL_ROUND])
    result = await run_child(
        _parent_ctx(),
        _card(),
        TaskSpec(goal="x"),
        provider=provider,
        parent_tools=[_tool_schema("web_search")],
        tool_executor=None,
    )
    assert executed == []
    assert "最终答案" in result.output_text
