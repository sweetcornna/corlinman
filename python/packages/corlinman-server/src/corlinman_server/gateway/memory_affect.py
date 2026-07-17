"""Shared affect-anchor cache for the W6 EPA lens.

One place (instead of the previous two divergent copies in the servicer
and the reconcile builtin) that:

- keys the cached anchors by the LIVE embedding (provider, model) so a
  config hot-swap rebuilds them instead of silently projecting against
  a stale/mismatched space;
- single-flights the build (an asyncio.Lock on app_state) so two cold
  callers don't both pay the anchor-embedding request;
- prefers the batch embed seam (one provider request) over ~40 serial
  round trips.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_ANCHORS_ATTR = "memory_affect_anchors"
_LOCK_ATTR = "_memory_affect_anchor_lock"


def _embedding_cache_key(app_state: Any) -> tuple[str, str] | None:
    """Cache key for the current embedding source.

    With a gateway ``config["embedding"]`` section the key is
    (provider, model) so hot-swaps rebuild the anchors. A directly-
    injected ``memory_embed_fn`` with no config section (standalone
    boots, tests) gets a stable generic key — the seam must not depend
    on the gateway config shape. None = embedding disabled entirely.
    """
    config = getattr(app_state, "config", None)
    emb = config.get("embedding") if isinstance(config, dict) else None
    if isinstance(emb, dict):
        if not emb.get("enabled", True):
            return None
        provider = emb.get("provider")
        model = emb.get("model")
        if provider and model:
            return (str(provider), str(model))
        return None
    if getattr(app_state, "memory_embed_fn", None) is not None:
        return ("custom", "custom")
    return None


async def get_affect_anchors(app_state: Any) -> Any | None:
    """The process-cached EPA anchors for the CURRENT embedding model.

    Returns None when no embedding provider is configured or the build
    fails — affect then stays off for this call and will retry later.
    """
    if app_state is None:
        return None
    key = _embedding_cache_key(app_state)
    if key is None:
        return None
    cached = getattr(app_state, _ANCHORS_ATTR, None)
    if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == key:
        return cached[1]

    embed_fn = getattr(app_state, "memory_embed_fn", None)
    if embed_fn is None:
        return None
    lock = getattr(app_state, _LOCK_ATTR, None)
    if lock is None:
        lock = asyncio.Lock()
        try:
            setattr(app_state, _LOCK_ATTR, lock)
        except (AttributeError, TypeError):  # pragma: no cover — odd stubs
            pass
    async with lock:
        cached = getattr(app_state, _ANCHORS_ATTR, None)
        if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == key:
            return cached[1]
        try:
            from corlinman_memory_kernel.affect import build_anchors

            anchors = await build_anchors(
                embed_fn,
                embed_many=getattr(app_state, "memory_embed_many_fn", None),
            )
        except Exception as exc:  # noqa: BLE001 — affect is an enhancement
            logger.warning("memory.affect.anchor_build_failed", error=str(exc))
            return None
        if anchors is None:
            return None
        try:
            setattr(app_state, _ANCHORS_ATTR, (key, anchors))
        except (AttributeError, TypeError):  # pragma: no cover
            pass
        logger.info(
            "memory.affect.anchors_built", provider=key[0], model=key[1]
        )
        return anchors
