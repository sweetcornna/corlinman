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
        target_tool = _strip_server_namespace(self._manager, server, tool)

        # Dim 5 client-resources — the advertise side synthesizes a
        # ``{server}_read_resource`` tool for servers exposing resources
        # but no literal tool of that name. Such a call must route to
        # ``resources/read``, not ``tools/call`` (the server has no such
        # tool). Literal-wins: when the server DOES advertise a real
        # ``read_resource`` tool, the normal tools/call path runs.
        bare = _bare_tool_name(server, target_tool)
        if (
            bare == "read_resource"
            and not _server_has_tool(self._manager, server, "read_resource")
        ):
            read = getattr(self._manager, "read_resource", None)
            if callable(read):
                uri = ""
                if isinstance(args, dict):
                    uri = str(args.get("uri") or "")
                outcome = await read(server, uri)
                return ToolInvocation(
                    content=outcome.content,
                    is_error=outcome.is_error,
                )

        outcome = await self._manager.call_tool(server, target_tool, args)
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


def _bare_tool_name(server: str, tool: str) -> str:
    """The tool name with a ``{server}_`` prefix unconditionally removed
    (for classification only — dispatch still uses the stripped-or-not
    name from :func:`_strip_server_namespace`)."""
    prefix = f"{server}_"
    return tool[len(prefix) :] if tool.startswith(prefix) else tool


def _server_has_tool(manager: Any, server: str, tool: str) -> bool:
    """Best-effort ``manager.has_tool`` probe — missing/raising → False."""
    has = getattr(manager, "has_tool", None)
    if not callable(has):
        return False
    try:
        return bool(has(server, tool))
    except Exception:  # noqa: BLE001 — classification must not break dispatch
        return False


def _strip_server_namespace(manager: Any, server: str, tool: str) -> str:
    """Map a namespaced ``{server}_{tool}`` call back to the bare tool name.

    The gateway advertises discovered MCP tools as ``{server}_{tool}`` (see
    ``gateway/mcp/advertise.namespaced_tool_name``) so cross-server names cannot
    collide; the MCP server itself only knows the bare tool. Strip the prefix,
    but only when the bare form is a real tool on the server AND the namespaced
    form is not — so a real on-disk manifest advertising a literal
    ``{server}_x`` tool is left untouched. ``has_tool`` missing/raising → no-op.

    Preferring the literal is safe by construction: the advertise side skips a
    namespaced name that collides with a literal tool on the same server (see
    ``advertise._advertisable_tools``), so an advertised name arriving here is
    never ambiguous — when both ``x`` and a literal ``{server}_x`` exist, only
    the literal was advertised and it correctly resolves to itself.
    """
    prefix = f"{server}_"
    if not tool.startswith(prefix):
        return tool
    has = getattr(manager, "has_tool", None)
    if not callable(has):
        return tool
    bare = tool[len(prefix) :]
    try:
        if bare and has(server, bare) and not has(server, tool):
            return bare
    except Exception:  # noqa: BLE001 — resolution must never break dispatch
        return tool
    return tool


async def invoke_mcp_plugin(
    bridge: McpToolBridge,
    entry: Any,
    tool: str,
    args: Any,
) -> ToolInvocation:
    """Module-level convenience wrapper over
    :meth:`McpToolBridge.dispatch`."""
    return await bridge.dispatch(entry, tool, args)
