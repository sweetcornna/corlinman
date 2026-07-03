"""Dynamic MCP re-advertisement + stale-entry prune (Dim 5 / issue #108)."""

from __future__ import annotations

from typing import Any

import pytest
from corlinman_server.gateway.mcp.advertise import (
    build_mcp_registry_entries,
    prune_stale_mcp_entries,
)


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = ""
        self.input_schema = {"type": "object"}


async def _registry_with(*servers: str):
    from corlinman_providers.plugins.registry import PluginRegistry

    registry = PluginRegistry.from_roots([])
    discovered = {s: [_FakeTool("echo")] for s in servers}
    for entry in build_mcp_registry_entries(discovered):
        await registry.upsert(entry)
    return registry


def _mcp_entry_names(registry) -> set[str]:
    from corlinman_providers.plugins.manifest import PluginType

    return {
        e.manifest.name
        for e in registry.list()
        if getattr(e.manifest, "plugin_type", None) == PluginType.MCP
    }


@pytest.mark.asyncio
async def test_prune_removes_vanished_servers() -> None:
    registry = await _registry_with("srv-a", "srv-b", "srv-c")
    assert _mcp_entry_names(registry) == {"srv-a", "srv-b", "srv-c"}

    removed = await prune_stale_mcp_entries(registry, frozenset({"srv-a"}))
    assert removed == 2
    assert _mcp_entry_names(registry) == {"srv-a"}


@pytest.mark.asyncio
async def test_prune_keeps_real_on_disk_manifests() -> None:
    from pathlib import Path

    from corlinman_providers.plugins.discovery import Origin
    from corlinman_providers.plugins.manifest import (
        Capabilities,
        EntryPoint,
        PluginManifest,
        PluginType,
        Tool,
    )
    from corlinman_providers.plugins.registry import PluginEntry, PluginRegistry

    registry = PluginRegistry.from_roots([])
    # A real (non-synthesized) manifest — different manifest_path prefix.
    real = PluginEntry(
        manifest=PluginManifest(
            manifest_version=3,
            name="real-tool",
            version="1.0.0",
            description="on-disk",
            plugin_type=PluginType.SYNC,
            entry_point=EntryPoint(command="python"),
            capabilities=Capabilities(tools=[Tool(name="t", description="", parameters={"type": "object"})]),
        ),
        origin=Origin.CONFIG,
        manifest_path=Path("/real/plugin/manifest.toml"),
    )
    await registry.upsert(real)
    for entry in build_mcp_registry_entries({"srv-a": [_FakeTool("echo")]}):
        await registry.upsert(entry)

    # Prune with an empty live set — the synthesized srv-a goes, real stays.
    removed = await prune_stale_mcp_entries(registry, frozenset())
    assert removed == 1
    names = {e.manifest.name for e in registry.list()}
    assert "real-tool" in names
    assert "srv-a" not in names


@pytest.mark.asyncio
async def test_prune_none_registry_is_noop() -> None:
    assert await prune_stale_mcp_entries(None, frozenset()) == 0


@pytest.mark.asyncio
async def test_refresh_readvertises_and_prunes() -> None:
    from corlinman_providers.plugins.registry import PluginRegistry
    from corlinman_server.gateway.lifecycle.entrypoint import refresh_mcp_advertisement

    registry = PluginRegistry.from_roots([])

    class _FakeManager:
        def __init__(self) -> None:
            self._tools = {"srv-a": [_FakeTool("echo")], "srv-b": [_FakeTool("echo")]}

        def discovered_tools(self) -> dict[str, list[Any]]:
            return self._tools

    manager = _FakeManager()
    refreshed: list[bool] = []

    from types import SimpleNamespace

    state = SimpleNamespace(
        config={},
        plugin_registry=registry,
        extras={
            "mcp_manager": manager,
            "chat_refresh_fn": lambda: refreshed.append(True),
        },
    )

    # First advertisement: both servers.
    await refresh_mcp_advertisement(state)
    assert _mcp_entry_names(registry) == {"srv-a", "srv-b"}
    assert refreshed == [True]

    # srv-b disappears → refresh prunes it.
    manager._tools = {"srv-a": [_FakeTool("echo")]}
    await refresh_mcp_advertisement(state)
    assert _mcp_entry_names(registry) == {"srv-a"}
    assert refreshed == [True, True]


@pytest.mark.asyncio
async def test_refresh_updates_existing_server_tools() -> None:
    """A live server changing its tool list must re-advertise the NEW tools
    (Codex #110 P1 — the boot-time synthetic entry must not be treated as a
    collision that blocks the rebuild)."""
    import json

    from corlinman_providers.plugins.registry import PluginRegistry
    from corlinman_server.gateway.lifecycle.entrypoint import refresh_mcp_advertisement

    registry = PluginRegistry.from_roots([])

    class _FakeManager:
        def __init__(self) -> None:
            self._tools = {"srv": [_FakeTool("echo")]}

        def discovered_tools(self):
            return self._tools

    manager = _FakeManager()
    from types import SimpleNamespace

    state = SimpleNamespace(
        config={}, plugin_registry=registry, extras={"mcp_manager": manager}
    )

    await refresh_mcp_advertisement(state)
    names1 = [s["function"]["name"] for s in json.loads(state.extras["mcp_tools_json"])]
    assert names1 == ["srv_echo"]

    # The server swaps its tool set — refresh must reflect the new tool.
    manager._tools = {"srv": [_FakeTool("newtool")]}
    await refresh_mcp_advertisement(state)
    names2 = [s["function"]["name"] for s in json.loads(state.extras["mcp_tools_json"])]
    assert names2 == ["srv_newtool"]  # not stale "srv_echo"
    assert _mcp_entry_names(registry) == {"srv"}


@pytest.mark.asyncio
async def test_refresh_prunes_server_that_lost_all_tools() -> None:
    """A server whose tools all vanish produces no entry, so it must fall out
    of the advertised set and be pruned — not linger as a dead route
    (Codex #110 P2)."""
    from corlinman_providers.plugins.registry import PluginRegistry
    from corlinman_server.gateway.lifecycle.entrypoint import refresh_mcp_advertisement

    registry = PluginRegistry.from_roots([])

    class _FakeManager:
        def __init__(self) -> None:
            self._tools = {"srv-a": [_FakeTool("echo")], "srv-b": [_FakeTool("echo")]}

        def discovered_tools(self):
            return self._tools

    manager = _FakeManager()
    from types import SimpleNamespace

    state = SimpleNamespace(
        config={}, plugin_registry=registry, extras={"mcp_manager": manager}
    )
    await refresh_mcp_advertisement(state)
    assert _mcp_entry_names(registry) == {"srv-a", "srv-b"}

    # srv-b still present but with an EMPTY tool list → no entry → pruned.
    manager._tools = {"srv-a": [_FakeTool("echo")], "srv-b": []}
    await refresh_mcp_advertisement(state)
    assert _mcp_entry_names(registry) == {"srv-a"}
    assert state.extras["mcp_advertised_servers"] == frozenset({"srv-a"})
