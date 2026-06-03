"""Frame-drain, cancellation, and exception-bridge helpers extracted from
:mod:`corlinman_server.gateway.services.chat_service`.

Also holds the builtin sentinel prefixes the agent servicer reserves for
in-process observation / tool-completion frames. Moved here verbatim;
this module MUST NOT import ``chat_service`` (no cycle).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from corlinman_grpc._generated.corlinman.v1 import (
    agent_pb2,
)

from corlinman_server.gateway_api import (
    InternalChatError,
)

# Plugin names starting with this prefix are reserved for the agent servicer's observation-only frames. Real plugins MUST NOT use this prefix.
_BUILTIN_OBSERVATION_PREFIX = "_builtin:"
# Companion prefix the servicer uses to broadcast "tool finished" events
# after an in-process builtin returns. The ``args_json`` payload of these
# frames carries ``{"duration_ms", "is_error", "error_summary"}``.
_BUILTIN_DONE_PREFIX = "_builtin_done:"


def _internal_error_from_exception(exc: BaseException) -> InternalChatError:
    """Lift a connector/transport exception to
    :class:`InternalChatError`. Mirrors the Rust
    ``InternalChatError::from(CorlinmanError)`` blanket impl."""
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return InternalChatError(reason=reason, message=str(exc))
    return InternalChatError(reason="unknown", message=str(exc))


async def _next_frame(
    rx: AsyncIterator[agent_pb2.ServerFrame],
) -> agent_pb2.ServerFrame | None:
    """Drain the next frame from an async iterator, returning ``None``
    on clean end-of-stream so the caller can synthesise a terminal
    ``Done`` event (mirrors the Rust ``Option<...>`` shape)."""
    try:
        return await rx.__anext__()
    except StopAsyncIteration:
        return None


class _suppress_cancelled:
    """Tiny ctx mgr to swallow ``asyncio.CancelledError`` raised by
    awaiting a cancelled task. Equivalent of
    ``contextlib.suppress(asyncio.CancelledError)`` but with the
    explicit naming the chat-service flow expects."""

    def __enter__(self) -> None:  # pragma: no cover — trivial
        return None

    def __exit__(self, _exc_type, exc, _tb) -> bool:  # noqa: ANN001
        return isinstance(exc, asyncio.CancelledError)
