"""Client-side tools/list_changed listener + debounce (Dim 5).

When an external MCP server pushes ``notifications/tools/list_changed``,
the client re-lists that server's tools (debounced to coalesce bursts) and
fires ``on_tools_changed`` so the gateway re-advertises the tool plane.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from corlinman_mcp_server import McpClientManager, McpServerSpec
from corlinman_mcp_server.types import TOOLS_LIST_CHANGED_NOTIFICATION


class _ReListPeer:
    """FakePeer whose ``tools/list`` returns a different set each call."""

    def __init__(self) -> None:
        self.closed = False
        self.list_calls = 0
        self.on_server_request = None
        self.on_notification = None

    async def call(self, method: str, params: Any = None) -> Any:
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "capabilities": {}}
        if method == "tools/list":
            self.list_calls += 1
            name = "echo" if self.list_calls == 1 else "echo2"
            return {"tools": [{"name": name, "description": "", "inputSchema": {"type": "object"}}]}
        raise AssertionError(f"unexpected method {method!r}")

    async def notify(self, method: str, params: Any = None) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def relist_manager(monkeypatch: pytest.MonkeyPatch) -> McpClientManager:
    peer = _ReListPeer()

    async def _fake_connect_peer(self: McpClientManager, spec: McpServerSpec) -> _ReListPeer:
        return peer

    monkeypatch.setattr(McpClientManager, "_connect_peer", _fake_connect_peer)
    mgr = McpClientManager([])
    mgr._list_changed_debounce_ms = 20  # fast for tests
    mgr._test_peer = peer  # type: ignore[attr-defined]
    return mgr


def _spec(name: str = "srv") -> McpServerSpec:
    return McpServerSpec(name=name, transport="stdio", command="fake")


async def _connected(mgr: McpClientManager) -> McpClientManager:
    await mgr.connect_all()  # flips _connected so add_server brings up live
    return mgr


@pytest.mark.asyncio
async def test_list_changed_relists_and_fires_callback(relist_manager: McpClientManager) -> None:
    fired: list[bool] = []
    relist_manager.on_tools_changed = lambda: fired.append(True)
    await _connected(relist_manager)
    await relist_manager.add_server(_spec(), replace=True)
    server = relist_manager.server("srv")
    assert server is not None and server.is_ready
    assert [t.name for t in server.tools] == ["echo"]

    # Simulate the server pushing list_changed.
    relist_manager._schedule_list_changed_refresh("srv")
    await asyncio.sleep(0.1)

    assert [t.name for t in server.tools] == ["echo2"]  # re-listed
    assert fired == [True]


@pytest.mark.asyncio
async def test_list_changed_debounces_burst(relist_manager: McpClientManager) -> None:
    fired: list[bool] = []
    relist_manager.on_tools_changed = lambda: fired.append(True)
    await _connected(relist_manager)
    await relist_manager.add_server(_spec(), replace=True)
    peer = relist_manager._test_peer  # type: ignore[attr-defined]
    calls_before = peer.list_calls

    # A burst of 5 notifications within the debounce window → one refresh.
    for _ in range(5):
        relist_manager._schedule_list_changed_refresh("srv")
    await asyncio.sleep(0.1)

    assert peer.list_calls - calls_before == 1  # coalesced
    assert fired == [True]


@pytest.mark.asyncio
async def test_notification_handler_recognizes_list_changed(relist_manager: McpClientManager) -> None:
    fired: list[bool] = []
    relist_manager.on_tools_changed = lambda: fired.append(True)
    await _connected(relist_manager)
    await relist_manager.add_server(_spec(), replace=True)
    peer = relist_manager._test_peer  # type: ignore[attr-defined]
    assert peer.on_notification is not None

    # An unrelated notification does nothing; the list_changed one refreshes.
    await peer.on_notification("notifications/something_else", {})
    await asyncio.sleep(0.05)
    assert fired == []

    await peer.on_notification(TOOLS_LIST_CHANGED_NOTIFICATION, {})
    await asyncio.sleep(0.1)
    assert fired == [True]


@pytest.mark.asyncio
async def test_notification_during_relist_queues_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A notification arriving WHILE a relist is in flight must not cancel
    it — it queues exactly one follow-up refresh (Codex #110)."""
    started = asyncio.Event()
    release = asyncio.Event()
    relist_calls = [0]

    class _SlowPeer:
        on_server_request = None
        on_notification = None

        async def call(self, method: str, params: Any = None) -> Any:
            if method == "initialize":
                return {"protocolVersion": "2024-11-05", "capabilities": {}}
            if method == "tools/list":
                relist_calls[0] += 1
                if relist_calls[0] >= 2:  # the second (relist) call blocks
                    started.set()
                    await release.wait()
                return {"tools": [{"name": "echo", "description": "", "inputSchema": {"type": "object"}}]}
            raise AssertionError(method)

        async def notify(self, *a, **k):
            return None

        async def close(self):
            return None

    peer = _SlowPeer()

    async def _fake_connect(self, spec):
        return peer

    monkeypatch.setattr(McpClientManager, "_connect_peer", _fake_connect)
    mgr = McpClientManager([])
    mgr._list_changed_debounce_ms = 5
    await mgr.connect_all()
    fired: list[int] = []
    mgr.on_tools_changed = lambda: fired.append(1)
    await mgr.add_server(_spec(), replace=True)

    # First notification → debounced relist starts and blocks in tools/list.
    mgr._schedule_list_changed_refresh("srv")
    await asyncio.wait_for(started.wait(), timeout=1)
    assert "srv" in mgr._list_changed_committed  # past the cancel point

    # Second notification mid-relist → must NOT cancel; queues a follow-up.
    mgr._schedule_list_changed_refresh("srv")
    assert "srv" in mgr._list_changed_redo

    release.set()  # let the in-flight relist finish → follow-up runs
    await asyncio.sleep(0.1)
    assert fired  # at least the first refresh landed (not aborted)


@pytest.mark.asyncio
async def test_relist_bounded_by_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that stalls on the follow-up tools/list must not hang the
    debounce task forever (Codex #110)."""

    class _StallPeer:
        on_server_request = None
        on_notification = None

        async def call(self, method: str, params: Any = None) -> Any:
            if method == "initialize":
                return {"protocolVersion": "2024-11-05", "capabilities": {}}
            if method == "tools/list":
                if getattr(self, "_listed", False):
                    await asyncio.sleep(30)  # stall the relist
                self._listed = True
                return {"tools": [{"name": "echo", "description": "", "inputSchema": {"type": "object"}}]}
            raise AssertionError(method)

        async def notify(self, *a, **k):
            return None

        async def close(self):
            return None

    peer = _StallPeer()

    async def _fake_connect(self, spec):
        return peer

    monkeypatch.setattr(McpClientManager, "_connect_peer", _fake_connect)
    mgr = McpClientManager([])
    mgr._list_changed_debounce_ms = 5
    await mgr.connect_all()
    fired: list[int] = []
    mgr.on_tools_changed = lambda: fired.append(1)
    await mgr.add_server(_spec(), replace=True)
    mgr.server("srv").spec.handshake_timeout_s = 0.1  # tight bound

    mgr._schedule_list_changed_refresh("srv")
    await asyncio.sleep(0.3)  # > timeout — the relist gives up, task clears
    assert "srv" not in mgr._list_changed_tasks
    assert fired == []  # timed-out relist never fired the callback


@pytest.mark.asyncio
async def test_teardown_cancels_pending_debounce(relist_manager: McpClientManager) -> None:
    fired: list[bool] = []
    relist_manager.on_tools_changed = lambda: fired.append(True)
    await _connected(relist_manager)
    await relist_manager.add_server(_spec(), replace=True)
    relist_manager._schedule_list_changed_refresh("srv")
    # Remove the server before the debounce fires.
    await relist_manager.remove_server("srv")
    await asyncio.sleep(0.1)
    assert fired == []  # cancelled, no stale refresh
