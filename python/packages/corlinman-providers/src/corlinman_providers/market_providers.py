"""Market-LLM adapters added with the free-form-providers refactor.

Most market LLM vendors (Mistral, Cohere, Together, Groq, Replicate, …) speak
the OpenAI wire format under their own base URLs. Rather than ask operators
to reach for ``kind = "openai_compatible"`` plus a hand-rolled ``base_url``,
the Rust schema now exposes them as first-class :class:`ProviderKind`
variants. Each adapter here is a thin wrapper that delegates to
:class:`corlinman_providers.openai_compatible.OpenAICompatibleProvider` —
the only thing that differs is a documented default ``base_url`` and the
class-level ``kind`` discriminator.

Bedrock and Azure are *no longer* stubs: the real adapters live in
:mod:`corlinman_providers.bedrock_provider` (SigV4-signed
``InvokeModelWithResponseStream``) and
:mod:`corlinman_providers.azure_provider` (OpenAI wire shape with Azure's
deployment-id routing + ``api-key`` auth). They are re-exported here so the
historic ``from corlinman_providers.market_providers import
AzureProvider`` / ``BedrockProvider`` import path keeps working.
"""

from __future__ import annotations

from typing import ClassVar

from corlinman_providers.azure_provider import AzureProvider
from corlinman_providers.bedrock_provider import BedrockProvider
from corlinman_providers.openai_compatible import OpenAICompatibleProvider
from corlinman_providers.specs import ProviderKind, ProviderSpec


def _build_compat(
    spec: ProviderSpec,
    *,
    default_base_url: str,
    kind: ProviderKind,
) -> OpenAICompatibleProvider:
    """Shared helper: build an OpenAI-compat adapter with a sensible default
    ``base_url`` so configs that omit it still resolve to the vendor's
    documented endpoint."""
    base_url = spec.base_url or default_base_url
    provider = OpenAICompatibleProvider(
        base_url=base_url,
        api_key=spec.api_key,
        instance_name=spec.name,
    )
    # Stamp the user-visible kind on the instance so admin listings report
    # `mistral` / `cohere` / etc. instead of generic `openai_compatible`.
    provider.__dict__["kind"] = kind
    return provider


class MistralProvider(OpenAICompatibleProvider):
    """Mistral La Plateforme — OpenAI-compat at ``api.mistral.ai/v1``."""

    name: ClassVar[str] = "mistral"
    kind: ClassVar[ProviderKind] = ProviderKind.MISTRAL
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.mistral.ai/v1"

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAICompatibleProvider:
        return _build_compat(spec, default_base_url=cls.DEFAULT_BASE_URL, kind=cls.kind)


class CohereProvider(OpenAICompatibleProvider):
    """Cohere — OpenAI-compat endpoint at ``api.cohere.ai/compatibility/v1``."""

    name: ClassVar[str] = "cohere"
    kind: ClassVar[ProviderKind] = ProviderKind.COHERE
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.cohere.ai/compatibility/v1"

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAICompatibleProvider:
        return _build_compat(spec, default_base_url=cls.DEFAULT_BASE_URL, kind=cls.kind)


class TogetherProvider(OpenAICompatibleProvider):
    """Together AI — OpenAI-compat at ``api.together.xyz/v1``."""

    name: ClassVar[str] = "together"
    kind: ClassVar[ProviderKind] = ProviderKind.TOGETHER
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.together.xyz/v1"

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAICompatibleProvider:
        return _build_compat(spec, default_base_url=cls.DEFAULT_BASE_URL, kind=cls.kind)


class GroqProvider(OpenAICompatibleProvider):
    """Groq Cloud — OpenAI-compat at ``api.groq.com/openai/v1``."""

    name: ClassVar[str] = "groq"
    kind: ClassVar[ProviderKind] = ProviderKind.GROQ
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.groq.com/openai/v1"

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAICompatibleProvider:
        return _build_compat(spec, default_base_url=cls.DEFAULT_BASE_URL, kind=cls.kind)


class ReplicateProvider(OpenAICompatibleProvider):
    """Replicate — OpenAI-compat at ``api.replicate.com/openai/v1``."""

    name: ClassVar[str] = "replicate"
    kind: ClassVar[ProviderKind] = ProviderKind.REPLICATE
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.replicate.com/openai/v1"

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAICompatibleProvider:
        return _build_compat(spec, default_base_url=cls.DEFAULT_BASE_URL, kind=cls.kind)


__all__ = [
    "AzureProvider",
    "BedrockProvider",
    "CohereProvider",
    "GroqProvider",
    "MistralProvider",
    "ReplicateProvider",
    "TogetherProvider",
]
