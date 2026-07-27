"""G2 (b): :class:`EmbeddingRouter` — config-driven embedding binding.

The router must resolve strictly from the ``[embedding]`` section (never
silently fall back to the chat model) and raise the explicit
:class:`EmbeddingNotConfiguredError` naming what is missing.
"""

from __future__ import annotations

from typing import Any

import pytest
from corlinman_providers.embedding_router import (
    EmbeddingNotConfiguredError,
    EmbeddingRouter,
)


class _Provider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], Any]] = []

    async def embed(
        self, *, model: str, inputs: Any, extra: Any = None
    ) -> list[list[float]]:
        self.calls.append((model, list(inputs), extra))
        return [[1.0, 2.0] for _ in inputs]


class _Registry:
    def __init__(self, providers: dict[str, Any]) -> None:
        self._providers = providers

    def get(self, name: str) -> Any:
        return self._providers.get(name)


def _router(config: Any, registry: Any) -> EmbeddingRouter:
    return EmbeddingRouter(
        registry_getter=lambda: registry, config_getter=lambda: config
    )


@pytest.mark.asyncio
async def test_missing_section_raises_explicit_error() -> None:
    router = _router(None, _Registry({}))
    assert router.configured() is False
    with pytest.raises(EmbeddingNotConfiguredError, match="not configured"):
        await router.embed(["hello"])


@pytest.mark.asyncio
async def test_disabled_section_raises_explicit_error() -> None:
    config = {"provider": "openai", "model": "emb", "enabled": False}
    router = _router(config, _Registry({"openai": _Provider()}))
    assert router.configured() is False
    with pytest.raises(EmbeddingNotConfiguredError, match="disabled"):
        await router.embed(["hello"])


@pytest.mark.asyncio
async def test_partial_section_raises_explicit_error() -> None:
    router = _router({"provider": "openai"}, _Registry({"openai": _Provider()}))
    with pytest.raises(EmbeddingNotConfiguredError, match="provider and model"):
        await router.embed(["hello"])


@pytest.mark.asyncio
async def test_unregistered_provider_raises_with_its_name() -> None:
    config = {"provider": "ghost", "model": "emb"}
    router = _router(config, _Registry({}))
    with pytest.raises(EmbeddingNotConfiguredError, match="'ghost'"):
        await router.embed(["hello"])


@pytest.mark.asyncio
async def test_happy_path_routes_to_the_configured_provider() -> None:
    provider = _Provider()
    config = {"provider": "openai", "model": "text-embedding-3-small"}
    router = _router(config, _Registry({"openai": provider}))

    assert router.configured() is True
    vectors = await router.embed(["a", "b"])

    assert vectors == [[1.0, 2.0], [1.0, 2.0]]
    assert provider.calls == [("text-embedding-3-small", ["a", "b"], None)]


@pytest.mark.asyncio
async def test_params_forwarded_as_extra() -> None:
    provider = _Provider()
    config = {
        "provider": "openai",
        "model": "emb",
        "params": {"dimensions": 256},
    }
    router = _router(config, _Registry({"openai": provider}))

    await router.embed(["a"])

    assert provider.calls[0][2] == {"dimensions": 256}


@pytest.mark.asyncio
async def test_config_is_read_live_per_call() -> None:
    """A hot-swapped section takes effect without rebuilding the router."""
    provider = _Provider()
    registry = _Registry({"openai": provider})
    state: dict[str, Any] = {"config": None}
    router = EmbeddingRouter(
        registry_getter=lambda: registry,
        config_getter=lambda: state["config"],
    )

    assert router.configured() is False
    state["config"] = {"provider": "openai", "model": "emb-live"}
    assert router.configured() is True
    await router.embed(["x"])
    assert provider.calls[0][0] == "emb-live"
