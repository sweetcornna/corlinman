"""Tests for ``subagent_spawn_inline`` — the ad-hoc / temporary agent tool.

Mirrors the named-spawn tests but for the inline path: an ephemeral
:class:`AgentCard` built from a freeform ``system_prompt``, never written
to the registry, run through the SAME runner/supervisor as named spawns.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import nullcontext
from typing import Any

import pytest
from corlinman_agent.agents.card import _safe_slug, build_ephemeral_card
from corlinman_agent.subagent import (
    ARGS_INVALID_ERROR,
    BACKGROUND_NOT_IMPLEMENTED_ERROR,
    SUBAGENT_SPAWN_INLINE_TOOL,
    FinishReason,
    ParentContext,
    dispatch_subagent_spawn_inline,
    subagent_spawn_inline_tool_schema,
)
from corlinman_agent.subagent.runner import (
    SUBAGENT_SPAWN_INLINE_TOOL as _INLINE_NAME,
)
from corlinman_agent.subagent.runner import (
    TOOL_ALLOWLIST_ESCALATION_ERROR,
    _filter_tools_for_child,
)
from corlinman_providers.base import ProviderChunk


def _parent_ctx(depth: int = 0) -> ParentContext:
    return ParentContext(
        tenant_id="tenant-a",
        parent_agent_id="main",
        parent_session_key="root",
        depth=depth,
        trace_id="trace-test",
    )


class _FakeProvider:
    def __init__(self, text: str = "inline output") -> None:
        self._text = text
        self.calls = 0

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.calls += 1
        yield ProviderChunk(kind="token", text=self._text)
        yield ProviderChunk(kind="done", finish_reason="stop")


def _tool(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name}}


def _args(**kw: Any) -> str:
    return json.dumps(kw)


def _acquire_ok(_ctx: Any) -> Any:
    return nullcontext()


def _acquire_reject(reason: str):
    def _a(_ctx: Any) -> Any:
        return reason
    return _a


# ---------------------------------------------------------------------------
# schema + ephemeral card unit
# ---------------------------------------------------------------------------


def test_inline_schema_shape() -> None:
    schema = subagent_spawn_inline_tool_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == SUBAGENT_SPAWN_INLINE_TOOL == "subagent_spawn_inline"
    assert set(fn["parameters"]["required"]) == {"goal", "system_prompt"}


def test_build_ephemeral_card_is_unregistered() -> None:
    card = build_ephemeral_card(
        name="My Research Bot!", system_prompt="you are a researcher", model="m1"
    )
    assert card.source_path is None  # never on disk
    assert card.source == "inline"
    assert card.tools_allowed == ["*"]  # inherits parent tools
    assert card.name == "my-research-bot"  # slugified
    assert card.model == "m1"


def test_safe_slug_fallback() -> None:
    assert _safe_slug("@@@") == "inline"
    assert _safe_slug("") == "inline"
    assert _safe_slug(None) == "inline"
    assert _safe_slug("Web  Crawler") == "web-crawler"


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


async def test_inline_happy_path_no_registry() -> None:
    out = json.loads(
        await dispatch_subagent_spawn_inline(
            args_json=_args(goal="summarise X", system_prompt="you are concise"),
            parent_ctx=_parent_ctx(),
            provider=_FakeProvider("done"),
            supervisor_acquire=_acquire_ok,
        )
    )
    assert out["finish_reason"] == FinishReason.STOP.value
    assert out["output_text"] == "done"
    # ephemeral card name is mangled into the child agent id
    assert "::inline::" in out["child_agent_id"]


async def test_inline_missing_system_prompt_rejected() -> None:
    out = json.loads(
        await dispatch_subagent_spawn_inline(
            args_json=_args(goal="do a thing"),
            parent_ctx=_parent_ctx(),
            provider=_FakeProvider(),
        )
    )
    assert out["finish_reason"] == FinishReason.REJECTED.value
    assert ARGS_INVALID_ERROR in out["error"]


async def test_inline_missing_goal_rejected() -> None:
    out = json.loads(
        await dispatch_subagent_spawn_inline(
            args_json=_args(system_prompt="you are X"),
            parent_ctx=_parent_ctx(),
            provider=_FakeProvider(),
        )
    )
    assert out["finish_reason"] == FinishReason.REJECTED.value
    assert ARGS_INVALID_ERROR in out["error"]


async def test_inline_tool_escalation_rejected() -> None:
    # Parent holds only 'calculator'; the inline agent asks for 'web_fetch'.
    out = json.loads(
        await dispatch_subagent_spawn_inline(
            args_json=_args(
                goal="g", system_prompt="s", tool_allowlist=["web_fetch"]
            ),
            parent_ctx=_parent_ctx(),
            provider=_FakeProvider(),
            parent_tools=[_tool("calculator")],
            supervisor_acquire=_acquire_ok,
        )
    )
    assert out["finish_reason"] == FinishReason.REJECTED.value
    assert TOOL_ALLOWLIST_ESCALATION_ERROR in out["error"]


async def test_inline_supervisor_depth_cap() -> None:
    out = json.loads(
        await dispatch_subagent_spawn_inline(
            args_json=_args(goal="g", system_prompt="s"),
            parent_ctx=_parent_ctx(depth=2),
            provider=_FakeProvider(),
            supervisor_acquire=_acquire_reject("depth_capped"),
        )
    )
    assert out["finish_reason"] == FinishReason.DEPTH_CAPPED.value
    assert "depth_capped" in out["error"]


async def test_inline_supervisor_concurrency_reject_maps_to_rejected() -> None:
    out = json.loads(
        await dispatch_subagent_spawn_inline(
            args_json=_args(goal="g", system_prompt="s"),
            parent_ctx=_parent_ctx(),
            provider=_FakeProvider(),
            supervisor_acquire=_acquire_reject("parent_concurrency_exceeded"),
        )
    )
    assert out["finish_reason"] == FinishReason.REJECTED.value


async def test_inline_run_in_background_rejected() -> None:
    out = json.loads(
        await dispatch_subagent_spawn_inline(
            args_json=_args(goal="g", system_prompt="s", run_in_background=True),
            parent_ctx=_parent_ctx(),
            provider=_FakeProvider(),
        )
    )
    assert out["finish_reason"] == FinishReason.REJECTED.value
    assert out["error"] == BACKGROUND_NOT_IMPLEMENTED_ERROR


# ---------------------------------------------------------------------------
# depth prune
# ---------------------------------------------------------------------------


def test_inline_tool_pruned_at_max_depth() -> None:
    # A child at depth == max_depth-1 must NOT keep subagent_spawn_inline
    # (it can't legally spawn a grandchild).
    effective = _filter_tools_for_child(
        parent_tool_names=frozenset({_INLINE_NAME, "calculator"}),
        card_tools_allowed=["*"],
        requested_allowlist=None,
        child_depth=1,
        max_depth=2,
    )
    assert _INLINE_NAME not in effective
    assert "calculator" in effective

    # At a shallower depth it survives.
    shallow = _filter_tools_for_child(
        parent_tool_names=frozenset({_INLINE_NAME, "calculator"}),
        card_tools_allowed=["*"],
        requested_allowlist=None,
        child_depth=0,
        max_depth=3,
    )
    assert _INLINE_NAME in shallow


async def test_slot_released_on_cancellation_at_spawn_emit() -> None:
    """D5 — a cancellation in the post-acquire window (here parked at the
    ``SubagentSpawned`` emit await) must still release the supervisor slot.

    The guard is now entered BEFORE the first post-acquire await, so the
    per-parent + per-tenant counters return to zero on cancel instead of
    leaking until a non-deterministic ``Slot.__del__``.
    """
    from corlinman_subagent import (
        AcquireRejectError,
        Supervisor,
        SupervisorPolicy,
    )

    sup = Supervisor(SupervisorPolicy())

    def _acquire(ctx: Any) -> Any:
        # Same adapter the gateway servicer uses: return the Slot on success,
        # the reject reason string on a cap hit.
        try:
            return sup.try_acquire(ctx)
        except AcquireRejectError as exc:
            return exc.reason.value

    entered = asyncio.Event()
    never = asyncio.Event()  # never set → the emit await parks here

    class _BlockingEmitter:
        async def emit_event(self, *_a: Any, **_k: Any) -> None:
            entered.set()
            await never.wait()

    ctx = _parent_ctx()
    task = asyncio.ensure_future(
        dispatch_subagent_spawn_inline(
            args_json=_args(system_prompt="you are a helper", goal="hi"),
            parent_ctx=ctx,
            provider=_FakeProvider(),
            supervisor_acquire=_acquire,
            event_emitter=_BlockingEmitter(),
            parent_turn_id="turn-1",
            parent_session_key="root",
        )
    )

    # Park at the emit await — the slot is held right now.
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    assert sup.parent_count(ctx.parent_session_key) == 1
    assert sup.tenant_count(ctx.tenant_id) == 1

    # Cancel at the emit await; the D5 guard must release the slot.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sup.parent_count(ctx.parent_session_key) == 0, "per-parent slot leaked"
    assert sup.tenant_count(ctx.tenant_id) == 0, "per-tenant slot leaked"
