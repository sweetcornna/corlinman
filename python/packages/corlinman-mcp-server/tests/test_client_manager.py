"""P14 — MCP *client* manager tests.

Drives :class:`corlinman_mcp_server.McpClientManager` against a fake
stdio MCP server (a tiny Python script speaking newline-delimited
JSON-RPC: ``initialize`` → ``tools/list`` → ``tools/call``). Covers
connection, handshake, tool discovery, a real ``tools/call`` round
trip, and the degradation paths (unreachable server, unknown tool,
disabled server, no MCP config).
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from corlinman_mcp_server import (
    McpClientManager,
    McpServerSpec,
    load_server_specs,
)

# ─── A real, minimal stdio MCP server (newline-delimited JSON-RPC) ────

_FAKE_MCP_SERVER = textwrap.dedent(
    """
    import json, sys

    TOOLS = [
        {
            "name": "echo",
            "description": "echo back the arguments",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "ping",
            "description": "always pongs",
            "inputSchema": {"type": "object"},
        },
    ]

    def reply(rid, result):
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n"
        )
        sys.stdout.flush()

    def err(rid, code, message):
        sys.stdout.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
            )
            + "\\n"
        )
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            reply(rid, {"protocolVersion": "2024-11-05", "capabilities": {}})
        elif method == "notifications/initialized":
            pass  # notification — no reply
        elif method == "tools/list":
            reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                reply(
                    rid,
                    {
                        "content": [{"type": "text", "text": json.dumps(args)}],
                        "isError": False,
                    },
                )
            elif name == "ping":
                reply(
                    rid,
                    {"content": [{"type": "text", "text": "pong"}], "isError": False},
                )
            else:
                err(rid, -32601, "no such tool: " + str(name))
        else:
            err(rid, -32601, "no such method: " + str(method))
    """
).strip()


@pytest.fixture
def fake_mcp_server(tmp_path: Path) -> Path:
    """Write the fake stdio MCP server script and return its path."""
    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_MCP_SERVER, encoding="utf-8")
    return script


def _stdio_spec(name: str, script: Path) -> McpServerSpec:
    return McpServerSpec(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=[str(script)],
    )


# ─── connect + discover ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_and_discover_tools(fake_mcp_server: Path) -> None:
    """The manager connects a stdio MCP server, runs the handshake and
    discovers the tools it advertises."""
    manager = McpClientManager([_stdio_spec("fake", fake_mcp_server)])
    try:
        await manager.connect_all()
        server = manager.server("fake")
        assert server is not None
        assert server.is_ready, server.error
        names = {t.name for t in server.tools}
        assert names == {"echo", "ping"}
        assert "fake" in manager.discovered_tools()
        assert manager.has_tool("fake", "echo")
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_call_tool_round_trip(fake_mcp_server: Path) -> None:
    """A ``tools/call`` against the connected server returns the tool's
    text content."""
    manager = McpClientManager([_stdio_spec("fake", fake_mcp_server)])
    try:
        await manager.connect_all()
        outcome = await manager.call_tool("fake", "echo", {"hello": "world"})
        assert outcome.is_error is False
        assert outcome.content == '{"hello": "world"}'

        pong = await manager.call_tool("fake", "ping", {})
        assert pong.is_error is False
        assert pong.content == "pong"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_call_tool_resolves_by_bare_tool_name(fake_mcp_server: Path) -> None:
    """When the caller passes the tool name where a server name is
    expected, the manager routes to the unique owning server."""
    manager = McpClientManager([_stdio_spec("fake", fake_mcp_server)])
    try:
        await manager.connect_all()
        # "echo" is not a server name — but it is a tool on "fake".
        outcome = await manager.call_tool("echo", "echo", {"k": 1})
        assert outcome.is_error is False
        assert outcome.content == '{"k": 1}'
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_connect_all_is_idempotent(fake_mcp_server: Path) -> None:
    """Calling ``connect_all`` twice does not re-connect."""
    manager = McpClientManager([_stdio_spec("fake", fake_mcp_server)])
    try:
        await manager.connect_all()
        await manager.connect_all()  # no-op
        assert manager.server("fake").is_ready
    finally:
        await manager.aclose()


# ─── degradation paths ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unreachable_stdio_server_degrades_cleanly() -> None:
    """A server whose command does not exist is recorded as ``error`` —
    ``connect_all`` does not raise."""
    spec = McpServerSpec(
        name="ghost",
        transport="stdio",
        command="/nonexistent/mcp-binary-xyz",
        args=[],
        handshake_timeout_s=3.0,
    )
    manager = McpClientManager([spec])
    await manager.connect_all()
    server = manager.server("ghost")
    assert server is not None
    assert server.status == "error"
    assert server.error
    assert manager.ready_servers() == []


@pytest.mark.asyncio
async def test_call_unknown_server_degrades_cleanly() -> None:
    """Calling a tool on a server that was never configured returns a
    structured error, never raises."""
    manager = McpClientManager([])
    outcome = await manager.call_tool("nope", "tool", {})
    assert outcome.is_error is True
    assert "mcp_server_not_found" in outcome.content


@pytest.mark.asyncio
async def test_call_unknown_tool_on_ready_server(fake_mcp_server: Path) -> None:
    """A tool the server does not advertise yields ``mcp_tool_not_found``,
    never reaching the wire."""
    manager = McpClientManager([_stdio_spec("fake", fake_mcp_server)])
    try:
        await manager.connect_all()
        outcome = await manager.call_tool("fake", "does_not_exist", {})
        assert outcome.is_error is True
        assert "mcp_tool_not_found" in outcome.content
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_disabled_server_is_not_connected(fake_mcp_server: Path) -> None:
    """A spec with ``enabled = False`` is skipped — recorded as error,
    never spawned."""
    spec = _stdio_spec("fake", fake_mcp_server)
    spec.enabled = False
    manager = McpClientManager([spec])
    await manager.connect_all()
    server = manager.server("fake")
    assert server.status == "error"
    assert "disabled" in (server.error or "")


@pytest.mark.asyncio
async def test_call_before_connect_degrades() -> None:
    """Calling a configured-but-not-connected server degrades cleanly."""
    spec = McpServerSpec(name="fake", transport="stdio", command="true")
    manager = McpClientManager([spec])
    outcome = await manager.call_tool("fake", "echo", {})
    assert outcome.is_error is True
    assert "mcp_server_unavailable" in outcome.content


# ─── config loading ──────────────────────────────────────────────────


def test_load_server_specs_from_mcp_section() -> None:
    """``config["mcp"]["servers"]`` as a name→body mapping loads."""
    config = {
        "mcp": {
            "servers": {
                "files": {"command": "mcp-files", "args": ["--root", "/x"]},
                "web": {"url": "ws://localhost:9000/mcp"},
            }
        }
    }
    specs = {s.name: s for s in load_server_specs(config)}
    assert set(specs) == {"files", "web"}
    assert specs["files"].transport == "stdio"
    assert specs["files"].args == ["--root", "/x"]
    # url present, no explicit transport → inferred as ws.
    assert specs["web"].transport == "ws"
    assert specs["web"].url == "ws://localhost:9000/mcp"


def test_load_server_specs_from_top_level_list() -> None:
    """``config["mcp_servers"]`` as a list of objects loads."""
    config = {
        "mcp_servers": [
            {"name": "a", "command": "a-bin"},
            {"name": "b", "transport": "ws", "url": "ws://h/mcp"},
        ]
    }
    specs = {s.name: s for s in load_server_specs(config)}
    assert set(specs) == {"a", "b"}
    assert specs["b"].transport == "ws"


def test_load_server_specs_no_config_is_empty() -> None:
    """A config with no MCP section yields an empty list — clean no-op."""
    assert load_server_specs({}) == []
    assert load_server_specs(None) == []


def test_from_config_with_no_mcp_section_is_idle() -> None:
    """``McpClientManager.from_config`` on an MCP-less config is an idle,
    zero-server manager."""
    manager = McpClientManager.from_config({"other": 1})
    assert manager.server_count == 0


@pytest.mark.asyncio
async def test_idle_manager_connect_all_is_noop() -> None:
    """An empty manager's ``connect_all`` runs and yields no servers."""
    manager = McpClientManager([])
    await manager.connect_all()
    assert manager.discovered_tools() == {}
    await manager.aclose()


# ─── ws ("http") transport ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_transport_connect_discover_and_call() -> None:
    """The manager connects an MCP server over the websocket ("http")
    transport, discovers tools and runs a ``tools/call``."""
    import json as _json

    import websockets

    async def _handler(ws):
        async for raw in ws:
            req = _json.loads(raw)
            method = req.get("method")
            rid = req.get("id")
            if method == "notifications/initialized":
                continue  # notification — no reply
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {}}
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "remote_echo",
                            "description": "echo",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                }
            elif method == "tools/call":
                args = (req.get("params") or {}).get("arguments") or {}
                result = {
                    "content": [{"type": "text", "text": _json.dumps(args)}],
                    "isError": False,
                }
            else:
                await ws.send(
                    _json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {"code": -32601, "message": "nope"},
                        }
                    )
                )
                continue
            await ws.send(
                _json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})
            )

    server = await websockets.asyncio.server.serve(_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    spec = McpServerSpec(
        name="remote",
        transport="ws",
        url=f"ws://127.0.0.1:{port}",
    )
    manager = McpClientManager([spec])
    try:
        await manager.connect_all()
        assert manager.server("remote").is_ready, manager.server("remote").error
        assert manager.has_tool("remote", "remote_echo")
        outcome = await manager.call_tool("remote", "remote_echo", {"v": 7})
        assert outcome.is_error is False
        assert outcome.content == '{"v": 7}'
    finally:
        await manager.aclose()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ws_transport_unreachable_degrades() -> None:
    """A ws server that is not listening is recorded as ``error`` — no
    crash out of ``connect_all``."""
    spec = McpServerSpec(
        name="dead",
        transport="ws",
        url="ws://127.0.0.1:1",  # nothing listening
        handshake_timeout_s=3.0,
    )
    manager = McpClientManager([spec])
    await manager.connect_all()
    assert manager.server("dead").status == "error"
    assert manager.ready_servers() == []
