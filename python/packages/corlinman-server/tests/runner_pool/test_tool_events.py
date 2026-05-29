"""W3.1 — :func:`dispatch_with_observability` state machine + heartbeat.

The reasoning loop's W1.1 emits ``ToolStateRunning`` /
``ToolStateCompleted`` once per round, with coarse per-batch timing.
The tool dispatcher (this module) emits richer events per dispatch:

* ``ToolStateRunning`` immediately before the wrapped coroutine awaits.
* ``ToolStateHeartbeat`` every ``heartbeat_interval_s`` while the
  coroutine is still running. Cancels itself on completion so a fast
  tool never sees a heartbeat at all.
* ``ToolStateCompleted`` after the coroutine returns (or raises),
  carrying the per-call elapsed_ms and a truncated result summary.

These tests pin the contract directly so a regression in the dispatch
helper surfaces immediately — and so future plumbing into
``chat_service.py`` / subagent supervisor can reuse the same helper
with confidence.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from corlinman_agent.events import (
    EventEnvelope,
    MockEventEmitter,
    ToolStateCompleted,
    ToolStateHeartbeat,
    ToolStateRunning,
)
from corlinman_server.runner_pool import (
    DispatchContext,
    dispatch_with_observability,
)

_TURN = "turn-abc-123"
_SESSION = "tg::42"


def _make_ctx(emitter: Any) -> DispatchContext:
    return DispatchContext(turn_id=_TURN, session_key=_SESSION, emitter=emitter)


def _events_of(emitter: MockEventEmitter, cls: type) -> list[Any]:
    return [env.event for env in emitter.envelopes if isinstance(env.event, cls)]


# ---------------------------------------------------------------------------
# ToolStateRunning — emitted BEFORE the invoke coroutine awaits.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_pool_emits_tool_state_running_before_dispatch() -> None:
    """The first envelope on the wire must be ``ToolStateRunning`` and
    it must arrive BEFORE the wrapped coroutine has produced any side
    effect."""
    emitter = MockEventEmitter()
    invoked_order: list[str] = []

    async def fake_invoke() -> str:
        # Snapshot whatever envelopes the emitter has at this point —
        # the dispatcher MUST have emitted Running by now.
        types_seen = [type(env.event).__name__ for env in emitter.envelopes]
        invoked_order.append("|".join(types_seen))
        return "ok"

    result = await dispatch_with_observability(
        _make_ctx(emitter),
        tool_call_id="c1",
        tool_name="run_shell",
        args_json=b'{"cmd": "ls"}',
        invoke=fake_invoke,
    )

    assert result == "ok"
    # The invoker observed exactly one prior envelope: ToolStateRunning.
    assert invoked_order == ["ToolStateRunning"]
    # And the emitter's full list now has Running then Completed.
    assert emitter.event_types == ["ToolStateRunning", "ToolStateCompleted"]
    running = _events_of(emitter, ToolStateRunning)
    assert running[0].tool_call_id == "c1"
    assert running[0].tool_name == "run_shell"
    assert running[0].args_json == '{"cmd": "ls"}'
    assert running[0].started_at_ms > 0


# ---------------------------------------------------------------------------
# ToolStateCompleted — elapsed_ms is per-call, populated from monotonic
# clock measured around the invoke coroutine.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_pool_emits_tool_state_completed_with_correct_elapsed_ms() -> None:
    """``elapsed_ms`` on the Completed event must reflect the duration
    of the wrapped coroutine, NOT include the heartbeat-loop scheduling
    overhead."""
    emitter = MockEventEmitter()

    async def fake_invoke() -> str:
        await asyncio.sleep(0.05)  # 50ms — well under heartbeat (10s)
        return "result-body"

    await dispatch_with_observability(
        _make_ctx(emitter),
        tool_call_id="c2",
        tool_name="fast_tool",
        args_json="{}",
        invoke=fake_invoke,
    )
    completed = _events_of(emitter, ToolStateCompleted)
    assert len(completed) == 1
    assert completed[0].tool_call_id == "c2"
    assert completed[0].result_summary == "result-body"
    assert completed[0].is_error is False
    # 50ms ≤ elapsed ≤ 5s (very generous upper bound for CI variance).
    assert 30 <= completed[0].elapsed_ms <= 5000


# ---------------------------------------------------------------------------
# Heartbeat — fires every interval while the tool is running, cancels
# on completion. Use a short interval so the test runs in <1s rather
# than mocking time.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_pool_emits_heartbeat_every_interval_for_long_tool() -> None:
    """A 350ms tool with a 100ms heartbeat interval emits ≥3 heartbeats."""
    emitter = MockEventEmitter()

    async def slow_invoke() -> str:
        await asyncio.sleep(0.35)
        return "done"

    await dispatch_with_observability(
        _make_ctx(emitter),
        tool_call_id="c3",
        tool_name="long_tool",
        args_json="{}",
        invoke=slow_invoke,
        heartbeat_interval_s=0.1,
    )

    hbs = _events_of(emitter, ToolStateHeartbeat)
    assert len(hbs) >= 3, (
        f"expected >= 3 heartbeats for a 350ms tool at 100ms interval, "
        f"got {len(hbs)} events: {[h.elapsed_ms for h in hbs]}"
    )
    # All heartbeats share the right tool_call_id and have monotonically
    # increasing elapsed_ms.
    for hb in hbs:
        assert hb.tool_call_id == "c3"
        assert hb.stdout_tail is None
    elapsed = [hb.elapsed_ms for hb in hbs]
    assert elapsed == sorted(elapsed)


@pytest.mark.asyncio
async def test_runner_pool_no_heartbeat_for_fast_tool() -> None:
    """A tool that finishes well before the heartbeat interval emits
    zero ``ToolStateHeartbeat`` envelopes."""
    emitter = MockEventEmitter()

    async def fast_invoke() -> str:
        await asyncio.sleep(0.01)
        return "fast"

    await dispatch_with_observability(
        _make_ctx(emitter),
        tool_call_id="c4",
        tool_name="fast_tool",
        args_json="{}",
        invoke=fast_invoke,
        heartbeat_interval_s=10.0,
    )
    hbs = _events_of(emitter, ToolStateHeartbeat)
    assert hbs == []


@pytest.mark.asyncio
async def test_runner_pool_heartbeat_cancelled_on_completion() -> None:
    """No pending heartbeat task should leak after a dispatch.

    Check by sampling ``asyncio.all_tasks()`` after the dispatch
    returns: no task should match the heartbeat name pattern.
    """
    emitter = MockEventEmitter()

    async def invoke() -> str:
        await asyncio.sleep(0.05)
        return "ok"

    await dispatch_with_observability(
        _make_ctx(emitter),
        tool_call_id="c5",
        tool_name="t",
        args_json="{}",
        invoke=invoke,
        heartbeat_interval_s=0.01,
    )
    # Yield once so any pending cancellation gets a chance to drain.
    await asyncio.sleep(0)
    leaked = [
        t for t in asyncio.all_tasks()
        if "runner_pool.heartbeat.c5" in (t.get_name() or "")
        and not t.done()
    ]
    assert not leaked, f"heartbeat task leaked: {leaked}"


# ---------------------------------------------------------------------------
# No-emitter path — the helper degrades to a pass-through.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_pool_no_emitter_no_emit() -> None:
    """When ``DispatchContext.emitter`` is ``None`` the wrapped
    coroutine still runs and its return value is forwarded — and
    nothing crashes."""
    invocations: list[str] = []

    async def invoke() -> str:
        invocations.append("ran")
        return "result-without-emitter"

    ctx = DispatchContext(
        turn_id="anything", session_key="anything", emitter=None
    )
    result = await dispatch_with_observability(
        ctx,
        tool_call_id="c6",
        tool_name="t",
        args_json="{}",
        invoke=invoke,
    )
    assert result == "result-without-emitter"
    assert invocations == ["ran"]


# ---------------------------------------------------------------------------
# Error path — exception in invoke must still emit ToolStateCompleted
# with is_error=True before re-raising.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_pool_emits_completed_with_is_error_on_exception() -> None:
    """When the wrapped coroutine raises, the dispatcher must still
    emit a ``ToolStateCompleted`` envelope (with ``is_error=True``)
    before propagating the exception."""
    emitter = MockEventEmitter()

    class BoomError(RuntimeError):
        pass

    async def boom() -> str:
        raise BoomError("kaboom")

    with pytest.raises(BoomError):
        await dispatch_with_observability(
            _make_ctx(emitter),
            tool_call_id="c7",
            tool_name="t",
            args_json="{}",
            invoke=boom,
        )

    completed = _events_of(emitter, ToolStateCompleted)
    assert len(completed) == 1
    assert completed[0].is_error is True
    assert "kaboom" in completed[0].result_summary


# ---------------------------------------------------------------------------
# Summariser path — `summarise_result` controls `result_summary` /
# `is_error` so a JSON error envelope is reported correctly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_pool_summarise_result_sets_is_error_flag() -> None:
    """When the summariser flags the result as an error, the Completed
    envelope's ``is_error`` field must reflect that."""
    emitter = MockEventEmitter()

    async def invoke() -> str:
        return '{"error": "no_such_path", "tool": "read_file"}'

    def summarise(r: str) -> tuple[str, bool]:
        # Same shape as the in-process builtin path in agent_servicer.
        import json
        try:
            parsed = json.loads(r or "{}")
            is_err = isinstance(parsed, dict) and bool(parsed.get("error"))
        except (json.JSONDecodeError, TypeError, ValueError):
            is_err = False
        return r, is_err

    await dispatch_with_observability(
        _make_ctx(emitter),
        tool_call_id="c8",
        tool_name="read_file",
        args_json="{}",
        invoke=invoke,
        summarise_result=summarise,
    )

    completed = _events_of(emitter, ToolStateCompleted)
    assert len(completed) == 1
    assert completed[0].is_error is True
    assert "no_such_path" in completed[0].result_summary


# ---------------------------------------------------------------------------
# Envelope correlation — every envelope shares the same turn_id /
# session_key and has a strictly-monotonic sequence.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_pool_envelope_correlation_and_sequence() -> None:
    """Sanity: turn_id / session_key propagate to every envelope, and
    sequences are strictly monotonic per turn."""
    emitter = MockEventEmitter()

    async def invoke() -> str:
        await asyncio.sleep(0.05)
        return "ok"

    await dispatch_with_observability(
        _make_ctx(emitter),
        tool_call_id="c9",
        tool_name="t",
        args_json="{}",
        invoke=invoke,
        heartbeat_interval_s=0.02,
    )

    envs: list[EventEnvelope] = emitter.envelopes
    assert envs, "dispatcher should have emitted at least Running + Completed"
    assert all(e.turn_id == _TURN for e in envs)
    assert all(e.session_key == _SESSION for e in envs)
    sequences = [e.sequence for e in envs]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
