"""Tests for :class:`McpAdapter` — the live-manager ↔ store bridge.

Focus of this suite (the bug + the new feature):

* **Config-row toggle durability.** Enabling/disabling a *config-declared*
  server (one the live manager knows but the store does not) must
  *materialise* a store row carrying the captured launch spec, so the
  toggle survives a restart instead of silently reverting. Previously the
  ``McpServerNotFound`` from ``set_enabled`` was swallowed at debug and no
  row was written.
* **Reconfigure.** ``reconfigure`` edits an installed/config server's
  launch spec in place (env/version/command/url), persists the merged
  spec, and re-registers it live.

The adapter is exercised against the *real* :class:`McpServerStore` (sqlite
on ``tmp_path``) and a tiny in-memory fake of the MCP client manager that
mirrors the bits the adapter touches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_b.marketplace.mcp_adapter import (
    McpAdapter,
)
from corlinman_server.system.marketplace.mcp_store import McpServerStore

# ---------------------------------------------------------------------------
# Fakes mirroring the corlinman_mcp_server surface the adapter touches.
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
    status: str = "pending"
    tools: list[Any] = field(default_factory=list)
    error: str | None = None


class _FakeManager:
    """Minimal stand-in for ``McpClientManager``.

    Only implements what the adapter calls: ``servers()``,
    ``enable_one`` / ``disable_one`` (flip ``spec.enabled``), and
    ``add_server`` (register/replace by name).
    """

    def __init__(self, specs: list[_FakeSpec]) -> None:
        self._servers: dict[str, _FakeManaged] = {
            s.name: _FakeManaged(spec=s) for s in specs
        }

    def servers(self) -> list[_FakeManaged]:
        return list(self._servers.values())

    async def enable_one(self, name: str) -> bool:
        m = self._servers.get(name)
        if m is None:
            return False
        m.spec.enabled = True
        m.status = "ready"
        return True

    async def disable_one(self, name: str) -> bool:
        m = self._servers.get(name)
        if m is None:
            return False
        m.spec.enabled = False
        m.status = "error"
        m.error = "disabled"
        return True

    async def add_server(self, spec: Any, *, replace: bool = False) -> Any:
        # The adapter constructs a real McpServerSpec; we just record the
        # final enabled state + key fields under the name.
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


def _store(tmp_path: Path) -> McpServerStore:
    return McpServerStore(tmp_path / "mcp_servers.sqlite")


# ---------------------------------------------------------------------------
# Config-row toggle durability — the silent-revert bug.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_config_server_materialises_store_row(
    tmp_path: Path,
) -> None:
    """Disabling a config-declared server must persist a store row so the
    toggle survives a restart (the row's enabled flag wins on boot)."""
    store = _store(tmp_path)
    try:
        spec = _FakeSpec(
            name="cfg-fs",
            command="fs-server",
            args=["--root", "/data"],
            env={"TOKEN": "abc"},
            enabled=True,
        )
        adapter = McpAdapter(_FakeManager([spec]), store)

        # Precondition: no persisted row (purely config-declared).
        assert store.get("cfg-fs") is None

        existed = await adapter.disable_one("cfg-fs")
        assert existed is True

        # A row now exists, disabled, tagged source="config", with the
        # captured launch spec so boot can re-register it.
        row = store.get("cfg-fs")
        assert row is not None
        assert row.enabled is False
        assert row.source == "config"
        assert row.spec.get("command") == "fs-server"
        assert row.spec.get("env") == {"TOKEN": "abc"}
        # The persisted enabled flag in the spec matches the row flag.
        assert row.spec.get("enabled") is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_enable_config_server_then_reverts_to_enabled_on_reboot(
    tmp_path: Path,
) -> None:
    """A config server that boots disabled, then is enabled via the UI,
    keeps the enabled state durably (store row overrides config)."""
    store = _store(tmp_path)
    try:
        spec = _FakeSpec(name="cfg-web", url="ws://x", transport="ws",
                         enabled=False)
        adapter = McpAdapter(_FakeManager([spec]), store)
        await adapter.enable_one("cfg-web")

        row = store.get("cfg-web")
        assert row is not None
        assert row.enabled is True
        assert row.spec.get("url") == "ws://x"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_toggle_installed_server_uses_cheap_set_enabled(
    tmp_path: Path,
) -> None:
    """An already-installed server (store row present) flips via the cheap
    set_enabled path — no spec re-capture, installed_at preserved."""
    store = _store(tmp_path)
    try:
        store.upsert(
            "github",
            {"transport": "stdio", "command": "gh-mcp", "enabled": False},
            source="github",
            version="1.0.0",
            enabled=False,
        )
        installed_at = store.get("github").installed_at  # type: ignore[union-attr]
        spec = _FakeSpec(name="github", command="gh-mcp", enabled=False)
        adapter = McpAdapter(_FakeManager([spec]), store)

        await adapter.enable_one("github")
        row = store.get("github")
        assert row is not None
        assert row.enabled is True
        assert row.source == "github"  # provenance untouched
        assert row.version == "1.0.0"
        assert row.installed_at == installed_at
    finally:
        store.close()


@pytest.mark.asyncio
async def test_toggle_unknown_server_no_store_row_written(
    tmp_path: Path,
) -> None:
    """Toggling a name unknown to both halves writes nothing (no crash)."""
    store = _store(tmp_path)
    try:
        adapter = McpAdapter(_FakeManager([]), store)
        existed = await adapter.enable_one("ghost")
        assert existed is False
        assert store.get("ghost") is None
        assert store.list() == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Reconfigure.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfigure_installed_server_merges_and_persists(
    tmp_path: Path,
) -> None:
    """Reconfigure replaces env wholesale + bumps version, leaves the
    command untouched (absent from patch), and preserves enabled."""
    store = _store(tmp_path)
    try:
        store.upsert(
            "github",
            {
                "transport": "stdio",
                "command": "gh-mcp",
                "args": ["--stdio"],
                "env": {"GITHUB_TOKEN": "old"},
                "enabled": True,
            },
            source="github",
            version="1.0.0",
            enabled=True,
        )
        mgr = _FakeManager([_FakeSpec(name="github", command="gh-mcp",
                                      enabled=True)])
        adapter = McpAdapter(mgr, store)

        row = await adapter.reconfigure(
            "github",
            {"env": {"GITHUB_TOKEN": "new", "GITHUB_ORG": "acme"}},
            version="1.2.0",
        )
        assert row is not None

        got = store.get("github")
        assert got is not None
        assert got.version == "1.2.0"
        assert got.enabled is True  # preserved
        assert got.source == "github"  # provenance preserved
        # env replaced wholesale; command untouched.
        assert got.spec.get("env") == {"GITHUB_TOKEN": "new", "GITHUB_ORG": "acme"}
        assert got.spec.get("command") == "gh-mcp"

        # Live manager re-registered with the new env.
        live = {s.spec.name: s for s in mgr.servers()}
        assert live["github"].spec.env == {
            "GITHUB_TOKEN": "new",
            "GITHUB_ORG": "acme",
        }
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reconfigure_config_server_materialises_row(
    tmp_path: Path,
) -> None:
    """Reconfigure of a config-declared server (no store row yet) captures
    the live spec, applies the patch, and persists it as source=config."""
    store = _store(tmp_path)
    try:
        spec = _FakeSpec(name="cfg-fs", command="fs-server",
                         env={"A": "1"}, enabled=True)
        adapter = McpAdapter(_FakeManager([spec]), store)
        assert store.get("cfg-fs") is None

        await adapter.reconfigure("cfg-fs", {"command": "fs-v2"})

        got = store.get("cfg-fs")
        assert got is not None
        assert got.source == "config"
        assert got.spec.get("command") == "fs-v2"
        # Untouched env carried over from the live spec.
        assert got.spec.get("env") == {"A": "1"}
        assert got.enabled is True
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reconfigure_unknown_server_raises_keyerror(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        adapter = McpAdapter(_FakeManager([]), store)
        with pytest.raises(KeyError):
            await adapter.reconfigure("ghost", {"command": "x"})
    finally:
        store.close()
