"""Dim 5 client-resources — advertisement + routing of the synthetic
``{server}_read_resource`` tool.

Covers: the ``with_resource_tools`` merge (incl. literal-wins), the
``register_mcp_tools(resources=…)`` end-to-end schema shape, the policy
filter applying to resources too, and the load-bearing dispatch proof —
an advertised ``{server}_read_resource`` call routes through the real
invoker → ``McpToolBridge`` → ``manager.read_resource`` (never
``tools/call``), while a server with a LITERAL ``read_resource`` tool
keeps the normal tools/call path.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from corlinman_server.gateway.mcp.advertise import (
    RESOURCE_READ_TOOL,
    build_mcp_registry_entries,
    register_mcp_tools,
    with_resource_tools,
)

pytestmark = pytest.mark.asyncio


class _FakeTool:
    def __init__(self, name: str, description: str = "", input_schema: Any = None) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema if input_schema is not None else {"type": "object"}


class _FakeResource:
    def __init__(self, uri: str, name: str = "", description: str = "") -> None:
        self.uri = uri
        self.name = name
        self.description = description


async def test_with_resource_tools_merges_synthetic() -> None:
    merged = with_resource_tools(
        {"srv": [_FakeTool("echo")]},
        {"srv": [_FakeResource("corl://a", "A doc", "the a doc")]},
    )
    names = [t.name for t in merged["srv"]]
    assert names == ["echo", RESOURCE_READ_TOOL]
    synth = merged["srv"][1]
    assert "corl://a" in synth.description
    assert synth.input_schema["required"] == ["uri"]


async def test_with_resource_tools_literal_wins() -> None:
    literal = _FakeTool(RESOURCE_READ_TOOL, "the real one")
    merged = with_resource_tools(
        {"srv": [literal]}, {"srv": [_FakeResource("corl://a")]}
    )
    assert merged["srv"] == [literal]  # synthetic skipped


async def test_with_resource_tools_resources_only_server() -> None:
    merged = with_resource_tools({}, {"docs": [_FakeResource("corl://a")]})
    assert [t.name for t in merged["docs"]] == [RESOURCE_READ_TOOL]


async def test_register_with_resources_advertises_read_resource() -> None:
    _n, tools_json, advertised = await register_mcp_tools(
        None,
        {"srv": [_FakeTool("echo")]},
        resources={"srv": [_FakeResource("corl://a")]},
    )
    names = [t["function"]["name"] for t in json.loads(tools_json)]
    assert names == ["srv_echo", "srv_read_resource"]
    assert advertised == frozenset({"srv"})


async def test_register_policy_applies_to_resources_too() -> None:
    _n, tools_json, _adv = await register_mcp_tools(
        None,
        {"ok": [_FakeTool("t")]},
        denied=frozenset({"blocked"}),
        resources={"blocked": [_FakeResource("corl://secret")]},
    )
    names = [t["function"]["name"] for t in json.loads(tools_json)]
    assert names == ["ok_t"]


async def test_read_resource_call_routes_to_resources_read() -> None:
    """Dispatch proof: the synthetic namespaced name reaches
    ``manager.read_resource`` — never ``tools/call``."""
    from corlinman_providers.plugins.registry import PluginRegistry
    from corlinman_server.gateway.grpc.plugin_invoker import (
        build_registry_invoker,
    )

    merged = with_resource_tools(
        {"docs": []}, {"docs": [_FakeResource("corl://a")]}
    )
    registry = PluginRegistry.from_roots([])
    for entry in build_mcp_registry_entries(merged):
        await registry.upsert(entry)

    reads: list[tuple[str, str]] = []

    class _Outcome:
        content = "resource text"
        is_error = False

    class _FakeManager:
        def has_tool(self, server: str, tool: str) -> bool:
            return False  # no literal read_resource on the server

        async def read_resource(self, server: str, uri: str) -> _Outcome:
            reads.append((server, uri))
            return _Outcome()

        async def call_tool(self, server: str, tool: str, args: Any) -> _Outcome:
            raise AssertionError("must not fall through to tools/call")

    invoker = build_registry_invoker(registry, mcp_manager=_FakeManager())
    result = await invoker(
        "docs_read_resource", "docs_read_resource", b'{"uri": "corl://a"}'
    )
    assert reads == [("docs", "corl://a")]
    assert getattr(result, "is_error", None) is False


async def test_literal_read_resource_tool_keeps_tools_call_path() -> None:
    """A server ACTUALLY exposing a ``read_resource`` tool keeps normal
    dispatch — the bridge must not hijack it into resources/read."""
    from corlinman_providers.plugins.registry import PluginRegistry
    from corlinman_server.gateway.grpc.plugin_invoker import (
        build_registry_invoker,
    )

    registry = PluginRegistry.from_roots([])
    for entry in build_mcp_registry_entries(
        {"srv": [_FakeTool(RESOURCE_READ_TOOL, "literal")]}
    ):
        await registry.upsert(entry)

    calls: list[tuple[str, str]] = []

    class _Outcome:
        content = "tool result"
        is_error = False

    class _FakeManager:
        def has_tool(self, server: str, tool: str) -> bool:
            return server == "srv" and tool == RESOURCE_READ_TOOL

        async def read_resource(self, server: str, uri: str) -> _Outcome:
            raise AssertionError("literal tool must not route to resources/read")

        async def call_tool(self, server: str, tool: str, args: Any) -> _Outcome:
            calls.append((server, tool))
            return _Outcome()

    invoker = build_registry_invoker(registry, mcp_manager=_FakeManager())
    result = await invoker(
        "srv_read_resource", "srv_read_resource", b'{"uri": "corl://a"}'
    )
    assert calls == [("srv", RESOURCE_READ_TOOL)]
    assert getattr(result, "is_error", None) is False
