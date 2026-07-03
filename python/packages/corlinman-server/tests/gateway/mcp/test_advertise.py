"""Advertise + route discovered external-MCP tools into the agent tool plane.

Covers the pure converters (discovered tools -> namespaced OpenAI schemas /
tools_json), the server allow/deny policy, and the load-bearing integration: a
synthesized ``mcp``-kind registry entry makes a namespaced tool call route
through the real ``build_registry_invoker`` -> ``mcp`` branch -> ``McpToolBridge``
(which strips the ``{server}_`` prefix) with no new dispatch code.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from corlinman_server.gateway.mcp.advertise import (
    build_mcp_registry_entries,
    discovered_openai_schemas,
    filter_servers_by_policy,
    mcp_advertised_tools_json,
    namespaced_tool_name,
)


class _FakeTool:
    """``ToolDescriptor``-shaped stand-in (name / description / input_schema)."""

    def __init__(self, name: str, description: str = "", input_schema: Any = None) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema if input_schema is not None else {"type": "object"}


_ECHO_SCHEMA = {"type": "object", "properties": {"msg": {"type": "string"}}}


def test_discovered_openai_schemas_namespaced_shape() -> None:
    schemas = discovered_openai_schemas({"srv": [_FakeTool("echo", "Echo back", _ECHO_SCHEMA)]})
    assert schemas == [
        {
            "type": "function",
            "function": {
                "name": "srv_echo",  # namespaced {server}_{tool}
                "description": "Echo back",
                "parameters": _ECHO_SCHEMA,
            },
        }
    ]


def test_cross_server_same_tool_not_dropped() -> None:
    """Namespacing keeps a same-named tool on two servers distinct — the old
    bare-name first-wins dedup silently dropped the second."""
    schemas = discovered_openai_schemas(
        {
            "a": [_FakeTool("dup", "from a"), _FakeTool("only_a")],
            "b": [_FakeTool("dup", "from b")],
        }
    )
    names = sorted(s["function"]["name"] for s in schemas)
    assert names == ["a_dup", "a_only_a", "b_dup"]  # both dups survive


def test_bad_input_schema_degrades_not_crashes() -> None:
    schemas = discovered_openai_schemas({"srv": [_FakeTool("t", input_schema="not-a-dict")]})
    assert schemas[0]["function"]["name"] == "srv_t"
    assert schemas[0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_tools_json_empty_when_no_tools() -> None:
    assert mcp_advertised_tools_json({}) == b""
    assert mcp_advertised_tools_json(None) == b""
    assert mcp_advertised_tools_json({"srv": []}) == b""


def test_tools_json_roundtrips() -> None:
    raw = mcp_advertised_tools_json({"srv": [_FakeTool("echo", "e", _ECHO_SCHEMA)]})
    assert json.loads(raw)[0]["function"]["name"] == "srv_echo"


def test_filter_servers_by_policy() -> None:
    disc = {"a": [_FakeTool("t")], "b": [_FakeTool("t")], "c": [_FakeTool("t")]}
    # deny wins
    assert set(filter_servers_by_policy(disc, denied=frozenset({"b"}))) == {"a", "c"}
    # non-empty allow-list is exclusive
    assert set(filter_servers_by_policy(disc, allowed=frozenset({"a", "b"}))) == {"a", "b"}
    # deny overrides allow
    assert set(
        filter_servers_by_policy(disc, allowed=frozenset({"a", "b"}), denied=frozenset({"b"}))
    ) == {"a"}
    # None allow-list = everything not denied
    assert set(filter_servers_by_policy(disc)) == {"a", "b", "c"}


def test_invalid_charset_names_never_advertised_or_routed() -> None:
    """Bug 2: an advertised name outside the OpenAI function-name charset
    (``^[a-zA-Z0-9_-]+$``) fails every chat turn upstream — a tool (or server)
    name with a dot/space/unicode must be skipped from BOTH the tools_json
    advertisement AND the synthesized registry entries."""
    discovered = {
        "srv": [_FakeTool("bad.name"), _FakeTool("ok", "OK", _ECHO_SCHEMA)],
        "my server": [_FakeTool("echo")],  # space in the server name
    }
    names = [s["function"]["name"] for s in json.loads(mcp_advertised_tools_json(discovered))]
    assert names == ["srv_ok"]  # bad.name / "my server" tools dropped
    entries = build_mcp_registry_entries(discovered)
    assert [e.manifest.name for e in entries] == ["srv"]  # no all-invalid server entry
    (entry,) = entries
    assert [t.name for t in entry.manifest.capabilities.tools] == ["srv_ok"]


def test_namespaced_name_colliding_with_literal_tool_skipped() -> None:
    """Bug 4: on server ``srv`` exposing both ``echo`` and a literal
    ``srv_echo``, the namespaced form of ``echo`` IS ``srv_echo`` — at
    dispatch the bridge prefers the literal (``_strip_server_namespace``
    refuses to strip), so advertising it would run the literal ``srv_echo``
    against ``echo``'s schema. Skip the ambiguous name by construction: only
    the literal survives, advertised as ``srv_srv_echo``."""
    discovered = {"srv": [_FakeTool("echo", "Echo", _ECHO_SCHEMA), _FakeTool("srv_echo")]}
    names = [s["function"]["name"] for s in json.loads(mcp_advertised_tools_json(discovered))]
    assert names == ["srv_srv_echo"]  # the literal, namespaced
    assert "srv_echo" not in names  # the ambiguous name never reaches the model
    # The synthesized entry stays consistent with the advertisement.
    (entry,) = build_mcp_registry_entries(discovered)
    assert [t.name for t in entry.manifest.capabilities.tools] == ["srv_srv_echo"]


def test_build_entries_skips_empty_and_existing() -> None:
    discovered = {
        "": [_FakeTool("x")],  # empty server name → skip
        "no_tools": [],  # no tools → skip
        "real_manifest": [_FakeTool("y")],  # collides with an on-disk manifest
        "fresh": [_FakeTool("z", "Z", _ECHO_SCHEMA)],
    }
    entries = build_mcp_registry_entries(discovered, existing_names=frozenset({"real_manifest"}))
    assert [e.manifest.name for e in entries] == ["fresh"]
    (entry,) = entries
    assert entry.manifest.plugin_type.value == "mcp"
    assert [t.name for t in entry.manifest.capabilities.tools] == ["fresh_z"]  # namespaced
    entry.manifest.validate_all()  # a synthesized MCP manifest is valid


async def test_namespaced_call_routes_and_strips_prefix() -> None:
    """The core proof: a namespaced tool call routes through the real invoker to
    ``McpClientManager.call_tool`` with the bridge stripping ``{server}_`` back
    to the bare tool — no new dispatch code."""
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
        def has_tool(self, server: str, tool: str) -> bool:
            return server == "echo-server" and tool == "echo"

        async def call_tool(self, server: str, tool: str, args: Any) -> _Outcome:
            calls.append((server, tool, args))
            return _Outcome()

    invoker = build_registry_invoker(registry, mcp_manager=_FakeManager())
    # The model calls the advertised namespaced name (plugin == tool).
    result = await invoker("echo-server_echo", "echo-server_echo", b'{"msg": "hi"}')

    # Bridge stripped the prefix → the MCP server sees the bare tool.
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
    assert json.loads(tools_json)[0]["function"]["name"] == "srv_echo"


async def test_register_mcp_tools_applies_deny_policy() -> None:
    from corlinman_providers.plugins.registry import PluginRegistry
    from corlinman_server.gateway.mcp.advertise import register_mcp_tools

    registry = PluginRegistry.from_roots([])
    added, tools_json = await register_mcp_tools(
        registry,
        {"good": [_FakeTool("echo")], "blocked": [_FakeTool("rm")]},
        denied=frozenset({"blocked"}),
    )
    assert added == 1
    assert registry.get("good") is not None and registry.get("blocked") is None
    names = [s["function"]["name"] for s in json.loads(tools_json)]
    assert names == ["good_echo"]  # blocked server's tools never advertised


async def test_register_mcp_tools_colliding_server_not_advertised() -> None:
    """Bug 3: a server whose name already exists in the registry is never
    upserted (never clobber a real manifest) — advertising its tools anyway
    would hand the model names with no routing entry (``plugin_not_found`` on
    every call). The advertisement must honor the same skip."""
    from corlinman_providers.plugins.registry import PluginRegistry
    from corlinman_server.gateway.mcp.advertise import register_mcp_tools

    registry = PluginRegistry.from_roots([])
    # A pre-existing entry already occupies the name "taken".
    for entry in build_mcp_registry_entries({"taken": [_FakeTool("real")]}):
        await registry.upsert(entry)

    added, tools_json = await register_mcp_tools(
        registry, {"taken": [_FakeTool("ghost")], "fresh": [_FakeTool("echo")]}
    )
    assert added == 1  # only "fresh" got a routing entry…
    names = [s["function"]["name"] for s in json.loads(tools_json)]
    assert names == ["fresh_echo"]  # …so only its tools are advertised


async def test_register_mcp_tools_none_registry_still_advertises() -> None:
    from corlinman_server.gateway.mcp.advertise import register_mcp_tools

    added, tools_json = await register_mcp_tools(
        None, {"srv": [_FakeTool("echo", "e", _ECHO_SCHEMA)]}
    )
    assert added == 0  # nothing upserted…
    assert json.loads(tools_json)[0]["function"]["name"] == "srv_echo"  # …still advertised


async def test_unknown_tool_still_unresolved_without_entry() -> None:
    """Guard: without a synthesized entry, an MCP tool name does not route
    (proves the entry is what enables execution, not an accidental catch-all)."""
    from corlinman_providers.plugins.registry import PluginRegistry
    from corlinman_server.gateway.grpc.plugin_invoker import build_registry_invoker

    registry = PluginRegistry.from_roots([])

    class _FakeManager:
        async def call_tool(self, server: str, tool: str, args: Any) -> Any:
            raise AssertionError("must not be called without a registry entry")

    invoker = build_registry_invoker(registry, mcp_manager=_FakeManager())
    result = await invoker("srv_echo", "srv_echo", b"{}")
    assert getattr(result, "is_error", None) is True


def test_namespaced_tool_name_helper() -> None:
    assert namespaced_tool_name("srv", "echo") == "srv_echo"
