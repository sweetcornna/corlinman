"""W3.2 — task-observability emits from the subagent supervisor.

Three tests covering the W3.2 contract:

1. ``test_supervisor_emits_spawned_and_completed`` — the supervisor's
   ``spawn_child`` happy path fires one ``SubagentSpawned`` envelope
   (with the parent's turn_id / session_key, the child's mangled
   identifiers, the depth, and a truncated prompt preview) followed by
   one ``SubagentCompleted`` envelope carrying the child's finish
   reason / tool count / elapsed / output summary.
2. ``test_child_events_bubble_via_subagent_event`` — events emitted by
   the *child* through a :class:`BubbleEmitter` arrive at the parent's
   emitter wrapped in :class:`SubagentEvent` with the child's session
   key, so the frontend can route them under the right sub-tree.
3. ``test_depth_cap_enforced`` — at-or-over the depth cap, the
   supervisor refuses the spawn before any observability emit fires
   (the existing reject path is the load-bearing piece — we just
   verify it still works alongside the W3.2 wiring).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from corlinman_agent.events import (
    EventEnvelope,
    MockEventEmitter,
    SubagentCompleted,
    SubagentEvent,
    SubagentSpawned,
    TextDelta,
)
from corlinman_server.gateway.observability.emitter import BubbleEmitter
from corlinman_subagent import (
    AcquireReject,
    AcquireRejectError,
    FinishReason,
    ParentContext,
    Supervisor,
    SupervisorPolicy,
    TaskResult,
    TaskSpec,
)


def _parent(depth: int = 0) -> ParentContext:
    return ParentContext(
        tenant_id="tenant-a",
        parent_agent_id="agent-of-root",
        parent_session_key="sess_root",
        depth=depth,
        trace_id="trace-of-root",
    )


@pytest.mark.asyncio
async def test_supervisor_emits_spawned_and_completed() -> None:
    """End-to-end: spawn_child → ``SubagentSpawned`` + ``SubagentCompleted``
    envelopes both land on the parent's emitter with the right shape.
    """
    emitter = MockEventEmitter()
    sup = Supervisor(
        SupervisorPolicy(),
        event_emitter=emitter,
        parent_turn_id="turn-42",
        parent_session_key="sess_root",
    )
    parent = _parent()
    task = TaskSpec(goal="summarise the transformer paper")

    async def runner(spec: TaskSpec, child_ctx: ParentContext) -> TaskResult:
        return TaskResult(
            output_text="done",
            tool_calls_made=[],
            child_session_key=child_ctx.parent_session_key,
            child_agent_id=child_ctx.parent_agent_id,
            elapsed_ms=42,
            finish_reason=FinishReason.STOP,
        )

    result = await sup.spawn_child(runner, parent, task, agent_card="researcher")
    assert result.finish_reason is FinishReason.STOP

    # The supervisor schedules obs emits via ``asyncio.create_task`` so
    # they run on the next event-loop tick. Yield once so the tasks
    # complete before we inspect the captured envelopes.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    spawned = emitter.events_of(SubagentSpawned)
    completed = emitter.events_of(SubagentCompleted)
    assert len(spawned) == 1, f"expected 1 SubagentSpawned, got {emitter.event_types}"
    assert len(completed) == 1, f"expected 1 SubagentCompleted, got {emitter.event_types}"

    s = spawned[0]
    assert s.parent_session_key == "sess_root"
    assert s.child_session_key == "sess_root::child::0"
    assert s.child_agent_id == "agent-of-root::researcher::0"
    assert s.depth == 1
    assert s.prompt_preview == "summarise the transformer paper"

    c = completed[0]
    assert c.child_session_key == "sess_root::child::0"
    assert c.finish_reason == "stop"
    assert c.tool_calls_made == 0
    assert c.elapsed_ms >= 0
    assert c.summary == "done"

    # Every envelope is stamped with the parent's correlation pair so
    # the SSE consumer routes them to the right turn.
    spawned_env = next(
        e for e in emitter.envelopes if isinstance(e.event, SubagentSpawned)
    )
    assert spawned_env.turn_id == "turn-42"
    assert spawned_env.session_key == "sess_root"


@pytest.mark.asyncio
async def test_child_events_bubble_via_subagent_event() -> None:
    """A child's emits, routed through a :class:`BubbleEmitter`, arrive
    at the parent's stream wrapped in :class:`SubagentEvent`. The
    wrapped inner envelope is preserved verbatim so the UI can
    re-render the child's part inside the nested sub-tree.
    """
    parent_emitter = MockEventEmitter()
    bubble = BubbleEmitter(
        parent=parent_emitter,
        parent_turn_id="turn-99",
        parent_session_key="sess_root",
        child_session_key="sess_root::child::0",
    )

    # Simulate the child agent emitting a text delta on its own
    # ReasoningLoop — the bubble wrapper should fold it into a
    # SubagentEvent under the parent's turn id.
    inner = TextDelta(index=0, text="child text", cumulative_len=10)
    await bubble.emit_event("turn-child", "sess_root::child::0", inner)

    assert len(parent_emitter.envelopes) == 1
    outer = parent_emitter.envelopes[0]
    assert outer.turn_id == "turn-99"
    assert outer.session_key == "sess_root"
    assert isinstance(outer.event, SubagentEvent)
    wrapped = outer.event
    assert wrapped.child_session_key == "sess_root::child::0"
    # The inner envelope carries the original event payload.
    assert isinstance(wrapped.envelope, EventEnvelope)
    assert isinstance(wrapped.envelope.event, TextDelta)
    assert wrapped.envelope.event.text == "child text"

    # Also exercise the ``emit`` (envelope-already-built) surface to
    # cover the second branch.
    other_inner_env = EventEnvelope(
        turn_id="x",
        session_key="y",
        sequence=7,
        timestamp_ms=1,
        event=TextDelta(index=0, text="second", cumulative_len=6),
    )
    await bubble.emit(other_inner_env)
    assert len(parent_emitter.envelopes) == 2
    second = parent_emitter.envelopes[1]
    assert isinstance(second.event, SubagentEvent)
    assert second.event.envelope.event.text == "second"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_depth_cap_enforced() -> None:
    """The existing depth cap still short-circuits before any
    observability emit fires. The W3.2 wiring must not regress the
    cap-accounting contract.
    """
    emitter = MockEventEmitter()
    sup = Supervisor(
        SupervisorPolicy(max_depth=2),
        event_emitter=emitter,
        parent_turn_id="turn-1",
        parent_session_key="sess_root",
    )
    # depth == max_depth → should refuse before any work.
    over_cap = _parent(depth=2)
    task = TaskSpec(goal="never runs")

    invoked: dict[str, Any] = {"called": False}

    async def runner(spec: TaskSpec, child_ctx: ParentContext) -> TaskResult:
        invoked["called"] = True
        raise AssertionError("agent must not run when depth cap rejects")

    with pytest.raises(AcquireRejectError) as ei:
        await sup.spawn_child(runner, over_cap, task)
    assert ei.value.reason is AcquireReject.DEPTH_CAPPED
    assert invoked["called"] is False

    # Yield to let any stray emit tasks run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # No SubagentSpawned / SubagentCompleted on the obs lane — the cap
    # path is owned by ``_emit_reject`` (hook-bus only). The W3.2
    # contract is "spawned + completed go together"; a rejected spawn
    # never emits the pair.
    assert emitter.events_of(SubagentSpawned) == []
    assert emitter.events_of(SubagentCompleted) == []
