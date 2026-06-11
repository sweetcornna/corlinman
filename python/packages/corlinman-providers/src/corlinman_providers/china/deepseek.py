"""DeepSeek provider — OpenAI-compatible endpoint at ``api.deepseek.com``.

Reuses :class:`corlinman_providers.openai_provider.OpenAIProvider` (via the
:class:`~corlinman_providers.china._errors.ChinaOpenAIProvider` error-mapping
base) with a DeepSeek-specific ``base_url`` and ``DEEPSEEK_API_KEY`` env var.
Covers the reasoning family too: ``deepseek-r1`` / ``deepseek-reasoner``
match the ``deepseek-`` prefix, and the inherited stream loop surfaces
their ``delta.reasoning_content`` as ``is_reasoning`` token chunks.
"""

from __future__ import annotations

from typing import ClassVar

from corlinman_providers.china._errors import ChinaOpenAIProvider
from corlinman_providers.specs import ProviderKind, ProviderSpec


class DeepSeekProvider(ChinaOpenAIProvider):
    """DeepSeek adapter — inherits OpenAI-standard tool_calls support."""

    name: ClassVar[str] = "deepseek"
    kind: ClassVar[ProviderKind] = ProviderKind.DEEPSEEK
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.deepseek.com/v1"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url or self.DEFAULT_BASE_URL,
            env_key="DEEPSEEK_API_KEY",
        )

    @classmethod
    def build(cls, spec: ProviderSpec) -> DeepSeekProvider:
        return cls(api_key=spec.api_key, base_url=spec.base_url)

    @classmethod
    def supports(cls, model: str) -> bool:
        return model.startswith("deepseek-")
