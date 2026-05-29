"""Real :class:`ChatService` implementation.

Port of :rust:`corlinman_gateway::services::chat_service`. Bridges
in-process callers (channels, scheduler, admin tasks) to the same chat
backend that serves ``/v1/chat/completions``, so an HTTP request and a
QQ-channel message go through identical reasoning-loop wiring.

The Rust crate factors the backend out as a ``trait ChatBackend``;
the Python equivalent is a :class:`Protocol` (structural typing) so
test backends don't have to inherit anything. The production backend
:class:`GrpcAgentChatBackend` wraps a
:class:`corlinman_grpc.agent_client.AgentClient` — i.e. it dials the
Python agent over gRPC, exactly mirroring how the Rust gateway used to
proxy the HTTP request into the Python plane.

Scope mirrors the Rust M5 surface: ``TokenDelta``,
``ToolCall``, ``Done``, ``Error`` are surfaced as the corresponding
:class:`corlinman_server.gateway_api.InternalChatEvent` variants;
``AwaitingApproval`` and standalone ``Usage`` frames are silently
skipped (they land with the approval pipeline in M6+).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from corlinman_grpc._generated.corlinman.v1 import (
    agent_pb2,
    common_pb2,
)
from corlinman_grpc.agent_client import (
    AgentClient,
    ChatStream,
    PlaceholderExecutor,
    ToolExecutor,
)
from corlinman_grpc.agent_client.types import FailoverReason as GrpcFailoverReason

from corlinman_server import telemetry
from corlinman_server.gateway_api import (
    Attachment as ApiAttachment,
)
from corlinman_server.gateway_api import (
    AttachmentKind as ApiAttachmentKind,
)
from corlinman_server.gateway_api import (
    ChannelBinding,
    ChatEventStream,
    ChatServiceBase,
    DoneEvent,
    ErrorEvent,
    InternalChatError,
    InternalChatRequest,
    TokenDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from corlinman_server.gateway_api import (
    Role as ApiRole,
)
from corlinman_server.gateway_api import (
    Usage as ApiUsage,
)

__all__ = [
    "ChatBackend",
    "ChatService",
    "GrpcAgentChatBackend",
]


log = logging.getLogger(__name__)


# Plugin names starting with this prefix are reserved for the agent servicer's observation-only frames. Real plugins MUST NOT use this prefix.
_BUILTIN_OBSERVATION_PREFIX = "_builtin:"
# Companion prefix the servicer uses to broadcast "tool finished" events
# after an in-process builtin returns. The ``args_json`` payload of these
# frames carries ``{"duration_ms", "is_error", "error_summary"}``.
_BUILTIN_DONE_PREFIX = "_builtin_done:"


# ─── Backend protocol ────────────────────────────────────────────────


@runtime_checkable
class ChatBackend(Protocol):
    """Structural surface mirroring the Rust ``trait ChatBackend``.

    ``start`` opens an in-process pipeline that the :class:`ChatService`
    drives — the returned ``(tx, rx)`` pair is the same shape as the
    Rust ``(mpsc::Sender<ClientFrame>, BackendRx)`` tuple:

    * ``tx`` — outbound :class:`agent_pb2.ClientFrame` channel
      (``ToolResult`` / ``ApprovalDecision`` / ``Cancel``). Production
      backends forward this to a gRPC ``Agent.Chat`` bidi stream;
      tests can wire a no-op queue.
    * ``rx`` — async iterator of :class:`agent_pb2.ServerFrame` (or
      raised exception) that the service folds into
      :class:`InternalChatEvent` variants.

    The protocol is intentionally minimal — every concrete backend
    (gRPC, scripted-mock, future websocket bridge) implements the same
    two-half pattern.
    """

    async def start(
        self,
        start: agent_pb2.ChatStart,
    ) -> tuple[asyncio.Queue[Any], AsyncIterator[agent_pb2.ServerFrame]]: ...


# ─── ChatService ──────────────────────────────────────────────────────


class ChatService(ChatServiceBase):
    """Gateway-side service that wraps any :class:`ChatBackend` so it
    can be driven from in-process callers via the
    :class:`corlinman_server.gateway_api.ChatService` protocol.

    Mirrors :rust:`corlinman_gateway::services::chat_service::ChatService`:
    holds an :class:`Arc<dyn ChatBackend>` (Python: a shared backend
    reference) plus a :class:`ToolExecutor` that runs ``tool_call``
    frames and feeds the result back so the reasoning loop keeps
    progressing.

    Production wiring (:func:`corlinman_server.gateway.services.\
grpc_backend.build_grpc_chat_service`) injects a
    :class:`~corlinman_grpc.agent_client.RegistryToolExecutor` bound to
    the gateway's plugin registry — i.e. tool calls are *really*
    executed. The :class:`~corlinman_grpc.agent_client.PlaceholderExecutor`
    remains the constructor default only as a degraded fallback for
    callers (and tests) that have no executor to inject.
    """

    def __init__(
        self,
        backend: ChatBackend,
        *,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        self._backend = backend
        self._tool_executor: ToolExecutor = tool_executor or PlaceholderExecutor()

    def with_tool_executor(self, executor: ToolExecutor) -> ChatService:
        """Customise the tool executor — used by tests and by the
        gateway assembly layer to inject the real
        :class:`~corlinman_grpc.agent_client.RegistryToolExecutor`.
        Returns ``self`` so callers can chain (mirrors the Rust builder
        shape)."""
        self._tool_executor = executor
        return self

    def run(
        self,
        req: InternalChatRequest,
        cancel: asyncio.Event,
    ) -> ChatEventStream:
        """Open the backend pipeline and yield
        :class:`InternalChatEvent` until the stream terminates.

        Implements the :class:`~corlinman_server.gateway_api.ChatService`
        protocol contract: emits any number of
        :class:`TokenDeltaEvent` / :class:`ToolCallEvent` followed by
        exactly one terminal :class:`DoneEvent` or :class:`ErrorEvent`.
        Honours ``cancel`` between every yield.
        """
        return _run_chat_traced(self._backend, self._tool_executor, req, cancel)


async def _run_chat_traced(
    backend: ChatBackend,
    executor: ToolExecutor,
    req: InternalChatRequest,
    cancel: asyncio.Event,
) -> AsyncIterator[Any]:
    """Thin span-aware wrapper around :func:`_run_chat`.

    Opens a ``chat.service`` span with backend kind and model, then drives
    the inner generator unchanged. Token and tool-call counts are recorded on
    the span before it closes. This is a pure passthrough when telemetry is
    not initialised — the ``telemetry.span`` helper is a no-op in that case.
    """
    backend_kind = type(backend).__name__
    with telemetry.span(
        "chat.service",
        attributes={
            "chat.backend": backend_kind,
            "chat.model": req.model,
            "chat.stream": req.stream,
        },
    ) as svc_span:
        token_count = 0
        chunk_count = 0
        async for event in _run_chat(backend, executor, req, cancel):
            if isinstance(event, TokenDeltaEvent):
                token_count += len(event.text)
                chunk_count += 1
            elif isinstance(event, DoneEvent):
                svc_span.set_attribute("chat.token_chars", token_count)
                svc_span.set_attribute("chat.chunks", chunk_count)
                if event.usage is not None:
                    svc_span.set_attribute(
                        "chat.prompt_tokens", event.usage.prompt_tokens
                    )
                    svc_span.set_attribute(
                        "chat.completion_tokens", event.usage.completion_tokens
                    )
                    svc_span.set_attribute(
                        "chat.total_tokens", event.usage.total_tokens
                    )
                svc_span.set_attribute("chat.finish_reason", event.finish_reason)
            elif isinstance(event, ErrorEvent):
                svc_span.set_attribute("chat.error_reason", event.error.reason)
                svc_span.set_attribute("chat.error_message", event.error.message)
            yield event


async def _run_chat(
    backend: ChatBackend,
    executor: ToolExecutor,
    req: InternalChatRequest,
    cancel: asyncio.Event,
) -> AsyncIterator[Any]:
    """Async generator implementing the Rust ``into_event_stream`` loop.

    Returned by :meth:`ChatService.run`; callers ``async for ev in s``
    just as Rust callers ``while let Some(ev) = s.next().await``.
    """
    start = _build_chat_start(req)
    try:
        tx, rx = await backend.start(start)
    except Exception as err:  # noqa: BLE001 — surface as terminal error
        yield ErrorEvent(error=_internal_error_from_exception(err))
        return

    # Bridge cancel → drop upstream call. We poll-and-select using
    # ``asyncio.wait`` so a fired cancel unblocks the loop even when
    # the backend has nothing pending.
    cancel_task = asyncio.create_task(cancel.wait())
    try:
        while True:
            if cancel.is_set():
                yield ErrorEvent(
                    error=InternalChatError(reason="unknown", message="cancelled"),
                )
                return

            next_task = asyncio.create_task(_next_frame(rx))
            done, _pending = await asyncio.wait(
                {next_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if cancel_task in done:
                next_task.cancel()
                # Drain the cancellation so it doesn't leak as a warning.
                with _suppress_cancelled():
                    await next_task
                yield ErrorEvent(
                    error=InternalChatError(reason="unknown", message="cancelled"),
                )
                return

            try:
                frame = await next_task
            except Exception as err:  # noqa: BLE001 — terminal
                yield ErrorEvent(error=_internal_error_from_exception(err))
                return

            if frame is None:
                # Stream ended without ``Done`` — synthesise one so
                # callers always see a terminal event. Matches the
                # Rust ``None`` arm.
                yield DoneEvent(finish_reason="stop", usage=None)
                return

            kind = frame.WhichOneof("kind")
            if kind == "token":
                yield TokenDeltaEvent(
                    text=frame.token.text,
                    is_reasoning=bool(frame.token.is_reasoning),
                )
                continue

            if kind == "tool_call":
                tc = frame.tool_call
                # ``_builtin_done:`` prefix marks a tool-completion
                # observation: the in-process dispatch returned, and the
                # servicer broadcasts duration + error info so channel
                # UIs can render "✅ tool (1.2s)" or "❌ tool failed".
                # No execution, no round-trip — purely a notification.
                if tc.plugin.startswith(_BUILTIN_DONE_PREFIX):
                    try:
                        meta = (
                            __import__("json").loads(bytes(tc.args_json).decode("utf-8"))
                            if tc.args_json else {}
                        )
                    except Exception:  # noqa: BLE001
                        meta = {}
                    yield ToolResultEvent(
                        plugin=tc.plugin[len(_BUILTIN_DONE_PREFIX):],
                        tool=tc.tool,
                        call_id=tc.call_id,
                        duration_ms=int(meta.get("duration_ms", 0) or 0),
                        is_error=bool(meta.get("is_error", False)),
                        error_summary=str(meta.get("error_summary", ""))[:200],
                    )
                    continue
                # ``_builtin:`` prefix on ``plugin`` marks an observation-
                # only frame — the agent servicer already dispatched the
                # tool in-process and fed the result back to its loop, so
                # round-tripping a second ``tool_result`` here would
                # double-feed the call_id and corrupt the conversation.
                # Strip the prefix and yield the event without executing.
                if tc.plugin.startswith(_BUILTIN_OBSERVATION_PREFIX):
                    yield ToolCallEvent(
                        plugin=tc.plugin[len(_BUILTIN_OBSERVATION_PREFIX):],
                        tool=tc.tool,
                        args_json=bytes(tc.args_json),
                        call_id=tc.call_id,
                    )
                    continue
                # Defense in depth: catch an upstream that's mistakenly
                # stripping the sentinel prefix before we see it. A real
                # plugin name should never contain the reserved literal.
                if _BUILTIN_OBSERVATION_PREFIX in tc.plugin:
                    log.warning(
                        "chat_service.suspicious_plugin_name plugin=%s tool=%s",
                        tc.plugin,
                        tc.tool,
                    )
                # Execute the tool via the injected executor and feed the
                # genuine result back into the reasoning loop so it makes
                # real multi-round progress. The production executor
                # (RegistryToolExecutor) never raises — but a custom
                # executor might, and a failed feedback send must not
                # tear the stream down, so this stays guarded.
                try:
                    result = await executor.execute(tc)
                    await tx.put(agent_pb2.ClientFrame(tool_result=result))
                except Exception as exc:  # noqa: BLE001 — ack failure is non-fatal
                    log.debug(
                        "chat_service.tool_ack_failed plugin=%s tool=%s err=%s",
                        tc.plugin,
                        tc.tool,
                        exc,
                    )
                yield ToolCallEvent(
                    plugin=tc.plugin,
                    tool=tc.tool,
                    args_json=bytes(tc.args_json),
                    call_id=tc.call_id,
                )
                continue

            if kind == "done":
                d = frame.done
                usage: ApiUsage | None = None
                if d.HasField("usage"):
                    usage = ApiUsage(
                        prompt_tokens=int(d.usage.prompt_tokens),
                        completion_tokens=int(d.usage.completion_tokens),
                        total_tokens=int(d.usage.total_tokens),
                    )
                yield DoneEvent(finish_reason=d.finish_reason, usage=usage)
                return

            if kind == "error":
                e = frame.error
                yield ErrorEvent(
                    error=InternalChatError(
                        reason=_reason_from_proto(int(e.reason)),
                        message=e.message,
                    ),
                )
                return

            # ``awaiting`` and ``usage`` are not surfaced in this milestone
            # — pull the next frame. ``None`` (unset oneof) is treated
            # the same way.
            continue
    finally:
        cancel_task.cancel()
        with _suppress_cancelled():
            await cancel_task


# ─── Helpers (proto translation) ──────────────────────────────────────


def _build_chat_start(req: InternalChatRequest) -> agent_pb2.ChatStart:
    """Build the protobuf ``ChatStart`` from an
    :class:`InternalChatRequest`. Mirrors :rust:`build_chat_start`."""
    messages = [
        common_pb2.Message(
            role=_role_to_proto(m.role),
            content=m.content,
            name="",
            tool_call_id="",
        )
        for m in req.messages
    ]
    attachments = [_attachment_to_proto(a) for a in req.attachments]
    binding = _binding_to_proto(req.binding) if req.binding is not None else None

    start = agent_pb2.ChatStart(
        model=req.model,
        messages=messages,
        tools_json=b"",
        session_key=req.session_key,
        temperature=float(req.temperature or 0.0),
        max_tokens=int(req.max_tokens or 0),
        stream=req.stream,
        provider_config_json=b"",
        attachments=attachments,
    )
    if binding is not None:
        start.binding.CopyFrom(binding)
    return start


def _binding_to_proto(b: ChannelBinding) -> common_pb2.ChannelBinding:
    """Convert the in-process :class:`ChannelBinding` to its protobuf
    twin. The ``session_key`` field on the proto side is the pre-derived
    key so the Python agent doesn't need to re-hash. Mirrors
    :rust:`binding_to_proto`."""
    return common_pb2.ChannelBinding(
        channel=b.channel,
        account=b.account,
        thread=b.thread,
        sender=b.sender,
        session_key=b.session_key(),
    )


def _attachment_to_proto(a: ApiAttachment) -> agent_pb2.Attachment:
    """Convert :class:`ApiAttachment` → protobuf ``Attachment``. The
    enum mapping is explicit — silently defaulting to ``UNSPECIFIED``
    would drop multimodal inputs without a trace. Mirrors
    :rust:`attachment_to_proto`."""
    if a.kind == ApiAttachmentKind.IMAGE:
        kind = agent_pb2.ATTACHMENT_KIND_IMAGE
    elif a.kind == ApiAttachmentKind.AUDIO:
        kind = agent_pb2.ATTACHMENT_KIND_AUDIO
    elif a.kind == ApiAttachmentKind.VIDEO:
        kind = agent_pb2.ATTACHMENT_KIND_VIDEO
    elif a.kind == ApiAttachmentKind.FILE:
        kind = agent_pb2.ATTACHMENT_KIND_FILE
    else:  # pragma: no cover — exhaustive over StrEnum
        kind = agent_pb2.ATTACHMENT_KIND_UNSPECIFIED
    return agent_pb2.Attachment(
        kind=kind,
        url=a.url or "",
        bytes=a.bytes_ or b"",
        mime=a.mime or "",
        file_name=a.file_name or "",
    )


def _role_to_proto(role: ApiRole) -> common_pb2.Role:
    if role == ApiRole.USER:
        return common_pb2.USER
    if role == ApiRole.ASSISTANT:
        return common_pb2.ASSISTANT
    if role == ApiRole.SYSTEM:
        return common_pb2.SYSTEM
    if role == ApiRole.TOOL:
        return common_pb2.TOOL
    return common_pb2.ROLE_UNSPECIFIED  # pragma: no cover


# Lowercase string discriminants matching ``InternalChatError.reason``.
# Same set as ``corlinman_grpc.agent_client.types.FailoverReason``.
_REASON_FROM_PROTO: dict[int, str] = {
    int(GrpcFailoverReason.UNSPECIFIED): "unspecified",
    int(GrpcFailoverReason.BILLING): "billing",
    int(GrpcFailoverReason.RATE_LIMIT): "rate_limit",
    int(GrpcFailoverReason.AUTH): "auth",
    int(GrpcFailoverReason.AUTH_PERMANENT): "auth_permanent",
    int(GrpcFailoverReason.TIMEOUT): "timeout",
    int(GrpcFailoverReason.MODEL_NOT_FOUND): "model_not_found",
    int(GrpcFailoverReason.FORMAT): "format",
    int(GrpcFailoverReason.CONTEXT_OVERFLOW): "context_overflow",
    int(GrpcFailoverReason.OVERLOADED): "overloaded",
    int(GrpcFailoverReason.UNKNOWN): "unknown",
}


def _reason_from_proto(code: int) -> str:
    """Mirror :rust:`reason_from_proto` — unknown codes fall back to
    ``"unspecified"`` so a future proto enum addition doesn't crash
    the event stream."""
    return _REASON_FROM_PROTO.get(code, "unspecified")


def _internal_error_from_exception(exc: BaseException) -> InternalChatError:
    """Lift a connector/transport exception to
    :class:`InternalChatError`. Mirrors the Rust
    ``InternalChatError::from(CorlinmanError)`` blanket impl."""
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return InternalChatError(reason=reason, message=str(exc))
    return InternalChatError(reason="unknown", message=str(exc))


# ─── Frame helpers ────────────────────────────────────────────────────


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


# ─── gRPC-backed production backend ──────────────────────────────────


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
