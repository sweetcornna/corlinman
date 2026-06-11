"""BrainSession window semantics against a scripted fake brain."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from corlinman_server.console.brain import BrainSession, new_session_key
from corlinman_server.console.events import (
    ConsoleEvent,
    TextDelta,
    TurnDone,
    TurnError,
)


class ScriptedBrain:
    """Yields a canned event list per turn; records what it was sent."""

    descriptor = "scripted"

    def __init__(self, script: list[list[ConsoleEvent]]) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    def run_turn(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        session_key: str,
        cancel: asyncio.Event,
    ) -> AsyncIterator[ConsoleEvent]:
        self.calls.append(
            {
                "model": model,
                "messages": [dict(m) for m in messages],
                "session_key": session_key,
            }
        )
        events = self._script.pop(0)

        async def _gen() -> AsyncIterator[ConsoleEvent]:
            for ev in events:
                yield ev

        return _gen()

    async def aclose(self) -> None:  # pragma: no cover - protocol filler
        pass


async def test_clean_turn_commits_user_and_assistant() -> None:
    brain = ScriptedBrain(
        [[TextDelta(text="4"), TurnDone(finish_reason="stop", total_tokens=7)]]
    )
    session = BrainSession(brain=brain, model="m")
    events = [ev async for ev in session.send("2+2?")]
    assert isinstance(events[-1], TurnDone)
    assert session.window == [
        {"role": "user", "content": "2+2?"},
        {"role": "assistant", "content": "4"},
    ]
    assert session.stats.turns == 1
    assert session.stats.total_tokens == 7


async def test_errored_turn_keeps_user_message_only() -> None:
    brain = ScriptedBrain([[TurnError(reason="rate_limit", message="429")]])
    session = BrainSession(brain=brain, model="m")
    _ = [ev async for ev in session.send("hi")]
    assert session.window == [{"role": "user", "content": "hi"}]
    assert session.stats.turns == 0


async def test_window_replayed_on_next_turn() -> None:
    brain = ScriptedBrain(
        [
            [TextDelta(text="a"), TurnDone()],
            [TextDelta(text="b"), TurnDone()],
        ]
    )
    session = BrainSession(brain=brain, model="m")
    _ = [ev async for ev in session.send("one")]
    _ = [ev async for ev in session.send("two")]
    sent = brain.calls[1]["messages"]
    assert sent == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "two"},
    ]


async def test_system_prompt_prepended_not_committed() -> None:
    brain = ScriptedBrain([[TextDelta(text="x"), TurnDone()]])
    session = BrainSession(brain=brain, model="m", system_prompt="be brief")
    _ = [ev async for ev in session.send("q")]
    assert brain.calls[0]["messages"][0] == {"role": "system", "content": "be brief"}
    assert all(m["role"] != "system" for m in session.window)


async def test_per_turn_model_override() -> None:
    brain = ScriptedBrain([[TurnDone()]])
    session = BrainSession(brain=brain, model="big")
    _ = [ev async for ev in session.send("q", model="small")]
    assert brain.calls[0]["model"] == "small"


async def test_reset_clears_window_and_rotates_key() -> None:
    brain = ScriptedBrain([[TextDelta(text="x"), TurnDone()]])
    session = BrainSession(brain=brain, model="m")
    _ = [ev async for ev in session.send("q")]
    old_key = session.session_key
    session.reset()
    assert session.window == []
    assert session.session_key != old_key


def test_new_session_key_is_namespaced_and_unique() -> None:
    a, b = new_session_key(), new_session_key()
    assert a.startswith("console:") and b.startswith("console:")
    assert a != b


async def test_cancel_turn_only_when_busy() -> None:
    brain = ScriptedBrain([[TurnDone()]])
    session = BrainSession(brain=brain, model="m")
    assert session.cancel_turn() is False  # idle — nothing to cancel
    _ = [ev async for ev in session.send("q")]
    assert session.busy is False
