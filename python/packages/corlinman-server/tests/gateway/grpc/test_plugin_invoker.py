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

import contextlib
import json
import sys
import tempfile
import textwrap
import tomllib
import uuid
from pathlib import Path

import pytest
from corlinman_grpc._generated.corlinman.v1 import agent_pb2, plugin_pb2, plugin_pb2_grpc
from corlinman_grpc.agent_client import RegistryToolExecutor
from corlinman_providers.plugins import (
    MANIFEST_FILENAME,
    Origin,
    PluginEntry,
    PluginManifest,
    PluginRegistry,
)
from corlinman_server.gateway.grpc.plugin_invoker import (
    ServicePluginDispatcher,
    build_registry_invoker,
)
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


def _write_service_plugin(tmp_path: Path) -> PluginEntry:
    """Write a ``service``-kind plugin manifest (no real child needed —
    these tests stub the supervisor / dispatch)."""
    plugin_dir = tmp_path / "svc"
    plugin_dir.mkdir(exist_ok=True)
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
    return PluginEntry(
        manifest=manifest,
        origin=Origin.WORKSPACE,
        manifest_path=plugin_dir / MANIFEST_FILENAME,
    )


@pytest.mark.asyncio
async def test_invoker_service_plugin_without_supervisor_degrades(
    tmp_path: Path,
) -> None:
    """A ``service`` plugin with no supervisor wired degrades to a clear
    ``service_supervisor_unavailable`` result, never a crash."""
    registry = PluginRegistry()
    await registry.upsert(_write_service_plugin(tmp_path))
    invoker = build_registry_invoker(registry)
    result = await invoker("svc", "ping", b"{}")
    assert result.is_error is True
    assert json.loads(result.content)["error"] == "service_supervisor_unavailable"


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


# ─── P16: service-plugin dispatch via the supervisor ─────────────────


class _FakePluginBridge(plugin_pb2_grpc.PluginBridgeServicer):
    """In-process ``PluginBridge`` server: streams a progress event then
    a terminal event chosen by the test (result / error)."""

    def __init__(self, *, mode: str = "result") -> None:
        self.mode = mode
        self.seen: list[plugin_pb2.PluginToolCall] = []

    async def Execute(self, request, context):  # noqa: N802 — gRPC method
        self.seen.append(request)
        yield plugin_pb2.ToolEvent(
            progress=plugin_pb2.Progress(message="working", fraction=0.5)
        )
        if self.mode == "error":
            from corlinman_grpc._generated.corlinman.v1 import common_pb2

            yield plugin_pb2.ToolEvent(
                error=common_pb2.ErrorInfo(
                    reason=common_pb2.FailoverReason.TIMEOUT,
                    message="service failed",
                )
            )
            return
        if self.mode == "silent":
            return
        yield plugin_pb2.ToolEvent(
            result=plugin_pb2.PluginToolResult(
                call_id=request.call_id,
                result_json=json.dumps({"pong": True, "tool": request.tool}).encode(),
                duration_ms=12,
            )
        )


class _FakeSupervisor:
    """Stub :class:`PluginSupervisor` — ``spawn_service`` hands back a
    fixed UDS path; tracks spawn count so a get-or-spawn reuse can be
    asserted."""

    def __init__(self, socket_path: Path) -> None:
        self._socket = socket_path
        self.spawn_calls = 0
        self._children: dict[str, object] = {}

    async def spawn_service(self, manifest):
        del manifest  # stub — manifest unused
        self.spawn_calls += 1
        return self._socket


def _short_socket_path() -> Path:
    """A UDS path short enough for the macOS 103-char ``sun_path`` cap.

    ``tmp_path`` under pytest is too deep on macOS, so service-plugin
    tests bind their UDS under the system temp dir with a short name.
    """
    return Path(tempfile.gettempdir()) / f"clm-svc-{uuid.uuid4().hex[:8]}.sock"


@contextlib.asynccontextmanager
async def _running_bridge(socket_path: Path, *, mode: str = "result"):
    """Stand up a real grpc.aio ``PluginBridge`` server on a UDS."""
    import grpc.aio

    bridge = _FakePluginBridge(mode=mode)
    server = grpc.aio.server()
    plugin_pb2_grpc.add_PluginBridgeServicer_to_server(bridge, server)
    server.add_insecure_port(f"unix:{socket_path}")
    await server.start()
    try:
        yield bridge
    finally:
        await server.stop(grace=0)
        with contextlib.suppress(OSError):
            socket_path.unlink()


@pytest.mark.asyncio
async def test_service_plugin_dispatch_round_trip(tmp_path: Path) -> None:
    """A ``service`` plugin call is routed through the supervisor +
    PluginBridge gRPC stream and the terminal result flows back."""
    socket_path = _short_socket_path()
    async with _running_bridge(socket_path) as bridge:
        registry = PluginRegistry()
        await registry.upsert(_write_service_plugin(tmp_path))
        supervisor = _FakeSupervisor(socket_path)
        invoker = build_registry_invoker(registry, supervisor=supervisor)

        result = await invoker("svc", "ping", json.dumps({"n": 1}).encode())

    assert result.is_error is False
    body = json.loads(result.content)
    assert body == {"pong": True, "tool": "ping"}
    assert supervisor.spawn_calls == 1
    assert bridge.seen[0].tool == "ping"
    assert json.loads(bridge.seen[0].args_json) == {"n": 1}


@pytest.mark.asyncio
async def test_service_plugin_error_event_is_tool_error(tmp_path: Path) -> None:
    """A ``ToolEvent.error`` from the service folds into an ``is_error``
    invocation, not a crash."""
    socket_path = _short_socket_path()
    async with _running_bridge(socket_path, mode="error"):
        registry = PluginRegistry()
        await registry.upsert(_write_service_plugin(tmp_path))
        invoker = build_registry_invoker(
            registry, supervisor=_FakeSupervisor(socket_path)
        )
        result = await invoker("svc", "ping", b"{}")

    assert result.is_error is True
    body = json.loads(result.content)
    assert body["error"] == "service_plugin_error"
    assert body["message"] == "service failed"
    assert body["code"] == "TIMEOUT"


@pytest.mark.asyncio
async def test_service_plugin_silent_stream_degrades(tmp_path: Path) -> None:
    """A service whose stream ends with no terminal event degrades to a
    clear ``service_no_result`` error."""
    socket_path = _short_socket_path()
    async with _running_bridge(socket_path, mode="silent"):
        registry = PluginRegistry()
        await registry.upsert(_write_service_plugin(tmp_path))
        invoker = build_registry_invoker(
            registry, supervisor=_FakeSupervisor(socket_path)
        )
        result = await invoker("svc", "ping", b"{}")

    assert result.is_error is True
    assert json.loads(result.content)["error"] == "service_no_result"


@pytest.mark.asyncio
async def test_service_plugin_dispatch_reuses_channel(tmp_path: Path) -> None:
    """Two calls to the same service reuse one spawn + one channel — the
    dispatcher does not respawn the child per call."""
    socket_path = _short_socket_path()
    async with _running_bridge(socket_path):
        registry = PluginRegistry()
        await registry.upsert(_write_service_plugin(tmp_path))
        supervisor = _FakeSupervisor(socket_path)
        dispatcher = ServicePluginDispatcher(supervisor)
        entry = registry.get("svc")
        await dispatcher.dispatch(entry, "ping", {}, timeout_ms=5_000)
        await dispatcher.dispatch(entry, "ping", {}, timeout_ms=5_000)
        await dispatcher.aclose()

    assert supervisor.spawn_calls == 1


@pytest.mark.asyncio
async def test_service_plugin_dial_failure_degrades(tmp_path: Path) -> None:
    """A service whose UDS has no server listening degrades to a clear
    ``service_call_failed`` error within the deadline."""
    registry = PluginRegistry()
    await registry.upsert(_write_service_plugin(tmp_path))
    # No server is bound to this socket path.
    supervisor = _FakeSupervisor(_short_socket_path())
    invoker = build_registry_invoker(registry, supervisor=supervisor)
    result = await invoker("svc", "ping", b"{}")
    assert result.is_error is True
    assert json.loads(result.content)["error"] in (
        "service_call_failed",
        "service_timeout",
    )


# ─── P14/P16: mcp-plugin dispatch via the MCP bridge ─────────────────


def _write_mcp_plugin(tmp_path: Path, *, server: str | None = None) -> PluginEntry:
    """Write an ``mcp``-kind plugin manifest. ``server`` populates the
    free-form ``[meta]`` ``mcp_server`` override key."""
    plugin_dir = tmp_path / "mcpkit"
    plugin_dir.mkdir(exist_ok=True)
    meta_block = f'\n[meta]\nmcp_server = "{server}"\n' if server else ""
    body = textwrap.dedent(
        f"""
        manifest_version = 3
        name = "mcpkit"
        version = "0.1.0"
        plugin_type = "mcp"

        [entry_point]
        command = "true"

        [mcp]
        autostart = false

        [[capabilities.tools]]
        name = "search"
        {meta_block}"""
    ).strip()
    manifest = PluginManifest.model_validate(tomllib.loads(body))
    manifest.migrate_to_current_in_memory()
    return PluginEntry(
        manifest=manifest,
        origin=Origin.WORKSPACE,
        manifest_path=plugin_dir / MANIFEST_FILENAME,
    )


class _FakeMcpManager:
    """Stub :class:`McpClientManager` — records the routed call and
    returns a configured outcome."""

    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.seen: list[tuple[str, str, object]] = []

    async def call_tool(self, server: str, tool: str, arguments):
        self.seen.append((server, tool, arguments))
        return self.outcome


@pytest.mark.asyncio
async def test_mcp_plugin_dispatch_round_trip(tmp_path: Path) -> None:
    """An ``mcp``-kind plugin call is routed through the MCP bridge to
    the manager's ``call_tool`` and the outcome flows back."""
    from corlinman_mcp_server import McpToolCallOutcome

    registry = PluginRegistry()
    await registry.upsert(_write_mcp_plugin(tmp_path))
    manager = _FakeMcpManager(McpToolCallOutcome(content="hit!", is_error=False))
    invoker = build_registry_invoker(registry, mcp_manager=manager)

    result = await invoker("mcpkit", "search", json.dumps({"q": "x"}).encode())

    assert result.is_error is False
    assert result.content == "hit!"
    assert manager.seen == [("mcpkit", "search", {"q": "x"})]


@pytest.mark.asyncio
async def test_mcp_plugin_uses_meta_server_override(tmp_path: Path) -> None:
    """A ``[meta]`` ``mcp_server`` override routes to that external
    server name instead of the manifest name."""
    from corlinman_mcp_server import McpToolCallOutcome

    registry = PluginRegistry()
    await registry.upsert(_write_mcp_plugin(tmp_path, server="upstream-mcp"))
    manager = _FakeMcpManager(McpToolCallOutcome(content="ok"))
    invoker = build_registry_invoker(registry, mcp_manager=manager)

    await invoker("mcpkit", "search", b"{}")
    assert manager.seen[0][0] == "upstream-mcp"


@pytest.mark.asyncio
async def test_mcp_plugin_error_outcome_is_tool_error(tmp_path: Path) -> None:
    """An ``is_error`` outcome from the manager folds into an ``is_error``
    invocation."""
    from corlinman_mcp_server import McpToolCallOutcome

    registry = PluginRegistry()
    await registry.upsert(_write_mcp_plugin(tmp_path))
    manager = _FakeMcpManager(
        McpToolCallOutcome(content='{"error": "mcp_call_failed"}', is_error=True)
    )
    invoker = build_registry_invoker(registry, mcp_manager=manager)
    result = await invoker("mcpkit", "search", b"{}")
    assert result.is_error is True


@pytest.mark.asyncio
async def test_mcp_plugin_without_bridge_degrades(tmp_path: Path) -> None:
    """An ``mcp`` plugin with no MCP manager wired degrades to a clear
    ``mcp_bridge_unavailable`` result."""
    registry = PluginRegistry()
    await registry.upsert(_write_mcp_plugin(tmp_path))
    invoker = build_registry_invoker(registry)
    result = await invoker("mcpkit", "search", b"{}")
    assert result.is_error is True
    assert json.loads(result.content)["error"] == "mcp_bridge_unavailable"
