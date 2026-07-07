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

import contextlib
import json
import re
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

DiscoveredTools = dict[str, list[Any]]

# OpenAI function names must match this charset; a dot/space/unicode in an
# advertised name is rejected upstream and can fail every chat turn.
_VALID_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


def namespaced_tool_name(server: str, tool_name: str) -> str:
    """The model-facing name for an MCP tool: ``{server}_{tool}``.

    Namespacing keeps tools from different servers distinct (so a same-named
    tool on two servers is not silently dropped) and stops an MCP tool from
    shadowing a bare builtin (e.g. ``calculator``). Execution strips the
    ``{server}_`` prefix back to the bare tool the MCP server knows (guarded by
    ``has_tool`` in :class:`McpToolBridge`).
    """
    return f"{server}_{tool_name}"


def _advertisable_tools(server: str, tools: list[Any] | None) -> list[tuple[str, Any]]:
    """``(advertised_name, tool)`` pairs for one server's discovered tools.

    The single place the advertisement guards live, so ``tools_json`` and the
    synthesized registry entries can never drift apart:

    * an empty tool name is dropped;
    * an advertised (namespaced) name outside the OpenAI function-name
      charset (:data:`_VALID_TOOL_NAME`) is dropped — it would fail every
      chat turn upstream;
    * a namespaced name that collides with another *literal* tool on the
      same server is dropped — the dispatch bridge prefers the literal
      (``_strip_server_namespace`` refuses to strip when the namespaced
      form is real), so advertising the namespaced form would execute the
      wrong tool against a mismatched schema.
    """
    literals = {str(getattr(t, "name", "") or "") for t in tools or []}
    out: list[tuple[str, Any]] = []
    for tool in tools or []:
        bare = str(getattr(tool, "name", "") or "")
        if not bare:
            continue
        name = namespaced_tool_name(server, bare)
        if not _VALID_TOOL_NAME.fullmatch(name):
            logger.warning(
                "gateway.mcp.tool_name_invalid",
                server=server,
                tool=bare,
                advertised=name,
            )
            continue
        if name in literals:
            logger.warning(
                "gateway.mcp.tool_name_shadowed_by_literal",
                server=server,
                tool=bare,
                advertised=name,
            )
            continue
        out.append((name, tool))
    return out


def _tool_openai_schema(server: str, tool: Any) -> dict[str, Any]:
    """One discovered MCP tool (``ToolDescriptor``-shaped) -> OpenAI function."""
    params = getattr(tool, "input_schema", None)
    if not isinstance(params, dict):
        # MCP ``inputSchema`` is always a JSON-object schema; degrade defensively
        # so a malformed advertisement can never emit an invalid tool entry.
        params = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": namespaced_tool_name(server, str(getattr(tool, "name", "") or "")),
            "description": str(getattr(tool, "description", "") or ""),
            "parameters": params,
        },
    }


def discovered_openai_schemas(
    discovered: DiscoveredTools | None,
    *,
    existing_names: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """OpenAI function schemas for every discovered MCP tool.

    Tool names are namespaced ``{server}_{tool}`` (see
    :func:`namespaced_tool_name`), so cross-server collisions cannot occur;
    de-dup is retained only as a defensive guard. ``existing_names`` mirrors
    the :func:`build_mcp_registry_entries` skip: a server that gets no
    synthesized routing entry must not be advertised either, or the model
    sees names that die with ``plugin_not_found``.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for server, tools in (discovered or {}).items():
        if not server or server in existing_names:
            continue
        for name, tool in _advertisable_tools(server, tools):
            if name in seen:
                continue
            seen.add(name)
            out.append(_tool_openai_schema(server, tool))
    return out


def mcp_advertised_tools_json(
    discovered: DiscoveredTools | None,
    *,
    existing_names: frozenset[str] = frozenset(),
) -> bytes:
    """Serialize discovered MCP tools as a ``tools_json`` array (``b""`` if none)."""
    schemas = discovered_openai_schemas(discovered, existing_names=existing_names)
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
        for name, tool in _advertisable_tools(server, tools):
            params = getattr(tool, "input_schema", None)
            manifest_tools.append(
                Tool(
                    # Namespaced so it matches the advertised name the model
                    # calls; the bridge strips the ``{server}_`` prefix back to
                    # the bare tool for ``call_tool``.
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


#: The synthetic per-server resource-reader tool name (bare form; advertised
#: as ``{server}_read_resource``). Only synthesized when the server does NOT
#: itself expose a literal ``read_resource`` tool — the literal always wins,
#: matching the dispatch bridge's preference order.
RESOURCE_READ_TOOL = "read_resource"

#: How many resources the synthetic tool's description enumerates before
#: eliding — keeps a resource-heavy server from bloating every chat turn.
_RESOURCE_DESC_CAP = 10


def _resource_read_tool(resources: list[Any]) -> Any:
    """Synthesize the ``read_resource`` ToolDescriptor-shaped object for one
    server's discovered resources (Dim 5 client-resources gap)."""
    from types import SimpleNamespace  # noqa: PLC0415

    lines: list[str] = []
    for r in resources[:_RESOURCE_DESC_CAP]:
        uri = str(getattr(r, "uri", "") or "")
        label = str(getattr(r, "name", "") or "")
        desc = str(getattr(r, "description", "") or "").strip()
        note = " — ".join(x for x in (label, desc.splitlines()[0] if desc else "") if x)
        lines.append(f"  {uri}" + (f" ({note})" if note else ""))
    more = len(resources) - _RESOURCE_DESC_CAP
    if more > 0:
        lines.append(f"  … and {more} more")
    return SimpleNamespace(
        name=RESOURCE_READ_TOOL,
        description=(
            "Read a resource from this MCP server by URI. Available:\n"
            + "\n".join(lines)
        ),
        input_schema={
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "URI of the resource to read",
                }
            },
            "required": ["uri"],
        },
    )


def with_resource_tools(
    discovered: DiscoveredTools | None,
    resources: dict[str, list[Any]] | None,
) -> DiscoveredTools:
    """Merge a synthetic ``read_resource`` tool into each server's tool list.

    A server already exposing a literal ``read_resource`` tool keeps it
    untouched (the synthetic is skipped — same literal-wins rule the
    dispatch bridge applies). A resources-only server (no tools at all)
    gains an entry so its resources are still reachable.
    """
    out: DiscoveredTools = {s: list(ts) for s, ts in (discovered or {}).items()}
    for server, res_list in (resources or {}).items():
        if not server or not res_list:
            continue
        tools = out.setdefault(server, [])
        if any(
            str(getattr(t, "name", "") or "") == RESOURCE_READ_TOOL
            for t in tools
        ):
            continue
        tools.append(_resource_read_tool(list(res_list)))
    return out


def filter_servers_by_policy(
    discovered: DiscoveredTools | None,
    *,
    allowed: frozenset[str] | None = None,
    denied: frozenset[str] = frozenset(),
) -> DiscoveredTools:
    """Apply a server allow/deny policy to the discovered map.

    ``denied`` wins over ``allowed`` (deny-by-name is absolute). When
    ``allowed`` is a non-empty set, only listed servers survive; ``None`` means
    "no allow-list — everything not denied". Mirrors claude-code's
    ``deniedMcpServers`` / ``allowedMcpServers``.
    """
    out: DiscoveredTools = {}
    for server, tools in (discovered or {}).items():
        if not server or server in denied:
            continue
        if allowed is not None and server not in allowed:
            continue
        out[server] = tools
    return out


async def register_mcp_tools(
    registry: Any | None,
    discovered: DiscoveredTools | None,
    *,
    allowed: frozenset[str] | None = None,
    denied: frozenset[str] = frozenset(),
    resources: dict[str, list[Any]] | None = None,
) -> tuple[int, bytes, frozenset[str]]:
    """Wire discovered MCP tools into the agent tool plane.

    Applies the server allow/deny policy, then upserts one synthesized
    ``mcp``-kind entry per surviving ready server into ``registry`` (execution
    routing) and returns ``(entries_added, advertised_tools_json,
    advertised_servers)``:

    * ``advertised_tools_json`` — the ``tools_json`` bytes the gateway injects
      into ``ChatStart.tools_json``;
    * ``advertised_servers`` — the set of server names that ACTUALLY produced a
      routing entry this call (a server whose tool list is empty/all-invalid or
      whose name collides with a real manifest produces none). This is the
      ground truth a refresh diffs against to prune servers that dropped out
      (Codex #110) — it must NOT be inferred from the full registry, which
      still holds not-yet-pruned stale entries from a prior advertise.

    ``resources`` (Dim 5 client-resources): per-server discovered resources —
    each contributing server gains a synthetic ``{server}_read_resource``
    tool (advertised + routed like any other; the bridge maps it to
    ``resources/read``). The same allow/deny policy applies.

    No-op-safe on a ``None`` registry (bytes still returned). Never clobbers a
    real on-disk manifest of the same name.
    """
    discovered = filter_servers_by_policy(discovered, allowed=allowed, denied=denied)
    if resources:
        discovered = with_resource_tools(
            discovered,
            filter_servers_by_policy(resources, allowed=allowed, denied=denied),
        )
    if registry is None:
        return 0, mcp_advertised_tools_json(discovered), frozenset(discovered)
    # The existing-name skip must block only REAL on-disk manifests, not
    # our own synthesized ``<mcp:...>`` entries — otherwise a re-advertise
    # (list_changed / hot-plug) sees the boot-time synthetic entries as
    # collisions and skips rebuilding them, dropping a live server's tools
    # from the model-facing schema (Codex #110).
    existing = frozenset(
        e.manifest.name
        for e in registry.list()
        if not _is_synthetic_mcp_entry(e)
    )
    tools_json = mcp_advertised_tools_json(discovered, existing_names=existing)
    entries = build_mcp_registry_entries(discovered, existing_names=existing)
    for entry in entries:
        await registry.upsert(entry)
    advertised = frozenset(e.manifest.name for e in entries)
    return len(entries), tools_json, advertised


def _is_synthetic_mcp_entry(entry: Any) -> bool:
    """Whether ``entry`` is one this module synthesized (``<mcp:...>`` path)."""
    return str(getattr(entry, "manifest_path", "") or "").startswith(_MCP_SYNTH_PREFIX)


#: Prefix of the synthetic ``manifest_path`` stamped on a synthesized
#: ``mcp``-kind entry (``<mcp:{server}>``). Lets a refresh identify which
#: registry entries this module owns vs a real on-disk manifest.
_MCP_SYNTH_PREFIX = "<mcp:"


async def prune_stale_mcp_entries(
    registry: Any | None, live_servers: frozenset[str]
) -> int:
    """Remove synthesized ``mcp`` entries for servers no longer advertised.

    ``register_mcp_tools`` only upserts — so a disabled/removed server (or
    one that fell outside the allow/deny policy) leaves its synthesized
    routing entry behind, and the model keeps seeing dead tools until
    restart. A refresh calls this with the set of servers that ARE now
    advertised; every synthesized entry (identified by its ``<mcp:...>``
    manifest path) for a server outside that set is removed. Real on-disk
    manifests (any other path) are never touched. Returns the count
    removed. No-op-safe on a ``None`` registry.
    """
    if registry is None:
        return 0
    removed = 0
    for entry in list(registry.list()):
        path = str(getattr(entry, "manifest_path", "") or "")
        if not path.startswith(_MCP_SYNTH_PREFIX):
            continue
        server = path[len(_MCP_SYNTH_PREFIX) : -1] if path.endswith(">") else path[len(_MCP_SYNTH_PREFIX) :]
        if server not in live_servers:
            with contextlib.suppress(Exception):
                await registry.remove(entry.manifest.name)
                removed += 1
    return removed


__all__ = [
    "build_mcp_registry_entries",
    "discovered_openai_schemas",
    "filter_servers_by_policy",
    "mcp_advertised_tools_json",
    "namespaced_tool_name",
    "prune_stale_mcp_entries",
    "register_mcp_tools",
]
