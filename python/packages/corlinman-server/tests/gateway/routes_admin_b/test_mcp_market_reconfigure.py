"""Tests for ``PUT /admin/mcp/{name}`` — the reconfigure route.

The route resolves the :class:`McpAdapter` off ``AdminState.extras``
(``"mcp_adapter"``) and forwards only the explicitly-set body fields to
``adapter.reconfigure``. Coverage:

* A spec edit (env + version) round-trips through the real
  :class:`McpServerStore` and is reflected in the route response.
* An absent field leaves that part of the spec unchanged (only the set
  keys are forwarded).
* An unknown server → 404 ``mcp_not_found``.
* No adapter wired → 503 ``mcp_adapter_disabled``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corlinman_server.gateway.routes_admin_b.marketplace import mcp_market
from corlinman_server.gateway.routes_admin_b.marketplace.mcp_adapter import (
    McpAdapter,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from corlinman_server.system.marketplace.mcp_store import McpServerStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

# ---------------------------------------------------------------------------
# Minimal manager fake (mirrors test_mcp_adapter).
# ---------------------------------------------------------------------------


@dataclass
class _FakeSpec:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    handshake_timeout_s: float = 10.0
    call_timeout_s: float = 30.0


@dataclass
class _FakeManaged:
    spec: _FakeSpec
    status: str = "ready"
    tools: list[Any] = field(default_factory=list)
    error: str | None = None


class _FakeManager:
    def __init__(self, specs: list[_FakeSpec]) -> None:
        self._servers = {s.name: _FakeManaged(spec=s) for s in specs}

    def servers(self) -> list[_FakeManaged]:
        return list(self._servers.values())

    async def add_server(self, spec: Any, *, replace: bool = False) -> Any:
        m = _FakeManaged(
            spec=_FakeSpec(
                name=spec.name,
                transport=getattr(spec, "transport", "stdio"),
                command=getattr(spec, "command", ""),
                args=list(getattr(spec, "args", []) or []),
                env=dict(getattr(spec, "env", {}) or {}),
                url=getattr(spec, "url", ""),
                headers=dict(getattr(spec, "headers", {}) or {}),
                enabled=bool(getattr(spec, "enabled", False)),
            )
        )
        self._servers[spec.name] = m
        return m


def _client(state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(mcp_market.router())
    return authenticated_test_client(app)


def _state_with_adapter(
    tmp_path: Path, adapter: McpAdapter | None
) -> AdminState:
    state = AdminState(data_dir=tmp_path)
    configure_admin_auth(state)
    if adapter is not None:
        state.extras["mcp_adapter"] = adapter
    set_admin_state(state)
    return state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_put_reconfigure_round_trips(tmp_path: Path) -> None:
    store = McpServerStore(tmp_path / "mcp_servers.sqlite")
    try:
        store.upsert(
            "github",
            {
                "transport": "stdio",
                "command": "gh-mcp",
                "env": {"GITHUB_TOKEN": "old"},
                "enabled": True,
            },
            source="github",
            version="1.0.0",
            enabled=True,
        )
        mgr = _FakeManager(
            [_FakeSpec(name="github", command="gh-mcp", enabled=True)]
        )
        adapter = McpAdapter(mgr, store)
        state = _state_with_adapter(tmp_path, adapter)
        try:
            client = _client(state)
            resp = client.put(
                "/admin/mcp/github",
                json={"env": {"GITHUB_TOKEN": "new"}, "version": "2.0.0"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["name"] == "github"
            assert body["version"] == "2.0.0"
            assert body["enabled"] is True

            # Durable: the store reflects the new env + version, command
            # left intact (absent from the patch).
            got = store.get("github")
            assert got is not None
            assert got.version == "2.0.0"
            assert got.spec.get("env") == {"GITHUB_TOKEN": "new"}
            assert got.spec.get("command") == "gh-mcp"
        finally:
            set_admin_state(None)
    finally:
        store.close()


def test_put_reconfigure_unknown_server_404(tmp_path: Path) -> None:
    store = McpServerStore(tmp_path / "mcp_servers.sqlite")
    try:
        adapter = McpAdapter(_FakeManager([]), store)
        state = _state_with_adapter(tmp_path, adapter)
        try:
            client = _client(state)
            resp = client.put(
                "/admin/mcp/ghost", json={"command": "x"}
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"] == "mcp_not_found"
        finally:
            set_admin_state(None)
    finally:
        store.close()


def test_put_reconfigure_no_adapter_503(tmp_path: Path) -> None:
    state = _state_with_adapter(tmp_path, None)
    try:
        client = _client(state)
        resp = client.put("/admin/mcp/github", json={"command": "x"})
        assert resp.status_code == 503, resp.text
        assert resp.json()["error"] == "mcp_adapter_disabled"
    finally:
        set_admin_state(None)
