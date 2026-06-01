"""Tests for :class:`corlinman_server.system.marketplace.plugin_store.PluginStore`.

The plugin index is the marketplace analogue of the profile/MCP store: a
small sqlite3 + ``threading.Lock`` table tracking installed plugins (slug,
version, source, enabled toggle, provenance timestamps). These tests cover
the CRUD surface and the domain errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_server.system.marketplace.plugin_store import (
    InstalledPluginRow,
    PluginInvalid,
    PluginNotFound,
    PluginStore,
)


def _store(tmp_path: Path) -> PluginStore:
    return PluginStore(tmp_path / "plugins" / "index.sqlite")


def test_empty_store_lists_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.list() == []
    assert store.get("nope") is None


def test_upsert_inserts_row_with_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row = store.upsert("echo-plugin", version="1.0.0", source="github")
    assert isinstance(row, InstalledPluginRow)
    assert row.slug == "echo-plugin"
    assert row.version == "1.0.0"
    assert row.source == "github"
    # enabled defaults to False — a freshly installed plugin is inert.
    assert row.enabled is False
    assert row.installed_at  # ISO-8601 Z string
    assert row.updated_at
    assert "T" in row.installed_at

    # Round-trips through get + list.
    assert store.get("echo-plugin") == row
    listed = store.list()
    assert [r.slug for r in listed] == ["echo-plugin"]


def test_upsert_preserves_installed_at_on_reinstall(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.upsert("echo-plugin", version="1.0.0", source="github")
    second = store.upsert("echo-plugin", version="2.0.0", source="clawhub")
    # Version + source updated, installed_at preserved.
    assert second.version == "2.0.0"
    assert second.source == "clawhub"
    assert second.installed_at == first.installed_at
    # Only one row — upsert, not insert-twice.
    assert len(store.list()) == 1


def test_set_enabled_toggles_flag(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert("echo-plugin", version="1.0.0", source="github")
    enabled = store.set_enabled("echo-plugin", True)
    assert enabled.enabled is True
    assert store.get("echo-plugin").enabled is True
    disabled = store.set_enabled("echo-plugin", False)
    assert disabled.enabled is False


def test_set_enabled_unknown_slug_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(PluginNotFound):
        store.set_enabled("missing", True)


def test_delete_removes_row_and_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert("echo-plugin", version="1.0.0", source="github")
    assert store.delete("echo-plugin") is True
    assert store.get("echo-plugin") is None
    # Second delete is a no-op returning False.
    assert store.delete("echo-plugin") is False


@pytest.mark.parametrize("bad", ["", ".", "..", "foo/bar", "a\\b", "x\x00y"])
def test_invalid_slug_rejected(tmp_path: Path, bad: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(PluginInvalid):
        store.upsert(bad, version="1.0.0", source="github")


def test_persists_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "plugins" / "index.sqlite"
    store = PluginStore(db_path)
    store.upsert("echo-plugin", version="1.0.0", source="github")
    store.set_enabled("echo-plugin", True)
    store.close()

    reopened = PluginStore(db_path)
    row = reopened.get("echo-plugin")
    assert row is not None
    assert row.enabled is True
    assert row.version == "1.0.0"
    reopened.close()
