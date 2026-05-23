"""Push-based subscriber surface on :class:`HookBus`.

Covers the ``subscribe(predicate, subscriber)`` / ``unsubscribe(token)`` /
``emit(event)`` path that sits next to the existing tokio-broadcast-style
``subscribe(priority) -> HookSubscription`` pull surface. The two are
exercised side-by-side in ``test_bus.py``; this module focuses on the
push semantics:

- predicate-matched delivery,
- per-subscriber exception isolation (one raising sub does not block
  the others or the producer),
- idempotent unsubscribe,
- sync subscribers (those that return ``None`` instead of an awaitable),
- the ``match_kind`` convenience predicate.
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_hooks import (
    HookBus,
    HookEvent,
    SubscriptionToken,
    match_kind,
)


async def test_subscribers_receive_matching_events() -> None:
    """A predicate-matched subscriber observes every matching emit."""
    bus = HookBus()
    seen: list[HookEvent] = []

    bus.subscribe(match_kind("ToolCalled"), lambda ev: seen.append(ev))

    await bus.emit(
        HookEvent.ToolCalled(
            tool="calculator",
            runner_id="builtin",
            duration_ms=4,
            ok=True,
            error_code=None,
        )
    )
    # A non-matching event must not be delivered.
    await bus.emit(HookEvent.GatewayStartup(version="v1"))

    assert len(seen) == 1, f"expected exactly one ToolCalled, got {seen!r}"
    assert seen[0].kind() == "tool_called"
    assert seen[0].tool == "calculator"


async def test_subscriber_exception_isolated() -> None:
    """One subscriber raising must not block the other subscribers."""
    bus = HookBus()
    delivered: list[str] = []

    def angry(_ev: HookEvent) -> None:
        raise RuntimeError("boom")

    def happy(ev: HookEvent) -> None:
        delivered.append(ev.kind())

    bus.subscribe(match_kind("GatewayStartup"), angry)
    bus.subscribe(match_kind("GatewayStartup"), happy)

    # The producer must not see the subscriber's RuntimeError.
    await bus.emit(HookEvent.GatewayStartup(version="v1"))
    await bus.emit(HookEvent.GatewayStartup(version="v2"))

    assert delivered == ["gateway_startup", "gateway_startup"], (
        f"healthy subscriber must have received both events: {delivered!r}"
    )


async def test_unsubscribe_stops_delivery() -> None:
    """After ``unsubscribe(token)`` the subscriber sees no further events.

    Also asserts that calling ``unsubscribe`` again is a silent no-op
    so shutdown paths can detach unconditionally.
    """
    bus = HookBus()
    hits: list[str] = []

    token = bus.subscribe(match_kind("GatewayStartup"), lambda ev: hits.append(ev.kind()))
    assert isinstance(token, SubscriptionToken)

    await bus.emit(HookEvent.GatewayStartup(version="v1"))
    assert hits == ["gateway_startup"], "first emit should reach the subscriber"

    bus.unsubscribe(token)
    await bus.emit(HookEvent.GatewayStartup(version="v2"))
    assert hits == ["gateway_startup"], (
        f"post-unsubscribe emit must not reach the subscriber: {hits!r}"
    )

    # Idempotent: re-detaching the same token does not raise.
    bus.unsubscribe(token)


async def test_sync_subscriber_also_works() -> None:
    """A subscriber that returns ``None`` (not an awaitable) completes cleanly."""
    bus = HookBus()
    sync_hits: list[str] = []
    async_hits: list[str] = []

    async def coro_subscriber(ev: HookEvent) -> None:
        # Force an event loop yield so the async path is exercised.
        await asyncio.sleep(0)
        async_hits.append(ev.kind())

    bus.subscribe(match_kind("GatewayStartup"), lambda ev: sync_hits.append(ev.kind()))
    bus.subscribe(match_kind("GatewayStartup"), coro_subscriber)

    await bus.emit(HookEvent.GatewayStartup(version="v1"))

    assert sync_hits == ["gateway_startup"], (
        f"sync subscriber missed the event: {sync_hits!r}"
    )
    assert async_hits == ["gateway_startup"], (
        f"async subscriber missed the event: {async_hits!r}"
    )


async def test_subscriber_count_reports_live_callable_subs() -> None:
    bus = HookBus()
    assert bus.subscriber_count() == 0

    t1 = bus.subscribe(match_kind("GatewayStartup"), lambda _ev: None)
    t2 = bus.subscribe(lambda _ev: True, lambda _ev: None)
    assert bus.subscriber_count() == 2

    bus.unsubscribe(t1)
    assert bus.subscriber_count() == 1

    bus.unsubscribe(t2)
    assert bus.subscriber_count() == 0


async def test_match_kind_accepts_pascalcase_and_snakecase() -> None:
    """``match_kind`` is permissive — callers can use either form."""
    bus = HookBus()
    snake_hits: list[str] = []
    pascal_hits: list[str] = []

    bus.subscribe(match_kind("tool_called"), lambda ev: snake_hits.append(ev.kind()))
    bus.subscribe(match_kind("ToolCalled"), lambda ev: pascal_hits.append(ev.kind()))

    await bus.emit(
        HookEvent.ToolCalled(
            tool="calc",
            runner_id="builtin",
            duration_ms=0,
            ok=True,
            error_code=None,
        )
    )

    assert snake_hits == ["tool_called"]
    assert pascal_hits == ["tool_called"]


async def test_predicate_raise_isolated_from_emit() -> None:
    """A predicate that raises must not break the bus for other subs."""
    bus = HookBus()
    delivered: list[str] = []

    def bad_predicate(_ev: HookEvent) -> bool:
        raise RuntimeError("predicate boom")

    bus.subscribe(bad_predicate, lambda _ev: None)  # will be skipped
    bus.subscribe(match_kind("GatewayStartup"), lambda ev: delivered.append(ev.kind()))

    await bus.emit(HookEvent.GatewayStartup(version="v1"))

    assert delivered == ["gateway_startup"], (
        "healthy subscriber must still receive the event when another's "
        f"predicate raised: {delivered!r}"
    )


def test_subscribe_rejects_bad_argument_shapes() -> None:
    """``subscribe`` is overloaded; invalid combos must raise ``TypeError``."""
    bus = HookBus()

    with pytest.raises(TypeError):
        bus.subscribe("not-a-priority")  # type: ignore[arg-type]

    # Predicate without subscriber callable.
    with pytest.raises(TypeError):
        bus.subscribe(lambda _ev: True)


def test_unsubscribe_with_non_token_is_noop() -> None:
    bus = HookBus()
    # Should not raise.
    bus.unsubscribe("not-a-token")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Lifecycle event variants (UserPromptSubmit / TurnComplete / TurnErrored).
# ---------------------------------------------------------------------------


def test_user_prompt_submit_round_trips() -> None:
    ev = HookEvent.UserPromptSubmit(
        session_key_="qq:group:1:u1",
        user_text="hello there",
        model="claude-sonnet-4-5",
    )
    assert ev.kind() == "user_prompt_submit"
    assert ev.session_key() == "qq:group:1:u1"

    raw = ev.to_json()
    assert '"kind":"UserPromptSubmit"' in raw
    assert '"session_key":"qq:group:1:u1"' in raw
    back = HookEvent.from_json(raw)
    assert isinstance(back, HookEvent.UserPromptSubmit)
    assert back.user_text == "hello there"
    assert back.model == "claude-sonnet-4-5"


def test_turn_complete_round_trips_with_usage() -> None:
    ev = HookEvent.TurnComplete(
        session_key_="s1",
        turn_id=42,
        finish_reason="stop",
        usage={"prompt_tokens": 17, "completion_tokens": 8},
        duration_ms=1234,
    )
    assert ev.kind() == "turn_complete"
    assert ev.session_key() == "s1"
    d = ev.to_dict()
    assert d["usage"] == {"prompt_tokens": 17, "completion_tokens": 8}
    assert d["turn_id"] == 42

    back = HookEvent.from_json(ev.to_json())
    assert isinstance(back, HookEvent.TurnComplete)
    assert back.finish_reason == "stop"
    assert back.duration_ms == 1234


def test_turn_complete_skips_none_optionals() -> None:
    ev = HookEvent.TurnComplete(
        session_key_="s1",
        turn_id=None,
        finish_reason="stop",
        usage=None,
        duration_ms=0,
    )
    d = ev.to_dict()
    # Both optional fields drop out of the wire payload when ``None``.
    assert "turn_id" not in d
    assert "usage" not in d


def test_turn_errored_round_trips() -> None:
    ev = HookEvent.TurnErrored(
        session_key_="s1",
        turn_id=7,
        reason="model_not_found",
        message="no such alias",
    )
    assert ev.kind() == "turn_errored"
    assert ev.session_key() == "s1"
    back = HookEvent.from_json(ev.to_json())
    assert isinstance(back, HookEvent.TurnErrored)
    assert back.reason == "model_not_found"
    assert back.message == "no such alias"
