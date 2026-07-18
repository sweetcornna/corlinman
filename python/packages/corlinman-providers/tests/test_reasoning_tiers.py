"""Family-detection + clamp tests for the reasoning-tier registry."""

from __future__ import annotations

import pytest
from corlinman_providers.reasoning_tiers import (
    CANONICAL_REASONING_TIERS,
    clamp_reasoning_tier,
    reasoning_tiers_for_model,
)


@pytest.mark.parametrize(
    ("model", "tiers", "default"),
    [
        # OpenAI generations (suffix/date tolerant)
        ("gpt-5.6", ("none", "low", "medium", "high", "xhigh", "max"), "medium"),
        ("gpt-5.6-sol", ("none", "low", "medium", "high", "xhigh", "max"), "medium"),
        ("gpt-5.5", ("none", "low", "medium", "high", "xhigh"), "medium"),
        ("gpt-5.4-mini", ("none", "low", "medium", "high", "xhigh"), "medium"),
        ("gpt-5.2-2025-12-11", ("none", "low", "medium", "high", "xhigh"), "medium"),
        ("gpt-5.2-pro", ("medium", "high", "xhigh"), "high"),
        ("gpt-5.1-codex-max", ("low", "medium", "high", "xhigh"), "medium"),
        ("gpt-5.1-codex", ("low", "medium", "high"), "medium"),
        ("gpt-5.1", ("none", "low", "medium", "high"), "medium"),
        ("gpt-5-pro", ("high",), "high"),
        ("gpt-5", ("minimal", "low", "medium", "high"), "medium"),
        ("gpt-5-nano", ("minimal", "low", "medium", "high"), "medium"),
        ("o3-mini", ("low", "medium", "high"), "medium"),
        ("o4-mini", ("low", "medium", "high"), "medium"),
        ("o1", ("low", "medium", "high"), "medium"),
        ("codex-mini-latest", ("low", "medium", "high", "xhigh"), "medium"),
        # Anthropic
        ("claude-fable-5", ("low", "medium", "high", "xhigh", "max"), "high"),
        ("claude-opus-4-8", ("low", "medium", "high", "xhigh", "max"), "high"),
        ("claude-sonnet-5", ("low", "medium", "high", "xhigh", "max"), "high"),
        ("claude-opus-4-6", ("low", "medium", "high", "max"), "high"),
        ("claude-sonnet-4-6", ("low", "medium", "high", "max"), "high"),
        ("claude-opus-4-5", (), None),
        ("claude-haiku-4-5", (), None),
        # pre-4.6 4.x ids (budget_tokens era) and unrecognised claude both
        # degrade to "no picker" — never a guessed effort ladder.
        ("claude-opus-4-1", (), None),
        ("claude-sonnet-4", (), None),
        ("claude-opus-4-20250514", (), None),
        ("claude-opus-6", (), None),
        # Gemini
        ("gemini-3-pro-preview", ("low", "high"), "high"),
        ("gemini-3-flash-preview", ("minimal", "low", "medium", "high"), "high"),
        ("gemini-3.1-pro-preview", ("low", "medium", "high"), "high"),
        ("gemini-3.1-flash-lite", ("minimal", "low", "medium", "high"), "minimal"),
        ("gemini-3.5-flash", ("minimal", "low", "medium", "high"), "medium"),
        ("gemini-2.5-pro", ("low", "medium", "high"), "medium"),
        ("gemini-2.5-flash", ("none", "low", "medium", "high"), "medium"),
        ("gemini-2.0-flash", (), None),
        # xAI
        ("grok-3-mini", ("low", "high"), "low"),
        ("grok-4.5", ("low", "medium", "high"), "high"),
        ("grok-4", (), None),
        ("grok-4.1", (), None),
        # DeepSeek / Qwen / GLM / Kimi
        ("deepseek-v4-flash", ("none", "high", "max"), "high"),
        ("deepseek-reasoner", ("none", "high", "max"), "high"),
        ("deepseek-r1", (), None),
        ("qwq-plus", ("low", "medium", "high"), "high"),
        ("qwen3-235b-a22b-thinking-2507", ("low", "medium", "high"), "high"),
        ("qwen3-max", ("none", "low", "medium", "high"), "medium"),
        ("qwen-plus", ("none", "low", "medium", "high"), "medium"),
        ("glm-5", ("none", "high", "max"), "max"),
        ("glm-4.6", ("none", "on"), "on"),
        ("kimi-k2.5", ("none", "on"), "on"),
        ("kimi-k3", (), None),
        ("kimi-k2-thinking", (), None),
    ],
)
def test_family_detection(model: str, tiers: tuple[str, ...], default: str | None) -> None:
    got_tiers, got_default = reasoning_tiers_for_model(model)
    assert got_tiers == tiers
    assert got_default == default


def test_vendor_prefix_and_case_normalisation() -> None:
    assert reasoning_tiers_for_model("openai/GPT-5.6")[0] == (
        "none", "low", "medium", "high", "xhigh", "max",
    )


def test_unknown_model_has_no_opinion() -> None:
    tiers, default = reasoning_tiers_for_model("sol-pro-x")
    assert tiers is None
    assert default is None


def test_all_registry_tiers_are_canonical() -> None:
    # every tier string used by any family must be a canonical tier
    from corlinman_providers.reasoning_tiers import _FAMILY_RULES

    for _pat, tiers, default in _FAMILY_RULES:
        for t in tiers:
            assert t in CANONICAL_REASONING_TIERS
        if default is not None:
            assert default in tiers


class TestClamp:
    def test_supported_passes_through(self) -> None:
        assert clamp_reasoning_tier("gpt-5.6", "max") == "max"
        assert clamp_reasoning_tier("o3-mini", "medium") == "medium"

    def test_unsupported_snaps_to_nearest(self) -> None:
        # o-series has no max/xhigh → high
        assert clamp_reasoning_tier("o3-mini", "max") == "high"
        assert clamp_reasoning_tier("o3-mini", "xhigh") == "high"
        # gpt-5.1 has no minimal → tie between none and low resolves down
        assert clamp_reasoning_tier("gpt-5.1", "minimal") == "none"
        # gemini-3-pro only low/high: medium tie resolves to low
        assert clamp_reasoning_tier("gemini-3-pro-preview", "medium") == "low"
        # toggle family: graded request lands on the toggle
        assert clamp_reasoning_tier("glm-4.6", "medium") == "on"
        assert clamp_reasoning_tier("glm-4.6", "high") == "on"
        assert clamp_reasoning_tier("kimi-k2.6", "none") == "none"

    def test_no_knob_family_drops(self) -> None:
        assert clamp_reasoning_tier("grok-4", "high") is None
        assert clamp_reasoning_tier("deepseek-r1", "medium") is None
        assert clamp_reasoning_tier("claude-haiku-4-5", "high") is None

    def test_unknown_model_passes_through(self) -> None:
        assert clamp_reasoning_tier("sol-pro-x", "xhigh") == "xhigh"

    def test_garbage_request_dropped(self) -> None:
        assert clamp_reasoning_tier("gpt-5.6", "turbo") is None
        assert clamp_reasoning_tier("gpt-5.6", "") is None
