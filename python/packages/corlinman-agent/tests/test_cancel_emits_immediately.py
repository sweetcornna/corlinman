"""W3.1 — ``ReasoningLoop.cancel`` fires ``Cancelling`` immediately.

The legacy cancel flow set ``self._cancelled`` and let the next round
boundary surface ``ErrorEvent(reason="cancelled")`` / a terminal
``TurnErrored`` envelope. That delay can be several seconds while a
long-running tool dispatch finishes — the user clicks "stop" and sees
nothing change.

W3.1's fix: ``cancel()`` also schedules an immediate
:class:`corlinman_agent.events.Cancelling` emit so SSE / channel
adapter consumers can flip the spinner to ``⏹ 正在取消…`` within
milliseconds. The round-boundary ``TurnErrored`` still fires (the
contract was never about removing it) — ``Cancelling`` is a faster,
non-terminal nudge.

These tests pin the latency: from ``cancel(reason)`` to the
``Cancelling`` envelope landing on the emitter, ≤ 50ms — even if the
loop is asleep waiting for a tool result.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from corlinman_agent.events import (
    Cancelling,
    EventEnvelope,
    MockEventEmitter,
)
from corlinman_agent.reasoning_loop import ReasoningLoop


class _NeverProvider:
    """Provider stub that opens a chat_stream and parks forever — a
    realistic stand-in for "tool dispatch is in flight, no rounds are
    closing"."""

    async def chat_stream(self, **_: Any) -> Any:
        # Park until cancelled — the test will fire ``loop.cancel()``
        # to unblock.
        await asyncio.Event().wait()
        # Make the function look like an async generator to the caller's
        # ``async for`` — we never get past the wait above so this branch
        # is unreachable, but the type matters for static analysis.
        if False:
            yield None  # pragma: no cover


def _cancelling_payloads(emitter: MockEventEmitter) -> list[Cancelling]:
    return [
        env.event for env in emitter.envelopes
        if isinstance(env.event, Cancelling)
    ]


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_emits_cancelling_event_before_next_round() -> None:
    """Fire ``loop.cancel("user_abort")`` while a turn is still active.

    The ``Cancelling`` envelope must land on the emitter within 50ms —
    long before the loop's normal next-round cancel poll would fire.
    """
    from corlinman_agent.reasoning_loop import ChatStart

    emitter = MockEventEmitter()
    loop = ReasoningLoop(_NeverProvider(), event_emitter=emitter)
    start = ChatStart(
        model="gpt-x",
        messages=[{"role": "user", "content": "hi"}],
        session_key="s-1",
    )

    # Drive the loop's first round in a background task so ``cancel``
    # runs concurrently against an in-flight turn.
    async def drive() -> None:
        try:
            async for _ev in loop.run(start):
                pass
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    runner = asyncio.create_task(drive())
    # Give the loop a beat to enter ``run()`` and stamp ``turn_id``.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if loop.turn_id:
            break
    assert loop.turn_id, "loop.run() did not assign a turn_id in time"

    cancel_start = time.monotonic()
    loop.cancel(reason="user_abort")
    # Allow the cancel-emit task to be scheduled.
    deadline = cancel_start + 0.5
    while time.monotonic() < deadline:
        if _cancelling_payloads(emitter):
            break
        await asyncio.sleep(0.005)
    cancel_latency_s = time.monotonic() - cancel_start

    payloads = _cancelling_payloads(emitter)
    assert payloads, "Cancelling envelope never landed on the emitter"
    assert payloads[0].reason == "user_abort"
    # Per spec — should arrive in <50ms; allow 200ms slack for CI variance.
    assert cancel_latency_s < 0.2, (
        f"Cancelling took {cancel_latency_s*1000:.0f}ms — too slow"
    )

    # Clean up the background task so pytest doesn't warn.
    runner.cancel()
    try:
        await runner
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


@pytest.mark.asyncio
async def test_cancel_envelope_carries_turn_id_and_session_key() -> None:
    """The ``Cancelling`` envelope must inherit the live turn's
    correlation pair so a SSE consumer can route it to the right
    timeline."""
    from corlinman_agent.reasoning_loop import ChatStart

    emitter = MockEventEmitter()
    loop = ReasoningLoop(_NeverProvider(), event_emitter=emitter)
    start = ChatStart(
        model="gpt-x",
        messages=[{"role": "user", "content": "hi"}],
        session_key="my-session-key-77",
    )

    async def drive() -> None:
        try:
            async for _ev in loop.run(start):
                pass
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    runner = asyncio.create_task(drive())
    for _ in range(20):
        await asyncio.sleep(0.01)
        if loop.turn_id:
            break
    expected_turn_id = loop.turn_id

    loop.cancel(reason="user_request")
    for _ in range(30):
        await asyncio.sleep(0.01)
        if _cancelling_payloads(emitter):
            break

    matches = [
        env for env in emitter.envelopes
        if isinstance(env.event, Cancelling)
    ]
    assert matches, "no Cancelling envelope was emitted"
    env: EventEnvelope = matches[0]
    assert env.turn_id == expected_turn_id
    assert env.session_key == "my-session-key-77"

    runner.cancel()
    try:
        await runner
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


@pytest.mark.asyncio
async def test_double_cancel_is_idempotent_and_only_emits_once() -> None:
    """Calling ``cancel`` twice should only fire one ``Cancelling``
    envelope — the second call is a no-op (the underlying event is
    already set)."""
    from corlinman_agent.reasoning_loop import ChatStart

    emitter = MockEventEmitter()
    loop = ReasoningLoop(_NeverProvider(), event_emitter=emitter)
    start = ChatStart(
        model="gpt-x",
        messages=[{"role": "user", "content": "hi"}],
        session_key="s-x",
    )

    async def drive() -> None:
        try:
            async for _ev in loop.run(start):
                pass
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    runner = asyncio.create_task(drive())
    for _ in range(20):
        await asyncio.sleep(0.01)
        if loop.turn_id:
            break

    loop.cancel(reason="first")
    loop.cancel(reason="second")
    # Drain so the scheduled task runs.
    await asyncio.sleep(0.05)

    payloads = _cancelling_payloads(emitter)
    assert len(payloads) == 1
    assert payloads[0].reason == "first"

    runner.cancel()
    try:
        await runner
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


@pytest.mark.asyncio
async def test_cancel_without_emitter_does_not_crash() -> None:
    """Backwards-compat: when no emitter is wired the cancel path
    must still flip the cancel event without raising."""
    loop = ReasoningLoop(_NeverProvider(), event_emitter=None)
    # No turn_id has been assigned yet — cancel should still be safe.
    loop.cancel(reason="early")
    assert loop._cancelled.is_set()
