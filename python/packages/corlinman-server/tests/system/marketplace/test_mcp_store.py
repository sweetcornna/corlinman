"""Tests for :class:`corlinman_server.system.marketplace.mcp_store.McpServerStore`.

Covers:
* Happy-path upsert → get → list round-trip.
* ``set_enabled`` toggling + ``delete`` returning a bool.
* Update (re-upsert) preserves ``installed_at`` and bumps ``updated_at``.
* Missing name → :class:`McpServerNotFound` on ``set_enabled``.
* Malformed names → :class:`McpServerInvalid`.
* Nested spec values round-trip through the JSON column.

All tests use ``tmp_path`` so they're parallel-safe.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
from corlinman_server.system.marketplace import mcp_store as mod
from corlinman_server.system.marketplace.mcp_store import (
    InstalledMcpServer,
    McpServerInvalid,
    McpServerNotFound,
    McpServerStore,
)


def _store(tmp_path: Path) -> McpServerStore:
    return McpServerStore(tmp_path / "mcp_servers.sqlite")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_upsert_get_list_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        assert store.list() == []
        assert store.get("ctx7") is None

        spec = {"transport": "stdio", "command": "uvx", "args": ["ctx7"]}
        installed = store.upsert(
            "ctx7", spec, source="github", version="1.2.3"
        )
        assert isinstance(installed, InstalledMcpServer)
        assert installed.name == "ctx7"
        assert installed.spec == spec
        assert installed.source == "github"
        assert installed.version == "1.2.3"
        assert installed.enabled is False
        assert installed.installed_at.tzinfo is not None
        assert installed.updated_at.tzinfo is not None

        fetched = store.get("ctx7")
        assert fetched is not None
        assert fetched.name == "ctx7"
        assert fetched.spec == spec
        assert fetched.source == "github"
        assert fetched.version == "1.2.3"

        assert [s.name for s in store.list()] == ["ctx7"]
    finally:
        store.close()


def test_upsert_defaults(tmp_path: Path) -> None:
    """source/version default to None, enabled defaults to False."""
    store = _store(tmp_path)
    try:
        installed = store.upsert("bare", {"command": "x"})
        assert installed.source is None
        assert installed.version is None
        assert installed.enabled is False
    finally:
        store.close()


def test_list_ordered_by_installed_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        base = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
        stamps = iter(
            [base + _dt.timedelta(minutes=i) for i in range(3)]
        )
        # Freeze time so each insert gets a strictly increasing stamp.
        original = mod._utc_now
        mod._utc_now = lambda: next(stamps)  # type: ignore[assignment]
        try:
            store.upsert("first", {"a": 1})
            store.upsert("second", {"a": 2})
            store.upsert("third", {"a": 3})
        finally:
            mod._utc_now = original  # type: ignore[assignment]

        assert [s.name for s in store.list()] == ["first", "second", "third"]
    finally:
        store.close()


def test_nested_spec_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        spec = {
            "transport": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer x", "X-Trace": "1"},
            "env": {"KEY": "v"},
            "args": ["--flag", "value", 42],
            "nested": {"deep": {"list": [1, {"k": "v"}, None]}},
            "enabled_default": True,
        }
        store.upsert("rich", spec)
        fetched = store.get("rich")
        assert fetched is not None
        assert fetched.spec == spec
    finally:
        store.close()


# ---------------------------------------------------------------------------
# set_enabled / delete
# ---------------------------------------------------------------------------


def test_set_enabled_toggles(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.upsert("toggle", {"command": "x"}, enabled=False)
        enabled = store.set_enabled("toggle", True)
        assert enabled.enabled is True
        assert store.get("toggle").enabled is True  # type: ignore[union-attr]

        disabled = store.set_enabled("toggle", False)
        assert disabled.enabled is False
    finally:
        store.close()


def test_set_enabled_missing_raises_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(McpServerNotFound):
            store.set_enabled("ghost", True)
    finally:
        store.close()


def test_delete_returns_true_then_false(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.upsert("ephemeral", {"command": "x"})
        assert store.delete("ephemeral") is True
        assert store.get("ephemeral") is None
        # Idempotent: second delete is a no-op.
        assert store.delete("ephemeral") is False
    finally:
        store.close()


def test_delete_missing_returns_false(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        assert store.delete("never-existed") is False
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Update semantics
# ---------------------------------------------------------------------------


def test_update_preserves_installed_at_and_bumps_updated_at(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        t0 = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.UTC)
        t1 = _dt.datetime(2026, 2, 2, 9, 30, 0, tzinfo=_dt.UTC)

        original = mod._utc_now
        mod._utc_now = lambda: t0  # type: ignore[assignment]
        try:
            first = store.upsert(
                "evolving", {"v": 1}, source="github", version="1.0.0"
            )
        finally:
            mod._utc_now = original  # type: ignore[assignment]

        assert first.installed_at == t0
        assert first.updated_at == t0

        mod._utc_now = lambda: t1  # type: ignore[assignment]
        try:
            second = store.upsert(
                "evolving",
                {"v": 2},
                source="clawhub",
                version="2.0.0",
                enabled=True,
            )
        finally:
            mod._utc_now = original  # type: ignore[assignment]

        # installed_at preserved from the first insert; updated_at bumped.
        assert second.installed_at == t0
        assert second.updated_at == t1
        # Spec + provenance reflect the latest upsert.
        assert second.spec == {"v": 2}
        assert second.source == "clawhub"
        assert second.version == "2.0.0"
        assert second.enabled is True

        # Still a single row.
        assert [s.name for s in store.list()] == ["evolving"]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "",                  # empty
        "with/slash",        # path separator
        "nul\x00byte",       # NUL byte
        "a/b/c",             # nested slashes
    ],
)
def test_invalid_names_raise_invalid_on_upsert(
    tmp_path: Path, bad_name: str
) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(McpServerInvalid):
            store.upsert(bad_name, {"command": "x"})
    finally:
        store.close()


@pytest.mark.parametrize(
    "method_name",
    ["get", "set_enabled", "delete"],
)
def test_invalid_names_raise_invalid_on_read_paths(
    tmp_path: Path, method_name: str
) -> None:
    store = _store(tmp_path)
    try:
        method = getattr(store, method_name)
        with pytest.raises(McpServerInvalid):
            if method_name == "set_enabled":
                method("bad/name", True)
            else:
                method("bad/name")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Persistence across reopen
# ---------------------------------------------------------------------------


def test_rows_survive_reopen(tmp_path: Path) -> None:
    db = tmp_path / "mcp_servers.sqlite"
    store = McpServerStore(db)
    store.upsert("persist", {"command": "x"}, source="github")
    store.close()

    reopened = McpServerStore(db)
    try:
        fetched = reopened.get("persist")
        assert fetched is not None
        assert fetched.spec == {"command": "x"}
        assert fetched.source == "github"
    finally:
        reopened.close()
