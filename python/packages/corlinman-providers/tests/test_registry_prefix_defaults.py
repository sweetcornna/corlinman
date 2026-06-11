"""Legacy ``MODEL_PREFIX_DEFAULTS`` resolutions (P5 — broader model cover).

A specs-less :class:`ProviderRegistry` resolves raw model ids purely via
the prefix table, so these tests pin the vendor each family lands on:
Mistral ids (``mistral-`` / ``codestral-`` / ``ministral-``) → Mistral,
Moonshot ids (``kimi-`` / ``moonshot-``) → Moonshot, bare ``llama-`` →
Groq, and the DeepSeek reasoning family (``deepseek-r1``) → DeepSeek.
"""

from __future__ import annotations

import pytest
from corlinman_providers import MoonshotProvider, OpenAIProvider, ProviderRegistry
from corlinman_providers.china import DeepSeekProvider
from corlinman_providers.market_providers import GroqProvider, MistralProvider


@pytest.fixture()
def registry() -> ProviderRegistry:
    return ProviderRegistry([])


@pytest.mark.parametrize(
    ("model", "cls"),
    [
        ("mistral-large-latest", MistralProvider),
        ("codestral-2501", MistralProvider),
        ("ministral-8b-latest", MistralProvider),
        ("kimi-k2-0905-preview", MoonshotProvider),
        ("moonshot-v1-32k", MoonshotProvider),
        ("llama-3.3-70b-versatile", GroqProvider),
        ("deepseek-r1", DeepSeekProvider),
        ("deepseek-reasoner", DeepSeekProvider),
        ("o4-mini", OpenAIProvider),
    ],
)
def test_prefix_resolution(
    registry: ProviderRegistry, model: str, cls: type[object]
) -> None:
    provider, upstream_model, params = registry.resolve(model)
    assert type(provider) is cls
    assert upstream_model == model
    assert params == {}


def test_moonshot_supports_kimi_and_moonshot_families() -> None:
    assert MoonshotProvider.supports("kimi-latest")
    assert MoonshotProvider.supports("moonshot-v1-8k")
    assert not MoonshotProvider.supports("gpt-4o")


def test_mistral_supports_family_prefixes() -> None:
    assert MistralProvider.supports("mistral-small-latest")
    assert MistralProvider.supports("codestral-latest")
    assert MistralProvider.supports("ministral-3b-latest")
    assert not MistralProvider.supports("llama-3.1-8b")
