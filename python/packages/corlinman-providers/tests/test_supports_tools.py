"""``supports_tools`` capability surface (P4 — tool degradation).

Default is ``True`` everywhere; only an operator-declared
``tools = false`` (``[providers.<name>].params`` for class-based specs,
``[params]`` for declarative TOML specs) flips a provider to tool-less.
The servicer reads this via ``getattr``-degrade, so the per-alias
``tools = false`` path is exercised at the servicer level, not here.
"""

from __future__ import annotations

from corlinman_providers import (
    DeclarativeProvider,
    DeclarativeProviderSpec,
    OpenAICompatibleProvider,
    OpenAIProvider,
    ProviderKind,
    ProviderSpec,
)
from corlinman_providers.market_providers import MistralProvider
from corlinman_providers.openai_compatible import tools_param_enabled


def _compat_spec(params: dict[str, object] | None = None) -> ProviderSpec:
    return ProviderSpec(
        name="local-vllm",
        kind=ProviderKind.OPENAI_COMPATIBLE,
        base_url="http://localhost:8000/v1",
        params=dict(params or {}),
    )


def _declarative_spec(params: dict[str, object] | None = None) -> DeclarativeProviderSpec:
    return DeclarativeProviderSpec(
        id="toolless",
        name="Tool-less gateway",
        base_url="http://localhost:8000/v1",
        auth_kind="none",
        auth_config={},
        request_format="openai_compatible",
        params=dict(params or {}),
    )


def test_openai_provider_defaults_to_tools_supported() -> None:
    assert OpenAIProvider(api_key="k").supports_tools("gpt-4o") is True


def test_tools_param_enabled_only_explicit_false_disables() -> None:
    assert tools_param_enabled({}) is True
    assert tools_param_enabled({"tools": True}) is True
    assert tools_param_enabled({"tools": False}) is False
    # Malformed values keep the historic always-on behaviour.
    assert tools_param_enabled({"tools": "no"}) is True


def test_openai_compatible_honours_provider_tools_false() -> None:
    prov = OpenAICompatibleProvider.build(_compat_spec({"tools": False}))
    assert prov.supports_tools("any-model") is False


def test_openai_compatible_defaults_to_tools_supported() -> None:
    prov = OpenAICompatibleProvider.build(_compat_spec())
    assert prov.supports_tools("any-model") is True


def test_market_provider_build_threads_tools_param() -> None:
    spec = ProviderSpec(
        name="mistral",
        kind=ProviderKind.MISTRAL,
        params={"tools": False},
    )
    prov = MistralProvider.build(spec)
    assert prov.supports_tools("mistral-large-latest") is False


def test_declarative_provider_honours_params_tools_false() -> None:
    prov = DeclarativeProvider(_declarative_spec({"tools": False}))
    assert prov.supports_tools("whatever") is False


def test_declarative_provider_defaults_to_tools_supported() -> None:
    prov = DeclarativeProvider(_declarative_spec())
    assert prov.supports_tools("whatever") is True
