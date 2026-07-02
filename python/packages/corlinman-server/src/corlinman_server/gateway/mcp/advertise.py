"""Surface external MCP-server tools into the agent tool plane (gateway-side).

External MCP servers are connected at gateway boot by :class:`McpClientManager`,
but their discovered tools were historically invisible to the model:
``McpClientManager.discovered_tools()`` had no production consumer, and nothing
mapped a bare tool name back onto a server for execution. This module closes
that seam, entirely in the **gateway** process — the only place the live
manager (and ``AppState.plugin_registry``) exist:

* :func:`discovered_openai_schemas` / :func:`mcp_advertised_tools_json` — convert
  discovered tools into OpenAI function schemas the gateway injects into
  ``ChatStart.tools_json`` so the agent servicer advertises them to the model.
* :func:`build_mcp_registry_entries` — synthesize one ``mcp``-kind
  :class:`PluginEntry` per ready server. The existing ``RegistryToolExecutor`` /
  ``build_registry_invoker`` ``mcp`` branch then routes a bare tool call to
  :class:`McpToolBridge` with **no new dispatch code** (``_resolve_by_tool``
  finds the entry by tool name; the bridge maps ``manifest.name`` -> server, and
  ``McpClientManager.call_tool`` itself falls back to a bare-name ``find_tool``).

Tool names are advertised bare (the agent collapses ``plugin == tool`` for
OpenAI function calls). Names are de-duplicated across servers (first ready
server wins); a synthesized entry never clobbers a real on-disk manifest of the
same name (see ``existing_names``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DiscoveredTools = dict[str, list[Any]]


def _tool_openai_schema(tool: Any) -> dict[str, Any]:
    """One discovered MCP tool (``ToolDescriptor``-shaped) -> OpenAI function."""
    params = getattr(tool, "input_schema", None)
    if not isinstance(params, dict):
        # MCP ``inputSchema`` is always a JSON-object schema; degrade defensively
        # so a malformed advertisement can never emit an invalid tool entry.
        params = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": str(getattr(tool, "name", "") or ""),
            "description": str(getattr(tool, "description", "") or ""),
            "parameters": params,
        },
    }


def discovered_openai_schemas(discovered: DiscoveredTools | None) -> list[dict[str, Any]]:
    """OpenAI function schemas for every discovered MCP tool.

    De-duplicated by tool name (first ready server wins) because the agent
    addresses a tool by bare name and execution resolves the server via
    ``find_tool``.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for _server, tools in (discovered or {}).items():
        for tool in tools or []:
            name = str(getattr(tool, "name", "") or "")
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(_tool_openai_schema(tool))
    return out


def mcp_advertised_tools_json(discovered: DiscoveredTools | None) -> bytes:
    """Serialize discovered MCP tools as a ``tools_json`` array (``b""`` if none)."""
    schemas = discovered_openai_schemas(discovered)
    if not schemas:
        return b""
    return json.dumps(schemas).encode("utf-8")


def build_mcp_registry_entries(
    discovered: DiscoveredTools | None,
    *,
    existing_names: frozenset[str] = frozenset(),
) -> list[Any]:
    """Synthesize one ``mcp``-kind ``PluginEntry`` per ready server.

    A server contributing no valid tools, an empty server name, or a name that
    already exists in the registry (``existing_names`` — never clobber a real
    on-disk manifest) is skipped. Import-lazy so importing this module stays
    cheap and layering-safe.
    """
    from corlinman_providers.plugins.discovery import Origin  # noqa: PLC0415
    from corlinman_providers.plugins.manifest import (  # noqa: PLC0415
        Capabilities,
        EntryPoint,
        McpConfig,
        PluginManifest,
        PluginType,
        Tool,
    )
    from corlinman_providers.plugins.registry import PluginEntry  # noqa: PLC0415

    entries: list[Any] = []
    for server, tools in (discovered or {}).items():
        if not server or server in existing_names:
            continue
        manifest_tools: list[Any] = []
        for tool in tools or []:
            name = str(getattr(tool, "name", "") or "")
            if not name:
                continue
            params = getattr(tool, "input_schema", None)
            manifest_tools.append(
                Tool(
                    name=name,
                    description=str(getattr(tool, "description", "") or ""),
                    parameters=params if isinstance(params, dict) else {"type": "object"},
                )
            )
        if not manifest_tools:
            continue
        manifest = PluginManifest(
            manifest_version=3,  # MCP requires >= 3
            name=server,
            version="0.0.0",
            description=f"External MCP server '{server}' (synthesized).",
            plugin_type=PluginType.MCP,
            # entry_point is required but unused: this server is already
            # connected by McpClientManager, never launched by the plugin
            # supervisor (which does not iterate the registry).
            entry_point=EntryPoint(command="mcp-external"),
            capabilities=Capabilities(tools=manifest_tools),
            mcp=McpConfig(),
        )
        entries.append(
            PluginEntry(
                manifest=manifest,
                origin=Origin.CONFIG,
                manifest_path=Path(f"<mcp:{server}>"),
            )
        )
    return entries


async def register_mcp_tools(
    registry: Any | None,
    discovered: DiscoveredTools | None,
) -> tuple[int, bytes]:
    """Wire discovered MCP tools into the agent tool plane at gateway boot.

    Upserts one synthesized ``mcp``-kind entry per ready server into
    ``registry`` (execution routing) and returns
    ``(entries_added, advertised_tools_json)`` — the second being the
    ``tools_json`` bytes the gateway injects into ``ChatStart.tools_json``
    (advertisement). No-op-safe on a ``None`` registry (advertisement bytes are
    still returned). Never clobbers a real on-disk manifest of the same name.
    """
    tools_json = mcp_advertised_tools_json(discovered)
    if registry is None:
        return 0, tools_json
    existing = frozenset(e.manifest.name for e in registry.list())
    entries = build_mcp_registry_entries(discovered, existing_names=existing)
    for entry in entries:
        await registry.upsert(entry)
    return len(entries), tools_json


__all__ = [
    "build_mcp_registry_entries",
    "discovered_openai_schemas",
    "mcp_advertised_tools_json",
    "register_mcp_tools",
]
