"""Factory MCP-server seeding.

The property that matters is "seeded once, ever" — not "insert if absent".
A server the operator deleted is absent, so an insert-if-absent seeder would
silently reinstall it on every restart, which is the gateway overruling the
operator once per boot.
"""

from __future__ import annotations

import json
from pathlib import Path

from corlinman_server.gateway.lifecycle.bundled_mcp import (
    BUNDLED_MCP_SERVERS,
    FREE_SEARCH_MIN_VERSION,
    BundledMcpServer,
    seed_bundled_mcp_servers,
)
from corlinman_server.system.marketplace.mcp_store import McpServerStore


def _store(tmp_path: Path) -> McpServerStore:
    return McpServerStore(tmp_path / "mcp_servers.sqlite")


# ─── the bundle itself ───────────────────────────────────────────────


def test_bundle_pins_a_minimum_free_search_version() -> None:
    """0.8.0 closed three SSRF guard bypasses; the spec must never resolve
    to an earlier release, since these tools fetch model-chosen URLs."""
    entry = next(e for e in BUNDLED_MCP_SERVERS if e.name == "search")
    assert entry.spec["command"] == "uvx"
    assert entry.spec["args"] == [f"free-search-mcp>={FREE_SEARCH_MIN_VERSION}"]
    assert FREE_SEARCH_MIN_VERSION == "0.8.0"


def test_bundled_spec_parses_as_a_server_spec() -> None:
    """The seeded mapping has to survive the same parser the boot path runs
    it through, or the row lands in the store and dies at registration."""
    from corlinman_mcp_server import McpServerSpec

    for entry in BUNDLED_MCP_SERVERS:
        spec = McpServerSpec.from_mapping(entry.name, entry.spec)
        assert spec.transport == "stdio"
        assert spec.command


# ─── seeding ─────────────────────────────────────────────────────────


def test_seeds_on_first_boot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    seeded = seed_bundled_mcp_servers(store, tmp_path)
    assert seeded == ["search"]
    row = store.get("search")
    assert row is not None
    assert row.enabled is True
    assert row.source == "bundled"


def test_second_boot_seeds_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    seed_bundled_mcp_servers(store, tmp_path)
    assert seed_bundled_mcp_servers(store, tmp_path) == []


def test_a_deleted_server_is_not_resurrected(tmp_path: Path) -> None:
    """The whole reason the sentinel exists."""
    store = _store(tmp_path)
    seed_bundled_mcp_servers(store, tmp_path)
    assert store.delete("search") is True

    assert seed_bundled_mcp_servers(store, tmp_path) == []
    assert store.get("search") is None


def test_operator_edits_are_not_overwritten(tmp_path: Path) -> None:
    """A reconfigured factory server stays reconfigured across boots."""
    store = _store(tmp_path)
    seed_bundled_mcp_servers(store, tmp_path)
    store.upsert("search", {"transport": "stdio", "command": "my-fork"}, enabled=False)

    seed_bundled_mcp_servers(store, tmp_path)
    row = store.get("search")
    assert row is not None
    assert row.spec["command"] == "my-fork"
    assert row.enabled is False


def test_an_existing_row_is_never_overwritten(tmp_path: Path) -> None:
    """No sentinel yet, but a row already exists — either the sentinel could
    not be written (read-only data dir) or the operator installed something
    under this name by hand. Overwriting would clobber their spec on every
    single boot, which is the worst version of this bug."""
    store = _store(tmp_path)
    store.upsert("search", {"transport": "stdio", "command": "mine"}, enabled=False)

    assert seed_bundled_mcp_servers(store, tmp_path) == []
    row = store.get("search")
    assert row is not None
    assert row.spec["command"] == "mine"
    assert row.enabled is False
    # …and it is recorded as offered, so the probe stops happening.
    sentinel = json.loads(
        (tmp_path / ".mcp_bundled_seeded.json").read_text(encoding="utf-8")
    )
    assert sentinel["seeded"] == ["search"]


def test_enabled_override_seeds_inert(tmp_path: Path) -> None:
    """``[mcp].bundle_enabled = false`` registers the server without
    letting anything spawn."""
    store = _store(tmp_path)
    seed_bundled_mcp_servers(store, tmp_path, enabled_override=False)
    row = store.get("search")
    assert row is not None
    assert row.enabled is False


def test_sentinel_records_every_offered_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    seed_bundled_mcp_servers(store, tmp_path)
    sentinel = json.loads(
        (tmp_path / ".mcp_bundled_seeded.json").read_text(encoding="utf-8")
    )
    assert sentinel["seeded"] == ["search"]


# ─── degradation ─────────────────────────────────────────────────────


def test_corrupt_sentinel_reads_as_nothing_seeded(tmp_path: Path) -> None:
    """Re-offering a deleted server once is recoverable; permanently
    suppressing the bundle on a fresh install is invisible. Prefer the
    recoverable failure."""
    (tmp_path / ".mcp_bundled_seeded.json").write_text("{not json", encoding="utf-8")
    store = _store(tmp_path)
    assert seed_bundled_mcp_servers(store, tmp_path) == ["search"]


def test_a_failing_entry_does_not_block_the_rest(tmp_path: Path) -> None:
    """One malformed bundle entry must not cost the others."""
    store = _store(tmp_path)
    bundle = (
        BundledMcpServer(name="bad/name", spec={}, version="1"),
        BundledMcpServer(name="good", spec={"command": "x"}, version="1"),
    )
    assert seed_bundled_mcp_servers(store, tmp_path, bundle=bundle) == ["good"]


def test_seeding_survives_an_unwritable_sentinel(tmp_path: Path, monkeypatch) -> None:
    """A read-only data dir costs idempotence, not the boot."""
    store = _store(tmp_path)

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", _boom)
    assert seed_bundled_mcp_servers(store, tmp_path) == ["search"]
    assert store.get("search") is not None
