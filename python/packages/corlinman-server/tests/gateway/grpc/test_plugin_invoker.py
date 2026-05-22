"""Parcel **P5** — real tool-executor wiring tests.

Covers the gateway-assembly half of the tool-execution split:

* ``gateway.grpc.plugin_invoker`` — resolving a plugin from a real
  :class:`corlinman_providers.plugins.PluginRegistry` and actually
  *running* a spawn-per-call ``sync`` JSON-RPC stdio plugin.
* ``gateway.services.grpc_backend.build_tool_executor`` — the boot-time
  builder that turns ``AppState.plugin_registry`` into a wired
  :class:`~corlinman_grpc.agent_client.RegistryToolExecutor`.

The end-to-end test writes a tiny real Python plugin to disk, registers
it, and drives a tool call through
``RegistryToolExecutor → build_registry_invoker → invoke_sync_plugin``,
proving the placeholder is gone and a genuine plugin result comes back.
"""

from __future__ import annotations

import json
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
from corlinman_grpc._generated.corlinman.v1 import agent_pb2
from corlinman_grpc.agent_client import RegistryToolExecutor
from corlinman_providers.plugins import (
    MANIFEST_FILENAME,
    Origin,
    PluginEntry,
    PluginManifest,
    PluginRegistry,
)
from corlinman_server.gateway.grpc.plugin_invoker import build_registry_invoker
from corlinman_server.gateway.services.grpc_backend import build_tool_executor

# ─── Fake sync plugin on disk ─────────────────────────────────────────

#: A minimal real JSON-RPC 2.0 stdio plugin: reads one request line,
#: echoes ``{"echoed": <params>, "method": <method>}`` as the result.
_ECHO_PLUGIN_SOURCE = textwrap.dedent(
    """
    import json, sys
    line = sys.stdin.readline()
    req = json.loads(line)
    resp = {
        "jsonrpc": "2.0",
        "id": req.get("id"),
        "result": {"echoed": req.get("params"), "method": req.get("method")},
    }
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
    """
).strip()

#: A plugin that always answers with a JSON-RPC error object.
_ERROR_PLUGIN_SOURCE = textwrap.dedent(
    """
    import json, sys
    sys.stdin.readline()
    resp = {
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -32000, "message": "deliberate failure"},
    }
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
    """
).strip()


def _write_plugin(
    root: Path,
    name: str,
    source: str,
    *,
    tool: str = "echo",
) -> PluginEntry:
    """Write a real sync plugin (manifest + script) and return its
    :class:`PluginEntry`."""
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    script = plugin_dir / "plugin.py"
    script.write_text(source, encoding="utf-8")

    manifest_body = textwrap.dedent(
        f"""
        name = "{name}"
        version = "0.1.0"
        plugin_type = "sync"

        [entry_point]
        command = "{sys.executable}"
        args = ["plugin.py"]

        [[capabilities.tools]]
        name = "{tool}"
        description = "test tool"
        """
    ).strip()
    (plugin_dir / MANIFEST_FILENAME).write_text(manifest_body, encoding="utf-8")

    manifest = PluginManifest.model_validate(tomllib.loads(manifest_body))
    manifest.migrate_to_current_in_memory()
    return PluginEntry(
        manifest=manifest,
        origin=Origin.WORKSPACE,
        manifest_path=plugin_dir / MANIFEST_FILENAME,
    )


def _call(plugin: str, tool: str, args: dict) -> agent_pb2.ToolCall:
    return agent_pb2.ToolCall(
        call_id="c1",
        plugin=plugin,
        tool=tool,
        args_json=json.dumps(args).encode("utf-8"),
    )


# ─── invoker: real sync-plugin execution ─────────────────────────────


@pytest.mark.asyncio
async def test_invoker_runs_real_sync_plugin(tmp_path: Path) -> None:
    """A registered sync plugin is actually spawned and its JSON-RPC
    result flows back."""
    registry = PluginRegistry()
    await registry.upsert(_write_plugin(tmp_path, "echoer", _ECHO_PLUGIN_SOURCE))

    invoker = build_registry_invoker(registry)
    result = await invoker("echoer", "echo", json.dumps({"x": 9}).encode("utf-8"))

    assert result.is_error is False
    body = json.loads(result.content)
    assert body["echoed"] == {"x": 9}
    assert body["method"] == "echo"


@pytest.mark.asyncio
async def test_invoker_resolves_plugin_by_tool_name(tmp_path: Path) -> None:
    """OpenAI tool calls collapse plugin == tool == function.name; the
    invoker still resolves when only the tool name is known."""
    registry = PluginRegistry()
    await registry.upsert(
        _write_plugin(tmp_path, "mathkit", _ECHO_PLUGIN_SOURCE, tool="sum")
    )

    invoker = build_registry_invoker(registry)
    # Both ``plugin`` and ``tool`` are the tool name "sum" — the registry
    # has no plugin called "sum", so the by-tool fallback must kick in.
    result = await invoker("sum", "sum", b"{}")
    assert result.is_error is False
    assert json.loads(result.content)["method"] == "sum"


@pytest.mark.asyncio
async def test_invoker_surfaces_plugin_jsonrpc_error(tmp_path: Path) -> None:
    """A JSON-RPC error object from the plugin becomes an ``is_error``
    invocation, not a crash."""
    registry = PluginRegistry()
    await registry.upsert(_write_plugin(tmp_path, "boom", _ERROR_PLUGIN_SOURCE))

    invoker = build_registry_invoker(registry)
    result = await invoker("boom", "echo", b"{}")
    assert result.is_error is True
    body = json.loads(result.content)
    assert body["error"] == "plugin_error"
    assert body["code"] == -32000


@pytest.mark.asyncio
async def test_invoker_unknown_plugin_degrades_cleanly() -> None:
    """An unknown plugin name yields a clear error, never an exception."""
    invoker = build_registry_invoker(PluginRegistry())
    result = await invoker("ghost", "ghost", b"{}")
    assert result.is_error is True
    assert json.loads(result.content)["error"] == "plugin_not_found"


@pytest.mark.asyncio
async def test_invoker_none_registry_degrades_cleanly() -> None:
    """No registry at all → ``plugin_registry_unavailable``, no crash."""
    invoker = build_registry_invoker(None)
    result = await invoker("anything", "anything", b"{}")
    assert result.is_error is True
    assert json.loads(result.content)["error"] == "plugin_registry_unavailable"


@pytest.mark.asyncio
async def test_invoker_unknown_tool_on_known_plugin(tmp_path: Path) -> None:
    """A known plugin asked for a tool it does not advertise → a clear
    ``tool_not_found`` result."""
    registry = PluginRegistry()
    await registry.upsert(
        _write_plugin(tmp_path, "echoer", _ECHO_PLUGIN_SOURCE, tool="echo")
    )
    invoker = build_registry_invoker(registry)
    result = await invoker("echoer", "not_a_tool", b"{}")
    assert result.is_error is True
    assert json.loads(result.content)["error"] == "tool_not_found"


@pytest.mark.asyncio
async def test_invoker_bad_arguments_json(tmp_path: Path) -> None:
    """Malformed OpenAI ``arguments`` is folded into a tool error."""
    registry = PluginRegistry()
    await registry.upsert(_write_plugin(tmp_path, "echoer", _ECHO_PLUGIN_SOURCE))
    invoker = build_registry_invoker(registry)
    result = await invoker("echoer", "echo", b"{not json")
    assert result.is_error is True
    assert json.loads(result.content)["error"] == "bad_tool_arguments"


@pytest.mark.asyncio
async def test_invoker_rejects_non_sync_plugin(tmp_path: Path) -> None:
    """Service / mcp plugin types are out of scope — the invoker returns
    a clear ``unsupported_plugin_type`` result instead of guessing."""
    plugin_dir = tmp_path / "svc"
    plugin_dir.mkdir()
    body = textwrap.dedent(
        """
        name = "svc"
        version = "0.1.0"
        plugin_type = "service"

        [entry_point]
        command = "true"

        [[capabilities.tools]]
        name = "ping"
        """
    ).strip()
    manifest = PluginManifest.model_validate(tomllib.loads(body))
    manifest.migrate_to_current_in_memory()
    registry = PluginRegistry()
    await registry.upsert(
        PluginEntry(
            manifest=manifest,
            origin=Origin.WORKSPACE,
            manifest_path=plugin_dir / MANIFEST_FILENAME,
        )
    )
    invoker = build_registry_invoker(registry)
    result = await invoker("svc", "ping", b"{}")
    assert result.is_error is True
    assert json.loads(result.content)["error"] == "unsupported_plugin_type"


# ─── full executor round-trip ────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_executor_round_trip(tmp_path: Path) -> None:
    """End-to-end: RegistryToolExecutor → invoker → real plugin → a
    genuine, non-placeholder ToolResult comes back."""
    registry = PluginRegistry()
    await registry.upsert(_write_plugin(tmp_path, "echoer", _ECHO_PLUGIN_SOURCE))

    executor = RegistryToolExecutor(build_registry_invoker(registry))
    result = await executor.execute(_call("echoer", "echo", {"q": "hi"}))

    assert result.call_id == "c1"
    assert result.is_error is False
    decoded = result.result_json.decode("utf-8")
    assert "awaiting_plugin_runtime" not in decoded
    assert json.loads(decoded)["echoed"] == {"q": "hi"}


# ─── build_tool_executor ─────────────────────────────────────────────


class _State:
    """Minimal AppState stand-in carrying a plugin registry."""

    def __init__(self, plugin_registry: object = None) -> None:
        self.plugin_registry = plugin_registry


def test_build_tool_executor_wired_with_registry() -> None:
    """With a registry on AppState, the builder returns a wired
    RegistryToolExecutor."""
    executor = build_tool_executor(_State(PluginRegistry()))
    assert isinstance(executor, RegistryToolExecutor)
    assert executor.is_wired is True


def test_build_tool_executor_without_registry_still_wired() -> None:
    """With no registry the builder still returns a wired executor —
    its invoker degrades each call to ``plugin_registry_unavailable``
    rather than the builder returning a placeholder/None."""
    executor = build_tool_executor(_State(None))
    assert isinstance(executor, RegistryToolExecutor)
    assert executor.is_wired is True


@pytest.mark.asyncio
async def test_build_tool_executor_degrades_calls_without_registry() -> None:
    """The no-registry executor runs without crashing and reports the
    degradation cleanly."""
    executor = build_tool_executor(_State(None))
    result = await executor.execute(_call("x", "x", {}))
    assert result.is_error is True
    assert json.loads(result.result_json)["error"] == "plugin_registry_unavailable"
