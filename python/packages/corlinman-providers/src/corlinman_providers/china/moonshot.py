"""Moonshot (月之暗面) provider — OpenAI-compatible endpoint at ``api.moonshot.cn``.

Reuses :class:`corlinman_providers.openai_provider.OpenAIProvider` with a
Moonshot-specific ``base_url`` and ``MOONSHOT_API_KEY`` env var. Claims the
``kimi-*`` (Kimi K-series) and legacy ``moonshot-*`` (``moonshot-v1-32k``)
model-id families.

``kind`` is stamped ``openai_compatible`` — Moonshot has no first-class
:class:`ProviderKind` variant (operators configure it via
``kind = "openai_compatible"`` or a declarative TOML spec); this class
exists so the legacy ``MODEL_PREFIX_DEFAULTS`` raw-model-id path resolves
``kimi-*`` / ``moonshot-*`` without configuration.
"""

from __future__ import annotations

from typing import ClassVar

from corlinman_providers.openai_provider import OpenAIProvider
from corlinman_providers.specs import ProviderKind, ProviderSpec


class MoonshotProvider(OpenAIProvider):
    """Moonshot / Kimi adapter — inherits OpenAI-standard tool_calls support."""

    name: ClassVar[str] = "moonshot"
    kind: ClassVar[ProviderKind] = ProviderKind.OPENAI_COMPATIBLE
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.moonshot.cn/v1"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url or self.DEFAULT_BASE_URL,
            env_key="MOONSHOT_API_KEY",
        )

    @classmethod
    def build(cls, spec: ProviderSpec) -> MoonshotProvider:
        return cls(api_key=spec.api_key, base_url=spec.base_url)

    @classmethod
    def supports(cls, model: str) -> bool:
        return model.startswith(("kimi-", "moonshot-"))
