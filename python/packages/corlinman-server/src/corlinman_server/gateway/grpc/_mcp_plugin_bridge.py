"""MCP-plugin dispatch — extracted from
:mod:`corlinman_server.gateway.grpc.plugin_invoker`.

P14/P16 — ``mcp``-kind plugins routed through the
:class:`corlinman_mcp_server.McpClientManager`. MUST NOT import the
source module (no cycle); the source re-imports :class:`McpToolBridge`
and :func:`invoke_mcp_plugin` from here. The shared
:func:`_error_invocation` helper is imported from the service-dispatcher
sibling.
"""

from __future__ import annotations

from typing import Any

from corlinman_grpc.agent_client import ToolInvocation

from corlinman_server.gateway.grpc._service_plugin_dispatcher import _error_invocation

__all__ = ["McpToolBridge", "invoke_mcp_plugin"]


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
