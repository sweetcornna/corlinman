"""Advertise + route discovered external-MCP tools into the agent tool plane.

Covers the pure converters (discovered tools -> OpenAI schemas / tools_json)
and the load-bearing integration: a synthesized ``mcp``-kind registry entry
makes a bare tool call route through the real ``build_registry_invoker`` ->
``mcp`` branch -> ``McpToolBridge`` with no new dispatch code.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from corlinman_server.gateway.mcp.advertise import (
    build_mcp_registry_entries,
    discovered_openai_schemas,
    mcp_advertised_tools_json,
)


class _FakeTool:
    """``ToolDescriptor``-shaped stand-in (name / description / input_schema)."""

    def __init__(self, name: str, description: str = "", input_schema: Any = None) -> None:
        self.name = name
        self.description = description
        self.input_schema = (
            input_schema if input_schema is not None else {"type": "object"}
        )


_ECHO_SCHEMA = {"type": "object", "properties": {"msg": {"type": "string"}}}


def test_discovered_openai_schemas_shape() -> None:
    schemas = discovered_openai_schemas(
        {"srv": [_FakeTool("echo", "Echo back", _ECHO_SCHEMA)]}
    )
    assert schemas == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo back",
                "parameters": _ECHO_SCHEMA,
            },
        }
    ]


def test_schemas_dedupe_by_name_first_server_wins() -> None:
    schemas = discovered_openai_schemas(
        {
            "a": [_FakeTool("dup", "from a"), _FakeTool("only_a")],
            "b": [_FakeTool("dup", "from b")],
        }
    )
    names = [s["function"]["name"] for s in schemas]
    assert names == ["dup", "only_a"]
    dup = next(s for s in schemas if s["function"]["name"] == "dup")
    assert dup["function"]["description"] == "from a"  # first server wins


def test_bad_input_schema_degrades_not_crashes() -> None:
    schemas = discovered_openai_schemas({"srv": [_FakeTool("t", input_schema="not-a-dict")]})
    assert schemas[0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_tools_json_empty_when_no_tools() -> None:
    assert mcp_advertised_tools_json({}) == b""
    assert mcp_advertised_tools_json(None) == b""
    assert mcp_advertised_tools_json({"srv": []}) == b""


def test_tools_json_roundtrips() -> None:
    raw = mcp_advertised_tools_json({"srv": [_FakeTool("echo", "e", _ECHO_SCHEMA)]})
    assert json.loads(raw)[0]["function"]["name"] == "echo"


def test_build_entries_skips_empty_and_existing() -> None:
    discovered = {
        "": [_FakeTool("x")],  # empty server name → skip
        "no_tools": [],  # no tools → skip
        "real_manifest": [_FakeTool("y")],  # collides with an on-disk manifest
        "fresh": [_FakeTool("z", "Z", _ECHO_SCHEMA)],
    }
    entries = build_mcp_registry_entries(
        discovered, existing_names=frozenset({"real_manifest"})
    )
    assert [e.manifest.name for e in entries] == ["fresh"]
    (entry,) = entries
    assert entry.manifest.plugin_type.value == "mcp"
    assert [t.name for t in entry.manifest.capabilities.tools] == ["z"]
    entry.manifest.validate_all()  # a synthesized MCP manifest is valid


async def test_synthesized_entry_routes_bare_tool_call_to_mcp_bridge() -> None:
    """The core proof: with a synthesized entry, the real invoker routes a bare
    tool call all the way to ``McpClientManager.call_tool`` — no dispatch code."""
    from corlinman_providers.plugins.registry import PluginRegistry
    from corlinman_server.gateway.grpc.plugin_invoker import build_registry_invoker

    discovered = {"echo-server": [_FakeTool("echo", "Echo", _ECHO_SCHEMA)]}
    registry = PluginRegistry.from_roots([])
    for entry in build_mcp_registry_entries(discovered):
        await registry.upsert(entry)

    calls: list[tuple[str, str, Any]] = []

    class _Outcome:
        content: ClassVar[list[dict[str, str]]] = [{"type": "text", "text": "pong"}]
        is_error: ClassVar[bool] = False

    class _FakeManager:
        async def call_tool(self, server: str, tool: str, args: Any) -> _Outcome:
            calls.append((server, tool, args))
            return _Outcome()

    invoker = build_registry_invoker(registry, mcp_manager=_FakeManager())
    # The model emits a bare function name; the agent collapses plugin == tool.
    result = await invoker("echo", "echo", b'{"msg": "hi"}')

    assert calls == [("echo-server", "echo", {"msg": "hi"})]
    assert getattr(result, "is_error", None) is False


async def test_register_mcp_tools_upserts_and_advertises() -> None:
    from corlinman_providers.plugins.registry import PluginRegistry
    from corlinman_server.gateway.mcp.advertise import register_mcp_tools

    registry = PluginRegistry.from_roots([])
    added, tools_json = await register_mcp_tools(
        registry, {"srv": [_FakeTool("echo", "e", _ECHO_SCHEMA)]}
    )
    assert added == 1
    assert registry.get("srv") is not None
    assert json.loads(tools_json)[0]["function"]["name"] == "echo"


async def test_register_mcp_tools_none_registry_still_advertises() -> None:
    from corlinman_server.gateway.mcp.advertise import register_mcp_tools

    added, tools_json = await register_mcp_tools(
        None, {"srv": [_FakeTool("echo", "e", _ECHO_SCHEMA)]}
    )
    assert added == 0  # nothing upserted…
    assert json.loads(tools_json)[0]["function"]["name"] == "echo"  # …but still advertised


async def test_unknown_tool_still_unresolved_without_entry() -> None:
    """Guard: without a synthesized entry, a bare MCP tool name does not route
    (proves the entry is what enables execution, not an accidental catch-all)."""
    from corlinman_providers.plugins.registry import PluginRegistry
    from corlinman_server.gateway.grpc.plugin_invoker import build_registry_invoker

    registry = PluginRegistry.from_roots([])

    class _FakeManager:
        async def call_tool(self, server: str, tool: str, args: Any) -> Any:
            raise AssertionError("must not be called without a registry entry")

    invoker = build_registry_invoker(registry, mcp_manager=_FakeManager())
    result = await invoker("echo", "echo", b"{}")
    assert getattr(result, "is_error", None) is True
