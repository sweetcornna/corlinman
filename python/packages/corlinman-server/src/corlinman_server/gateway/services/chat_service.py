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
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from corlinman_grpc._generated.corlinman.v1 import (
    agent_pb2,
    common_pb2,
)
from corlinman_grpc.agent_client import (
    PlaceholderExecutor,
    ToolExecutor,
)

from corlinman_server import telemetry

# ─── Re-exports from extracted siblings ───────────────────────────────
# These names were moved into sibling modules but are re-exported here so
# external importers (agent_servicer.py, services/__init__.py,
# grpc_backend.py, tests) keep working via
# ``from ...chat_service import X`` unchanged.
from corlinman_server.gateway.services._frame_handlers import (
    _BUILTIN_DONE_PREFIX,
    _BUILTIN_OBSERVATION_PREFIX,
    _internal_error_from_exception,
    _next_frame,
    _suppress_cancelled,
)
from corlinman_server.gateway.services._grpc_backend_impl import (
    GrpcAgentChatBackend,
)
from corlinman_server.gateway.services._proto_converters import (
    _attachment_to_proto,
    _binding_to_proto,
    _reason_from_proto,
    _role_to_proto,
)
from corlinman_server.gateway_api import (
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
    Usage as ApiUsage,
)
from corlinman_server.gateway_api.types import AttachmentEvent

__all__ = [
    "ChatBackend",
    "ChatService",
    "GrpcAgentChatBackend",
]

#: Sentinel ``plugin`` prefix on a ToolCall frame carrying live
#: attachment metadata: the agent servicer registered a tool-produced
#: file into the gateway file store mid-turn and broadcast the slim
#: ``{kind,url,name,mime}`` meta so UI consumers can render the file
#: before the turn ends. Companion to ``_builtin:`` / ``_builtin_done:``
#: (see :mod:`._frame_handlers`) — no execution, no round-trip.
_BUILTIN_ATTACHMENT_PREFIX = "_builtin_attachment:"


log = logging.getLogger(__name__)


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
    try:
        # Build the proto request inside the try so a malformed request
        # (e.g. a channel-built ``SimpleNamespace`` missing an optional
        # field) degrades to a terminal ErrorEvent the caller can surface
        # as "[corlinman error] ..." instead of escaping as a raw
        # exception that silently kills the turn with no reply.
        start = _build_chat_start(req)
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
                # ``_builtin_attachment:`` prefix carries live attachment
                # metadata (file already registered into the gateway file
                # store). Best-effort parse — a malformed payload is
                # dropped, never fatal. Yielded as AttachmentEvent so the
                # web chat renders the file mid-turn.
                if tc.plugin.startswith(_BUILTIN_ATTACHMENT_PREFIX):
                    try:
                        meta = (
                            json.loads(bytes(tc.args_json).decode("utf-8"))
                            if tc.args_json else {}
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        meta = {}
                    if not isinstance(meta, dict):
                        meta = {}
                    if meta.get("url"):
                        yield AttachmentEvent(
                            kind=str(meta.get("kind") or "file"),
                            url=str(meta.get("url") or ""),
                            name=str(meta.get("name") or ""),
                            mime=str(meta.get("mime") or ""),
                            size=_attachment_size(meta),
                            call_id=tc.call_id,
                        )
                    continue
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
    provider_config_json = _provider_config_json(req)

    start = agent_pb2.ChatStart(
        model=req.model,
        messages=messages,
        tools_json=b"",
        session_key=req.session_key,
        temperature=float(req.temperature or 0.0),
        max_tokens=int(req.max_tokens or 0),
        stream=req.stream,
        provider_config_json=provider_config_json,
        attachments=attachments,
        # Channel turns hand in a lightweight ``SimpleNamespace`` request
        # that carries no ``persona_id`` unless humanlike persona injection
        # is enabled (off by default), so read it tolerantly. The pydantic
        # ``InternalChatRequest`` from the web path always carries it.
        persona_id=getattr(req, "persona_id", None) or "",
    )
    if binding is not None:
        start.binding.CopyFrom(binding)
    return start


def _attachment_size(meta: object) -> int | None:
    if not isinstance(meta, dict):
        return None
    try:
        size = int(meta.get("size") or meta.get("size_bytes") or 0)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _provider_config_json(req: InternalChatRequest) -> bytes:
    provider_hint = getattr(req, "provider_hint", None)
    if not isinstance(provider_hint, str) or not provider_hint.strip():
        return b""
    return json.dumps(
        {"provider_hint": provider_hint.strip()},
        separators=(",", ":"),
    ).encode("utf-8")
