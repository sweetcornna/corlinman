"""Model-adaptation tests for the OpenAI-wire adapter family.

Covers the three "适配几乎所有模型" provider-layer behaviours:

* P1 — reasoning-model param shaping: o1/o3/o4/gpt-5 get
  ``max_completion_tokens`` (never ``max_tokens``) and NO ``temperature``;
  standard models are untouched.
* P2 — reasoning content: ``delta.reasoning_content`` (DeepSeek-R1, Qwen
  QwQ via many gateways) surfaces as ``is_reasoning=True`` token chunks,
  and any ``reasoning_content`` on an *outbound* message is stripped
  before replay (R1 rejects requests that echo reasoning back).
* P3 — strict-alternation pre-flight: deepseek*/qwen*/qwq*/glm* merge
  consecutive same-role user/assistant messages instead of letting the
  vendor 400; system messages are exempt and non-strict models pass
  messages through verbatim.

Offline — monkeypatches ``AsyncOpenAI`` with ``SimpleNamespace`` fakes
(same pattern as ``test_openai_provider_tool_stream.py``) and captures the
``create(**kwargs)`` body for assertions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_providers import OpenAIProvider, ProviderChunk
from corlinman_providers.openai_provider import (
    _merge_consecutive_roles,
    _normalise_message,
)


def _delta_text_chunk(text: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=None),
                finish_reason=None,
            )
        ]
    )


def _delta_reasoning_chunk(text: str) -> Any:
    """A DeepSeek-R1 / QwQ style delta: reasoning_content, no content."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None, tool_calls=None, reasoning_content=text
                ),
                finish_reason=None,
            )
        ]
    )


def _finish_chunk(reason: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=None),
                finish_reason=reason,
            )
        ]
    )


class _FakeAsyncIter:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def __aiter__(self) -> AsyncIterator[Any]:
        items = self._items

        async def _gen() -> AsyncIterator[Any]:
            for it in items:
                yield it

        return _gen()


class _FakeCompletions:
    """Captures the ``create`` kwargs so tests can assert the wire body."""

    def __init__(self, chunks: list[Any], captured: dict[str, Any]) -> None:
        self._chunks = chunks
        self._captured = captured

    async def create(self, **kwargs: Any) -> _FakeAsyncIter:
        self._captured.update(kwargs)
        return _FakeAsyncIter(self._chunks)


class _FakeOpenAI:
    def __init__(self, chunks: list[Any], captured: dict[str, Any]) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(chunks, captured))


def _patch_openai(
    monkeypatch: pytest.MonkeyPatch, chunks: list[Any]
) -> dict[str, Any]:
    """Patch ``AsyncOpenAI`` with a capture fake; returns the kwargs sink."""
    import openai  # type: ignore[import-not-found]

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        openai, "AsyncOpenAI", lambda **_: _FakeOpenAI(chunks, captured)
    )
    return captured


async def _drain(
    prov: OpenAIProvider, *, model: str, messages: list[dict[str, Any]], **kw: Any
) -> list[ProviderChunk]:
    out: list[ProviderChunk] = []
    async for c in prov.chat_stream(model=model, messages=messages, **kw):
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# P1 — reasoning-model param shaping.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["o1", "o1-mini", "o3-mini", "o4-mini", "gpt-5", "gpt-5-turbo"])
async def test_reasoning_models_use_max_completion_tokens_and_drop_temperature(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = _patch_openai(monkeypatch, [_finish_chunk("stop")])
    prov = OpenAIProvider()
    await _drain(
        prov,
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        max_tokens=128,
    )
    assert captured["max_completion_tokens"] == 128
    assert "max_tokens" not in captured
    assert "temperature" not in captured


async def test_standard_models_keep_classic_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = _patch_openai(monkeypatch, [_finish_chunk("stop")])
    prov = OpenAIProvider()
    await _drain(
        prov,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        max_tokens=128,
    )
    assert captured["max_tokens"] == 128
    assert captured["temperature"] == 0.7
    assert "max_completion_tokens" not in captured


@pytest.mark.parametrize("model", ["o3-mini", "o4-mini", "gpt-5"])
async def test_reasoning_models_strip_sampling_knobs_from_extra(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """Alias/provider params merged via ``extra`` must not smuggle the
    classic sampling knobs onto a reasoning model — every one of them 400s.
    Non-sampling extras (``reasoning_effort``) still pass through."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = _patch_openai(monkeypatch, [_finish_chunk("stop")])
    prov = OpenAIProvider()
    extra = {
        "temperature": 0.7,
        "top_p": 0.9,
        "presence_penalty": 0.5,
        "frequency_penalty": 0.2,
        "logprobs": True,
        "top_logprobs": 5,
        "logit_bias": {"50256": -100},
        "reasoning_effort": "high",
    }
    await _drain(
        prov,
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        extra=extra,
    )
    for knob in (
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "logprobs",
        "top_logprobs",
        "logit_bias",
    ):
        assert knob not in captured, f"{knob} must be stripped for {model}"
    assert captured["reasoning_effort"] == "high"
    # The caller's dict is never mutated — strip happens on a copy.
    assert extra["top_p"] == 0.9


async def test_standard_models_keep_sampling_knobs_from_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standard models keep alias-supplied sampling knobs untouched."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = _patch_openai(monkeypatch, [_finish_chunk("stop")])
    prov = OpenAIProvider()
    await _drain(
        prov,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        extra={"top_p": 0.9, "presence_penalty": 0.5},
    )
    assert captured["top_p"] == 0.9
    assert captured["presence_penalty"] == 0.5


# ---------------------------------------------------------------------------
# P2 — reasoning_content: stream surfacing + strip-on-replay.
# ---------------------------------------------------------------------------


async def test_reasoning_content_surfaces_as_is_reasoning_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _patch_openai(
        monkeypatch,
        [
            _delta_reasoning_chunk("let me think"),
            _delta_reasoning_chunk(" about this"),
            _delta_text_chunk("the answer is 4"),
            _finish_chunk("stop"),
        ],
    )
    prov = OpenAIProvider()
    chunks = await _drain(
        prov, model="deepseek-reasoner", messages=[{"role": "user", "content": "2+2"}]
    )

    reasoning = [c for c in chunks if c.kind == "token" and c.is_reasoning]
    answer = [c for c in chunks if c.kind == "token" and not c.is_reasoning]
    assert [c.text for c in reasoning] == ["let me think", " about this"]
    assert [c.text for c in answer] == ["the answer is 4"]
    assert chunks[-1].kind == "done"


def test_normalise_message_strips_reasoning_content() -> None:
    """R1 replay rule: reasoning never goes back on the wire."""
    original = {
        "role": "assistant",
        "content": "the answer is 4",
        "reasoning_content": "let me think about this",
    }
    out = _normalise_message(original)
    assert "reasoning_content" not in out
    assert out["content"] == "the answer is 4"
    # The caller's dict is never mutated — strip happens on a copy.
    assert original["reasoning_content"] == "let me think about this"


async def test_reasoning_content_stripped_from_outbound_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = _patch_openai(monkeypatch, [_finish_chunk("stop")])
    prov = OpenAIProvider()
    await _drain(
        prov,
        model="deepseek-reasoner",
        messages=[
            {"role": "user", "content": "2+2"},
            {"role": "assistant", "content": "4", "reasoning_content": "hmm"},
            {"role": "user", "content": "and 3+3?"},
        ],
    )
    sent = captured["messages"]
    assert all("reasoning_content" not in m for m in sent)
    assert [m["content"] for m in sent] == ["2+2", "4", "and 3+3?"]


# ---------------------------------------------------------------------------
# P3 — strict-alternation pre-flight merge.
# ---------------------------------------------------------------------------


async def test_strict_alternation_model_merges_consecutive_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = _patch_openai(monkeypatch, [_finish_chunk("stop")])
    prov = OpenAIProvider()
    await _drain(
        prov,
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "reply a"},
            {"role": "assistant", "content": "reply b"},
            {"role": "user", "content": "next"},
        ],
    )
    sent = captured["messages"]
    assert [(m["role"], m["content"]) for m in sent] == [
        ("system", "be brief"),
        ("user", "first\n\nsecond"),
        ("assistant", "reply a\n\nreply b"),
        ("user", "next"),
    ]


async def test_non_strict_model_messages_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = _patch_openai(monkeypatch, [_finish_chunk("stop")])
    prov = OpenAIProvider()
    messages = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    await _drain(prov, model="gpt-4o-mini", messages=messages)
    assert captured["messages"] == messages


def test_merge_keeps_consecutive_system_messages_separate() -> None:
    merged = _merge_consecutive_roles(
        [
            {"role": "system", "content": "rule one"},
            {"role": "system", "content": "rule two"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert len(merged) == 3
    assert [m["role"] for m in merged] == ["system", "system", "user"]


def test_merge_skips_tool_carrying_and_tool_role_messages() -> None:
    """Tool results and tool_calls-bearing assistant turns stay intact —
    merging them would corrupt the call protocol."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "assistant", "content": "after tools"},
        {"role": "tool", "content": "r1", "tool_call_id": "c1"},
        {"role": "tool", "content": "r2", "tool_call_id": "c2"},
    ]
    merged = _merge_consecutive_roles(messages)
    assert merged == messages


def test_merge_does_not_mutate_caller_messages() -> None:
    first = {"role": "user", "content": "a"}
    second = {"role": "user", "content": "b"}
    merged = _merge_consecutive_roles([first, second])
    assert merged == [{"role": "user", "content": "a\n\nb"}]
    assert first["content"] == "a"
    assert second["content"] == "b"
