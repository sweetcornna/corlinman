"""Tests for ``corlinman_channels.service._consume_observability_events``.

W4.1 — the consumer task subscribes to the gateway's
:class:`JournalBackedEmitter` and surfaces tool heartbeats / cancel
events on the channel's :class:`MutableSpinner`, plus stashes
``TurnComplete`` cost data into the per-turn ``_FooterState``.

Uses a hand-rolled fake emitter that quacks like the real one (it owns
an :class:`asyncio.Queue` per subscriber and exposes
``await subscribe(session_key) -> (queue, unsubscribe)``) so the test
suite stays decoupled from the gateway package's actual emitter
implementation. The :class:`corlinman_agent.events` types are imported
directly — those are the real wire dataclasses the production emitter
hands the consumer.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from corlinman_agent.events import (
    Cancelling,
    EventEnvelope,
    ToolStateCompleted,
    ToolStateHeartbeat,
    ToolStateRunning,
    TurnComplete,
)

from corlinman_channels._status import MutableSpinner
from corlinman_channels.service import (
    _consume_observability_events,
    _FooterState,
)


class _FakeEmitter:
    """Minimal stand-in for the gateway's ``JournalBackedEmitter``.

    Implements just the surface the consumer task uses: ``subscribe()``
    returning a ``(queue, unsubscribe)`` pair. The test pushes
    pre-built :class:`EventEnvelope` instances onto ``queue`` directly to
    drive the consumer.
    """

    def __init__(self) -> None:
        self.queues: list[asyncio.Queue[EventEnvelope]] = []
        self.unsubscribed: int = 0

    async def subscribe(
        self,
        session_key: str,
    ) -> tuple[asyncio.Queue[EventEnvelope], Any]:
        # session_key irrelevant for these unit tests — they exercise the
        # consumer's branching, not the emitter's fan-out.
        del session_key
        q: asyncio.Queue[EventEnvelope] = asyncio.Queue()
        self.queues.append(q)

        async def _unsubscribe() -> None:
            self.unsubscribed += 1

        return q, _unsubscribe


def _envelope(
    event: Any,
    *,
    sequence: int = 0,
    turn_id: str = "t1",
    session_key: str = "s1",
) -> EventEnvelope:
    """Build an :class:`EventEnvelope` wrapping ``event`` for queueing."""
    return EventEnvelope(
        turn_id=turn_id,
        session_key=session_key,
        sequence=sequence,
        timestamp_ms=0,
        event=event,
    )


class _NoopEditCallback:
    """Captures every ``edit(text)`` call so the test can assert on the
    spinner's visible state without spinning up a real channel sender."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, text: str) -> None:
        self.calls.append(text)


@pytest.mark.asyncio
async def test_consumer_handles_tool_heartbeat() -> None:
    """A ``ToolStateRunning`` followed by a heartbeat with the same
    ``tool_call_id`` must call ``spinner.on_tool_heartbeat`` with the
    correct tool name + elapsed_ms — pending_tools must thread the name
    forward across the two events."""
    emitter = _FakeEmitter()
    edit_cb = _NoopEditCallback()
    spinner = MutableSpinner(edit_cb)
    footer_state = _FooterState()

    # Seed the spinner with an in-progress op-flow line (the heartbeat
    # branch is suppressed when no op is showing — see the spinner
    # docstring for ``on_tool_heartbeat``).
    from types import SimpleNamespace
    await spinner.on_tool_call(
        SimpleNamespace(tool="run_shell", args_json=b'{"command":"sleep 60"}')
    )
    prior = list(edit_cb.calls)

    task = asyncio.create_task(
        _consume_observability_events(emitter, "s1", spinner, footer_state)
    )
    try:
        # Wait until subscribe() has handed back its queue.
        for _ in range(50):
            if emitter.queues:
                break
            await asyncio.sleep(0)
        assert emitter.queues, "consumer never subscribed"
        q = emitter.queues[0]

        await q.put(_envelope(ToolStateRunning(
            tool_call_id="call-1",
            tool_name="run_shell",
            args_json="{}",
            started_at_ms=0,
        )))
        await q.put(_envelope(ToolStateHeartbeat(
            tool_call_id="call-1",
            elapsed_ms=15_000,
        ), sequence=1))

        # Give the consumer a couple of event-loop cycles to drain.
        for _ in range(50):
            if any("15s" in c for c in edit_cb.calls[len(prior):]):
                break
            await asyncio.sleep(0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The latest edit should be the heartbeat line.
    new_calls = edit_cb.calls[len(prior):]
    assert new_calls, "consumer never edited the spinner"
    assert new_calls[-1] == "🔧 run_shell … 15s"


@pytest.mark.asyncio
async def test_consumer_handles_cancelling() -> None:
    """A ``Cancelling`` envelope must flip the spinner to
    ``STATUS_CANCELLING`` via ``spinner.on_cancelling()``."""
    emitter = _FakeEmitter()
    edit_cb = _NoopEditCallback()
    spinner = MutableSpinner(edit_cb)
    footer_state = _FooterState()

    task = asyncio.create_task(
        _consume_observability_events(emitter, "s1", spinner, footer_state)
    )
    try:
        for _ in range(50):
            if emitter.queues:
                break
            await asyncio.sleep(0)
        q = emitter.queues[0]
        await q.put(_envelope(Cancelling(reason="user")))
        for _ in range(50):
            if any("⏹" in c for c in edit_cb.calls):
                break
            await asyncio.sleep(0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The cancellation line must have been edited at least once.
    assert any("⏹ 正在取消…" == c for c in edit_cb.calls)


@pytest.mark.asyncio
async def test_consumer_skips_when_emitter_none() -> None:
    """``emitter=None`` is the migration / test-only path. The consumer
    must return immediately without raising — and certainly without
    blocking on a non-existent queue."""
    spinner = MutableSpinner(_NoopEditCallback())
    footer_state = _FooterState()
    # ``asyncio.wait_for`` with a tight deadline catches a regression
    # that would hang the coroutine.
    await asyncio.wait_for(
        _consume_observability_events(None, "s1", spinner, footer_state),
        timeout=0.5,
    )
    # State must be untouched — the consumer must be a true no-op.
    assert footer_state.populated is False
    assert footer_state.elapsed_ms == 0


@pytest.mark.asyncio
async def test_consumer_stashes_turn_complete_into_footer_state() -> None:
    """``TurnComplete`` envelope must populate ``footer_state`` with
    elapsed / cost / cost_status so the channel adapter can render the
    post-turn footer. ``ToolStateCompleted`` events increment
    ``tool_call_count`` for the same reason."""
    emitter = _FakeEmitter()
    edit_cb = _NoopEditCallback()
    spinner = MutableSpinner(edit_cb)
    footer_state = _FooterState()

    task = asyncio.create_task(
        _consume_observability_events(emitter, "s1", spinner, footer_state)
    )
    try:
        for _ in range(50):
            if emitter.queues:
                break
            await asyncio.sleep(0)
        q = emitter.queues[0]
        await q.put(_envelope(ToolStateRunning(
            tool_call_id="c1",
            tool_name="read_file",
            args_json="{}",
            started_at_ms=0,
        )))
        await q.put(_envelope(ToolStateCompleted(
            tool_call_id="c1",
            result_summary="ok",
            elapsed_ms=200,
            is_error=False,
        ), sequence=1))
        await q.put(_envelope(TurnComplete(
            finish_reason="stop",
            usage={"input_tokens": 100, "output_tokens": 50},
            elapsed_ms=8_500,
            estimated_cost_usd=0.0042,
            cost_status="estimated",
        ), sequence=2))

        for _ in range(50):
            if footer_state.populated:
                break
            await asyncio.sleep(0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert footer_state.populated is True
    assert footer_state.elapsed_ms == 8_500
    assert footer_state.estimated_cost_usd == pytest.approx(0.0042)
    assert footer_state.cost_status == "estimated"
    assert footer_state.tool_call_count == 1


@pytest.mark.asyncio
async def test_consumer_unsubscribes_on_cancel() -> None:
    """The consumer's ``finally`` must always call ``unsubscribe`` —
    otherwise a long-lived channel would leak subscriber queues on the
    emitter side. Asserts the fake emitter's unsubscribe counter ticks
    when the consumer is cancelled mid-flight."""
    emitter = _FakeEmitter()
    spinner = MutableSpinner(_NoopEditCallback())
    footer_state = _FooterState()

    task = asyncio.create_task(
        _consume_observability_events(emitter, "s1", spinner, footer_state)
    )
    try:
        for _ in range(50):
            if emitter.queues:
                break
            await asyncio.sleep(0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert emitter.unsubscribed == 1


@pytest.mark.asyncio
async def test_consumer_ignores_heartbeat_without_matching_running() -> None:
    """A heartbeat for an unknown ``tool_call_id`` must NOT fire
    ``spinner.on_tool_heartbeat`` — pending_tools is the gate, so an
    out-of-order or duplicated heartbeat can't surface a fake tool
    name."""
    emitter = _FakeEmitter()
    edit_cb = _NoopEditCallback()
    spinner = MutableSpinner(edit_cb)
    footer_state = _FooterState()

    task = asyncio.create_task(
        _consume_observability_events(emitter, "s1", spinner, footer_state)
    )
    try:
        for _ in range(50):
            if emitter.queues:
                break
            await asyncio.sleep(0)
        q = emitter.queues[0]
        await q.put(_envelope(ToolStateHeartbeat(
            tool_call_id="unknown",
            elapsed_ms=5_000,
        )))
        # Give the consumer a few cycles to drain.
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # No edit fired — the unknown id was dropped silently.
    assert edit_cb.calls == []
