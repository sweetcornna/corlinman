"""Reasoning loop unit tests — aggregate ProviderChunk streams into events.

The loop consumes a provider object that matches the :class:`CorlinmanProvider`
Protocol; we substitute a minimal async-iterator stub that yields
:class:`ProviderChunk` values so these tests stay offline.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_agent import (
    Attachment,
    ChatStart,
    DoneEvent,
    ErrorEvent,
    ReasoningLoop,
    TokenEvent,
    ToolCallEvent,
    ToolResult,
)
from corlinman_providers.base import ProviderChunk


class _FakeProvider:
    """Emits a preset list of ProviderChunk values."""

    def __init__(self, chunks: list[ProviderChunk]) -> None:
        self._chunks = chunks

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        for c in self._chunks:
            yield c


class _ExplodingProvider:
    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        yield ProviderChunk(kind="token", text="partial")
        raise RuntimeError("provider blew up")


class _MultiRoundProvider:
    """Yields a different chunk list per call — used to test tool-result feedback."""

    def __init__(self, rounds: list[list[ProviderChunk]]) -> None:
        self._rounds = rounds
        self.calls_seen: list[list[dict[str, Any]]] = []

    async def chat_stream(
        self, *, messages: list[dict[str, Any]], **_: Any
    ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.calls_seen.append(list(messages))
        idx = len(self.calls_seen) - 1
        if idx >= len(self._rounds):
            yield ProviderChunk(kind="done", finish_reason="stop")
            return
        for c in self._rounds[idx]:
            yield c


async def _collect(loop: ReasoningLoop, start: ChatStart) -> list:
    events = []
    async for e in loop.run(start):
        events.append(e)
    return events


@pytest.mark.asyncio
async def test_pure_text_stream() -> None:
    prov = _FakeProvider(
        [
            ProviderChunk(kind="token", text="hello "),
            ProviderChunk(kind="token", text="world"),
            ProviderChunk(kind="done", finish_reason="stop"),
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    tokens = [e.text for e in events if isinstance(e, TokenEvent)]
    assert tokens == ["hello ", "world"]
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_single_tool_call_aggregated() -> None:
    prov = _FakeProvider(
        [
            ProviderChunk(kind="token", text="ok, calling "),
            ProviderChunk(
                kind="tool_call_start",
                tool_call_id="call_abc",
                tool_name="FooPlugin",
            ),
            ProviderChunk(
                kind="tool_call_delta",
                tool_call_id="call_abc",
                arguments_delta='{"query":',
            ),
            ProviderChunk(
                kind="tool_call_delta",
                tool_call_id="call_abc",
                arguments_delta='"hi"}',
            ),
            ProviderChunk(kind="tool_call_end", tool_call_id="call_abc"),
            ProviderChunk(kind="done", finish_reason="tool_calls"),
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].call_id == "call_abc"
    assert tool_events[0].plugin == "FooPlugin"
    args = json.loads(tool_events[0].args_json.decode("utf-8"))
    assert args == {"query": "hi"}


@pytest.mark.asyncio
async def test_multiple_tool_calls_aggregated() -> None:
    prov = _FakeProvider(
        [
            ProviderChunk(kind="tool_call_start", tool_call_id="a", tool_name="A"),
            ProviderChunk(kind="tool_call_delta", tool_call_id="a", arguments_delta="{}"),
            ProviderChunk(kind="tool_call_end", tool_call_id="a"),
            ProviderChunk(kind="tool_call_start", tool_call_id="b", tool_name="B"),
            ProviderChunk(kind="tool_call_delta", tool_call_id="b", arguments_delta="{}"),
            ProviderChunk(kind="tool_call_end", tool_call_id="b"),
            ProviderChunk(kind="done", finish_reason="tool_calls"),
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert [e.plugin for e in tool_events] == ["A", "B"]
    assert [e.call_id for e in tool_events] == ["a", "b"]


@pytest.mark.asyncio
async def test_missing_tool_call_end_still_flushes_at_done() -> None:
    """Provider forgets to emit ``tool_call_end`` — the loop still finalises
    the open call when ``done`` arrives."""
    prov = _FakeProvider(
        [
            ProviderChunk(kind="tool_call_start", tool_call_id="x", tool_name="X"),
            ProviderChunk(kind="tool_call_delta", tool_call_id="x", arguments_delta='{"k":1}'),
            ProviderChunk(kind="done", finish_reason="tool_calls"),
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].call_id == "x"


@pytest.mark.asyncio
async def test_provider_exception_emits_error_event() -> None:
    events = await _collect(ReasoningLoop(_ExplodingProvider()), ChatStart(model="x", messages=[]))
    assert any(isinstance(e, ErrorEvent) for e in events)
    assert not any(isinstance(e, DoneEvent) for e in events)


@pytest.mark.asyncio
async def test_token_then_tool_call_then_token_across_round() -> None:
    """Tokens and tool_calls interleave correctly in a single round."""
    prov = _FakeProvider(
        [
            ProviderChunk(kind="token", text="prefix "),
            ProviderChunk(kind="tool_call_start", tool_call_id="t1", tool_name="Tool"),
            ProviderChunk(kind="tool_call_delta", tool_call_id="t1", arguments_delta="{}"),
            ProviderChunk(kind="tool_call_end", tool_call_id="t1"),
            ProviderChunk(kind="token", text=" suffix"),
            ProviderChunk(kind="done", finish_reason="tool_calls"),
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    kinds = [type(e).__name__ for e in events]
    # TokenEvent -> ToolCallEvent -> TokenEvent -> DoneEvent
    assert kinds == ["TokenEvent", "ToolCallEvent", "TokenEvent", "DoneEvent"]


@pytest.mark.asyncio
async def test_no_tool_call_ends_with_stop() -> None:
    prov = _FakeProvider([ProviderChunk(kind="done", finish_reason="stop")])
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    assert len(events) == 1
    assert isinstance(events[0], DoneEvent)
    assert events[0].finish_reason == "stop"


@pytest.mark.asyncio
async def test_tool_result_drives_second_round() -> None:
    """After yielding a ToolCallEvent, feeding a ToolResult triggers another
    provider call with the tool message appended."""
    round1 = [
        ProviderChunk(kind="tool_call_start", tool_call_id="c1", tool_name="t"),
        ProviderChunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta="{}"),
        ProviderChunk(kind="tool_call_end", tool_call_id="c1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    round2 = [
        ProviderChunk(kind="token", text="done"),
        ProviderChunk(kind="done", finish_reason="stop"),
    ]
    prov = _MultiRoundProvider([round1, round2])
    loop = ReasoningLoop(prov)

    events: list = []

    async def driver() -> None:
        async for e in loop.run(ChatStart(model="x", messages=[{"role": "user", "content": "hi"}])):
            events.append(e)
            if isinstance(e, ToolCallEvent):
                loop.feed_tool_result(ToolResult(call_id=e.call_id, content='{"ok":true}'))

    await asyncio.wait_for(driver(), timeout=2.0)

    # Two rounds happened: the second call saw the tool result appended.
    assert len(prov.calls_seen) == 2
    round2_messages = prov.calls_seen[1]
    assert round2_messages[-1]["role"] == "tool"
    assert round2_messages[-1]["tool_call_id"] == "c1"
    # And the overall event stream ends with a DoneEvent(finish_reason="stop").
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_awaiting_placeholder_result_ends_loop() -> None:
    """If the gateway echoes ``awaiting_plugin_runtime`` the loop must stop
    after the first round — otherwise the model would re-request the tool."""
    round1 = [
        ProviderChunk(kind="tool_call_start", tool_call_id="c1", tool_name="t"),
        ProviderChunk(kind="tool_call_end", tool_call_id="c1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    prov = _MultiRoundProvider([round1])
    loop = ReasoningLoop(prov)

    events: list = []

    async def driver() -> None:
        async for e in loop.run(ChatStart(model="x", messages=[])):
            events.append(e)
            if isinstance(e, ToolCallEvent):
                loop.feed_tool_result(
                    ToolResult(
                        call_id=e.call_id,
                        content='{"status":"awaiting_plugin_runtime"}',
                    )
                )

    await asyncio.wait_for(driver(), timeout=2.0)
    # Exactly one provider round; loop terminated without a follow-up call.
    assert len(prov.calls_seen) == 1
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_attachments_forwarded_as_content_parts() -> None:
    """ChatStart.attachments rewrite the trailing user turn's content into
    OpenAI-shape multi-part blocks before the provider sees it."""
    prov = _MultiRoundProvider(
        [[ProviderChunk(kind="done", finish_reason="stop")]]
    )
    loop = ReasoningLoop(prov)
    start = ChatStart(
        model="x",
        messages=[{"role": "user", "content": "look at this"}],
        attachments=[
            Attachment(kind="image", url="https://cdn/pic.png", mime="image/png"),
        ],
    )
    await _collect(loop, start)
    # Exactly one round; the provider saw the rewritten user message.
    assert len(prov.calls_seen) == 1
    msgs = prov.calls_seen[0]
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look at this"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "https://cdn/pic.png"},
    }


@pytest.mark.asyncio
async def test_attachments_none_leaves_messages_unchanged() -> None:
    """Without attachments the loop must not touch the original messages."""
    prov = _MultiRoundProvider(
        [[ProviderChunk(kind="done", finish_reason="stop")]]
    )
    loop = ReasoningLoop(prov)
    msg = {"role": "user", "content": "plain text"}
    await _collect(loop, ChatStart(model="x", messages=[msg]))
    assert prov.calls_seen[0][0]["content"] == "plain text"


@pytest.mark.asyncio
async def test_attachments_audio_forwarded_as_file_part() -> None:
    """Non-image attachments land as a generic ``file`` content part so the
    provider adapter (not the loop) decides whether to skip or translate."""
    prov = _MultiRoundProvider(
        [[ProviderChunk(kind="done", finish_reason="stop")]]
    )
    loop = ReasoningLoop(prov)
    start = ChatStart(
        model="x",
        messages=[{"role": "user", "content": "voice note"}],
        attachments=[Attachment(kind="audio", url="https://cdn/v.amr")],
    )
    await _collect(loop, start)
    content = prov.calls_seen[0][0]["content"]
    assert isinstance(content, list)
    assert any(p.get("type") == "file" for p in content)
    file_part = next(p for p in content if p.get("type") == "file")
    assert file_part["file"]["kind"] == "audio"
    assert file_part["file"]["url"] == "https://cdn/v.amr"


@pytest.mark.asyncio
async def test_attachment_image_bytes_become_data_url() -> None:
    """Attachment with bytes (no url) encodes into a data: URI."""
    prov = _MultiRoundProvider(
        [[ProviderChunk(kind="done", finish_reason="stop")]]
    )
    loop = ReasoningLoop(prov)
    raw = b"\x89PNGFAKE"
    start = ChatStart(
        model="x",
        messages=[{"role": "user", "content": ""}],
        attachments=[Attachment(kind="image", bytes_=raw, mime="image/png")],
    )
    await _collect(loop, start)
    content = prov.calls_seen[0][0]["content"]
    assert isinstance(content, list)
    img = next(p for p in content if p.get("type") == "image_url")
    url = img["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# T1.1 — tool-result truncation + freeze
# ---------------------------------------------------------------------------


def test_truncate_tool_result_keeps_head_and_tail() -> None:
    """A 20k-char string is capped under the limit and keeps head+tail."""
    from corlinman_agent.reasoning_loop import (
        _TOOL_RESULT_CAP,
        _truncate_tool_result,
    )

    head_chunk = "H" * 1_000
    middle_chunk = "M" * 15_000
    tail_chunk = "T" * 4_000
    original = head_chunk + middle_chunk + tail_chunk  # 20_000 chars
    assert len(original) == 20_000

    out = _truncate_tool_result(original)

    # Capped under the limit; the elision notice + head + tail fit
    # comfortably inside _TOOL_RESULT_CAP.
    assert len(out) < _TOOL_RESULT_CAP
    # The first 1k chars of the original are at the start of the result
    # (the head slice is 2k chars so the leading 'H' block is fully
    # preserved).
    assert out.startswith(head_chunk)
    # The trailing 4k chars are at the end.
    assert out.endswith(tail_chunk)
    # The notice is in the middle and reports the elided count.
    assert "elided" in out
    assert "…[" in out


def test_truncate_tool_result_passthrough_under_cap() -> None:
    """Strings at or below the cap pass through unchanged (no notice)."""
    from corlinman_agent.reasoning_loop import _truncate_tool_result

    small = "a" * 500
    assert _truncate_tool_result(small) == small
    assert "elided" not in _truncate_tool_result(small)


def test_extend_with_tool_round_truncates_long_result() -> None:
    """``_extend_with_tool_round`` caps each result before history-append."""
    from corlinman_agent.reasoning_loop import (
        _TOOL_RESULT_CAP,
        _extend_with_tool_round,
    )

    call = ToolCallEvent(
        call_id="call_1",
        plugin="run_shell",
        tool="run_shell",
        args_json=b'{"command":"echo hi"}',
    )
    big = "x" * 50_000
    result = ToolResult(call_id="call_1", content=big, is_error=False)

    extended = _extend_with_tool_round([], [call], [result])

    # Assistant message + one tool message.
    assert len(extended) == 2
    tool_msg = extended[1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_1"
    capped = tool_msg["content"]
    assert isinstance(capped, str)
    # Strictly below the configured cap.
    assert len(capped) < _TOOL_RESULT_CAP
    # Carries the elision notice — proves truncation actually fired.
    assert "elided" in capped


# ---------------------------------------------------------------------------
# T1.4 — provider usage flows DoneEvent.usage onto the outer terminal Done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_event_carries_usage_from_provider() -> None:
    """Provider-reported usage on the done chunk reaches the outer DoneEvent.

    The reasoning loop's per-round :class:`DoneEvent` is consumed
    inside ``run()`` — only the outer terminal Done is yielded to the
    caller. T1.4 captures the LAST round's usage and attaches it onto
    the outer Done so the servicer's cost meter can fold a single
    record per turn.
    """
    prov = _FakeProvider(
        [
            ProviderChunk(kind="token", text="ok"),
            ProviderChunk(
                kind="done",
                finish_reason="stop",
                usage={"input_tokens": 5, "output_tokens": 8},
            ),
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    final = events[-1]
    assert isinstance(final, DoneEvent)
    assert final.finish_reason == "stop"
    assert final.usage == {"input_tokens": 5, "output_tokens": 8}


@pytest.mark.asyncio
async def test_done_event_usage_none_when_provider_omits() -> None:
    """A provider that never reports usage leaves DoneEvent.usage at None."""
    prov = _FakeProvider(
        [
            ProviderChunk(kind="token", text="hi"),
            ProviderChunk(kind="done", finish_reason="stop"),
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    final = events[-1]
    assert isinstance(final, DoneEvent)
    assert final.usage is None


@pytest.mark.asyncio
async def test_done_event_usage_reflects_last_round_in_multi_round_loop() -> None:
    """For tool-driven multi-round turns, the outer Done carries the LAST round's usage.

    The model is billed at each round; tracking the LAST round matches
    what a single ``response.completed`` event would report on a real
    Responses-API turn that ended cleanly. The cost meter is called
    once per ``Chat`` turn, so per-round granularity inside the loop
    would over-count the same prefix tokens.
    """
    rounds = [
        # Round 1: tool call + usage_a, loop continues after tool result.
        [
            ProviderChunk(
                kind="tool_call_start",
                tool_call_id="call_1",
                tool_name="echo",
            ),
            ProviderChunk(
                kind="tool_call_delta",
                tool_call_id="call_1",
                arguments_delta='{"x":1}',
            ),
            ProviderChunk(kind="tool_call_end", tool_call_id="call_1"),
            ProviderChunk(
                kind="done",
                finish_reason="tool_calls",
                usage={"input_tokens": 100, "output_tokens": 5},
            ),
        ],
        # Round 2: plain text, final usage — this is what the outer Done must carry.
        [
            ProviderChunk(kind="token", text="done"),
            ProviderChunk(
                kind="done",
                finish_reason="stop",
                usage={"input_tokens": 150, "output_tokens": 12},
            ),
        ],
    ]
    prov = _MultiRoundProvider(rounds)
    loop = ReasoningLoop(prov, tool_result_timeout=1.0)

    # Drive the loop, feeding a tool result so round 2 runs.
    async def _drive() -> list:
        out = []
        async for ev in loop.run(ChatStart(model="x", messages=[])):
            out.append(ev)
            if isinstance(ev, ToolCallEvent):
                loop.feed_tool_result(
                    ToolResult(call_id=ev.call_id, content='{"ok":true}')
                )
        return out

    events = await _drive()
    final = events[-1]
    assert isinstance(final, DoneEvent)
    assert final.finish_reason == "stop"
    # LAST round's usage — not the round-1 numbers.
    assert final.usage == {"input_tokens": 150, "output_tokens": 12}


# ---------------------------------------------------------------------------
# T2.3 — token-aware context compaction
# ---------------------------------------------------------------------------


def test_estimate_tokens_sums_string_and_multimodal_content() -> None:
    """Estimator sums string content + multimodal text parts, ignores images."""
    from corlinman_agent.reasoning_loop import _estimate_tokens

    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "world"},
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
            ],
        },
    ]
    # 5 chars + 5 chars = 10 → 10 // 4 = 2.
    assert _estimate_tokens(messages) == 10 // 4


def test_compact_history_passthrough_when_under_budget() -> None:
    """Small histories below budget are returned unchanged and unmutated."""
    from corlinman_agent.reasoning_loop import _compact_history

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are a helper"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    snapshot = [dict(m) for m in messages]
    out = _compact_history(messages, budget=100_000)
    # Same contents, no mutation of the input.
    assert out == snapshot
    assert messages == snapshot


def test_compact_history_elides_old_tool_rounds() -> None:
    """Older role=tool payloads collapse to the sentinel; recent 3 rounds + seed remain."""
    from corlinman_agent.reasoning_loop import (
        _ELIDED_TOOL_CONTENT,
        _compact_history,
    )

    huge = "X" * 1_000
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    for i in range(6):
        cid = f"c{i}"
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": cid,
                        "type": "function",
                        "function": {"name": "t", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": cid, "content": huge})

    original_len = len(messages)
    snapshot = [dict(m) for m in messages]
    out = _compact_history(messages, budget=200)

    # No deletions — assistant tool_calls shells must keep matching tool msgs.
    assert len(out) == original_len
    # Input wasn't mutated.
    assert messages == snapshot
    # Seed system + user preserved verbatim.
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[1] == {"role": "user", "content": "task"}
    # Pull out the tool messages in order.
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    assert len(tool_msgs) == 6
    # First three rounds → elided; last three rounds → verbatim.
    for tm in tool_msgs[:3]:
        assert tm["content"] == _ELIDED_TOOL_CONTENT
        assert tm["tool_call_id"]  # tool_call_id preserved
    for tm in tool_msgs[3:]:
        assert tm["content"] == huge
    # Older assistant shells still carry tool_calls (so the elided tool
    # messages still have a matching assistant entry).
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 6
    for am in assistants:
        assert isinstance(am.get("tool_calls"), list)
        assert len(am["tool_calls"]) == 1


def test_compact_history_idempotent_after_first_pass() -> None:
    """Re-running compaction on an already-compacted history is a no-op."""
    from corlinman_agent.reasoning_loop import _compact_history

    huge = "Y" * 1_000
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    for i in range(6):
        cid = f"c{i}"
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": cid,
                        "type": "function",
                        "function": {"name": "t", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": cid, "content": huge})

    first = _compact_history(messages, budget=200)
    second = _compact_history(first, budget=200)
    assert second == first


@pytest.mark.asyncio
async def test_run_invokes_compact_each_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reasoning loop calls _compact_history at the top of every round."""
    from corlinman_agent import reasoning_loop as rl_module

    counter = {"calls": 0}
    real = rl_module._compact_history

    def _spy(msgs: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
        counter["calls"] += 1
        return real(msgs, budget)

    monkeypatch.setattr(rl_module, "_compact_history", _spy)

    # Drive 3 rounds: tool_call → tool_call → final stop.
    round1 = [
        ProviderChunk(kind="tool_call_start", tool_call_id="c1", tool_name="t"),
        ProviderChunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta="{}"),
        ProviderChunk(kind="tool_call_end", tool_call_id="c1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    round2 = [
        ProviderChunk(kind="tool_call_start", tool_call_id="c2", tool_name="t"),
        ProviderChunk(kind="tool_call_delta", tool_call_id="c2", arguments_delta="{}"),
        ProviderChunk(kind="tool_call_end", tool_call_id="c2"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    round3 = [
        ProviderChunk(kind="token", text="done"),
        ProviderChunk(kind="done", finish_reason="stop"),
    ]
    prov = _MultiRoundProvider([round1, round2, round3])
    loop = ReasoningLoop(prov, tool_result_timeout=1.0)

    async def driver() -> None:
        async for e in loop.run(
            ChatStart(model="x", messages=[{"role": "user", "content": "go"}])
        ):
            if isinstance(e, ToolCallEvent):
                loop.feed_tool_result(ToolResult(call_id=e.call_id, content='{"ok":true}'))

    await asyncio.wait_for(driver(), timeout=2.0)

    # Three provider rounds happened, so compact ran at least 3 times.
    # Spec says "at least rounds + 1" (defensive lower bound is fine —
    # exactly-once-per-round is the contract).
    assert len(prov.calls_seen) == 3
    assert counter["calls"] >= 3
