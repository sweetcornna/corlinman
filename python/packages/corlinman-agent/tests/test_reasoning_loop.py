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
from corlinman_providers.specs import ProviderKind


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


class _RecordingProvider:
    """Records provider kwargs for boundary tests."""

    def __init__(
        self,
        *,
        name: str = "recording",
        kind: ProviderKind | str | None = None,
    ) -> None:
        self.name = name
        if kind is not None:
            self.kind = kind
        self.calls: list[dict[str, Any]] = []

    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.calls.append(kwargs)
        yield ProviderChunk(kind="done", finish_reason="stop")


class _SchemaRecordingProvider(_RecordingProvider):
    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "top_p": {"type": "number"},
            },
        }


class _CodexSchemaRecordingProvider(_RecordingProvider):
    def __init__(self) -> None:
        super().__init__(name="codex", kind=ProviderKind.CODEX)

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prompt_cache_key": {"type": "string"},
                "reasoning_effort": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "xhigh"],
                },
            },
        }


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
async def test_internal_chat_extra_is_not_forwarded_to_provider() -> None:
    prov = _RecordingProvider()

    await _collect(
        ReasoningLoop(prov),
        ChatStart(
            model="x",
            messages=[{"role": "user", "content": "hi"}],
            extra={
                "persona_id": "grantley",
                "binding": {
                    "channel": "telegram",
                    "account": "999",
                    "thread": "42",
                    "sender": "7",
                },
                "provider_hint": "persona-provider",
                "prompt_cache_key": "session-1",
                "top_p": 0.8,
                "reasoning_effort": "low",
            },
        ),
    )

    assert prov.calls
    assert prov.calls[0]["extra"] == {
        "top_p": 0.8,
        "reasoning_effort": "low",
    }


@pytest.mark.asyncio
async def test_codex_prompt_cache_extra_is_still_forwarded() -> None:
    prov = _CodexSchemaRecordingProvider()

    await _collect(
        ReasoningLoop(prov),
        ChatStart(
            model="x",
            messages=[{"role": "user", "content": "hi"}],
            extra={
                "persona_id": "grantley",
                "prompt_cache_key": "session-1",
                "top_p": 0.8,
            },
        ),
    )

    assert prov.calls
    assert prov.calls[0]["extra"] == {
        "prompt_cache_key": "session-1",
    }


@pytest.mark.asyncio
async def test_openai_compatible_provider_named_codex_drops_codex_only_extra() -> None:
    prov = _RecordingProvider(
        name="codex",
        kind=ProviderKind.OPENAI_COMPATIBLE,
    )

    await _collect(
        ReasoningLoop(prov),
        ChatStart(
            model="x",
            messages=[{"role": "user", "content": "hi"}],
            extra={
                "persona_id": "grantley",
                "prompt_cache_key": "session-1",
                "top_p": 0.8,
            },
        ),
    )

    assert prov.calls
    assert prov.calls[0]["extra"] == {"top_p": 0.8}


@pytest.mark.asyncio
async def test_undeclared_provider_extra_is_not_forwarded() -> None:
    prov = _SchemaRecordingProvider()

    await _collect(
        ReasoningLoop(prov),
        ChatStart(
            model="x",
            messages=[{"role": "user", "content": "hi"}],
            extra={
                "top_p": 0.8,
                "reasoning_effort": "high",
            },
        ),
    )

    assert prov.calls
    assert prov.calls[0]["extra"] == {"top_p": 0.8}


@pytest.mark.asyncio
async def test_provider_extra_rejected_by_schema_enum_is_not_forwarded() -> None:
    prov = _CodexSchemaRecordingProvider()

    await _collect(
        ReasoningLoop(prov),
        ChatStart(
            model="x",
            messages=[{"role": "user", "content": "hi"}],
            extra={
                "prompt_cache_key": "session-1",
                "reasoning_effort": "minimal",
            },
        ),
    )

    assert prov.calls
    assert prov.calls[0]["extra"] == {"prompt_cache_key": "session-1"}


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
    prov = _MultiRoundProvider([[ProviderChunk(kind="done", finish_reason="stop")]])
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
    prov = _MultiRoundProvider([[ProviderChunk(kind="done", finish_reason="stop")]])
    loop = ReasoningLoop(prov)
    msg = {"role": "user", "content": "plain text"}
    await _collect(loop, ChatStart(model="x", messages=[msg]))
    assert prov.calls_seen[0][0]["content"] == "plain text"


@pytest.mark.asyncio
async def test_attachments_audio_forwarded_as_file_part() -> None:
    """Non-image attachments land as a generic ``file`` content part so the
    provider adapter (not the loop) decides whether to skip or translate."""
    prov = _MultiRoundProvider([[ProviderChunk(kind="done", finish_reason="stop")]])
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
    prov = _MultiRoundProvider([[ProviderChunk(kind="done", finish_reason="stop")]])
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
                loop.feed_tool_result(ToolResult(call_id=ev.call_id, content='{"ok":true}'))
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
    """Estimator sums string + multimodal text AND charges image blocks.

    Gap ``chars-div-4-token-estimate``: image / file content blocks are no
    longer treated as free — each is charged a flat ~1.5k-token weight so
    the context budget reflects multimodal payloads.
    """
    from corlinman_agent.reasoning_loop import (
        _IMAGE_BLOCK_TOKEN_CHARGE,
        _estimate_tokens,
    )

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
    # 5 chars + 5 chars = 10 text chars → 2 text tokens, plus the image
    # block's fixed per-block charge.
    assert _estimate_tokens(messages) == 10 // 4 + _IMAGE_BLOCK_TOKEN_CHARGE


@pytest.mark.asyncio
async def test_compact_history_passthrough_when_under_budget() -> None:
    """Small histories below budget are returned unchanged and unmutated."""
    from corlinman_agent.reasoning_loop import _compact_history

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are a helper"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    snapshot = [dict(m) for m in messages]
    out = await _compact_history(messages, budget=100_000, fast_path_only=True)
    # Same contents, no mutation of the input.
    assert out == snapshot
    assert messages == snapshot


@pytest.mark.asyncio
async def test_compact_history_elides_old_tool_rounds() -> None:
    """Older role=tool payloads collapse to the elision one-liner; recent 3 rounds + seed remain."""
    from corlinman_agent.reasoning_loop import (
        _ELIDED_TOOL_PREFIX,
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
    # ``fast_path_only=True`` — keep the historic test intent (verify
    # the elision math) while the slow summarization path lives behind
    # a dedicated test below.
    out = await _compact_history(messages, budget=200, fast_path_only=True)

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
        assert tm["content"].startswith(_ELIDED_TOOL_PREFIX)
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


@pytest.mark.asyncio
async def test_compact_history_idempotent_after_first_pass() -> None:
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

    first = await _compact_history(messages, budget=200, fast_path_only=True)
    second = await _compact_history(first, budget=200, fast_path_only=True)
    assert second == first


@pytest.mark.asyncio
async def test_run_invokes_compact_each_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reasoning loop calls _compact_history at the top of every round."""
    from corlinman_agent import reasoning_loop as rl_module

    counter = {"calls": 0}
    real = rl_module._compact_history

    async def _spy(
        msgs: list[dict[str, Any]],
        *,
        budget: int,
        provider: Any = None,
        model: str | None = None,
        fast_path_only: bool = False,
        prev_estimate: int | None = None,
        summary_allowed: bool = True,
        outcome: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        counter["calls"] += 1
        return await real(
            msgs,
            budget=budget,
            provider=provider,
            model=model,
            fast_path_only=fast_path_only,
            prev_estimate=prev_estimate,
            summary_allowed=summary_allowed,
            outcome=outcome,
        )

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
        async for e in loop.run(ChatStart(model="x", messages=[{"role": "user", "content": "go"}])):
            if isinstance(e, ToolCallEvent):
                loop.feed_tool_result(ToolResult(call_id=e.call_id, content='{"ok":true}'))

    await asyncio.wait_for(driver(), timeout=2.0)

    # Three provider rounds happened, so compact ran at least 3 times.
    # Spec says "at least rounds + 1" (defensive lower bound is fine —
    # exactly-once-per-round is the contract).
    assert len(prov.calls_seen) == 3
    assert counter["calls"] >= 3


# ---------------------------------------------------------------------------
# C3 — signal_input_closed() wakes _collect_results promptly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_input_closed_terminates_collect_results_promptly() -> None:
    """The loop must terminate within the round's tool-result timeout
    once ``signal_input_closed()`` fires while it is waiting for results.

    Regression for C3: the helper previously watched only
    ``_tool_results.get()`` and ``_cancelled``, so a client half-close
    on a round with un-fulfilled tool calls would either spin on the
    per-iter timeout (0.05s) forever or — under the production wiring
    where ``tool_result_timeout`` is raised to 30s for real plugins —
    block for the full 30s. The fix wires the event into
    ``asyncio.wait`` and returns immediately when it fires.
    """
    round1 = [
        ProviderChunk(kind="tool_call_start", tool_call_id="c1", tool_name="t"),
        ProviderChunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta="{}"),
        ProviderChunk(kind="tool_call_end", tool_call_id="c1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    prov = _MultiRoundProvider([round1])
    # Big per-iter timeout proves we are NOT just timing out — we are
    # genuinely woken by signal_input_closed().
    loop = ReasoningLoop(prov, tool_result_timeout=30.0)

    events: list = []

    async def driver() -> None:
        async for e in loop.run(ChatStart(model="x", messages=[{"role": "user", "content": "hi"}])):
            events.append(e)

    async def closer() -> None:
        # Give the loop one round-trip to land in _collect_results.
        await asyncio.sleep(0.05)
        loop.signal_input_closed()

    # The fix must terminate well under the 30s tool_result_timeout.
    # Two seconds is comfortably above any realistic scheduler jitter
    # while still being a clear failure signal if the event is ignored.
    await asyncio.wait_for(asyncio.gather(driver(), closer()), timeout=2.0)

    # The terminal event must be a DoneEvent (NOT an ErrorEvent — close
    # is graceful, distinct from ``cancel()``). The finish_reason
    # reflects the provider's last report — "tool_calls" here.
    assert isinstance(events[-1], DoneEvent), f"unexpected terminal: {events[-1]!r}"
    assert events[-1].finish_reason == "tool_calls"
    # Exactly one provider round — we did NOT loop around looking for
    # the (impossible-to-fulfil) follow-up.
    assert len(prov.calls_seen) == 1


# ---------------------------------------------------------------------------
# C4 — stale tool results are dropped, not retained for a later round
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_tool_result_is_dropped_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``ToolResult`` whose ``call_id`` is not in the current round's
    ``needed`` set must be discarded with a warning, not retained for a
    future round or returned as part of ``got``.

    Regression for C4: ``_tool_results.get()`` previously consumed
    every queue entry indiscriminately, so a stale push could either
    block the round forever (mismatched id never satisfied ``needed``)
    or contaminate the *next* round's collection.
    """
    # Intercept the module-level structlog logger so we can assert the
    # warning fired without depending on global structlog configuration
    # (which differs between dev and CI).
    import corlinman_agent.reasoning_loop as rl_mod

    captured: list[tuple[str, dict]] = []

    class _StubLogger:
        def warning(self, event: str, **kw: object) -> None:
            captured.append((event, dict(kw)))

        def info(self, event: str, **kw: object) -> None:
            pass

        def exception(self, event: str, **kw: object) -> None:
            pass

    monkeypatch.setattr(rl_mod, "logger", _StubLogger())

    round1 = [
        ProviderChunk(kind="tool_call_start", tool_call_id="real_call", tool_name="t"),
        ProviderChunk(kind="tool_call_delta", tool_call_id="real_call", arguments_delta="{}"),
        ProviderChunk(kind="tool_call_end", tool_call_id="real_call"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    round2 = [
        ProviderChunk(kind="token", text="ok"),
        ProviderChunk(kind="done", finish_reason="stop"),
    ]
    prov = _MultiRoundProvider([round1, round2])
    loop = ReasoningLoop(prov, tool_result_timeout=1.0)

    # Push a stale result BEFORE the round runs — when _collect_results
    # drains the queue, it must reject this entry (unknown call_id) and
    # then proceed to wait for the real_call result.
    loop.feed_tool_result(ToolResult(call_id="ghost_call", content='{"stale":true}'))

    async def driver() -> None:
        async for e in loop.run(ChatStart(model="x", messages=[{"role": "user", "content": "hi"}])):
            if isinstance(e, ToolCallEvent):
                loop.feed_tool_result(ToolResult(call_id=e.call_id, content='{"ok":true}'))

    await asyncio.wait_for(driver(), timeout=2.0)

    # Round 2 saw the real tool message — the ghost was never appended.
    assert len(prov.calls_seen) == 2
    round2_msgs = prov.calls_seen[1]
    tool_msgs = [m for m in round2_msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "real_call"

    # Warning fired exactly once for the stale id; never for real_call.
    stale_events = [(ev, kw) for ev, kw in captured if ev == "reasoning_loop.stale_tool_result"]
    assert len(stale_events) == 1, f"expected 1 stale warning, got: {captured!r}"
    assert stale_events[0][1].get("call_id") == "ghost_call"


# ---------------------------------------------------------------------------
# Fix 1 — Claude-Code-style summarization compaction
# ---------------------------------------------------------------------------


def _huge_tool_history(rounds: int = 6, char_count: int = 1_000) -> list[dict[str, Any]]:
    """Build a synthetic message list with ``rounds`` assistant/tool pairs.

    Each tool message carries ``char_count`` chars of payload — at the
    default 1k per round × 6 rounds the token estimate clears any
    "tight" budget set by the test (the slow path threshold is what
    decides between elision and summarization, not the absolute size).
    """
    huge = "X" * char_count
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    for i in range(rounds):
        cid = f"c{i}"
        msgs.append(
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
        msgs.append({"role": "tool", "tool_call_id": cid, "content": huge})
    return msgs


@pytest.mark.asyncio
async def test_compact_falls_back_to_elision_under_budget() -> None:
    """Pressure between 0.6*budget and 0.95*budget → elision, not summarization.

    The summarization sub-call is the heavy-weight escape hatch — it
    should only fire once the model is genuinely approaching its
    window (≥95% of budget). At sub-summary pressure the fast elision
    path must run instead, leaving the older tool payloads as the
    ``_ELIDED_TOOL_PREFIX`` one-liner and the recent 3 rounds verbatim.
    """
    from corlinman_agent.reasoning_loop import (
        _COMPACT_ELIDE_THRESHOLD,
        _ELIDED_TOOL_PREFIX,
        _compact_history,
        _estimate_tokens,
    )

    messages = _huge_tool_history(rounds=6, char_count=1_000)
    tokens = _estimate_tokens(messages)
    # Pick a budget that puts the message list between the elide and
    # summary thresholds: tokens >= 0.6 * budget, tokens < 0.95 *
    # budget. ``budget = int(tokens / 0.7)`` lands the estimate at
    # ~70% — comfortably above the elide cutoff (60%) and below the
    # summary cutoff (95%).
    budget = int(tokens / 0.7)
    assert tokens >= int(budget * _COMPACT_ELIDE_THRESHOLD), "bracket invariant"
    assert tokens < int(budget * 0.95), "bracket invariant"

    # Provider stub that fails the assertion if the summary path runs —
    # exposing a regression where the elision fast path is accidentally
    # bypassed at sub-threshold pressure.
    class _NeverCalledProvider:
        async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            raise AssertionError("summary path must not fire at sub-threshold pressure")
            # Unreachable — kept so this is a valid async generator.
            yield ProviderChunk(kind="done")

    out = await _compact_history(
        messages,
        budget=budget,
        provider=_NeverCalledProvider(),
        model="x",
    )
    # Elision path observable: tool messages collapsed to the one-liner.
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    assert any(m["content"].startswith(_ELIDED_TOOL_PREFIX) for m in tool_msgs)


@pytest.mark.asyncio
async def test_compact_summarizes_when_threshold_hit() -> None:
    """At ≥ 0.95 * budget pressure, the summarization sub-call runs.

    Injects a fake provider that emits a deterministic summary text;
    the compaction result should be ``[system, summary_block, *recent]``
    where ``summary_block`` carries the marker prefix.
    """
    from corlinman_agent.reasoning_loop import _compact_history

    messages = _huge_tool_history(rounds=6, char_count=1_000)

    # Tight budget so any non-trivial history clears the 0.95 threshold.
    budget = 200

    summary_text = "Task: refactor; decisions made; pending work captured."

    class _SummaryProvider:
        def __init__(self) -> None:
            self.calls_seen: list[dict[str, Any]] = []

        async def chat_stream(
            self,
            *,
            model: str,
            messages: list[dict[str, Any]],
            tools: Any = None,
            temperature: Any = None,
            max_tokens: Any = None,
            extra: Any = None,
        ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            self.calls_seen.append(
                {
                    "model": model,
                    "messages": list(messages),
                    "tools": tools,
                    "max_tokens": max_tokens,
                }
            )
            yield ProviderChunk(kind="token", text=summary_text)
            yield ProviderChunk(kind="done", finish_reason="stop")

    prov = _SummaryProvider()
    out = await _compact_history(
        messages,
        budget=budget,
        provider=prov,
        model="claude-sonnet-test",
    )

    # Sub-call fired exactly once, tools suppressed, model echoed.
    assert len(prov.calls_seen) == 1
    call = prov.calls_seen[0]
    assert call["tools"] is None
    assert call["model"] == "claude-sonnet-test"
    # The sub-call saw a leading system prompt + the older messages.
    sub_messages = call["messages"]
    assert sub_messages[0]["role"] == "system"
    assert "compacting" in sub_messages[0]["content"]

    # Result shape: leading system blocks + ONE synthetic summary block
    # + the last 3 assistant rounds (each with its matching tool msg).
    roles = [m.get("role") for m in out]
    assert roles[0] == "system"  # leading system preserved
    # The synthetic summary block sits right after the leading system.
    summary_block = out[1]
    assert summary_block["role"] == "system"
    assert summary_block["content"].startswith("PRIOR CONVERSATION SUMMARY:")
    assert summary_text in summary_block["content"]
    # Recent 3 assistant rounds (each = assistant + tool) preserved.
    recent_assistant = [m for m in out[2:] if m.get("role") == "assistant"]
    assert len(recent_assistant) == 3
    recent_tools = [m for m in out[2:] if m.get("role") == "tool"]
    assert len(recent_tools) == 3
    # Recent tool content NOT elided — the slow path drops the old
    # tool messages entirely and replaces them with the summary block.
    for tm in recent_tools:
        assert tm["content"] == "X" * 1_000


@pytest.mark.asyncio
async def test_compact_summary_provider_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the sub-provider call raises, compaction degrades to elision.

    Context overflow must never brick the chat — a transient 5xx /
    timeout on the summarization call should silently fall back to the
    cheap path so the parent reasoning loop still gets a sub-budget
    message list to feed the next round.
    """
    from corlinman_agent.reasoning_loop import (
        _ELIDED_TOOL_PREFIX,
        _compact_history,
        _estimate_tokens,
    )

    messages = _huge_tool_history(rounds=6, char_count=1_000)
    budget = 200

    class _BrokenProvider:
        async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            raise RuntimeError("simulated upstream 5xx")
            yield ProviderChunk(kind="done")  # unreachable

    # Intercept the warning so we can confirm the fallback fired
    # without depending on global structlog configuration.
    import corlinman_agent.reasoning_loop as rl_mod

    captured: list[tuple[str, dict]] = []

    class _StubLogger:
        def warning(self, event: str, **kw: object) -> None:
            captured.append((event, dict(kw)))

        def info(self, event: str, **kw: object) -> None:
            pass

        def exception(self, event: str, **kw: object) -> None:
            pass

    monkeypatch.setattr(rl_mod, "logger", _StubLogger())

    before = _estimate_tokens(messages)
    out = await _compact_history(
        messages,
        budget=budget,
        provider=_BrokenProvider(),
        model="x",
    )

    # Fallback observable: result is strictly smaller than the input
    # AND carries elided tool sentinels (the elision path, not the
    # summary path). We can't promise a strict sub-budget bound — the
    # elision strategy keeps the recent 3 rounds verbatim and only
    # collapses the older tool payloads to the sentinel.
    after = _estimate_tokens(out)
    assert after < before, "elision must reduce token estimate"
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    elided = [m for m in tool_msgs if m["content"].startswith(_ELIDED_TOOL_PREFIX)]
    assert elided, "elision one-liner should appear after summary fallback"

    # Warning fired with the failure reason captured.
    failure_warnings = [(ev, kw) for ev, kw in captured if ev == "agent.context.summarize_failed"]
    assert len(failure_warnings) == 1
    assert "5xx" in str(failure_warnings[0][1].get("error", ""))


@pytest.mark.asyncio
async def test_elided_tool_content_is_informative() -> None:
    """Elided tool payloads carry a per-tool one-liner, not a flat sentinel.

    ABSORB_MATRIX Dim 2 (c): hermes writes informative 1-line tool
    summaries on prune; claude-code's microcompact keeps enough shape
    for the model to know WHAT was elided. The sentinel must keep a
    stable prefix (idempotence + prompt-cache stability) while naming
    the tool, hinting its arguments, and recording the original size.
    """
    from corlinman_agent.reasoning_loop import (
        _ELIDED_TOOL_PREFIX,
        _compact_history,
    )

    messages = _huge_tool_history(rounds=6, char_count=1_000)
    out = await _compact_history(messages, budget=200, fast_path_only=True)

    tool_msgs = [m for m in out if m.get("role") == "tool"]
    elided = [m for m in tool_msgs if m["content"].startswith(_ELIDED_TOOL_PREFIX)]
    assert len(elided) == 3, "older 3 rounds elided"
    for tm in elided:
        # Tool name + args hint from the matching assistant shell.
        assert " t({})" in tm["content"], tm["content"]
        # Original payload size recorded.
        assert "1000 chars" in tm["content"], tm["content"]


@pytest.mark.asyncio
async def test_elided_summary_falls_back_to_plain_sentinel() -> None:
    """A tool message with no matching assistant shell gets the legacy
    flat sentinel — never a half-empty summary like `` (…) · 0 chars``."""
    from corlinman_agent.reasoning_loop import (
        _ELIDED_TOOL_CONTENT,
        _compact_history,
    )

    huge = "Z" * 1_000
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        # Orphan tool message: no assistant tool_calls shell anywhere.
        {"role": "tool", "tool_call_id": "orphan", "content": huge},
    ]
    for i in range(4):
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

    out = await _compact_history(messages, budget=200, fast_path_only=True)
    orphan = next(m for m in out if m.get("tool_call_id") == "orphan")
    assert orphan["content"] == _ELIDED_TOOL_CONTENT


@pytest.mark.asyncio
async def test_elide_returns_input_identity_when_nothing_new() -> None:
    """A second elide pass with nothing left to elide returns the SAME list.

    ABSORB_MATRIX Dim 2 (b): the reasoning loop invalidates its
    incremental token cache whenever compaction returns a fresh list.
    Without an identity return on the no-op pass, a saturated-but-
    unshrinkable history (heavy recent rounds) re-triggers a full copy
    + cache re-walk EVERY round for zero savings.
    """
    from corlinman_agent.reasoning_loop import _compact_history

    messages = _huge_tool_history(rounds=6, char_count=1_000)
    first = await _compact_history(messages, budget=200, fast_path_only=True)
    assert first is not messages, "first pass genuinely elided"
    second = await _compact_history(first, budget=200, fast_path_only=True)
    assert second is first, "no-op pass must preserve list identity"


@pytest.mark.asyncio
async def test_summary_pressure_prefers_elide_when_it_saves_enough() -> None:
    """≥ summary-threshold pressure first measures what elision saves.

    ABSORB_MATRIX Dim 2 (b) saved-token feedback: claude-code's
    microcompact feeds saved tokens back into the auto-compact
    threshold. Port: when the cheap elide pass alone pulls the estimate
    back under the summary threshold, the expensive summarize sub-call
    must NOT fire.
    """
    from corlinman_agent.reasoning_loop import (
        _COMPACT_SUMMARY_THRESHOLD,
        _ELIDED_TOOL_PREFIX,
        _compact_history,
        _estimate_tokens,
    )

    messages = _huge_tool_history(rounds=6, char_count=1_000)
    before = _estimate_tokens(messages)
    # Budget bracketing: original estimate ≥ 95% of budget (summary
    # pressure), but the post-elide estimate (recent 3 rounds verbatim ≈
    # half the payload) lands well under it.
    budget = int(before / _COMPACT_SUMMARY_THRESHOLD)
    assert before >= int(budget * _COMPACT_SUMMARY_THRESHOLD), "bracket invariant"

    class _NeverCalledProvider:
        async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            raise AssertionError("summarize must not fire when elision suffices")
            yield ProviderChunk(kind="done")  # unreachable

    out = await _compact_history(
        messages,
        budget=budget,
        provider=_NeverCalledProvider(),
        model="x",
    )
    after = _estimate_tokens(out)
    assert after < int(budget * _COMPACT_SUMMARY_THRESHOLD)
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    assert any(m["content"].startswith(_ELIDED_TOOL_PREFIX) for m in tool_msgs)


@pytest.mark.asyncio
async def test_real_output_starting_with_prefix_is_still_elided() -> None:
    """Tool output that happens to START with the sentinel prefix is not
    mistaken for an already-elided message (Codex PR#107 P2).

    The already-elided check must match the full sentinel shape (short,
    closed with ``]``), not any tool-controlled content sharing the
    prefix — otherwise a payload like ``[older tool output elided ...``
    + 100k chars silently bypasses compaction forever.
    """
    from corlinman_agent.reasoning_loop import (
        _ELIDED_TOOL_PREFIX,
        _compact_history,
    )

    adversarial = _ELIDED_TOOL_PREFIX + " …not really] " + "X" * 5_000
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
        messages.append({"role": "tool", "tool_call_id": cid, "content": adversarial})

    out = await _compact_history(messages, budget=200, fast_path_only=True)
    older_tools = [m for m in out if m.get("role") == "tool"][:3]
    for tm in older_tools:
        assert len(tm["content"]) < 300, "adversarial payload must be elided"
        assert tm["content"].startswith(_ELIDED_TOOL_PREFIX)


@pytest.mark.asyncio
async def test_duplicate_synthesized_call_ids_use_nearest_shell() -> None:
    """Elision labels a tool result with its OWN round's shell, not a
    later round's reusing the same synthesized id (Codex PR#107 P2).

    Providers that omit tool-call ids get per-response synthesized ids
    like ``call_0`` — multi-round histories then repeat the id across
    different tools. A transcript-wide last-wins map would label the old
    result with the WRONG tool; the lookup must be nearest-preceding.
    """
    from corlinman_agent.reasoning_loop import (
        _ELIDED_TOOL_PREFIX,
        _compact_history,
    )

    huge = "X" * 2_000

    def _round(name: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_0",  # synthesized, reused every round
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_0", "content": huge},
        ]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        *_round("first_tool"),
        *_round("second_tool"),
        *_round("r3"),
        *_round("r4"),
        *_round("r5"),
        *_round("r6"),
    ]

    out = await _compact_history(messages, budget=200, fast_path_only=True)
    elided = [
        m for m in out if m.get("role") == "tool" and m["content"].startswith(_ELIDED_TOOL_PREFIX)
    ]
    assert len(elided) == 3
    assert "first_tool" in elided[0]["content"], elided[0]["content"]
    assert "second_tool" in elided[1]["content"], elided[1]["content"]


# ---------------------------------------------------------------------------
# Fix 2 — Mid-task user message injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_user_message_drains_into_next_round() -> None:
    """``inject_user_message`` queues text that becomes a user msg next round.

    Drives a two-round loop. After the first round completes (a tool
    call), the test injects a supplemental user message; the second
    provider call's ``messages`` list must carry that text as a fresh
    ``role="user"`` block with the ``[追加上下文]`` marker prefix.
    """
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
    loop = ReasoningLoop(prov, tool_result_timeout=1.0)

    async def driver() -> None:
        async for e in loop.run(ChatStart(model="x", messages=[{"role": "user", "content": "go"}])):
            if isinstance(e, ToolCallEvent):
                # Inject BEFORE feeding the tool result so the queue
                # is populated when the next round starts. The order
                # doesn't actually matter — both happen between rounds.
                loop.inject_user_message("追问：还要查 X")
                loop.feed_tool_result(ToolResult(call_id=e.call_id, content='{"ok":true}'))

    await asyncio.wait_for(driver(), timeout=2.0)

    # Round 2's message list contains the supplement as the LAST user msg.
    assert len(prov.calls_seen) == 2
    round2_messages = prov.calls_seen[1]
    user_messages = [m for m in round2_messages if m.get("role") == "user"]
    # Original "go" + the injected supplement.
    assert any("追问" in m.get("content", "") for m in user_messages)
    supplements = [
        m
        for m in user_messages
        if isinstance(m.get("content"), str) and m["content"].startswith("[追加上下文] ")
    ]
    assert len(supplements) == 1
    assert supplements[0]["content"] == "[追加上下文] 追问：还要查 X"


@pytest.mark.asyncio
async def test_inject_user_message_thread_safe() -> None:
    """Multiple parallel injects all arrive on the next round.

    Validates the queue is unbounded enough to absorb a burst — a
    busy group chat can fire several messages between rounds, and
    silently dropping any of them would corrupt the conversation.
    """
    round1 = [
        ProviderChunk(kind="tool_call_start", tool_call_id="c1", tool_name="t"),
        ProviderChunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta="{}"),
        ProviderChunk(kind="tool_call_end", tool_call_id="c1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    round2 = [
        ProviderChunk(kind="token", text="ack"),
        ProviderChunk(kind="done", finish_reason="stop"),
    ]
    prov = _MultiRoundProvider([round1, round2])
    loop = ReasoningLoop(prov, tool_result_timeout=1.0)

    async def _injector(label: str) -> None:
        loop.inject_user_message(f"burst-{label}")

    async def driver() -> None:
        async for e in loop.run(ChatStart(model="x", messages=[{"role": "user", "content": "go"}])):
            if isinstance(e, ToolCallEvent):
                # Fan out 8 parallel injections from independent tasks
                # (gather guarantees the puts all complete before the
                # next round runs).
                await asyncio.gather(*(_injector(str(i)) for i in range(8)))
                loop.feed_tool_result(ToolResult(call_id=e.call_id, content='{"ok":true}'))

    await asyncio.wait_for(driver(), timeout=2.0)

    round2_messages = prov.calls_seen[1]
    supplements = [
        m
        for m in round2_messages
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m["content"].startswith("[追加上下文] burst-")
    ]
    # Every burst arrived — none silently dropped.
    assert len(supplements) == 8
    # Order is preserved (FIFO queue) — labels appear 0..7 in arrival
    # order. ``asyncio.gather`` is not ordering-guaranteed across awaits
    # but each ``put_nowait`` is synchronous, so the order matches the
    # iteration order over ``range(8)``.
    labels = [m["content"].split("-", 1)[1] for m in supplements]
    assert labels == [str(i) for i in range(8)]


@pytest.mark.asyncio
async def test_inject_empty_or_whitespace_is_dropped() -> None:
    """Empty / whitespace-only injects don't pollute the next round.

    A misbehaving channel handler that forwards a blank message
    shouldn't burn a user-supplement slot — drop quietly at the
    inject point so the queue stays clean.
    """
    round1 = [
        ProviderChunk(kind="tool_call_start", tool_call_id="c1", tool_name="t"),
        ProviderChunk(kind="tool_call_end", tool_call_id="c1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    round2 = [
        ProviderChunk(kind="done", finish_reason="stop"),
    ]
    prov = _MultiRoundProvider([round1, round2])
    loop = ReasoningLoop(prov, tool_result_timeout=1.0)

    async def driver() -> None:
        async for e in loop.run(ChatStart(model="x", messages=[{"role": "user", "content": "go"}])):
            if isinstance(e, ToolCallEvent):
                loop.inject_user_message("")
                loop.inject_user_message("   \n\t  ")
                loop.feed_tool_result(ToolResult(call_id=e.call_id, content='{"ok":true}'))

    await asyncio.wait_for(driver(), timeout=2.0)
    round2_messages = prov.calls_seen[1]
    supplements = [
        m
        for m in round2_messages
        if isinstance(m.get("content"), str) and m["content"].startswith("[追加上下文] ")
    ]
    assert supplements == []


# ---------------------------------------------------------------------------
# Perf — incremental token-estimate cache on ReasoningLoop
# ---------------------------------------------------------------------------


def test_token_cache_incremental_on_append() -> None:
    """After seeding the cache with N messages, appending M more
    re-walks only the M-message tail — NOT the full N+M list.

    Instruments the cache's underlying ``_estimate_chars`` helper with
    a counter wrapper to count how many messages were walked across
    two calls. With the cache the second call must walk strictly
    ``M`` messages, not ``N + M``.
    """
    from corlinman_agent import reasoning_loop as rl_mod
    from corlinman_agent.reasoning_loop import (
        ReasoningLoop,
        _estimate_chars,
        _estimate_tokens,
    )

    walked: list[int] = []
    original = _estimate_chars

    def _counting_estimate(msgs: list[dict[str, Any]]) -> int:
        walked.append(len(msgs))
        return original(msgs)

    # Monkeypatch the module-level reference so the bound method
    # (resolved via module attribute on each call) picks up the spy.
    saved = rl_mod._estimate_chars
    rl_mod._estimate_chars = _counting_estimate  # type: ignore[assignment]
    try:
        loop = ReasoningLoop(provider=object())
        first_batch = [{"role": "user", "content": f"msg-{i}"} for i in range(50)]
        # Seed the cache: full walk of 50 messages.
        seeded = loop.messages_total_token_estimate(first_batch)
        assert seeded == _estimate_tokens(first_batch)
        assert walked[-1] == 50  # full walk on cache miss

        # Append 10 more — the cache MUST only walk the new tail.
        extended = first_batch + [
            {"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 100} for i in range(10)
        ]
        before_count = len(walked)
        cached = loop.messages_total_token_estimate(extended)
        after_count = len(walked)

        # Exactly one new walk happened, and it walked exactly 10 msgs.
        assert after_count - before_count == 1
        assert walked[-1] == 10, (
            f"expected to walk only the 10 new tail messages, walked {walked[-1]}"
        )
        # And the cached running total matches the pure-function ground truth.
        assert cached == _estimate_tokens(extended)
    finally:
        rl_mod._estimate_chars = saved  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_token_cache_invalidates_on_compaction() -> None:
    """When the loop's compaction step returns a NEW list, the cache
    is invalidated so the next estimate call re-walks from scratch.
    """
    from corlinman_agent.reasoning_loop import ReasoningLoop

    loop = ReasoningLoop(provider=object())
    # Seed with a 10-message list.
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "a" * 20} for _ in range(10)]
    loop.messages_total_token_estimate(msgs)
    assert loop._messages_token_seen == 10
    assert loop._messages_char_total > 0

    # Compaction completed → fresh list returned → invalidate.
    loop._invalidate_token_cache()
    assert loop._messages_token_seen == 0
    assert loop._messages_char_total == 0

    # Next call re-walks from scratch and re-seeds.
    msgs_after = [{"role": "user", "content": "b" * 5} for _ in range(3)]
    out = loop.messages_total_token_estimate(msgs_after)
    assert loop._messages_token_seen == 3
    # 3 * 5 chars // 4 == 3 (per-message 5//4=1, summed) — exact match
    # against the pure walker.
    from corlinman_agent.reasoning_loop import _estimate_tokens as _et

    assert out == _et(msgs_after)


def test_token_cache_consistent_with_pure_function() -> None:
    """Across a randomized append/shrink/edit sequence the cache stays
    within ±1 of the pure ``_estimate_tokens`` result (the only
    permitted divergence is from integer-division rounding when a
    re-walked tail's chars don't align with the prefix's chars).
    """
    import random

    from corlinman_agent.reasoning_loop import (
        ReasoningLoop,
        _estimate_tokens,
    )

    rng = random.Random(0xC0FFEE)
    loop = ReasoningLoop(provider=object())
    msgs: list[dict[str, Any]] = []

    for _ in range(80):
        action = rng.choice(("append", "append", "append", "shrink", "edit_head"))
        if action == "append":
            length = rng.randint(0, 200)
            msgs.append(
                {"role": rng.choice(("user", "assistant", "tool")), "content": "z" * length}
            )
        elif action == "shrink" and msgs:
            # Mimic compaction: shrink list — cache must detect and re-walk.
            drop = rng.randint(1, max(1, len(msgs) // 2))
            msgs = msgs[:-drop]
        elif action == "edit_head" and msgs:
            # In-place head edit — fingerprint must catch this.
            new_msg = dict(msgs[0])
            new_msg["content"] = (new_msg.get("content") or "") + "X"
            msgs[0] = new_msg

        cached = loop.messages_total_token_estimate(msgs)
        truth = _estimate_tokens(msgs)
        # Cache tracks raw chars internally and divides by 4 at
        # retrieval, so the result is bit-exact equal to the pure
        # walker. (Spec allows ±1 for safety; we land at 0.)
        assert abs(cached - truth) <= 1, (
            f"cache diverged: cached={cached} truth={truth} "
            f"seen={loop._messages_token_seen} n={len(msgs)}"
        )


@pytest.mark.asyncio
async def test_token_cache_invalidates_through_run_when_compacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when ``_compact_history`` returns a fresh list during
    ``run()``, the cache invalidates so subsequent rounds re-seed.
    """
    from corlinman_agent import reasoning_loop as rl_mod

    # Force compaction to always return a fresh list (object identity
    # break) so the loop's identity check triggers the invalidation.
    async def _replacement_compact(
        msgs: list[dict[str, Any]],
        *,
        budget: int,
        provider: Any = None,
        model: str | None = None,
        fast_path_only: bool = False,
        prev_estimate: int | None = None,
        summary_allowed: bool = True,
        outcome: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        # Return a list with same content but different identity.
        return [dict(m) for m in msgs]

    monkeypatch.setattr(rl_mod, "_compact_history", _replacement_compact)

    round1 = [
        ProviderChunk(kind="tool_call_start", tool_call_id="c1", tool_name="t"),
        ProviderChunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta="{}"),
        ProviderChunk(kind="tool_call_end", tool_call_id="c1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    round2 = [
        ProviderChunk(kind="token", text="ok"),
        ProviderChunk(kind="done", finish_reason="stop"),
    ]
    prov = _MultiRoundProvider([round1, round2])
    loop = ReasoningLoop(prov, tool_result_timeout=1.0)

    invalidate_count = {"n": 0}
    real_invalidate = loop._invalidate_token_cache

    def _spy_invalidate() -> None:
        invalidate_count["n"] += 1
        real_invalidate()

    monkeypatch.setattr(loop, "_invalidate_token_cache", _spy_invalidate)

    async def driver() -> None:
        async for e in loop.run(ChatStart(model="x", messages=[{"role": "user", "content": "go"}])):
            if isinstance(e, ToolCallEvent):
                loop.feed_tool_result(ToolResult(call_id=e.call_id, content='{"ok":true}'))

    await asyncio.wait_for(driver(), timeout=2.0)
    # Two rounds → compaction ran twice → invalidate called twice
    # (identity always breaks with our replacement compactor).
    assert invalidate_count["n"] == 2


def test_compact_history_accepts_prev_estimate_kwarg() -> None:
    """The ``prev_estimate`` kwarg short-circuits the budget walk —
    when supplied with a sub-elide value, the function returns the
    input unchanged WITHOUT calling ``_estimate_tokens`` first.
    """
    import asyncio as _asyncio

    from corlinman_agent import reasoning_loop as rl_mod

    walked: list[int] = []
    real = rl_mod._estimate_tokens

    def _counting(msgs: list[dict[str, Any]]) -> int:
        walked.append(len(msgs))
        return real(msgs)

    saved = rl_mod._estimate_tokens
    rl_mod._estimate_tokens = _counting  # type: ignore[assignment]
    try:
        # A small message list — under the elide threshold of any
        # reasonable budget. Passthrough must take the fast exit.
        messages = [{"role": "user", "content": "hi"}]
        # 1 char -> 0 tokens (the pure function would compute this).
        # We supply prev_estimate=0 so the function skips the walk.
        out = _asyncio.run(
            rl_mod._compact_history(
                messages,
                budget=100_000,
                fast_path_only=True,
                prev_estimate=0,
            )
        )
        # Passthrough — input returned unchanged.
        assert out is messages
        # And the supplied prev_estimate short-circuited the initial
        # walk: zero recorded walks.
        assert walked == []
    finally:
        rl_mod._estimate_tokens = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# post_compact hook emission (Codex #109 round 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_emit_post_compact_fires_event() -> None:
    """The helper both compaction paths share fires ``post_compact`` with
    the before/after message counts."""
    from corlinman_agent.reasoning_loop import ReasoningLoop

    calls: list[tuple[str, dict, dict]] = []

    class _Runner:
        async def run_event_async(self, event, payload=None, ctx=None):
            calls.append((event, dict(payload or {}), dict(ctx or {})))

    loop = ReasoningLoop(provider=object())
    loop.set_hook_runner(_Runner())
    loop._session_key = "sess-pc"
    await loop._maybe_emit_post_compact(12, 5)
    assert calls == [
        ("post_compact", {"messages_before": 12, "messages_after": 5}, {"session_key": "sess-pc"})
    ]


@pytest.mark.asyncio
async def test_maybe_emit_post_compact_defensive() -> None:
    """No runner → no-op; a raising hook is swallowed (never wedges the turn)."""
    from corlinman_agent.reasoning_loop import ReasoningLoop

    loop = ReasoningLoop(provider=object())
    # No runner wired — must not raise.
    await loop._maybe_emit_post_compact(3, 1)

    class _Boom:
        async def run_event_async(self, event, payload=None, ctx=None):
            raise RuntimeError("hook blew up")

    loop.set_hook_runner(_Boom())
    await loop._maybe_emit_post_compact(3, 1)  # swallowed, no raise


# ---------------------------------------------------------------------------
# model-aware compaction budget (_resolve_context_budget)
# ---------------------------------------------------------------------------


def test_resolve_context_budget_uses_model_window_minus_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that declares a context window sizes the budget from it
    (window minus a capped reserve), not the flat default."""
    from corlinman_agent import reasoning_loop as rl

    monkeypatch.delenv("CORLINMAN_CONTEXT_BUDGET", raising=False)
    monkeypatch.setattr(rl, "_CONTEXT_BUDGET_OVERRIDE", None)

    class _P:
        def context_window(self, model: str) -> int | None:
            return 200_000 if model == "big" else None

    budget = rl._resolve_context_budget(_P(), "big")
    # 200k - min(0.15*200k=30k, cap 48k) = 170k
    assert budget == 200_000 - 30_000


def test_resolve_context_budget_reserve_is_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a huge window the reserve is capped (not 15%)."""
    from corlinman_agent import reasoning_loop as rl

    monkeypatch.delenv("CORLINMAN_CONTEXT_BUDGET", raising=False)
    monkeypatch.setattr(rl, "_CONTEXT_BUDGET_OVERRIDE", None)

    class _P:
        def context_window(self, model: str) -> int | None:
            return 1_000_000

    budget = rl._resolve_context_budget(_P(), "m")
    assert budget == 1_000_000 - rl._CONTEXT_OUTPUT_RESERVE_CAP


def test_resolve_context_budget_fixed_reserve_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fixed reserve (claude-code ``AUTOCOMPACT_BUFFER`` semantics:
    ``window - buffer``) wins over the proportional fraction (ABSORB_MATRIX
    Dim 2)."""
    from corlinman_agent import reasoning_loop as rl

    monkeypatch.delenv("CORLINMAN_CONTEXT_BUDGET", raising=False)
    monkeypatch.setattr(rl, "_CONTEXT_BUDGET_OVERRIDE", None)
    monkeypatch.setattr(rl, "_CONTEXT_RESERVE_TOKENS", 13_000)

    class _P:
        def context_window(self, model: str) -> int | None:
            return 200_000

    assert rl._resolve_context_budget(_P(), "big") == 200_000 - 13_000


def test_resolve_context_budget_reserve_fraction_and_cap_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reserve fraction + cap are operator-tunable (default-preserving)."""
    from corlinman_agent import reasoning_loop as rl

    monkeypatch.delenv("CORLINMAN_CONTEXT_BUDGET", raising=False)
    monkeypatch.setattr(rl, "_CONTEXT_BUDGET_OVERRIDE", None)
    monkeypatch.setattr(rl, "_CONTEXT_RESERVE_TOKENS", None)
    monkeypatch.setattr(rl, "_CONTEXT_OUTPUT_RESERVE_FRACTION", 0.25)
    monkeypatch.setattr(rl, "_CONTEXT_OUTPUT_RESERVE_CAP", 100_000)

    class _P:
        def context_window(self, model: str) -> int | None:
            return 200_000

    # 200k - min(0.25*200k=50k, cap 100k) = 150k
    assert rl._resolve_context_budget(_P(), "big") == 200_000 - 50_000


# ---------------------------------------------------------------------------
# ABSORB_MATRIX Dim 2 (d)/(e) — summary-LLM cooldown / anti-thrash + breaker
# ---------------------------------------------------------------------------


def _tool_round(cid: str) -> list[ProviderChunk]:
    """One tool-call round for :class:`_MultiRoundProvider`."""
    return [
        ProviderChunk(kind="tool_call_start", tool_call_id=cid, tool_name="t"),
        ProviderChunk(kind="tool_call_delta", tool_call_id=cid, arguments_delta="{}"),
        ProviderChunk(kind="tool_call_end", tool_call_id=cid),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]


async def _drive(loop: ReasoningLoop, start: ChatStart, *, tool_content: str) -> None:
    """Run the loop, satisfying every tool call with ``tool_content``."""
    async for e in loop.run(start):
        if isinstance(e, ToolCallEvent):
            loop.feed_tool_result(ToolResult(call_id=e.call_id, content=tool_content))


class _SummaryFailProvider:
    """Drives ``tool_rounds`` tool-call rounds; the compaction summary
    sub-call (detected by ``tools is None`` + the capped summary
    ``max_tokens``) is counted and always fails, so the loop's cooldown /
    breaker logic engages while the reasoning rounds keep flowing.
    """

    def __init__(self, *, tool_rounds: int) -> None:
        self._tool_rounds = tool_rounds
        self.summary_calls = 0
        self.main_calls = 0

    async def chat_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Any = None,
        temperature: Any = None,
        max_tokens: Any = None,
        extra: Any = None,
    ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        from corlinman_agent.reasoning_loop import _COMPACT_SUMMARY_MAX_TOKENS

        # Compaction summary sub-call — tools suppressed + capped output.
        if tools is None and max_tokens == _COMPACT_SUMMARY_MAX_TOKENS:
            self.summary_calls += 1
            raise RuntimeError("summary boom")
            yield ProviderChunk(kind="done")  # unreachable — keeps this a generator
        # Main reasoning round.
        self.main_calls += 1
        if self.main_calls <= self._tool_rounds:
            cid = f"call{self.main_calls}"
            yield ProviderChunk(kind="tool_call_start", tool_call_id=cid, tool_name="t")
            yield ProviderChunk(kind="tool_call_delta", tool_call_id=cid, arguments_delta="{}")
            yield ProviderChunk(kind="tool_call_end", tool_call_id=cid)
            yield ProviderChunk(kind="done", finish_reason="tool_calls")
        else:
            yield ProviderChunk(kind="token", text="final")
            yield ProviderChunk(kind="done", finish_reason="stop")


@pytest.mark.asyncio
async def test_summary_failure_sets_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed summary sub-call backs the slow path off for COOLDOWN rounds.

    ABSORB_MATRIX Dim 2 (d): with pressure pinned above the summary
    threshold every round, the summarizer is attempted, fails, and is NOT
    re-attempted until ``_COMPACT_SUMMARY_COOLDOWN_ROUNDS`` rounds later —
    counted via the provider's summary sub-calls.
    """
    from corlinman_agent import reasoning_loop as rl_mod

    # Tight budget so any non-trivial history clears the summary threshold;
    # small cooldown; breaker high so it can't interfere with this test.
    monkeypatch.setattr(rl_mod, "_CONTEXT_BUDGET_OVERRIDE", 200)
    monkeypatch.setattr(rl_mod, "_COMPACT_SUMMARY_COOLDOWN_ROUNDS", 3)
    monkeypatch.setattr(rl_mod, "_COMPACT_SUMMARY_BREAKER_LIMIT", 99)

    prov = _SummaryFailProvider(tool_rounds=5)
    loop = ReasoningLoop(prov, tool_result_timeout=1.0)
    start = ChatStart(model="x", messages=_huge_tool_history(rounds=6, char_count=1_000))

    await asyncio.wait_for(_drive(loop, start, tool_content="Z" * 4_000), timeout=3.0)

    # Compaction rounds r=0..5. Attempts: r=0 (fail → cooldown_until=0+3+1=4,
    # i.e. exactly 3 rounds skipped), suppressed r=1,2,3, re-attempt r=4
    # (fail → cooldown_until=8), suppressed r=5. Exactly two sub-calls fire.
    assert prov.summary_calls == 2
    assert loop._summary_failures == 2
    assert loop._summary_disabled is False


@pytest.mark.asyncio
async def test_summary_breaker_disables_after_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """After ``_COMPACT_SUMMARY_BREAKER_LIMIT`` failures the slow path is
    disabled for the rest of the turn — no further attempts even once the
    cooldown would have elapsed (ABSORB_MATRIX Dim 2 (e)).
    """
    from corlinman_agent import reasoning_loop as rl_mod

    monkeypatch.setattr(rl_mod, "_CONTEXT_BUDGET_OVERRIDE", 200)
    # Cooldown of 1 so a would-be re-attempt is allowed every round; only
    # the breaker keeps the summarizer off.
    monkeypatch.setattr(rl_mod, "_COMPACT_SUMMARY_COOLDOWN_ROUNDS", 1)
    monkeypatch.setattr(rl_mod, "_COMPACT_SUMMARY_BREAKER_LIMIT", 3)

    prov = _SummaryFailProvider(tool_rounds=6)
    loop = ReasoningLoop(prov, tool_result_timeout=1.0)
    start = ChatStart(model="x", messages=_huge_tool_history(rounds=6, char_count=1_000))

    await asyncio.wait_for(_drive(loop, start, tool_content="Z" * 4_000), timeout=3.0)

    # r=0 fail (cd=1), r=1 fail (cd=2), r=2 fail → breaker trips. r=3+ are
    # never re-attempted despite the 1-round cooldown having elapsed.
    assert prov.summary_calls == 3
    assert loop._summary_disabled is True


@pytest.mark.asyncio
async def test_summary_success_resets_failure_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful summary re-arms the failure streak (Dim 2 (e)).

    Uses a stubbed ``_compact_history`` so the outcome fed to the breaker
    state machine is deterministic: the first allowed attempt fails, the
    next succeeds — the failure count must climb to 1 then reset to 0.
    """
    from corlinman_agent import reasoning_loop as rl_mod

    monkeypatch.setattr(rl_mod, "_COMPACT_SUMMARY_COOLDOWN_ROUNDS", 1)
    monkeypatch.setattr(rl_mod, "_COMPACT_SUMMARY_BREAKER_LIMIT", 99)

    prov = _MultiRoundProvider([_tool_round(f"c{i}") for i in range(4)])
    loop = ReasoningLoop(prov, tool_result_timeout=1.0)

    failures_history: list[int] = []
    calls = {"n": -1}

    async def _fake_compact(
        msgs: list[dict[str, Any]],
        *,
        budget: int,
        provider: Any = None,
        model: str | None = None,
        fast_path_only: bool = False,
        prev_estimate: int | None = None,
        summary_allowed: bool = True,
        outcome: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if outcome is not None and summary_allowed:
            failures_history.append(loop._summary_failures)
            calls["n"] += 1
            outcome["summary_attempted"] = True
            if calls["n"] == 0:
                outcome["summary_failed"] = True  # first attempt fails
            else:
                outcome["summary_failed"] = False
                outcome["summary_saved_fraction"] = 0.5  # healthy savings
        return msgs

    monkeypatch.setattr(rl_mod, "_compact_history", _fake_compact)

    start = ChatStart(model="x", messages=[{"role": "user", "content": "go"}])
    await asyncio.wait_for(_drive(loop, start, tool_content="ok"), timeout=3.0)

    # Snapshot taken at the START of each attempt: 0 (nothing yet), 1 (the
    # first attempt failed), 0 (the success reset it).
    assert failures_history[:3] == [0, 1, 0]
    assert loop._summary_failures == 0
    assert loop._summary_low_savings_streak == 0


@pytest.mark.asyncio
async def test_summary_low_savings_streak_trips_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two consecutive low-savings successes trip the cooldown (Dim 2 (d)).

    A summary that shrinks the window by < 10% is barely worth its cost;
    a run of them should back the slow path off just like a failure would.
    """
    from corlinman_agent import reasoning_loop as rl_mod

    monkeypatch.setattr(rl_mod, "_COMPACT_SUMMARY_COOLDOWN_ROUNDS", 3)
    monkeypatch.setattr(rl_mod, "_COMPACT_SUMMARY_BREAKER_LIMIT", 99)

    allowed_seen: list[bool] = []

    async def _fake_compact(
        msgs: list[dict[str, Any]],
        *,
        budget: int,
        provider: Any = None,
        model: str | None = None,
        fast_path_only: bool = False,
        prev_estimate: int | None = None,
        summary_allowed: bool = True,
        outcome: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        allowed_seen.append(summary_allowed)
        if outcome is not None and summary_allowed:
            outcome["summary_attempted"] = True
            outcome["summary_failed"] = False
            outcome["summary_saved_fraction"] = 0.02  # < 0.10 → low savings
        return msgs

    monkeypatch.setattr(rl_mod, "_compact_history", _fake_compact)

    prov = _MultiRoundProvider([_tool_round(f"c{i}") for i in range(6)])
    loop = ReasoningLoop(prov, tool_result_timeout=1.0)
    start = ChatStart(model="x", messages=[{"role": "user", "content": "go"}])
    await asyncio.wait_for(_drive(loop, start, tool_content="ok"), timeout=3.0)

    # r=0 low-savings success (streak→1); r=1 low-savings success (streak→2
    # → cooldown_until = 1+3+1 = 5, exactly 3 rounds skipped, streak reset);
    # r=2,3,4 suppressed; r=5 allowed again.
    assert allowed_seen[:6] == [True, True, False, False, False, True]


@pytest.mark.asyncio
async def test_summary_failure_resets_low_savings_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed attempt between two low-savings successes breaks the streak
    (Codex #111): anti-thrash fires only on TRULY consecutive low-savings
    successes, not ones separated by a failure + its cooldown.
    """
    from corlinman_agent import reasoning_loop as rl_mod

    monkeypatch.setattr(rl_mod, "_COMPACT_SUMMARY_COOLDOWN_ROUNDS", 2)
    monkeypatch.setattr(rl_mod, "_COMPACT_SUMMARY_BREAKER_LIMIT", 99)

    # Scripted per-attempt outcomes: low-savings success, then failure,
    # then low-savings success — the third must NOT trip the streak
    # cooldown (streak restarted at 1 after the failure).
    script = [
        {"summary_failed": False, "summary_saved_fraction": 0.02},
        {"summary_failed": True, "summary_saved_fraction": 0.0},
        {"summary_failed": False, "summary_saved_fraction": 0.02},
    ]
    attempts: list[int] = []
    allowed_seen: list[bool] = []

    async def _fake_compact(
        msgs: list[dict[str, Any]],
        *,
        budget: int,
        provider: Any = None,
        model: str | None = None,
        fast_path_only: bool = False,
        prev_estimate: int | None = None,
        summary_allowed: bool = True,
        outcome: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        allowed_seen.append(summary_allowed)
        if outcome is not None and summary_allowed and script:
            step = script.pop(0)
            attempts.append(1)
            outcome["summary_attempted"] = True
            outcome["summary_failed"] = step["summary_failed"]
            outcome["summary_saved_fraction"] = step["summary_saved_fraction"]
        return msgs

    monkeypatch.setattr(rl_mod, "_compact_history", _fake_compact)

    prov = _MultiRoundProvider([_tool_round(f"c{i}") for i in range(6)])
    loop = ReasoningLoop(prov, tool_result_timeout=1.0)
    start = ChatStart(model="x", messages=[{"role": "user", "content": "go"}])
    await asyncio.wait_for(_drive(loop, start, tool_content="ok"), timeout=3.0)

    # r=0 low-savings success (streak→1); r=1 failure (cooldown_until=
    # 1+2+1=4, exactly 2 rounds skipped, streak RESET to 0); r=2,3
    # suppressed; r=4 attempt: low-savings success → streak→1 only — NO
    # cooldown, so r=5 stays allowed. (Without the reset, r=4 would read
    # streak=2, trip, and suppress r=5.)
    assert allowed_seen[:6] == [True, True, False, False, True, True]
    assert loop._summary_low_savings_streak == 1
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_summary_allowed_false_gates_only_llm_call() -> None:
    """``summary_allowed=False`` suppresses the LLM sub-call only — the
    cheap elide path still runs (Dim 2 (d): elide is never gated).
    """
    from corlinman_agent.reasoning_loop import _ELIDED_TOOL_PREFIX, _compact_history

    messages = _huge_tool_history(rounds=6, char_count=1_000)

    class _NeverSummaryProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            self.calls += 1
            raise AssertionError("summary sub-call must not fire when summary_allowed=False")
            yield ProviderChunk(kind="done")  # unreachable

    prov = _NeverSummaryProvider()
    outcome: dict[str, Any] = {}
    out = await _compact_history(
        messages,
        budget=200,
        provider=prov,
        model="x",
        summary_allowed=False,
        outcome=outcome,
    )

    # Provider never touched; no summary attempt recorded.
    assert prov.calls == 0
    assert outcome.get("summary_attempted", False) is False
    # Elide STILL happened — older tool payloads collapsed to the sentinel.
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    assert any(m["content"].startswith(_ELIDED_TOOL_PREFIX) for m in tool_msgs)


@pytest.mark.asyncio
async def test_compact_outcome_records_summary_success() -> None:
    """The ``outcome`` dict captures a produced summary + its saved fraction."""
    from corlinman_agent.reasoning_loop import _compact_history

    messages = _huge_tool_history(rounds=6, char_count=1_000)

    class _SummaryProvider:
        async def chat_stream(
            self,
            *,
            model: str,
            messages: list[dict[str, Any]],
            tools: Any = None,
            temperature: Any = None,
            max_tokens: Any = None,
            extra: Any = None,
        ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            yield ProviderChunk(kind="token", text="dense summary")
            yield ProviderChunk(kind="done", finish_reason="stop")

    outcome: dict[str, Any] = {}
    await _compact_history(
        messages, budget=200, provider=_SummaryProvider(), model="x", outcome=outcome
    )
    assert outcome["summary_attempted"] is True
    assert outcome["summary_failed"] is False
    assert outcome["summary_saved_fraction"] > 0.0


@pytest.mark.asyncio
async def test_compact_outcome_records_summary_failure() -> None:
    """A raising sub-call records ``summary_failed=True`` + zero savings."""
    from corlinman_agent.reasoning_loop import _compact_history

    messages = _huge_tool_history(rounds=6, char_count=1_000)

    class _BrokenProvider:
        async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            raise RuntimeError("simulated 5xx")
            yield ProviderChunk(kind="done")  # unreachable

    outcome: dict[str, Any] = {}
    await _compact_history(
        messages, budget=200, provider=_BrokenProvider(), model="x", outcome=outcome
    )
    assert outcome["summary_attempted"] is True
    assert outcome["summary_failed"] is True
    assert outcome["summary_saved_fraction"] == 0.0


def test_env_positive_int_parses_and_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_env_positive_int``: default when unset/garbage, override when
    valid, floored when below the floor.
    """
    from corlinman_agent import reasoning_loop as rl

    name = "CORLINMAN_TEST_POSITIVE_INT_XYZ"
    monkeypatch.delenv(name, raising=False)
    assert rl._env_positive_int(name, 5, floor=1) == 5  # default
    monkeypatch.setenv(name, "8")
    assert rl._env_positive_int(name, 5, floor=1) == 8  # override
    monkeypatch.setenv(name, "garbage")
    assert rl._env_positive_int(name, 5, floor=1) == 5  # bad → default
    monkeypatch.setenv(name, "0")
    assert rl._env_positive_int(name, 5, floor=1) == 1  # below floor → floored
    monkeypatch.setenv(name, "-4")
    assert rl._env_positive_int(name, 5, floor=1) == 1  # negative → floored


def test_compact_summary_cooldown_breaker_knob_defaults() -> None:
    """The import-time cooldown / breaker knobs land at their floored defaults."""
    from corlinman_agent import reasoning_loop as rl

    assert rl._COMPACT_SUMMARY_COOLDOWN_ROUNDS >= 1
    assert rl._COMPACT_SUMMARY_BREAKER_LIMIT >= 1


def test_env_positive_float_rejects_bad_values() -> None:
    from corlinman_agent import reasoning_loop as rl

    assert rl._env_positive_float("CORLINMAN_NO_SUCH_ENV_XYZ", 0.15) == 0.15


def test_resolve_context_budget_falls_back_without_accessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider with no context_window accessor → flat default."""
    from corlinman_agent import reasoning_loop as rl

    monkeypatch.delenv("CORLINMAN_CONTEXT_BUDGET", raising=False)
    monkeypatch.setattr(rl, "_CONTEXT_BUDGET_OVERRIDE", None)

    budget = rl._resolve_context_budget(object(), "anything")
    assert budget == rl._CONTEXT_BUDGET_DEFAULT


def test_resolve_context_budget_override_pins_every_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit operator override wins over the model window."""
    from corlinman_agent import reasoning_loop as rl

    monkeypatch.setattr(rl, "_CONTEXT_BUDGET_OVERRIDE", 50_000)

    class _P:
        def context_window(self, model: str) -> int | None:
            return 200_000

    assert rl._resolve_context_budget(_P(), "big") == 50_000


def test_resolve_context_budget_bad_accessor_value_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-positive / non-int / raising accessor falls back to default."""
    from corlinman_agent import reasoning_loop as rl

    monkeypatch.delenv("CORLINMAN_CONTEXT_BUDGET", raising=False)
    monkeypatch.setattr(rl, "_CONTEXT_BUDGET_OVERRIDE", None)

    class _Zero:
        def context_window(self, model: str) -> int:
            return 0

    class _Raises:
        def context_window(self, model: str) -> int:
            raise RuntimeError("boom")

    assert rl._resolve_context_budget(_Zero(), "m") == rl._CONTEXT_BUDGET_DEFAULT
    assert rl._resolve_context_budget(_Raises(), "m") == rl._CONTEXT_BUDGET_DEFAULT


# --------------------------------------------------------------------- #
# gap empty-answer-recovery: reasoning-only turn → one-shot final-answer  #
# nudge (the "agent replied blank" bug).                                 #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reasoning_only_turn_nudges_once_for_final_answer() -> None:
    """A turn that streams only reasoning (no visible text, no tool calls)
    must be nudged exactly once for the visible final answer, instead of
    surfacing an empty reply."""
    from corlinman_agent.reasoning_loop import _EMPTY_ANSWER_NUDGE

    prov = _MultiRoundProvider(
        [
            # Round 0: chain-of-thought only — no visible content.
            [
                ProviderChunk(kind="token", text="thinking…", is_reasoning=True),
                ProviderChunk(kind="done", finish_reason="stop"),
            ],
            # Round 1 (after the nudge): the visible answer.
            [
                ProviderChunk(kind="token", text="42"),
                ProviderChunk(kind="done", finish_reason="stop"),
            ],
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))

    # The provider was re-invoked once (the nudge round).
    assert len(prov.calls_seen) == 2
    # The injected nudge rode a user turn into the second call.
    assert prov.calls_seen[1][-1] == {
        "role": "user",
        "content": _EMPTY_ANSWER_NUDGE,
    }
    # The visible reply is the second round's answer (reasoning excluded).
    visible = [e.text for e in events if isinstance(e, TokenEvent) and not e.is_reasoning]
    assert visible == ["42"]
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_reasoning_only_nudge_is_one_shot() -> None:
    """If the model stays reasoning-only even after the nudge, the loop
    terminates (one-shot latch) rather than nudging forever."""
    prov = _MultiRoundProvider(
        [
            [
                ProviderChunk(kind="token", text="thinking…", is_reasoning=True),
                ProviderChunk(kind="done", finish_reason="stop"),
            ],
            # Still reasoning-only after the nudge.
            [
                ProviderChunk(kind="token", text="still thinking…", is_reasoning=True),
                ProviderChunk(kind="done", finish_reason="stop"),
            ],
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))

    # Exactly one nudge: round 0 + one retry, then it gives up.
    assert len(prov.calls_seen) == 2
    visible = [e.text for e in events if isinstance(e, TokenEvent) and not e.is_reasoning]
    assert visible == []
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_reasoning_plus_visible_text_does_not_nudge() -> None:
    """A normal turn that emits reasoning AND a visible answer in the same
    round must NOT trigger the recovery nudge."""
    prov = _MultiRoundProvider(
        [
            [
                ProviderChunk(kind="token", text="thinking…", is_reasoning=True),
                ProviderChunk(kind="token", text="the answer"),
                ProviderChunk(kind="done", finish_reason="stop"),
            ],
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))

    # Single provider call — no nudge round.
    assert len(prov.calls_seen) == 1
    visible = [e.text for e in events if isinstance(e, TokenEvent) and not e.is_reasoning]
    assert visible == ["the answer"]
    assert isinstance(events[-1], DoneEvent)


def test_retry_backoff_honors_provider_hint() -> None:
    """A positive delay hint (provider retry-after / reset) is returned
    verbatim — no jitter applied on top."""
    from corlinman_agent.reasoning_loop import _retry_backoff_seconds

    assert _retry_backoff_seconds(1, 5.0) == 5.0
    assert _retry_backoff_seconds(3, 12.5, rand=lambda: 0.9) == 12.5


def test_retry_backoff_equal_jitter_range() -> None:
    """No hint (0.0) → exponential 0.5*2^(n-1) cap 16 with equal jitter over
    [base/2, base]; deterministic via injected rand (ABSORB_MATRIX Dim 1)."""
    from corlinman_agent.reasoning_loop import _retry_backoff_seconds

    # attempt 1: base 0.5 → [0.25, 0.5]
    assert _retry_backoff_seconds(1, 0.0, rand=lambda: 0.0) == 0.25
    assert _retry_backoff_seconds(1, 0.0, rand=lambda: 1.0) == 0.5
    # attempt 6: base capped at 16 → [8, 16]
    assert _retry_backoff_seconds(6, 0.0, rand=lambda: 0.0) == 8.0
    assert _retry_backoff_seconds(6, 0.0, rand=lambda: 1.0) == 16.0
    # a mid-jitter value spreads off the fixed base (thundering-herd defence)
    assert _retry_backoff_seconds(4, 0.0, rand=lambda: 0.5) == 3.0  # base 4 → 2+2*0.5
