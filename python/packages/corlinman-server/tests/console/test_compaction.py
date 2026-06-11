"""Compaction semantics against a scripted fake brain."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

from corlinman_server.console.brain import BrainSession
from corlinman_server.console.commands import dispatch
from corlinman_server.console.compaction import (
    Compactor,
    estimate_tokens,
    maybe_auto_compact,
)
from corlinman_server.console.events import (
    ConsoleEvent,
    TextDelta,
    TurnDone,
    TurnError,
)
from corlinman_server.console.router import ModelRouter


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


def _window(n: int, *, chars: int = 40) -> list[dict[str, str]]:
    """n alternating user/assistant messages, each `chars` chars long."""
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"m{i}:".ljust(chars, "x"),
        }
        for i in range(n)
    ]


def test_estimate_tokens_chars_over_four() -> None:
    assert estimate_tokens([]) == 0
    assert estimate_tokens([{"role": "user", "content": "x" * 40}]) == 10
    # Sums across messages; role text does not count.
    msgs = [
        {"role": "user", "content": "a" * 100},
        {"role": "assistant", "content": "b" * 100},
    ]
    assert estimate_tokens(msgs) == 50


async def test_manual_compact_replaces_head_keeps_protected_tail() -> None:
    brain = ScriptedBrain([[TextDelta(text="the summary"), TurnDone()]])
    session = BrainSession(brain=brain, model="big")
    session.window.extend(_window(10))
    tail_before = [dict(m) for m in session.window[-6:]]

    compactor = Compactor(keep_recent=6)
    result = await compactor.compact(session, model="tiny")

    assert result.ok is True
    assert result.before_tokens > result.after_tokens
    # Window = summary pair + protected tail, in order.
    assert session.window[0]["role"] == "user"
    assert session.window[0]["content"].startswith(
        "[Conversation summary — earlier turns were compacted]\n"
    )
    assert "the summary" in session.window[0]["content"]
    assert session.window[1] == {
        "role": "assistant",
        "content": "Understood — continuing from the summary.",
    }
    assert session.window[2:] == tail_before

    # Exactly one summarization turn, on the given (utility) model, NOT
    # via session.send — so the head transcript rode in a single user msg.
    assert len(brain.calls) == 1
    assert brain.calls[0]["model"] == "tiny"
    sent = brain.calls[0]["messages"]
    assert len(sent) == 1 and sent[0]["role"] == "user"
    assert "Summarize this conversation compactly" in sent[0]["content"]
    assert "m0:" in sent[0]["content"]  # head made it into the transcript
    assert "m9:" not in sent[0]["content"]  # protected tail did not


async def test_failed_summarization_leaves_window_intact() -> None:
    brain = ScriptedBrain([[TurnError(reason="rate_limit", message="429")]])
    session = BrainSession(brain=brain, model="big")
    session.window.extend(_window(10))
    snapshot = [dict(m) for m in session.window]

    result = await Compactor().compact(session, model="tiny")

    assert result.ok is False
    assert "429" in result.error
    assert result.after_tokens == result.before_tokens
    assert session.window == snapshot


async def test_empty_summary_counts_as_failure() -> None:
    brain = ScriptedBrain([[TurnDone()]])  # no TextDelta at all
    session = BrainSession(brain=brain, model="big")
    session.window.extend(_window(10))
    snapshot = [dict(m) for m in session.window]

    result = await Compactor().compact(session, model="tiny")
    assert result.ok is False
    assert session.window == snapshot


async def test_nothing_to_compact_when_window_fits_in_tail() -> None:
    brain = ScriptedBrain([])
    session = BrainSession(brain=brain, model="big")
    session.window.extend(_window(4))

    compactor = Compactor(keep_recent=6)
    result = await compactor.compact(session, model="tiny")

    assert result.ok is False
    assert brain.calls == []  # never called the model
    assert compactor.auto_compact_disabled is False  # no-op ≠ breaker failure


async def test_auto_trigger_fires_only_above_threshold() -> None:
    brain = ScriptedBrain([[TextDelta(text="s"), TurnDone()]])
    session = BrainSession(brain=brain, model="big")
    session.window.extend(_window(8, chars=40))  # 8*40/4 = 80 est. tokens
    session.compactor = Compactor(threshold_tokens=100, keep_recent=2)

    # At/below threshold → no attempt, no brain call.
    assert await maybe_auto_compact(session, "tiny") is None
    assert brain.calls == []

    # Push above the threshold → compacts.
    session.window.extend(_window(4, chars=40))  # now 120 est. tokens
    result = await maybe_auto_compact(session, "tiny")
    assert result is not None and result.ok is True
    assert len(brain.calls) == 1
    assert len(session.window) == 2 + 2  # summary pair + keep_recent tail


async def test_auto_trigger_noop_without_compactor_or_when_disabled() -> None:
    brain = ScriptedBrain([])
    session = BrainSession(brain=brain, model="big")
    session.window.extend(_window(50))
    assert await maybe_auto_compact(session, "tiny") is None  # no compactor

    session.compactor = Compactor(threshold_tokens=1, enabled=False)
    assert await maybe_auto_compact(session, "tiny") is None  # auto_compact=false
    assert brain.calls == []


async def test_circuit_breaker_disables_auto_after_three_failures() -> None:
    failures: list[list[ConsoleEvent]] = [
        [TurnError(reason="unknown", message=f"boom{i}")] for i in range(3)
    ]
    brain = ScriptedBrain([*failures, [TextDelta(text="ok"), TurnDone()]])
    session = BrainSession(brain=brain, model="big")
    session.window.extend(_window(20))
    compactor = Compactor(threshold_tokens=1, keep_recent=2)
    session.compactor = compactor

    for _ in range(3):
        result = await maybe_auto_compact(session, "tiny")
        assert result is not None and result.ok is False
    assert compactor.auto_compact_disabled is True

    # Breaker open: auto path is a no-op (no 4th brain call) …
    assert await maybe_auto_compact(session, "tiny") is None
    assert len(brain.calls) == 3

    # … but manual compact still runs, and success re-arms the breaker.
    result = await compactor.compact(session, model="tiny")
    assert result.ok is True
    assert compactor.auto_compact_disabled is False


async def test_from_config_reads_console_keys_with_defaults() -> None:
    c = Compactor.from_config(
        {
            "console": {
                "compact_threshold_tokens": 5000,
                "compact_keep_recent": 2,
                "auto_compact": False,
            }
        }
    )
    assert c.threshold_tokens == 5000
    assert c.keep_recent == 2
    assert c.enabled is False

    d = Compactor.from_config({})
    assert d.threshold_tokens == 150_000
    assert d.keep_recent == 6
    assert d.enabled is True

    # Garbage values fall back to defaults instead of raising.
    g = Compactor.from_config(
        {"console": {"compact_threshold_tokens": "lots", "compact_keep_recent": True}}
    )
    assert g.threshold_tokens == 150_000
    assert g.keep_recent == 6


async def test_slash_compact_command_reports_before_after() -> None:
    brain = ScriptedBrain([[TextDelta(text="brief"), TurnDone()]])
    session = BrainSession(brain=brain, model="big")
    session.window.extend(_window(10))
    session.compactor = Compactor(keep_recent=2)
    app = SimpleNamespace(
        session=session,
        router=ModelRouter(default_model="big", small_fast_model="tiny"),
    )

    reply = await dispatch(app, "/compact")

    assert reply is not None and "context compacted:" in reply
    assert brain.calls[0]["model"] == "tiny"  # utility model, not the big one
    assert len(session.window) == 4


async def test_slash_compact_command_reports_error_and_keeps_window() -> None:
    brain = ScriptedBrain([[TurnError(reason="billing", message="no credit")]])
    session = BrainSession(brain=brain, model="big")
    session.window.extend(_window(10))
    snapshot = [dict(m) for m in session.window]
    app = SimpleNamespace(
        session=session,
        router=ModelRouter(default_model="big", small_fast_model=None),
    )

    reply = await dispatch(app, "/compact")  # session.compactor unset → fallback

    assert reply is not None and reply.startswith("compact failed:")
    assert "no credit" in reply
    assert session.window == snapshot
