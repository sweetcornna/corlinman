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

from typing import Any

import structlog
from corlinman_grpc.agent_client import ToolInvocation

# Behaviour-preserving god-file split: the sync / service / mcp dispatch
# groups were extracted to private siblings. Re-export every name the
# public surface (``grpc.__init__``) and external importers depend on so
# nothing outside this directory needs editing.
from corlinman_server.gateway.grpc._mcp_plugin_bridge import (
    McpToolBridge,
    invoke_mcp_plugin,
)
from corlinman_server.gateway.grpc._service_plugin_dispatcher import (
    ServicePluginDispatcher,
    _error_invocation,
    invoke_service_plugin,
)
from corlinman_server.gateway.grpc._sync_plugin_invoker import (
    _decode_args,
    invoke_sync_plugin,
)

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
