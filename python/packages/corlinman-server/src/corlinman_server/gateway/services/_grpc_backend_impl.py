"""gRPC-backed production backend extracted from
:mod:`corlinman_server.gateway.services.chat_service`.

Holds :class:`GrpcAgentChatBackend` (the production :class:`ChatBackend`
implementation) and its :class:`_ServerFrameIter` async-iterator wrapper.
Moved here verbatim; this module MUST NOT import ``chat_service``
(no cycle).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from corlinman_grpc._generated.corlinman.v1 import (
    agent_pb2,
)
from corlinman_grpc.agent_client import (
    AgentClient,
    ChatStream,
)


class GrpcAgentChatBackend:
    """Production :class:`ChatBackend` that dials the Python agent over
    ``grpc.aio``.

    Wraps an :class:`AgentClient`. Each :meth:`start` call opens a new
    bidi ``Agent.Chat`` stream and sends ``ChatStart`` as the first
    frame; the returned ``(tx, rx)`` pair is the same shape the
    :class:`ChatService` consumer expects.

    The ``tx`` queue is the same bounded queue the underlying
    :class:`ChatStream` uses internally — see
    :data:`corlinman_grpc.agent_client.CHANNEL_CAPACITY`.
    """

    def __init__(self, client: AgentClient) -> None:
        self._client = client

    async def start(
        self,
        start: agent_pb2.ChatStart,
    ) -> tuple[asyncio.Queue[Any], AsyncIterator[agent_pb2.ServerFrame]]:
        stream: ChatStream = await self._client.chat()
        # First frame must be ``ChatStart`` (cf. Rust ``ChatBackend::start``).
        await stream.send(agent_pb2.ClientFrame(start=start))
        # Hand callers the same internal queue the stream writes into so
        # ``tool_result`` / ``cancel`` frames flow back into the bidi
        # half-channel without an extra queue layer.
        tx: asyncio.Queue[Any] = stream._tx  # noqa: SLF001 — same-package access
        return tx, _ServerFrameIter(stream)


class _ServerFrameIter:
    """Async iterator wrapper around :class:`ChatStream` that yields
    raw protobuf frames (the inner half-channel reads them directly
    via ``grpc.aio``'s ``__aiter__``)."""

    def __init__(self, stream: ChatStream) -> None:
        self._stream = stream
        self._aiter = stream.__aiter__()

    def __aiter__(self) -> _ServerFrameIter:
        return self

    async def __anext__(self) -> agent_pb2.ServerFrame:
        return await self._aiter.__anext__()
