"""Regression: the persisted ``image_model`` knob must reach the built adapter.

The agent image dispatcher resolves the image-generation model id by reading
``getattr(provider, "image_model", None)`` off the **built provider adapter**
(see ``corlinman_agent.image.generate._resolve_runtime_config``). Before the
fix, ``OpenAIProvider.build`` (and the OpenAI-compatible / market subclasses)
dropped ``spec.image_model`` entirely, so the operator's persisted
``[providers.<name>] image_model`` knob silently fell back to the historical
``gpt-image-1`` default. These tests assert the knob is now stamped onto the
adapter by every ``build`` path.
"""

from __future__ import annotations

import pytest
from corlinman_providers.market_providers import GroqProvider, MistralProvider
from corlinman_providers.openai_compatible import OpenAICompatibleProvider
from corlinman_providers.openai_provider import OpenAIProvider
from corlinman_providers.specs import ProviderKind, ProviderSpec


def test_openai_build_threads_image_model() -> None:
    spec = ProviderSpec(
        name="openai",
        kind=ProviderKind.OPENAI,
        api_key="sk-test",
        image_model="dall-e-3",
        image_capable=True,
    )
    provider = OpenAIProvider.build(spec)
    assert provider.image_model == "dall-e-3"
    assert provider.image_capable is True


def test_openai_build_defaults_are_backwards_compatible() -> None:
    """A spec that never set the knob keeps the historic None/False defaults."""
    spec = ProviderSpec(name="openai", kind=ProviderKind.OPENAI, api_key="sk-test")
    provider = OpenAIProvider.build(spec)
    assert provider.image_model is None
    assert provider.image_capable is False


def test_openai_compatible_build_threads_image_model() -> None:
    spec = ProviderSpec(
        name="my-vllm",
        kind=ProviderKind.OPENAI_COMPATIBLE,
        base_url="http://localhost:8000/v1",
        image_model="flux-pro-1.1",
        image_capable=True,
    )
    provider = OpenAICompatibleProvider.build(spec)
    assert provider.image_model == "flux-pro-1.1"
    assert provider.image_capable is True


@pytest.mark.parametrize("cls", [MistralProvider, GroqProvider])
def test_market_provider_build_threads_image_model(cls: type) -> None:
    spec = ProviderSpec(
        name="market",
        kind=cls.kind,
        api_key="k",
        image_model="imagen-3.0-generate-001",
        image_capable=True,
    )
    provider = cls.build(spec)
    assert provider.image_model == "imagen-3.0-generate-001"
    assert provider.image_capable is True


def test_dispatcher_resolver_honours_adapter_image_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: with no env override, the agent resolver picks the
    persisted model off the built adapter instead of the gpt-image-1
    default."""
    from corlinman_agent.image.generate import _resolve_runtime_config

    monkeypatch.delenv("CORLINMAN_IMAGE_MODEL", raising=False)

    spec = ProviderSpec(
        name="openai",
        kind=ProviderKind.OPENAI,
        api_key="sk-test",
        image_model="gpt-image-1-mini",
        image_capable=True,
    )
    provider = OpenAIProvider.build(spec)
    model, _quality, _timeout = _resolve_runtime_config(provider)
    assert model == "gpt-image-1-mini"


def test_dispatcher_resolver_env_still_overrides_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``CORLINMAN_IMAGE_MODEL`` env knob keeps highest precedence."""
    from corlinman_agent.image.generate import _resolve_runtime_config

    monkeypatch.setenv("CORLINMAN_IMAGE_MODEL", "env-wins")

    spec = ProviderSpec(
        name="openai",
        kind=ProviderKind.OPENAI,
        api_key="sk-test",
        image_model="from-config",
    )
    provider = OpenAIProvider.build(spec)
    model, _quality, _timeout = _resolve_runtime_config(provider)
    assert model == "env-wins"
