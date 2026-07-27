"""EmbeddingRouter — resolves the ``[embedding]`` config to a provider call.

The RAG dense-vector pipeline (G2 phase 1) needs one authoritative place
that answers "which provider + model computes embeddings right now?".
Mirrors the ``image_provider`` / ``image_model`` precedent: the binding is
config-driven (the ``[embedding]`` section — :class:`~corlinman_providers.
specs.EmbeddingSpec` shape), never inferred from the chat model, and a
missing binding is an *explicit* :class:`EmbeddingNotConfiguredError`
rather than a silent fallback to the chat provider (embeddings cost money
and must be opted into).

Live-state by construction: the router holds zero resolved state. Both
collaborators arrive as zero-arg getters (``registry_getter`` /
``config_getter``) evaluated per call, so config hot-reloads and registry
rebuilds are picked up without rewiring — the same pattern the gateway's
``memory_embed_fn`` closure used before this module absorbed it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from corlinman_providers.failover import CorlinmanError


class EmbeddingNotConfiguredError(CorlinmanError):
    """No usable ``[embedding]`` binding — the caller must not fall back
    to the chat model. Message always states what is missing."""

    reason = "embedding_not_configured"


class EmbeddingRouter:
    """Resolve the configured embedding binding and forward ``embed`` calls.

    Parameters
    ----------
    registry_getter:
        Zero-arg callable returning the live provider registry (an object
        with ``get(name) -> provider | None``) or ``None`` when no
        registry is available yet.
    config_getter:
        Zero-arg callable returning the live ``[embedding]`` section as a
        mapping (``provider`` / ``model`` / ``enabled`` / ``params``) or
        ``None`` when the section is absent.
    """

    def __init__(
        self,
        *,
        registry_getter: Callable[[], Any],
        config_getter: Callable[[], Any],
    ) -> None:
        self._registry_getter = registry_getter
        self._config_getter = config_getter

    # ---- resolution --------------------------------------------------------

    def _section(self) -> dict[str, Any] | None:
        raw = self._config_getter()
        if isinstance(raw, dict):
            return raw
        # Tolerate EmbeddingSpec-like objects (duck-typed attribute shape).
        if raw is not None and hasattr(raw, "provider") and hasattr(raw, "model"):
            return {
                "provider": raw.provider,
                "model": raw.model,
                "enabled": getattr(raw, "enabled", True),
                "params": getattr(raw, "params", None) or {},
            }
        return None

    def resolve(self) -> tuple[Any, str, dict[str, Any]]:
        """Return ``(provider_adapter, model_id, params)`` for the live
        binding, raising :class:`EmbeddingNotConfiguredError` with a
        precise message when any piece is missing."""
        section = self._section()
        if section is None:
            raise EmbeddingNotConfiguredError(
                "embedding model not configured: set [embedding] provider/model"
            )
        if section.get("enabled", True) is False:
            raise EmbeddingNotConfiguredError(
                "embedding is disabled: set [embedding] enabled = true"
            )
        provider_name = section.get("provider")
        model = section.get("model")
        if not provider_name or not model:
            raise EmbeddingNotConfiguredError(
                "embedding model not configured: [embedding] needs both "
                "provider and model"
            )
        registry = self._registry_getter()
        if registry is None:
            raise EmbeddingNotConfiguredError(
                "embedding provider registry unavailable"
            )
        provider = registry.get(str(provider_name))
        if provider is None:
            raise EmbeddingNotConfiguredError(
                f"embedding provider {provider_name!r} is not registered "
                "under [providers]"
            )
        params = section.get("params")
        return provider, str(model), dict(params) if isinstance(params, dict) else {}

    def configured(self) -> bool:
        """Whether :meth:`embed` would resolve — never raises."""
        try:
            self.resolve()
        except EmbeddingNotConfiguredError:
            return False
        return True

    # ---- forwarding --------------------------------------------------------

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts`` through the configured provider.

        Raises :class:`EmbeddingNotConfiguredError` when unbound; provider
        errors (auth / rate-limit / …) propagate as their
        :class:`~corlinman_providers.failover.CorlinmanError` subtypes.
        """
        provider, model, params = self.resolve()
        vectors = await provider.embed(
            model=model, inputs=list(texts), extra=params or None
        )
        return [[float(v) for v in vector] for vector in vectors]


__all__ = ["EmbeddingNotConfiguredError", "EmbeddingRouter"]
