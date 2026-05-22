"""Concrete plugin invoker — runs a tool call against a real plugin.

This is the gateway-assembly half of the tool-execution split documented
in :mod:`corlinman_grpc.agent_client.tool_executor`. The
:class:`~corlinman_grpc.agent_client.RegistryToolExecutor` lives in
``corlinman-grpc`` and stays free of any plugin import; this module owns
the :class:`corlinman_providers.plugins.PluginRegistry` knowledge and
exposes a :data:`~corlinman_grpc.agent_client.PluginInvoker`-shaped
callable the executor delegates to.

Supported plugin types
----------------------

* ``sync`` — spawn-per-call JSON-RPC stdio child (P5). Resolve the
  plugin, build a JSON-RPC 2.0 request, run the ``entry_point`` once,
  read one response line, decode it.
* ``async`` — classified by the registry; a sync-shaped dispatch with
  an ``accepted_for_later`` ``task_id`` outcome surfaced verbatim.
* ``service`` — **P16**. Long-lived process managed by the
  :class:`corlinman_providers.plugins.PluginSupervisor`. The invoker
  asks the supervisor for the plugin's UDS socket, dials the
  ``corlinman.v1.PluginBridge`` gRPC service the child hosts, calls
  ``Execute`` and consumes the ``ToolEvent`` stream to its terminal
  ``result`` / ``error`` event.
* ``mcp`` — **P14/P16**. Routed through the
  :class:`corlinman_mcp_server.McpClientManager`: the MCP bridge owns
  the connected external MCP servers and runs a ``tools/call`` against
  the owning server.

Gate, never crash
-----------------

Every failure mode — registry absent, plugin not found, supervisor
unavailable, MCP bridge unreachable, child crashed — folds into a clear
``is_error`` :class:`~corlinman_grpc.agent_client.ToolInvocation` with a
structured ``{"error": ..., "message": ...}`` body. The
:class:`~corlinman_grpc.agent_client.RegistryToolExecutor` wraps the
whole thing so a raised exception can never reach the chat stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from typing import Any

import structlog
from corlinman_grpc.agent_client import ToolInvocation

__all__ = [
    "DEFAULT_TOOL_TIMEOUT_MS",
    "McpToolBridge",
    "ServicePluginDispatcher",
    "build_registry_invoker",
    "invoke_mcp_plugin",
    "invoke_service_plugin",
    "invoke_sync_plugin",
]

log = structlog.get_logger(__name__)

#: Fallback per-call deadline (ms) when a manifest does not pin
#: ``[communication].timeout_ms``. Mirrors a conservative sync-plugin
#: budget — plugins that need longer must declare it explicitly.
DEFAULT_TOOL_TIMEOUT_MS = 30_000


def _error_invocation(code: str, message: str, duration_ms: int = 0) -> ToolInvocation:
    """Build an ``is_error`` :class:`ToolInvocation` with a stable body."""
    return ToolInvocation(
        content=json.dumps({"error": code, "message": message}),
        is_error=True,
        duration_ms=duration_ms,
    )


def _decode_args(args_json: bytes) -> Any:
    """Decode the OpenAI ``arguments`` JSON. Empty / blank → ``{}``.

    Returns the parsed object on success; raises :class:`ValueError`
    with a human-readable message on malformed JSON so the caller can
    fold it into a tool-level error result.
    """
    raw = args_json.decode("utf-8", errors="replace").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"tool arguments are not valid JSON: {exc}") from exc


async def invoke_sync_plugin(
    entry: Any,
    tool: str,
    args: Any,
    *,
    timeout_ms: int,
) -> ToolInvocation:
    """Run one ``sync`` plugin tool call as a spawn-per-call stdio child.

    ``entry`` is a :class:`corlinman_providers.plugins.PluginEntry`. The
    child is launched from ``entry.manifest.entry_point`` with the
    manifest dir as CWD; one JSON-RPC request line is written, stdin is
    half-closed, and one response line is read back and decoded.

    Never raises — every failure (spawn error, timeout, malformed
    response, JSON-RPC error) is mapped to an ``is_error``
    :class:`ToolInvocation`.
    """
    from corlinman_providers.plugins import parse_response_line

    manifest = entry.manifest
    ep = manifest.entry_point
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": tool,
        "params": args,
    }
    request_line = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")

    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            ep.command,
            *ep.args,
            cwd=str(entry.plugin_dir()),
            env=_child_env(ep.env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        return _error_invocation(
            "plugin_spawn_failed",
            f"could not launch plugin {manifest.name!r}: {exc}",
        )

    deadline_s = max(timeout_ms, 1) / 1000.0
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=request_line),
            timeout=deadline_s,
        )
    except TimeoutError:
        with _SuppressProcessErrors():
            proc.kill()
        with _SuppressProcessErrors():
            await proc.wait()
        elapsed = int((time.monotonic() - started) * 1000)
        return _error_invocation(
            "plugin_timeout",
            f"plugin {manifest.name!r} did not respond within {timeout_ms}ms",
            elapsed,
        )

    elapsed = int((time.monotonic() - started) * 1000)

    if not stdout.strip():
        err_tail = stderr.decode("utf-8", errors="replace").strip()[-512:]
        return _error_invocation(
            "plugin_no_output",
            (
                f"plugin {manifest.name!r} exited without a JSON-RPC line "
                f"(rc={proc.returncode}); stderr: {err_tail or '<empty>'}"
            ),
            elapsed,
        )

    # Take the first newline-delimited line — sync plugins answer with
    # exactly one JSON-RPC response per request.
    first_line = stdout.split(b"\n", 1)[0]
    try:
        output = parse_response_line(first_line, elapsed)
    except Exception as exc:  # malformed plugin output
        return _error_invocation(
            "plugin_bad_response",
            f"plugin {manifest.name!r} returned an undecodable line: {exc}",
            elapsed,
        )

    if output.kind == "error":
        return ToolInvocation(
            content=json.dumps(
                {
                    "error": "plugin_error",
                    "code": output.code,
                    "message": output.message,
                }
            ),
            is_error=True,
            duration_ms=output.duration_ms,
        )
    if output.kind == "accepted_for_later":
        # Async-style ``task_id`` from a plugin the registry classified
        # as sync — surface it verbatim so the model can poll, but it is
        # not an error.
        return ToolInvocation(
            content=json.dumps({"status": "accepted", "task_id": output.task_id}),
            is_error=False,
            duration_ms=output.duration_ms,
        )

    # Success — ``content`` is the JSON-RPC ``result`` payload bytes.
    body = output.content or b"null"
    return ToolInvocation(
        content=body.decode("utf-8", errors="replace"),
        is_error=False,
        duration_ms=output.duration_ms,
    )


def _child_env(extra: dict[str, str]) -> dict[str, str]:
    """Build the child process environment: inherit the gateway's env,
    then layer the manifest's ``entry_point.env`` on top."""
    env = os.environ.copy()
    env.update(extra)
    return env


class _SuppressProcessErrors:
    """Tiny ctx mgr swallowing :class:`ProcessLookupError` / :class:`OSError`
    from killing / reaping an already-dead child."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, _t: object, exc: BaseException | None, _tb: object) -> bool:
        return isinstance(exc, (ProcessLookupError, OSError))


# ---------------------------------------------------------------------------
# P16 — service plugins (long-lived gRPC, via the PluginSupervisor).
# ---------------------------------------------------------------------------


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
        return error.DESCRIPTOR.fields_by_name["reason"].enum_type.values_by_number[
            int(reason)
        ].name
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


# ---------------------------------------------------------------------------
# P14/P16 — mcp plugins (routed through the MCP client bridge).
# ---------------------------------------------------------------------------


class McpToolBridge:
    """Adapts a :class:`corlinman_mcp_server.McpClientManager` onto the
    plugin-invoker contract.

    An ``mcp``-kind manifest names the external MCP *server* (the
    plugin's ``[mcp]`` table / manifest name maps to a configured MCP
    server) and the tool to call. This bridge resolves both against the
    manager's connected servers and runs a ``tools/call``.

    Never raises — a missing manager / unknown server / unreachable
    server folds into an ``is_error`` :class:`ToolInvocation`.
    """

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    @property
    def manager(self) -> Any:
        return self._manager

    async def dispatch(
        self,
        entry: Any,
        tool: str,
        args: Any,
    ) -> ToolInvocation:
        """Route an ``mcp`` plugin tool call through the MCP bridge."""
        if self._manager is None:
            return _error_invocation(
                "mcp_bridge_unavailable",
                "no MCP client manager is wired into the gateway",
            )

        manifest = entry.manifest
        # The manifest name is the corlinman-side plugin id; for an
        # ``mcp``-kind plugin it maps directly onto a configured
        # external MCP server (one manifest ⇄ one MCP server). An
        # optional ``[meta]`` ``mcp_server`` key can override the
        # target server name when the manifest id and the configured
        # server name diverge. The manager itself falls back to a bare
        # tool-name scan when the server name does not resolve.
        server = _mcp_server_name(manifest) or manifest.name
        outcome = await self._manager.call_tool(server, tool, args)
        return ToolInvocation(
            content=outcome.content,
            is_error=outcome.is_error,
        )


def _mcp_server_name(manifest: Any) -> str | None:
    """Optional external-server-name override from a manifest's
    ``[meta]`` table.

    The manifest ``[mcp]`` table has a fixed schema with no server-name
    field, so an override — used when the plugin id and the configured
    MCP server name differ — rides on the free-form ``[meta]`` table
    under the ``mcp_server`` key.
    """
    meta = getattr(manifest, "meta", None)
    if meta is None:
        return None
    # ``Meta`` is a pydantic model with ``extra="allow"``; the override
    # surfaces either as an attribute or in ``model_extra``.
    value = getattr(meta, "mcp_server", None)
    if value is None:
        extra = getattr(meta, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get("mcp_server")
    if isinstance(value, str) and value:
        return value
    return None


async def invoke_mcp_plugin(
    bridge: McpToolBridge,
    entry: Any,
    tool: str,
    args: Any,
) -> ToolInvocation:
    """Module-level convenience wrapper over
    :meth:`McpToolBridge.dispatch`."""
    return await bridge.dispatch(entry, tool, args)


# ---------------------------------------------------------------------------
# Invoker assembly.
# ---------------------------------------------------------------------------


def build_registry_invoker(
    registry: Any | None,
    *,
    supervisor: Any | None = None,
    mcp_manager: Any | None = None,
) -> Any:
    """Build a :data:`~corlinman_grpc.agent_client.PluginInvoker` bound to
    ``registry``.

    ``registry`` is a :class:`corlinman_providers.plugins.PluginRegistry`
    (or ``None``). The returned async callable has the
    ``(plugin, tool, args_json) -> ToolInvocation`` shape the
    :class:`~corlinman_grpc.agent_client.RegistryToolExecutor` expects.

    Optional wiring
    ---------------

    * ``supervisor`` — a :class:`corlinman_providers.plugins.\
PluginSupervisor`. When provided, ``service``-kind plugins are
      dispatched through a :class:`ServicePluginDispatcher` instead of
      degrading; absent, ``service`` calls return
      ``service_supervisor_unavailable``.
    * ``mcp_manager`` — a :class:`corlinman_mcp_server.McpClientManager`.
      When provided, ``mcp``-kind plugins route through an
      :class:`McpToolBridge`; absent, ``mcp`` calls return
      ``mcp_bridge_unavailable``.

    Degradation
    -----------

    * ``registry is None`` → every call returns a
      ``plugin_registry_unavailable`` error invocation.
    * plugin name not in the registry → ``plugin_not_found``.
    * tool not advertised by the plugin's manifest → ``tool_not_found``.
    * a ``service`` / ``mcp`` plugin with no supervisor / manager wired
      → a clear, non-crashing ``*_unavailable`` error.

    None of these raise; the executor would catch it anyway, but
    returning a structured result keeps the model's next round useful.
    """
    service_dispatcher = (
        ServicePluginDispatcher(supervisor) if supervisor is not None else None
    )
    mcp_bridge = McpToolBridge(mcp_manager) if mcp_manager is not None else None

    async def _invoke(plugin: str, tool: str, args_json: bytes) -> ToolInvocation:
        if registry is None:
            return _error_invocation(
                "plugin_registry_unavailable",
                "no plugin registry is wired into the gateway",
            )

        entry = registry.get(plugin)
        if entry is None:
            # OpenAI tool calls collapse plugin == tool == function.name
            # (see ReasoningLoop._finalise_tool_call), so the agent often
            # sends the *tool* name as the plugin. Fall back to a scan of
            # every registered plugin's advertised tools.
            entry = _resolve_by_tool(registry, tool if tool else plugin)
        if entry is None:
            return _error_invocation(
                "plugin_not_found",
                f"no registered plugin or tool named {plugin!r}",
            )

        manifest = entry.manifest
        tool_name = tool or plugin
        advertised = {t.name for t in manifest.capabilities.tools}
        if advertised and tool_name not in advertised:
            return _error_invocation(
                "tool_not_found",
                (
                    f"plugin {manifest.name!r} does not advertise a tool "
                    f"named {tool_name!r}; known: {sorted(advertised)}"
                ),
            )

        try:
            args = _decode_args(args_json)
        except ValueError as exc:
            return _error_invocation("bad_tool_arguments", str(exc))

        plugin_type = str(getattr(manifest.plugin_type, "value", manifest.plugin_type))
        timeout_ms = manifest.communication.timeout_ms or DEFAULT_TOOL_TIMEOUT_MS

        if plugin_type in ("sync", "async"):
            log.debug(
                "plugin_invoker.dispatch",
                plugin=manifest.name,
                tool=tool_name,
                kind=plugin_type,
                timeout_ms=timeout_ms,
            )
            return await invoke_sync_plugin(
                entry, tool_name, args, timeout_ms=timeout_ms
            )

        if plugin_type == "service":
            if service_dispatcher is None:
                return _error_invocation(
                    "service_supervisor_unavailable",
                    (
                        f"plugin {manifest.name!r} is a 'service' plugin but "
                        "no plugin supervisor is wired into the gateway"
                    ),
                )
            log.debug(
                "plugin_invoker.dispatch",
                plugin=manifest.name,
                tool=tool_name,
                kind="service",
                timeout_ms=timeout_ms,
            )
            return await invoke_service_plugin(
                service_dispatcher, entry, tool_name, args, timeout_ms=timeout_ms
            )

        if plugin_type == "mcp":
            if mcp_bridge is None:
                return _error_invocation(
                    "mcp_bridge_unavailable",
                    (
                        f"plugin {manifest.name!r} is an 'mcp' plugin but no "
                        "MCP client manager is wired into the gateway"
                    ),
                )
            log.debug(
                "plugin_invoker.dispatch",
                plugin=manifest.name,
                tool=tool_name,
                kind="mcp",
            )
            return await invoke_mcp_plugin(mcp_bridge, entry, tool_name, args)

        return _error_invocation(
            "unsupported_plugin_type",
            (
                f"plugin {manifest.name!r} has unknown plugin_type "
                f"{plugin_type!r}"
            ),
        )

    return _invoke


def _resolve_by_tool(registry: Any, tool_name: str) -> Any | None:
    """Find the plugin whose manifest advertises ``tool_name``.

    The first match in the registry's alphabetical listing wins — tool
    names are expected to be unique across plugins; a collision is a
    manifest-authoring bug the registry's diagnostics already flag.
    """
    for entry in registry.list():
        for tool in entry.manifest.capabilities.tools:
            if tool.name == tool_name:
                return entry
    return None
