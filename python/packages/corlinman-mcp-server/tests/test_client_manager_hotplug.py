"""P14 — runtime hot-plug primitives for the MCP client manager.

Exercises :meth:`McpClientManager.add_server`, :meth:`remove_server`,
:meth:`restart_one`, :meth:`enable_one` and :meth:`disable_one` against an
in-memory :class:`FakePeer` (no subprocess / socket). ``_connect_peer`` is
monkeypatched to return the fake so the handshake + ``tools/list`` discovery
path runs unchanged, but the wire is a dict in memory.
"""

from __future__ import annotations

from typing import Any

import pytest
from corlinman_mcp_server import McpClientManager, McpServerSpec

_ONE_TOOL = [
    {
        "name": "echo",
        "description": "echo back the arguments",
        "inputSchema": {"type": "object"},
    }
]


class FakePeer:
    """In-memory :class:`McpClientPeer` — no process, no socket.

    ``initialize`` is acked, ``tools/list`` advertises a single ``echo``
    tool, and ``close()`` flips a flag so tests can assert teardown.
    """

    def __init__(self) -> None:
        self.closed = False

    async def call(self, method: str, params: Any = None) -> Any:
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "capabilities": {}}
        if method == "tools/list":
            return {"tools": _ONE_TOOL}
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "ok"}], "isError": False}
        raise AssertionError(f"unexpected method {method!r}")

    async def notify(self, method: str, params: Any = None) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_peer_manager(monkeypatch: pytest.MonkeyPatch) -> McpClientManager:
    """A connected, server-less manager whose ``_connect_peer`` always
    yields a fresh :class:`FakePeer`."""

    async def _fake_connect_peer(
        self: McpClientManager, spec: McpServerSpec
    ) -> FakePeer:
        return FakePeer()

    monkeypatch.setattr(McpClientManager, "_connect_peer", _fake_connect_peer)
    return McpClientManager([])


def _spec(name: str) -> McpServerSpec:
    return McpServerSpec(name=name, transport="stdio", command="fake")


# ─── add_server brings a server up live ──────────────────────────────


async def test_add_server_brings_up_ready(
    fake_peer_manager: McpClientManager,
) -> None:
    """Adding a server to an already-connected manager brings it up; its
    tool shows up in ``discovered_tools``."""
    manager = fake_peer_manager
    await manager.connect_all()  # no servers yet — flips _connected

    managed = await manager.add_server(_spec("a"))
    assert managed.is_ready, managed.error
    assert "a" in manager.discovered_tools()
    assert manager.has_tool("a", "echo")


async def test_add_server_before_connect_defers(
    fake_peer_manager: McpClientManager,
) -> None:
    """Adding before ``connect_all`` only registers; ``connect_all`` then
    brings it up."""
    manager = fake_peer_manager
    managed = await manager.add_server(_spec("a"))
    assert managed.status == "pending"
    assert "a" not in manager.discovered_tools()

    await manager.connect_all()
    assert manager.server("a").is_ready
    assert "a" in manager.discovered_tools()


async def test_add_server_duplicate_raises(
    fake_peer_manager: McpClientManager,
) -> None:
    """A second ``add_server`` for the same name without ``replace`` is a
    ``ValueError``."""
    manager = fake_peer_manager
    await manager.connect_all()
    await manager.add_server(_spec("a"))
    with pytest.raises(ValueError, match="already registered"):
        await manager.add_server(_spec("a"))


async def test_add_server_replace_tears_down_and_rebinds(
    fake_peer_manager: McpClientManager,
) -> None:
    """``replace=True`` closes the old peer and binds a fresh one."""
    manager = fake_peer_manager
    await manager.connect_all()
    first = await manager.add_server(_spec("a"))
    old_peer = first.peer
    assert isinstance(old_peer, FakePeer)

    second = await manager.add_server(_spec("a"), replace=True)
    assert old_peer.closed is True
    assert second.peer is not old_peer
    assert second.is_ready
    assert manager.has_tool("a", "echo")


# ─── enable / disable ────────────────────────────────────────────────


async def test_disable_one_removes_then_enable_restores(
    fake_peer_manager: McpClientManager,
) -> None:
    """``disable_one`` drops the server from discovery; ``enable_one``
    brings it back."""
    manager = fake_peer_manager
    await manager.connect_all()
    await manager.add_server(_spec("a"))
    assert "a" in manager.discovered_tools()

    assert await manager.disable_one("a") is True
    server = manager.server("a")
    assert server.spec.enabled is False
    assert server.status == "error"
    assert server.error == "disabled"
    assert server.peer is None
    assert "a" not in manager.discovered_tools()

    assert await manager.enable_one("a") is True
    assert manager.server("a").spec.enabled is True
    assert manager.server("a").is_ready
    assert "a" in manager.discovered_tools()


async def test_enable_one_already_ready_is_noop(
    fake_peer_manager: McpClientManager,
) -> None:
    """Enabling an already-ready server keeps its peer."""
    manager = fake_peer_manager
    await manager.connect_all()
    await manager.add_server(_spec("a"))
    peer = manager.server("a").peer

    assert await manager.enable_one("a") is True
    assert manager.server("a").peer is peer


# ─── restart ─────────────────────────────────────────────────────────


async def test_restart_one_keeps_server_ready(
    fake_peer_manager: McpClientManager,
) -> None:
    """``restart_one`` tears the peer down and reconnects; the server is
    ready again with a fresh peer."""
    manager = fake_peer_manager
    await manager.connect_all()
    await manager.add_server(_spec("a"))
    old_peer = manager.server("a").peer

    assert await manager.restart_one("a") is True
    server = manager.server("a")
    assert server.is_ready
    assert server.peer is not old_peer
    assert old_peer.closed is True
    assert manager.has_tool("a", "echo")


async def test_restart_one_disabled_resets_but_does_not_connect(
    fake_peer_manager: McpClientManager,
) -> None:
    """Restarting a disabled server resets it to pending without dialing."""
    manager = fake_peer_manager
    await manager.connect_all()
    await manager.add_server(_spec("a"))
    await manager.disable_one("a")

    assert await manager.restart_one("a") is True
    server = manager.server("a")
    assert server.status == "pending"
    assert server.peer is None
    assert "a" not in manager.discovered_tools()


# ─── remove ──────────────────────────────────────────────────────────


async def test_remove_server_drops_it(
    fake_peer_manager: McpClientManager,
) -> None:
    """``remove_server`` closes the peer and forgets the server."""
    manager = fake_peer_manager
    await manager.connect_all()
    await manager.add_server(_spec("a"))
    peer = manager.server("a").peer

    assert await manager.remove_server("a") is True
    assert manager.server("a") is None
    assert "a" not in manager.discovered_tools()
    assert peer.closed is True


# ─── missing-name returns False, never raises ────────────────────────


async def test_mutators_on_unknown_name_return_false(
    fake_peer_manager: McpClientManager,
) -> None:
    """Every mutator returns ``False`` (never raises) for an unknown
    server name."""
    manager = fake_peer_manager
    await manager.connect_all()
    assert await manager.remove_server("ghost") is False
    assert await manager.restart_one("ghost") is False
    assert await manager.enable_one("ghost") is False
    assert await manager.disable_one("ghost") is False
