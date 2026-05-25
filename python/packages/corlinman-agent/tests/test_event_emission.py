"""Typed event emission — W1.1 acceptance tests for the reasoning loop.

Drives a deterministic mock-provider turn through :class:`ReasoningLoop`
with a wired :class:`MockEventEmitter`, then asserts:

1. Every event envelope arrives in the order the plan §1.1 taxonomy
   specifies (TurnStart → BlockStart(reasoning) → ReasoningDelta… →
   BlockStop → BlockStart(text) → TextDelta… → BlockStop →
   BlockStart(tool_use) → ToolInputDelta… → BlockStop →
   ToolStateRunning → ToolStateCompleted → … → BlockStart(text) →
   TextDelta → BlockStop → TurnComplete).
2. Every envelope carries a monotonic ``sequence``, a plausible
   ``timestamp_ms``, and the right ``event_type`` discriminator on
   ``to_json()``.
3. The legacy ``TokenEvent`` / ``ToolCallEvent`` / ``DoneEvent`` yields
   still fire — the W1.1 path is additive, not a replacement, so the
   existing channel adapters keep working.

We bypass :class:`ProviderChunk`'s ``Literal["token", ...]`` constraint
on the ``is_reasoning`` field via a thin subclass that opts the flag
in. Production providers will land the same opt-in once W4 adds an
``Anthropic thinking`` adapter; the loop's emission logic is forward-
compatible today.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from corlinman_agent import (
    ChatStart,
    DoneEvent,
    ReasoningLoop,
    TokenEvent,
    ToolCallEvent,
    ToolResult,
)
from corlinman_agent.events import (
    BlockStart,
    BlockStop,
    EventEnvelope,
    MockEventEmitter,
    ReasoningDelta,
    TextDelta,
    ToolInputDelta,
    ToolStateCompleted,
    ToolStateRunning,
    TurnComplete,
    TurnErrored,
    TurnStart,
)
from corlinman_providers.base import ProviderChunk


@dataclass(slots=True)
class _ReasoningChunk:
    """Drop-in :class:`ProviderChunk` look-alike that carries a
    ``is_reasoning`` opt-in flag.

    The reasoning loop dispatches on ``chunk.kind`` and reads
    ``getattr(chunk, "is_reasoning", False)`` — so any dataclass with
    matching attributes works, regardless of the strict
    :class:`ProviderChunk` ``Literal[ChunkKind]`` typing.
    """

    kind: str
    text: str | None = None
    is_reasoning: bool = False
    signature: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


class _ScriptedProvider:
    """Yields one preset list of chunks per ``chat_stream`` invocation.

    Records the message list every call saw so a test can confirm the
    second round received the tool messages it expected.
    """

    def __init__(self, rounds: list[list[Any]]) -> None:
        self._rounds = rounds
        self.calls_seen: list[list[dict[str, Any]]] = []

    async def chat_stream(
        self, *, messages: list[dict[str, Any]], **_: Any
    ) -> AsyncIterator[Any]:  # type: ignore[override]
        self.calls_seen.append(list(messages))
        idx = len(self.calls_seen) - 1
        if idx >= len(self._rounds):
            yield ProviderChunk(kind="done", finish_reason="stop")
            return
        for c in self._rounds[idx]:
            yield c


def _collect_legacy(events: list, target_types: tuple[type, ...]) -> list:
    """Return events of any of the target types from a captured list."""
    return [e for e in events if isinstance(e, target_types)]


def _assert_monotonic_sequence(envelopes: list[EventEnvelope]) -> None:
    """Sequences must be strictly increasing across the captured stream."""
    seen: int | None = None
    for env in envelopes:
        if seen is not None:
            assert env.sequence > seen, (
                f"sequence regressed: prev={seen}, got={env.sequence}, "
                f"event_type={type(env.event).__name__}"
            )
        seen = env.sequence


def _assert_plausible_timestamps(envelopes: list[EventEnvelope]) -> None:
    """Each envelope's ``timestamp_ms`` must be a recent ms-resolution
    wall clock — within the past 60s of the test start, in the future
    by no more than 5s (clock skew tolerance)."""
    now_ms = time.time_ns() // 1_000_000
    for env in envelopes:
        assert env.timestamp_ms > 0
        # Reasonable bounds — these are loose because CI clocks may
        # drift, but they catch ``time.monotonic_ns`` being passed where
        # ``time.time_ns`` was expected.
        assert env.timestamp_ms <= now_ms + 5_000
        assert env.timestamp_ms >= now_ms - 60_000


# --------------------------------------------------------------------------
# 1. The big-picture: one full turn with reasoning + text + 2 tools.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_turn_event_sequence() -> None:
    """Drive a turn with the structure described in the W1.1 task brief:

    1 reasoning block, 1 text block, 2 tool calls (different tools),
    tool results provided, second round emits a closing text block.

    Asserts the exact envelope order matches the plan §1.1 taxonomy.
    """
    round1 = [
        # Reasoning prelude — opens & deltas a reasoning block.
        _ReasoningChunk(kind="token", text="thinking ", is_reasoning=True),
        _ReasoningChunk(
            kind="token",
            text="step by step",
            is_reasoning=True,
            signature="sig_abc",
        ),
        # Plain text prelude.
        ProviderChunk(kind="token", text="ok, "),
        ProviderChunk(kind="token", text="calling tools."),
        # First tool call.
        ProviderChunk(
            kind="tool_call_start", tool_call_id="t1", tool_name="search"
        ),
        ProviderChunk(
            kind="tool_call_delta",
            tool_call_id="t1",
            arguments_delta='{"query":',
        ),
        ProviderChunk(
            kind="tool_call_delta",
            tool_call_id="t1",
            arguments_delta='"weather"}',
        ),
        ProviderChunk(kind="tool_call_end", tool_call_id="t1"),
        # Second tool call.
        ProviderChunk(
            kind="tool_call_start", tool_call_id="t2", tool_name="lookup"
        ),
        ProviderChunk(
            kind="tool_call_delta",
            tool_call_id="t2",
            arguments_delta='{"id":42}',
        ),
        ProviderChunk(kind="tool_call_end", tool_call_id="t2"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    round2 = [
        ProviderChunk(kind="token", text="all done."),
        ProviderChunk(
            kind="done",
            finish_reason="stop",
            usage={"input_tokens": 100, "output_tokens": 20},
        ),
    ]

    provider = _ScriptedProvider([round1, round2])
    emitter = MockEventEmitter()
    loop = ReasoningLoop(provider, event_emitter=emitter)

    legacy_events: list[Any] = []

    async def driver() -> None:
        async for e in loop.run(
            ChatStart(
                model="claude-sonnet-4.5",
                messages=[
                    {"role": "system", "content": "you are helpful"},
                    {"role": "user", "content": "what's the weather?"},
                ],
                session_key="sess-xyz",
            )
        ):
            legacy_events.append(e)
            if isinstance(e, ToolCallEvent):
                loop.feed_tool_result(
                    ToolResult(call_id=e.call_id, content='{"ok":true}')
                )

    await asyncio.wait_for(driver(), timeout=2.0)

    types_seen = emitter.event_types

    # ----- Big-picture sequence assertion -----
    expected = [
        # Round 1 — provider call begins, model thinks, then talks.
        "TurnStart",
        "BlockStart",  # reasoning block
        "ReasoningDelta",
        "ReasoningDelta",
        "BlockStop",
        "BlockStart",  # text block
        "TextDelta",
        "TextDelta",
        "BlockStop",
        "BlockStart",  # tool_use #1
        "ToolInputDelta",
        "ToolInputDelta",
        "BlockStop",
        "BlockStart",  # tool_use #2
        "ToolInputDelta",
        "BlockStop",
        # Tool dispatch (both running, then both completed).
        "ToolStateRunning",
        "ToolStateRunning",
        "ToolStateCompleted",
        "ToolStateCompleted",
        # Round 2 — final assistant text reply, then turn end.
        "BlockStart",  # text block in round 2
        "TextDelta",
        "BlockStop",
        "TurnComplete",
    ]
    assert types_seen == expected, (
        f"event_type order mismatch.\nGot:      {types_seen}\nExpected: {expected}"
    )

    # ----- Monotonic sequence + plausible timestamps -----
    _assert_monotonic_sequence(emitter.envelopes)
    _assert_plausible_timestamps(emitter.envelopes)

    # ----- TurnStart carries the right correlation data -----
    turn_starts = emitter.events_of(TurnStart)
    assert len(turn_starts) == 1
    assert turn_starts[0].model == "claude-sonnet-4.5"
    assert turn_starts[0].user_text_preview == "what's the weather?"
    assert turn_starts[0].system_message_preview == "you are helpful"

    # ----- BlockStart types match the block_type discriminator -----
    block_starts = emitter.events_of(BlockStart)
    assert [b.block_type for b in block_starts] == [
        "reasoning",
        "text",
        "tool_use",
        "tool_use",
        "text",
    ]
    # block index is monotonic per-round; round 2 starts fresh.
    # Round 1 indices: 0 reasoning, 1 text, 2 tool_use #1, 3 tool_use #2
    # Round 2 indices: 0 text
    assert [b.index for b in block_starts] == [0, 1, 2, 3, 0]
    # The tool_use blocks must carry their tool_call_id + tool_name.
    tool_starts = [b for b in block_starts if b.block_type == "tool_use"]
    assert tool_starts[0].tool_name == "search"
    assert tool_starts[0].tool_call_id == "t1"
    assert tool_starts[1].tool_name == "lookup"
    assert tool_starts[1].tool_call_id == "t2"

    # ----- ReasoningDelta carries text + signature -----
    rdeltas = emitter.events_of(ReasoningDelta)
    assert [r.text for r in rdeltas] == ["thinking ", "step by step"]
    assert rdeltas[1].signature == "sig_abc"

    # ----- TextDelta accumulates cumulative_len correctly -----
    tdeltas = emitter.events_of(TextDelta)
    assert [t.text for t in tdeltas] == ["ok, ", "calling tools.", "all done."]
    # Round 1's text block: "ok, " then "calling tools." → cumulative
    # = 4 then 4 + 14 = 18.
    assert tdeltas[0].cumulative_len == 4
    assert tdeltas[1].cumulative_len == 18
    # Round 2's text block starts fresh; cumulative = len("all done.") = 9.
    assert tdeltas[2].cumulative_len == 9

    # ----- ToolInputDelta carries the right partial_json fragments -----
    tideltas = emitter.events_of(ToolInputDelta)
    assert [t.partial_json for t in tideltas] == [
        '{"query":',
        '"weather"}',
        '{"id":42}',
    ]

    # ----- ToolStateRunning / Completed pair per tool call -----
    running = emitter.events_of(ToolStateRunning)
    assert [r.tool_name for r in running] == ["search", "lookup"]
    assert [r.tool_call_id for r in running] == ["t1", "t2"]
    # ``args_json`` is the aggregated stringified args.
    assert json.loads(running[0].args_json) == {"query": "weather"}
    assert json.loads(running[1].args_json) == {"id": 42}

    completed = emitter.events_of(ToolStateCompleted)
    assert [c.tool_call_id for c in completed] == ["t1", "t2"]
    for c in completed:
        assert c.result_summary == '{"ok":true}'
        assert c.is_error is False

    # ----- TurnComplete carries finish_reason + last-round usage -----
    completes = emitter.events_of(TurnComplete)
    assert len(completes) == 1
    assert completes[0].finish_reason == "stop"
    assert completes[0].usage == {"input_tokens": 100, "output_tokens": 20}
    assert completes[0].elapsed_ms >= 0

    # ----- to_json() round-trip shape is stable -----
    first = emitter.envelopes[0].to_json()
    assert first["event_type"] == "TurnStart"
    assert first["sequence"] == 0
    assert first["turn_id"]
    assert first["session_key"] == "sess-xyz"
    assert first["payload"]["model"] == "claude-sonnet-4.5"
    assert "user_text_preview" in first["payload"]
    # JSON-serialisable end-to-end.
    raw = json.dumps([env.to_json() for env in emitter.envelopes])
    assert "TurnStart" in raw and "TurnComplete" in raw

    # ----- Backwards-compat: legacy yields still fire -----
    legacy_tokens = _collect_legacy(legacy_events, (TokenEvent,))
    assert [t.text for t in legacy_tokens] == [
        "thinking ",
        "step by step",
        "ok, ",
        "calling tools.",
        "all done.",
    ]
    # is_reasoning carries through on the legacy stream too.
    assert legacy_tokens[0].is_reasoning is True
    assert legacy_tokens[2].is_reasoning is False
    legacy_tool_calls = _collect_legacy(legacy_events, (ToolCallEvent,))
    assert [c.call_id for c in legacy_tool_calls] == ["t1", "t2"]
    assert any(
        isinstance(e, DoneEvent) and e.finish_reason == "stop"
        for e in legacy_events
    )


# --------------------------------------------------------------------------
# 2. Emitter is optional — default behaviour stays legacy.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emitter_omitted_skips_envelope_emission() -> None:
    """When no emitter is wired, no envelopes are produced and the
    legacy stream remains the only output (W1.1 backwards-compat)."""
    provider = _ScriptedProvider(
        [
            [
                ProviderChunk(kind="token", text="hi"),
                ProviderChunk(kind="done", finish_reason="stop"),
            ]
        ]
    )
    loop = ReasoningLoop(provider)
    events: list[Any] = []
    async for e in loop.run(ChatStart(model="x", messages=[])):
        events.append(e)
    # Legacy events come through normally.
    assert any(isinstance(e, TokenEvent) for e in events)
    assert isinstance(events[-1], DoneEvent)
    # No emitter → no crash, no extra outputs.


# --------------------------------------------------------------------------
# 3. Errors emit a TurnErrored envelope before the legacy ErrorEvent.
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _ExplodingProvider:
    calls_seen: list[list[dict[str, Any]]] = field(default_factory=list)

    async def chat_stream(
        self, *, messages: list[dict[str, Any]], **_: Any
    ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.calls_seen.append(list(messages))
        yield ProviderChunk(kind="token", text="partial ")
        raise RuntimeError("provider boom")


@pytest.mark.asyncio
async def test_error_path_emits_turn_errored() -> None:
    provider = _ExplodingProvider()
    emitter = MockEventEmitter()
    loop = ReasoningLoop(provider, event_emitter=emitter)
    events: list[Any] = []
    async for e in loop.run(ChatStart(model="x", messages=[])):
        events.append(e)
    # Legacy: ErrorEvent terminal.
    assert any(
        type(e).__name__ == "ErrorEvent" for e in events
    ), "legacy ErrorEvent must still fire"
    # Typed: TurnErrored is the last envelope.
    errored = emitter.events_of(TurnErrored)
    assert len(errored) == 1
    assert "provider boom" in errored[0].message
    assert errored[0].reason == "unknown"
    assert errored[0].elapsed_ms >= 0
    # No TurnComplete on the error path.
    assert not emitter.events_of(TurnComplete)


# --------------------------------------------------------------------------
# 4. to_json() shape — explicit contract for the W1.2 / W1.3 consumers.
# --------------------------------------------------------------------------


def test_envelope_to_json_shape() -> None:
    """The envelope serialises to the exact shape W1.2 + W1.3 consume."""
    env = EventEnvelope(
        turn_id="turn-1",
        session_key="sess-1",
        sequence=7,
        timestamp_ms=1_716_500_000_000,
        event=TextDelta(index=0, text="hello", cumulative_len=5),
    )
    js = env.to_json()
    assert js == {
        "turn_id": "turn-1",
        "session_key": "sess-1",
        "sequence": 7,
        "timestamp_ms": 1_716_500_000_000,
        "event_type": "TextDelta",
        "payload": {"index": 0, "text": "hello", "cumulative_len": 5},
    }
    # JSON-serialisable end-to-end.
    assert json.loads(json.dumps(js)) == js


def test_envelope_to_json_handles_block_start_optional_fields() -> None:
    """``BlockStart`` for a text block has ``None`` tool fields — they
    serialise as JSON ``null``, not crash."""
    env = EventEnvelope(
        turn_id="t",
        session_key="s",
        sequence=0,
        timestamp_ms=1,
        event=BlockStart(index=0, block_type="text"),
    )
    js = env.to_json()
    assert js["payload"]["tool_name"] is None
    assert js["payload"]["tool_call_id"] is None
    raw = json.dumps(js)
    assert '"tool_name": null' in raw


def test_envelope_to_json_handles_block_stop() -> None:
    """``BlockStop`` serialises with its elapsed_ms field intact."""
    env = EventEnvelope(
        turn_id="t",
        session_key="s",
        sequence=1,
        timestamp_ms=1,
        event=BlockStop(index=2, elapsed_ms=15),
    )
    js = env.to_json()
    assert js["event_type"] == "BlockStop"
    assert js["payload"] == {"index": 2, "elapsed_ms": 15}
