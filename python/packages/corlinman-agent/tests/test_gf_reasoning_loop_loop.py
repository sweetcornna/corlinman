"""Gap-fill unit tests for the reasoning loop (lane-reasoning-loop).

Covers the lane's additive behaviours:

* retry-then-succeed on transient overload (WP7 backoff loop);
* cross-model fallback switch on sustained overload (E4);
* context-overflow shrink-and-retry, including ``.limit``-aware sizing;
* consecutive duplicate-turn dedup at history-extend;
* CJK / multimodal token estimate > naive chars//4;
* per-model USD cost including cache_creation;
* C4 ``_is_error`` marker on the error tool message;
* C3 ``run_stop`` no-op when no hook runner is wired (and honour path);
* tool-result spill-to-disk for oversized results;
* gated auto-continue (off by default; nudges when autonomous).

These names are uniquely prefixed ``test_gf_reasoning_loop_*`` so they
never collide with the sibling-lane suites or the existing
``test_reasoning_loop.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_agent import (
    ChatStart,
    DoneEvent,
    ErrorEvent,
    ReasoningLoop,
    ToolCallEvent,
    ToolResult,
)
from corlinman_agent.reasoning_loop import (
    _append_user_turn,
    _content_hash,
    _dedup_consecutive_turns,
    _estimate_tokens,
    _estimate_turn_cost_usd,
    _extend_with_tool_round,
    _spill_tool_result,
)
from corlinman_providers.base import ProviderChunk
from corlinman_providers.failover import (
    ContextOverflowError,
    OverloadedError,
    RateLimitError,
)
from corlinman_providers.specs import AliasEntry


async def _collect(loop: ReasoningLoop, start: ChatStart) -> list:
    out = []
    async for e in loop.run(start):
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# specs.py — AliasEntry.fallback_models
# ---------------------------------------------------------------------------


def test_gf_reasoning_loop_alias_fallback_models_default_empty() -> None:
    entry = AliasEntry(provider="anthropic", model="claude-opus-4")
    assert entry.fallback_models == []


def test_gf_reasoning_loop_alias_fallback_models_set() -> None:
    entry = AliasEntry(
        provider="anthropic",
        model="claude-opus-4",
        fallback_models=["claude-sonnet-4-6", "claude-haiku-4-5"],
    )
    assert entry.fallback_models == ["claude-sonnet-4-6", "claude-haiku-4-5"]


# ---------------------------------------------------------------------------
# Retry / backoff (WP7) — transient error then success
# ---------------------------------------------------------------------------


class _FlakyProvider:
    """Raises ``exc`` on the first N attempts, then streams a clean turn."""

    def __init__(self, exc: BaseException, fail_times: int) -> None:
        self._exc = exc
        self._fail_times = fail_times
        self.attempts = 0

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise self._exc
        yield ProviderChunk(kind="token", text="ok")
        yield ProviderChunk(kind="done", finish_reason="stop")


@pytest.mark.asyncio
async def test_gf_reasoning_loop_backoff_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Avoid real sleeping.
    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    prov = _FlakyProvider(OverloadedError("overloaded"), fail_times=1)
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    assert prov.attempts == 2  # one failure + one success
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_gf_reasoning_loop_rate_limit_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    async def _capture_sleep(d: float) -> None:
        slept.append(d)

    monkeypatch.setattr(asyncio, "sleep", _capture_sleep)
    exc = RateLimitError("slow down", retry_after_ms=2_000)
    prov = _FlakyProvider(exc, fail_times=1)
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    assert isinstance(events[-1], DoneEvent)
    # The 2000ms Retry-After should be honoured as a 2.0s sleep.
    assert slept and abs(slept[0] - 2.0) < 0.01


# ---------------------------------------------------------------------------
# Cross-model fallback on SUSTAINED overload (E4)
# ---------------------------------------------------------------------------


class _ModelAwareProvider:
    """Overloads for any model in ``overloaded_models``; succeeds otherwise.

    Records the sequence of models it was asked to serve.
    """

    def __init__(self, overloaded_models: set[str]) -> None:
        self._overloaded = overloaded_models
        self.models_seen: list[str] = []

    async def chat_stream(
        self, *, model: str, **_: Any
    ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.models_seen.append(model)
        if model in self._overloaded:
            raise OverloadedError("overloaded")
        yield ProviderChunk(kind="token", text="served by " + model)
        yield ProviderChunk(kind="done", finish_reason="stop")


@pytest.mark.asyncio
async def test_gf_reasoning_loop_fallback_on_sustained_overload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    # Primary model is permanently overloaded; the fallback is healthy.
    prov = _ModelAwareProvider(overloaded_models={"primary"})
    loop = ReasoningLoop(prov, fallback_models=["secondary"])
    events = await _collect(loop, ChatStart(model="primary", messages=[]))
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].finish_reason == "stop"
    # It exhausted retries on "primary" then switched to "secondary".
    assert prov.models_seen[0] == "primary"
    assert prov.models_seen[-1] == "secondary"
    assert "secondary" in prov.models_seen


@pytest.mark.asyncio
async def test_gf_reasoning_loop_no_fallback_surfaces_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    prov = _ModelAwareProvider(overloaded_models={"primary"})
    # No fallback models configured → sustained overload surfaces an error.
    loop = ReasoningLoop(prov, fallback_models=[])
    events = await _collect(loop, ChatStart(model="primary", messages=[]))
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].reason == "overloaded"


class _MessageCapturingProvider(_ModelAwareProvider):
    """Also records the ``messages`` payload each call was given."""

    def __init__(self, overloaded_models: set[str]) -> None:
        super().__init__(overloaded_models)
        self.messages_seen: list[list[dict[str, Any]]] = []

    async def chat_stream(
        self, *, model: str, messages: list[dict[str, Any]] | None = None, **kw: Any
    ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.messages_seen.append(list(messages or []))
        async for chunk in super().chat_stream(model=model, **kw):
            yield chunk


@pytest.mark.asyncio
async def test_gf_reasoning_loop_fallback_strips_thinking_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E4: thinking blocks (and their model-minted signatures) must not be
    replayed to a DIFFERENT model after a fallback swap — Anthropic-style
    backends reject a signature minted by another model, turning a
    recoverable overload into a hard 400 on the fallback."""

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    prov = _MessageCapturingProvider(overloaded_models={"primary"})
    loop = ReasoningLoop(prov, fallback_models=["secondary"])
    history = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "let me think", "signature": "sig-A"},
                {"type": "text", "text": "prior answer"},
            ],
        },
        {"role": "user", "content": "follow-up"},
    ]
    events = await _collect(loop, ChatStart(model="primary", messages=history))
    assert isinstance(events[-1], DoneEvent)

    # The primary attempts saw the history verbatim.
    first_call = prov.messages_seen[0]
    assert any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "thinking" for b in m["content"])
        for m in first_call
    )
    # Every call on the fallback model carries neither a thinking block
    # nor any stray ``signature`` key.
    fallback_calls = [
        msgs
        for msgs, model in zip(prov.messages_seen, prov.models_seen, strict=True)
        if model == "secondary"
    ]
    assert fallback_calls
    for msgs in fallback_calls:
        for m in msgs:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                assert block.get("type") not in ("thinking", "redacted_thinking")
                assert "signature" not in block
    # The non-thinking text block survives the strip.
    assert any(
        isinstance(m.get("content"), list)
        and any(b.get("text") == "prior answer" for b in m["content"])
        for m in fallback_calls[0]
    )


def test_gf_strip_reasoning_signatures_helper() -> None:
    from corlinman_agent.reasoning_loop import _strip_reasoning_signatures

    messages = [
        {"role": "user", "content": "plain string untouched"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "t", "signature": "s1"},
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "text", "text": "keep me", "signature": "s2"},
            ],
        },
        {
            "role": "assistant",
            # Only thinking blocks → content collapses to "" (an empty
            # content-block list is rejected by some backends).
            "content": [{"type": "thinking", "thinking": "t2", "signature": "s3"}],
        },
    ]
    out = _strip_reasoning_signatures(messages)
    assert out[0] == {"role": "user", "content": "plain string untouched"}
    assert out[1]["content"] == [{"type": "text", "text": "keep me"}]
    assert out[2]["content"] == ""
    # Input untouched (the loop replays a NEW list).
    assert messages[1]["content"][0]["signature"] == "s1"


# ---------------------------------------------------------------------------
# Context-overflow shrink-and-retry
# ---------------------------------------------------------------------------


class _OverflowOnceProvider:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.attempts = 0

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.attempts += 1
        if self.attempts == 1:
            raise self._exc
        yield ProviderChunk(kind="token", text="recovered")
        yield ProviderChunk(kind="done", finish_reason="stop")


@pytest.mark.asyncio
async def test_gf_reasoning_loop_overflow_shrink_retry() -> None:
    prov = _OverflowOnceProvider(ContextOverflowError("context_length_exceeded"))
    big = [{"role": "user", "content": "u" * 4000} for _ in range(40)]
    events = await _collect(
        ReasoningLoop(prov), ChatStart(model="x", messages=big)
    )
    assert prov.attempts == 2
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_gf_reasoning_loop_overflow_uses_limit_attr() -> None:
    exc = ContextOverflowError("too big")
    exc.limit = 32_000  # type: ignore[attr-defined]
    prov = _OverflowOnceProvider(exc)
    big = [{"role": "user", "content": "u" * 4000} for _ in range(40)]
    events = await _collect(
        ReasoningLoop(prov), ChatStart(model="x", messages=big)
    )
    assert prov.attempts == 2
    assert isinstance(events[-1], DoneEvent)


# ---------------------------------------------------------------------------
# History dedup of consecutive duplicate turns
# ---------------------------------------------------------------------------


def test_gf_reasoning_loop_dedup_consecutive_user_turns() -> None:
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "hello"},  # exact dup
        {"role": "assistant", "content": "hi"},
    ]
    out = _dedup_consecutive_turns(msgs)
    users = [m for m in out if m.get("role") == "user"]
    assert len(users) == 1
    assert len(out) == 3


def test_gf_reasoning_loop_dedup_preserves_tool_pairing() -> None:
    # An assistant turn carrying tool_calls + the matching tool message
    # must never be collapsed even if it looks duplicated.
    msgs = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "t", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c2", "type": "function",
                 "function": {"name": "t", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c2", "content": "result"},
    ]
    out = _dedup_consecutive_turns(msgs)
    # Nothing collapsed — tool-bearing assistant + tool messages stay.
    assert out is msgs
    assert len([m for m in out if m.get("role") == "tool"]) == 2


def test_gf_reasoning_loop_dedup_no_change_returns_identity() -> None:
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]
    assert _dedup_consecutive_turns(msgs) is msgs


def test_gf_reasoning_loop_content_hash_distinguishes() -> None:
    assert _content_hash("x") == _content_hash("x")
    assert _content_hash("x") != _content_hash("y")


# ---------------------------------------------------------------------------
# Multimodal / CJK token estimate
# ---------------------------------------------------------------------------


def test_gf_reasoning_loop_cjk_estimate_higher_than_chars_div_4() -> None:
    cjk = "你" * 100
    msgs = [{"role": "user", "content": cjk}]
    est = _estimate_tokens(msgs)
    naive = len(cjk) // 4
    assert est > naive


def test_gf_reasoning_loop_image_block_charged() -> None:
    # A message with a single image block must estimate well above zero.
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        }
    ]
    est = _estimate_tokens(msgs)
    # ~1500 tokens for the image alone, far above the few chars of text.
    assert est >= 1000


def test_gf_reasoning_loop_file_block_charged() -> None:
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "file", "file": {"kind": "file", "url": "x"}},
            ],
        }
    ]
    assert _estimate_tokens(msgs) >= 1000


# ---------------------------------------------------------------------------
# USD cost including cache_creation
# ---------------------------------------------------------------------------


def test_gf_reasoning_loop_usd_cost_includes_cache_creation() -> None:
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,
    }
    cost = _estimate_turn_cost_usd("claude-opus-4", usage)
    # opus: input 15/M + cache_creation 18.75/M = 33.75 for 1M each.
    assert abs(cost - 33.75) < 1e-6


def test_gf_reasoning_loop_usd_cost_unknown_model_zero() -> None:
    assert _estimate_turn_cost_usd("some-random-model", {"input_tokens": 9}) == 0.0


@pytest.mark.asyncio
async def test_gf_reasoning_loop_done_event_carries_usd_cost() -> None:
    class _UsageProvider:
        async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            yield ProviderChunk(kind="token", text="hi")
            yield ProviderChunk(
                kind="done",
                finish_reason="stop",
                usage={"input_tokens": 1_000_000, "output_tokens": 0},
            )

    events = await _collect(
        ReasoningLoop(_UsageProvider()),
        ChatStart(model="claude-opus-4", messages=[]),
    )
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.usd_cost is not None and done.usd_cost > 0


# ---------------------------------------------------------------------------
# C4 — _is_error marker on the tool message
# ---------------------------------------------------------------------------


def test_gf_reasoning_loop_is_error_set_on_error_result() -> None:
    call = ToolCallEvent(
        call_id="c1", plugin="t", tool="t", args_json=b"{}"
    )
    err = ToolResult(call_id="c1", content="boom", is_error=True)
    extended = _extend_with_tool_round([], [call], [err])
    tool_msg = extended[-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg.get("_is_error") is True


def test_gf_reasoning_loop_is_error_absent_on_ok_result() -> None:
    call = ToolCallEvent(
        call_id="c1", plugin="t", tool="t", args_json=b"{}"
    )
    ok = ToolResult(call_id="c1", content="fine", is_error=False)
    extended = _extend_with_tool_round([], [call], [ok])
    assert "_is_error" not in extended[-1]


# ---------------------------------------------------------------------------
# C3 — run_stop hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gf_reasoning_loop_run_stop_noop_when_absent() -> None:
    class _Prov:
        async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            yield ProviderChunk(kind="token", text="done")
            yield ProviderChunk(kind="done", finish_reason="stop")

    # No hook_runner → loop ends normally with a single DoneEvent.
    loop = ReasoningLoop(_Prov())
    events = await _collect(loop, ChatStart(model="x", messages=[]))
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_gf_reasoning_loop_run_stop_inject_reopens_loop() -> None:
    class _TwoRound:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            self.calls += 1
            yield ProviderChunk(kind="token", text=f"round{self.calls}")
            yield ProviderChunk(kind="done", finish_reason="stop")

    class _HookDecision:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def stop(self) -> bool:
            return False

        @property
        def allow(self) -> bool:
            return True

        @property
        def inject_message(self) -> str | None:
            return None

    class _HookRunner:
        def __init__(self) -> None:
            self.calls = 0

        def run_stop(self, ctx: dict) -> Any:
            self.calls += 1
            # Inject once, then allow the second turn to end.
            if self.calls == 1:
                d = _HookDecision()
                object.__setattr__(d, "_inject", "keep going")

                class _D:
                    stop = False
                    allow = True
                    inject_message = "keep going"

                return _D()
            return None

    prov = _TwoRound()
    runner = _HookRunner()
    loop = ReasoningLoop(prov, hook_runner=runner)
    events = await _collect(loop, ChatStart(model="x", messages=[]))
    assert runner.calls == 2  # injected once, then allowed to stop
    assert prov.calls == 2  # the loop re-ran after the injected message
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_gf_reasoning_loop_run_stop_hook_error_swallowed() -> None:
    class _Prov:
        async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            yield ProviderChunk(kind="done", finish_reason="stop")

    class _BadRunner:
        def run_stop(self, ctx: dict) -> Any:
            raise RuntimeError("hook exploded")

    loop = ReasoningLoop(_Prov(), hook_runner=_BadRunner())
    events = await _collect(loop, ChatStart(model="x", messages=[]))
    # A broken hook must not wedge the loop.
    assert isinstance(events[-1], DoneEvent)


# ---------------------------------------------------------------------------
# Tool-result spill
# ---------------------------------------------------------------------------


def test_gf_reasoning_loop_spill_writes_handle(tmp_path: Any) -> None:
    huge = "z" * 200_000
    out = _spill_tool_result(huge, "callX")
    assert "spilled to" in out
    assert len(out) < len(huge)


def test_gf_reasoning_loop_extend_spills_huge_result() -> None:
    call = ToolCallEvent(call_id="c1", plugin="t", tool="t", args_json=b"{}")
    huge = "z" * 200_000  # above _TOOL_RESULT_SPILL_CAP (65536)
    result = ToolResult(call_id="c1", content=huge, is_error=False)
    extended = _extend_with_tool_round([], [call], [result])
    tool_msg = extended[-1]
    assert isinstance(tool_msg["content"], str)
    assert "spilled to" in tool_msg["content"]


# ---------------------------------------------------------------------------
# Gated auto-continue (off by default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gf_reasoning_loop_auto_continue_off_by_default() -> None:
    class _Prov:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            self.calls += 1
            yield ProviderChunk(kind="token", text="x")
            yield ProviderChunk(kind="done", finish_reason="stop")

    prov = _Prov()
    loop = ReasoningLoop(prov)  # autonomous defaults False
    events = await _collect(loop, ChatStart(model="x", messages=[]))
    assert prov.calls == 1  # no nudge → single turn
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_gf_reasoning_loop_auto_continue_nudges_when_autonomous() -> None:
    class _Prov:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            self.calls += 1
            # Produce a steady, non-diminishing reply each round so the
            # diminishing-returns guard keeps nudging up to the cap.
            yield ProviderChunk(
                kind="token",
                text="working " * 50,
            )
            yield ProviderChunk(
                kind="done",
                finish_reason="stop",
                usage={"output_tokens": 500},
            )

    prov = _Prov()
    loop = ReasoningLoop(prov, autonomous=True, turn_token_budget=1_000_000)
    events = await _collect(loop, ChatStart(model="x", messages=[]))
    # Auto-continue caps at 3 nudges → 4 provider calls total.
    assert prov.calls == 4
    assert isinstance(events[-1], DoneEvent)


def test_gf_reasoning_loop_append_user_turn() -> None:
    base = [{"role": "user", "content": "a"}]
    out = _append_user_turn(base, "more")
    assert out is not base
    assert out[-1] == {"role": "user", "content": "more"}
    assert len(base) == 1  # input unchanged
