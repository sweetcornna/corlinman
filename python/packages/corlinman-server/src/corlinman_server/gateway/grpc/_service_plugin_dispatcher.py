"""Service-plugin dispatch — extracted from
:mod:`corlinman_server.gateway.grpc.plugin_invoker`.

P16 — long-lived ``service``-kind plugins dialled over gRPC via the
:class:`corlinman_providers.plugins.PluginSupervisor`. Co-owns the
shared :func:`_error_invocation` helper (consumed here and re-imported by
the MCP bridge sibling). MUST NOT import the source module (no cycle);
the source re-imports :class:`ServicePluginDispatcher` and
:func:`invoke_service_plugin` from here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any, cast

import structlog
from corlinman_grpc.agent_client import ToolInvocation

__all__ = ["ServicePluginDispatcher", "invoke_service_plugin"]

log = structlog.get_logger(__name__)


def _error_invocation(code: str, message: str, duration_ms: int = 0) -> ToolInvocation:
    """Build an ``is_error`` :class:`ToolInvocation` with a stable body."""
    return ToolInvocation(
        content=json.dumps({"error": code, "message": message}),
        is_error=True,
        duration_ms=duration_ms,
    )


def _grpc_uds_target(socket_path: Any) -> str:
    """grpc.aio UDS target string for a Unix-domain socket path."""
    return f"unix:{socket_path}"


class ServicePluginDispatcher:
    """Routes a tool call to a ``service``-kind plugin.

    ``service`` plugins are long-lived processes the
    :class:`corlinman_providers.plugins.PluginSupervisor` spawns; each
    child hosts a ``corlinman.v1.PluginBridge`` gRPC server on a per-
    plugin UDS the supervisor exports via ``CORLINMAN_PLUGIN_ADDR``.

    This dispatcher owns the *client* half: it asks the supervisor to
    spawn the service once (lazily, on first use), caches the returned
    UDS path + gRPC channel, and dials ``PluginBridge.Execute`` per
    call. Re-spawns triggered by the supervisor watchdog change the
    socket path; a dial failure drops the cached channel so the next
    call re-resolves.

    Never raises out of :meth:`dispatch` — every failure folds into an
    ``is_error`` :class:`ToolInvocation`.
    """

    def __init__(self, supervisor: Any) -> None:
        self._supervisor = supervisor
        self._sockets: dict[str, Any] = {}
        self._channels: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def _resolve_socket(self, manifest: Any) -> Any:
        """Get-or-spawn the UDS socket for ``manifest``'s service.

        Reuses a live child if the supervisor still tracks one;
        otherwise spawns a fresh one. Returns the socket path.
        """
        name = manifest.name
        # Fast path: supervisor still tracks a live child for this name.
        tracked = self._tracked_socket(name)
        if tracked is not None:
            self._sockets[name] = tracked
            return tracked
        cached = self._sockets.get(name)
        if cached is not None:
            return cached
        socket_path = await self._supervisor.spawn_service(manifest)
        self._sockets[name] = socket_path
        return socket_path

    def _tracked_socket(self, name: str) -> Any | None:
        """Best-effort lookup of a live child's socket on the supervisor.

        The supervisor keeps children in a private ``_children`` map; we
        read it defensively so a different supervisor shape just falls
        back to a fresh spawn.
        """
        children = getattr(self._supervisor, "_children", None)
        if not isinstance(children, dict):
            return None
        child = children.get(name)
        if child is None:
            return None
        process = getattr(child, "process", None)
        if process is not None and getattr(process, "returncode", 0) is not None:
            # Child has exited — let the caller re-spawn.
            return None
        return getattr(child, "socket_path", None)

    async def _channel_for(self, name: str, socket_path: Any) -> Any:
        """Get-or-open a gRPC channel to ``socket_path``."""
        existing = self._channels.get(name)
        if existing is not None:
            return existing
        import grpc.aio

        channel = grpc.aio.insecure_channel(_grpc_uds_target(socket_path))
        self._channels[name] = channel
        return channel

    async def _drop_channel(self, name: str) -> None:
        """Drop the cached channel + socket so the next call re-resolves."""
        channel = self._channels.pop(name, None)
        self._sockets.pop(name, None)
        if channel is not None:
            with contextlib.suppress(Exception):
                await channel.close()

    async def dispatch(
        self,
        entry: Any,
        tool: str,
        args: Any,
        *,
        timeout_ms: int,
    ) -> ToolInvocation:
        """Run one ``service`` plugin tool call. Never raises."""
        if self._supervisor is None:
            return _error_invocation(
                "service_supervisor_unavailable",
                "no plugin supervisor is wired into the gateway",
            )

        manifest = entry.manifest
        name = manifest.name
        started = time.monotonic()

        try:
            from corlinman_grpc._generated.corlinman.v1 import (
                plugin_pb2,
                plugin_pb2_grpc,
            )
        except Exception as exc:  # pragma: no cover — corlinman-grpc is a dep
            return _error_invocation(
                "service_grpc_unavailable",
                f"PluginBridge gRPC stubs are unavailable: {exc}",
            )

        async with self._lock:
            try:
                socket_path = await self._resolve_socket(manifest)
            except Exception as exc:
                return _error_invocation(
                    "service_spawn_failed",
                    f"could not spawn service plugin {name!r}: {exc}",
                )
            try:
                channel = await self._channel_for(name, socket_path)
            except Exception as exc:
                return _error_invocation(
                    "service_dial_failed",
                    f"could not dial service plugin {name!r}: {exc}",
                )

        try:
            args_json = json.dumps(args, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return _error_invocation(
                "bad_tool_arguments",
                f"could not serialize arguments for {name!r}: {exc}",
            )

        request = plugin_pb2.PluginToolCall(
            call_id=f"svc-{int(time.monotonic() * 1000)}",
            plugin=name,
            tool=tool,
            args_json=args_json,
            session_key="agent",
        )
        stub = plugin_pb2_grpc.PluginBridgeStub(channel)
        deadline_s = max(timeout_ms, 1) / 1000.0

        try:
            outcome = await asyncio.wait_for(
                self._consume_stream(stub, request),
                timeout=deadline_s,
            )
        except TimeoutError:
            await self._drop_channel(name)
            elapsed = int((time.monotonic() - started) * 1000)
            return _error_invocation(
                "service_timeout",
                f"service plugin {name!r} did not finish within {timeout_ms}ms",
                elapsed,
            )
        except Exception as exc:
            await self._drop_channel(name)
            elapsed = int((time.monotonic() - started) * 1000)
            return _error_invocation(
                "service_call_failed",
                f"service plugin {name!r} call failed: {exc}",
                elapsed,
            )

        elapsed = int((time.monotonic() - started) * 1000)
        return _outcome_to_invocation(outcome, elapsed)

    async def _consume_stream(self, stub: Any, request: Any) -> dict[str, Any]:
        """Drive ``PluginBridge.Execute`` and reduce its ``ToolEvent``
        stream to a terminal outcome dict.

        Terminal events: ``result`` (success), ``error`` (tool-level
        failure), ``awaiting_approval`` (surfaced as a non-error status
        the model can react to). ``progress`` events are drained and
        dropped. A stream that ends without a terminal event yields an
        ``service_no_result`` error.
        """
        call = stub.Execute(request)
        async for event in call:
            which = event.WhichOneof("kind")
            if which == "result":
                return {
                    "kind": "result",
                    "result_json": bytes(event.result.result_json),
                    "duration_ms": int(event.result.duration_ms),
                }
            if which == "error":
                return {
                    "kind": "error",
                    "code": _error_reason_label(event.error),
                    "message": getattr(event.error, "message", ""),
                }
            if which == "awaiting_approval":
                return {
                    "kind": "awaiting_approval",
                    "call_id": event.awaiting_approval.call_id,
                    "reason": event.awaiting_approval.reason,
                }
            # progress — drain and continue.
        return {"kind": "no_result"}

    async def aclose(self) -> None:
        """Close every cached gRPC channel. Safe to call repeatedly."""
        for name in list(self._channels.keys()):
            await self._drop_channel(name)


def _error_reason_label(error: Any) -> str:
    """Render an ``ErrorInfo``'s ``reason`` enum as a stable string label.

    ``ErrorInfo.reason`` is a ``FailoverReason`` enum; surfacing the
    symbolic name (e.g. ``"TIMEOUT"``) keeps the tool-error body
    self-describing for the model.
    """
    reason = getattr(error, "reason", None)
    if reason is None:
        return ""
    try:
        # protobuf descriptor reflection is dynamically typed.
        return cast(
            "str",
            error.DESCRIPTOR.fields_by_name["reason"]
            .enum_type.values_by_number[int(reason)]
            .name,
        )
    except Exception:
        return str(reason)


def _outcome_to_invocation(outcome: dict[str, Any], elapsed: int) -> ToolInvocation:
    """Fold a service-plugin terminal outcome into a :class:`ToolInvocation`."""
    kind = outcome.get("kind")
    if kind == "result":
        body = outcome.get("result_json") or b"null"
        return ToolInvocation(
            content=body.decode("utf-8", errors="replace"),
            is_error=False,
            duration_ms=outcome.get("duration_ms") or elapsed,
        )
    if kind == "error":
        return ToolInvocation(
            content=json.dumps(
                {
                    "error": "service_plugin_error",
                    "code": outcome.get("code", ""),
                    "message": outcome.get("message", ""),
                }
            ),
            is_error=True,
            duration_ms=elapsed,
        )
    if kind == "awaiting_approval":
        return ToolInvocation(
            content=json.dumps(
                {
                    "status": "awaiting_approval",
                    "call_id": outcome.get("call_id", ""),
                    "reason": outcome.get("reason", ""),
                }
            ),
            is_error=False,
            duration_ms=elapsed,
        )
    return _error_invocation(
        "service_no_result",
        "service plugin stream ended without a result/error event",
        elapsed,
    )


async def invoke_service_plugin(
    dispatcher: ServicePluginDispatcher,
    entry: Any,
    tool: str,
    args: Any,
    *,
    timeout_ms: int,
) -> ToolInvocation:
    """Module-level convenience wrapper over
    :meth:`ServicePluginDispatcher.dispatch`."""
    return await dispatcher.dispatch(entry, tool, args, timeout_ms=timeout_ms)
