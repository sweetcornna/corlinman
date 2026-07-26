"""``corlinman_server.gateway.services`` — gateway-internal service layer.

Mirrors :rust:`corlinman_gateway::services`. These services expose the
chat pipeline as callable Python APIs so other in-process components
(channel adapters, scheduler jobs, admin tasks) can drive it without a
round-trip through HTTP.

``bootstrap`` is the sibling-seam hook the gateway lifespan calls (see
``docs/contracts/runtime-wiring.md`` §2): it wires the ChatService onto
``AppState.chat`` (P2 direct backend or P4 gRPC-agent backend, picked
per config) and then launches the P3 channel runtime, handing its
background tasks back to the lifespan for cancellation on shutdown.
"""

from __future__ import annotations

from typing import Any

import structlog

from corlinman_server.gateway.services.chat_service import (
    ChatBackend,
    ChatService,
    GrpcAgentChatBackend,
)
from corlinman_server.gateway.services.direct_backend import (
    DirectProviderBackend,
)
from corlinman_server.gateway.services.grpc_backend import build_chat_service

logger = structlog.get_logger(__name__)

__all__ = [
    "ChatBackend",
    "ChatService",
    "DirectProviderBackend",
    "GrpcAgentChatBackend",
    "bootstrap",
    "build_chat_service",
]


def bootstrap(state: Any) -> list[Any]:
    """Sibling-seam hook — wire chat, then launch channels.

    1. **Chat (P2/P4)** — if ``state.chat`` is unset, build a
       :class:`ChatService` via :func:`build_chat_service`, which picks
       the direct-provider backend or the gRPC-agent backend per
       ``config["models"]["backend"]`` / ``$CORLINMAN_CHAT_BACKEND``.
       An already-set ``state.chat`` is left untouched (idempotent).
    2. **Channels (P3)** — launch the QQ/Telegram channel runtime and
       return its background ``asyncio.Task`` list so the gateway
       lifespan cancels + awaits them on shutdown.

    Every step degrades rather than crashes: a failure logs a warning
    and leaves the corresponding attach point unfilled (degraded mode).
    """
    if getattr(state, "chat", None) is None:
        try:
            state.chat = build_chat_service(state)
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash boot
            logger.warning("gateway.services.chat_bootstrap_failed", error=str(exc))

    try:
        from corlinman_server.gateway.channels_runtime import (
            bootstrap as _channels_bootstrap,
        )
        from corlinman_server.gateway.services.channel_bridge import (
            ChannelChatServiceBridge,
        )

        chat = getattr(state, "chat", None)
        channel_chat = ChannelChatServiceBridge(chat) if chat is not None else None
        original = chat
        state.chat = channel_chat
        try:
            tasks = _channels_bootstrap(state)
        finally:
            state.chat = original
        return list(tasks) if tasks else []
    except Exception as exc:  # noqa: BLE001 — channels are optional
        logger.warning("gateway.services.channels_bootstrap_failed", error=str(exc))
        return []
