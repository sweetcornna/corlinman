"""Per-family wire translation of the canonical reasoning tier.

Offline: monkeypatches ``AsyncOpenAI`` with a recording fake and asserts
on the kwargs the provider would send.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_providers import OpenAIProvider


def _finish(reason: str) -> Any:
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


class _RecordingOpenAI:
    def __init__(self, sink: dict[str, Any]) -> None:
        async def _create(**kwargs: Any) -> _FakeAsyncIter:
            sink.clear()
            sink.update(kwargs)
            return _FakeAsyncIter([_finish("stop")])

        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=_create)
        )


async def _run(
    monkeypatch: pytest.MonkeyPatch, model: str, effort: str
) -> dict[str, Any]:
    sink: dict[str, Any] = {}
    import openai  # type: ignore[import-not-found]

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **_: _RecordingOpenAI(sink))
    prov = OpenAIProvider()
    async for _ in prov.chat_stream(
        model=model,
        messages=[{"role": "user", "content": "x"}],
        extra={"reasoning_effort": effort},
    ):
        pass
    return sink


@pytest.mark.asyncio
async def test_openai_family_passes_clamped_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = await _run(monkeypatch, "gpt-5.6-sol", "max")
    assert sent["reasoning_effort"] == "max"
    assert "extra_body" not in sent


@pytest.mark.asyncio
async def test_openai_old_gen_clamps_down(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = await _run(monkeypatch, "o3-mini", "max")
    assert sent["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_deepseek_v4_spells_thinking_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = await _run(monkeypatch, "deepseek-v4-flash", "max")
    assert sent["extra_body"]["thinking"] == {"type": "enabled"}
    assert sent["reasoning_effort"] == "max"

    sent = await _run(monkeypatch, "deepseek-v4-flash", "none")
    assert sent["extra_body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in sent


@pytest.mark.asyncio
async def test_glm_toggle_and_glm5_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    # GLM-4.6: pure toggle — a graded request lands on `on` → enabled
    sent = await _run(monkeypatch, "glm-4.6", "high")
    assert sent["extra_body"]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in sent

    # GLM-5: enabled + two-step effort
    sent = await _run(monkeypatch, "glm-5", "high")
    assert sent["extra_body"]["thinking"] == {"type": "enabled"}
    assert sent["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_qwen_budget_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = await _run(monkeypatch, "qwen3-max", "low")
    assert sent["extra_body"]["enable_thinking"] is True
    assert sent["extra_body"]["thinking_budget"] == 4096

    sent = await _run(monkeypatch, "qwen3-max", "none")
    assert sent["extra_body"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_kimi_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = await _run(monkeypatch, "kimi-k2.6", "on")
    assert sent["extra_body"]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in sent


@pytest.mark.asyncio
async def test_no_knob_family_drops_param(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = await _run(monkeypatch, "grok-4", "high")
    assert "reasoning_effort" not in sent
    assert "extra_body" not in sent


@pytest.mark.asyncio
async def test_unknown_model_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = await _run(monkeypatch, "sol-pro-x", "xhigh")
    assert sent["reasoning_effort"] == "xhigh"


@pytest.mark.asyncio
async def test_no_effort_requested_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    sink: dict[str, Any] = {}
    import openai  # type: ignore[import-not-found]

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **_: _RecordingOpenAI(sink))
    prov = OpenAIProvider()
    async for _ in prov.chat_stream(
        model="gpt-5.6", messages=[{"role": "user", "content": "x"}]
    ):
        pass
    assert "reasoning_effort" not in sink
    assert "extra_body" not in sink
