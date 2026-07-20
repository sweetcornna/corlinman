"""Client-side MCP resources (Dim 5): ``resources/list`` discovery +
``resources/read`` routing.

Unit-level against an in-process fake peer — the stdio integration path
is already covered by ``test_client_manager.py``; these pin the new
surface: paged discovery, capability-less degradation, content folding
(text + blob placeholder), and the never-raise error envelopes.
"""

from __future__ import annotations

from typing import Any

import pytest
from corlinman_mcp_server.client_manager import (
    McpClientManager,
    McpManagedServer,
    McpServerSpec,
)

pytestmark = pytest.mark.asyncio


class _FakePeer:
    """Answers ``resources/list`` / ``resources/read`` from canned data."""

    on_server_request: Any = None
    on_notification: Any = None

    def __init__(
        self,
        pages: list[dict[str, Any]] | None = None,
        contents: dict[str, list[dict[str, Any]]] | None = None,
        *,
        fail_list: bool = False,
    ) -> None:
        self.pages = pages or []
        self.contents = contents or {}
        self.fail_list = fail_list
        self.calls: list[tuple[str, Any]] = []

    async def call(self, method: str, params: Any = None) -> Any:
        self.calls.append((method, params))
        if method == "resources/list":
            if self.fail_list:
                raise RuntimeError("method not found: resources/list")
            cursor = (params or {}).get("cursor") if params else None
            idx = int(cursor) if cursor else 0
            page = dict(self.pages[idx])
            if idx + 1 < len(self.pages):
                page["nextCursor"] = str(idx + 1)
            return page
        if method == "resources/read":
            uri = (params or {}).get("uri")
            if uri not in self.contents:
                raise RuntimeError(f"unknown resource: {uri}")
            return {"contents": self.contents[uri]}
        raise AssertionError(f"unexpected method {method}")

    async def notify(self, method: str, params: Any = None) -> None:
        pass

    async def close(self) -> None:
        pass


def _manager_with(peer: _FakePeer, name: str = "srv") -> McpClientManager:
    manager = McpClientManager([])
    row = McpManagedServer(
        spec=McpServerSpec(name=name), status="ready", peer=peer
    )
    manager._servers[name] = row  # noqa: SLF001 — test fixture
    return manager


async def test_list_resources_pages_until_exhausted() -> None:
    peer = _FakePeer(
        pages=[
            {"resources": [{"uri": "corl://a", "name": "a"}]},
            {"resources": [{"uri": "corl://b", "name": "b"}]},
        ]
    )
    manager = McpClientManager([])
    out = await manager._list_resources(peer)  # noqa: SLF001
    assert [r.uri for r in out] == ["corl://a", "corl://b"]


async def test_list_resources_unsupported_yields_empty() -> None:
    peer = _FakePeer(fail_list=True)
    manager = McpClientManager([])
    assert await manager._list_resources(peer) == []  # noqa: SLF001


async def test_read_resource_folds_text_and_blob() -> None:
    peer = _FakePeer(
        contents={
            "corl://doc": [
                {"uri": "corl://doc", "text": "hello"},
                {"uri": "corl://doc.bin", "blob": "AAAA", "mimeType": "image/png"},
                {"uri": "corl://doc2", "text": "world"},
            ]
        }
    )
    manager = _manager_with(peer)
    outcome = await manager.read_resource("srv", "corl://doc")
    assert outcome.is_error is False
    assert "hello" in outcome.content
    assert "world" in outcome.content
    assert "binary resource corl://doc.bin (image/png) omitted" in outcome.content


async def test_read_resource_unknown_server_is_clean_error() -> None:
    manager = McpClientManager([])
    outcome = await manager.read_resource("ghost", "corl://x")
    assert outcome.is_error is True
    assert "mcp_server_not_found" in outcome.content


async def test_read_resource_not_ready_is_clean_error() -> None:
    manager = McpClientManager([])
    manager._servers["down"] = McpManagedServer(  # noqa: SLF001
        spec=McpServerSpec(name="down"), status="error", error="boom"
    )
    outcome = await manager.read_resource("down", "corl://x")
    assert outcome.is_error is True
    assert "mcp_server_unavailable" in outcome.content


async def test_read_resource_missing_uri_is_clean_error() -> None:
    manager = _manager_with(_FakePeer())
    outcome = await manager.read_resource("srv", "")
    assert outcome.is_error is True
    assert "mcp_resource_uri_missing" in outcome.content


async def test_read_resource_server_error_is_clean_error() -> None:
    manager = _manager_with(_FakePeer(contents={}))
    outcome = await manager.read_resource("srv", "corl://missing")
    assert outcome.is_error is True
    assert "mcp_call_failed" in outcome.content


async def test_discovered_resources_only_ready_and_nonempty() -> None:
    manager = McpClientManager([])
    ready = McpManagedServer(
        spec=McpServerSpec(name="ready"), status="ready", peer=_FakePeer()
    )
    from corlinman_mcp_server.types import Resource

    ready.resources = [Resource(uri="corl://a", name="a")]
    empty = McpManagedServer(
        spec=McpServerSpec(name="empty"), status="ready", peer=_FakePeer()
    )
    down = McpManagedServer(spec=McpServerSpec(name="down"), status="error")
    down.resources = [Resource(uri="corl://hidden", name="h")]
    manager._servers.update(  # noqa: SLF001
        {"ready": ready, "empty": empty, "down": down}
    )
    discovered = manager.discovered_resources()
    assert set(discovered) == {"ready"}
    assert [r.uri for r in discovered["ready"]] == ["corl://a"]
