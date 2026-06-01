"""Tests for live plugin hot-load (``plugin_runtime.sync_registry``)."""

from __future__ import annotations

from pathlib import Path

from corlinman_providers.plugins import PluginRegistry
from corlinman_server.system.marketplace.plugin_runtime import (
    make_reload_hook,
    sync_registry,
)

_MANIFEST = """\
manifest_version = 1
name = "{name}"
version = "0.1.0"
plugin_type = "sync"

[entry_point]
command = "python3"
args = ["run.py"]

[[capabilities.tools]]
name = "{tool}"
description = "demo"
"""


def _install(plugins_dir: Path, slug: str, *, name: str, tool: str) -> None:
    d = plugins_dir / slug
    d.mkdir(parents=True)
    (d / "plugin-manifest.toml").write_text(
        _MANIFEST.format(name=name, tool=tool), encoding="utf-8"
    )
    (d / "run.py").write_text("print('hi')\n", encoding="utf-8")


async def test_sync_registry_enables_and_disables(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    _install(plugins_dir, "echo-plugin", name="echo-plugin", tool="echo")
    registry = PluginRegistry.from_roots([])
    assert registry.get("echo-plugin") is None

    # Enable -> present in the live registry.
    out = await sync_registry(registry, plugins_dir, {"echo-plugin"})
    assert out["loaded"] == ["echo-plugin"]
    assert registry.get("echo-plugin") is not None

    # Disable -> removed from the live registry.
    out = await sync_registry(registry, plugins_dir, set())
    assert "echo-plugin" in out["removed"]
    assert registry.get("echo-plugin") is None


async def test_sync_registry_slug_differs_from_manifest_name(
    tmp_path: Path,
) -> None:
    # The store keys on the install-dir slug; the manifest name may differ.
    plugins_dir = tmp_path / "plugins"
    _install(plugins_dir, "my-slug", name="fancy-name", tool="do")
    registry = PluginRegistry.from_roots([])

    await sync_registry(registry, plugins_dir, {"my-slug"})
    assert registry.get("fancy-name") is not None  # keyed by manifest name

    await sync_registry(registry, plugins_dir, set())
    assert registry.get("fancy-name") is None


async def test_reload_hook_reads_enabled_provider(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    _install(plugins_dir, "p1", name="p1", tool="t1")
    registry = PluginRegistry.from_roots([])
    enabled: set[str] = set()
    hook = make_reload_hook(registry, plugins_dir, lambda: enabled)

    await hook()
    assert registry.get("p1") is None
    enabled.add("p1")
    await hook()
    assert registry.get("p1") is not None


async def test_sync_registry_missing_manifest_is_skipped(tmp_path: Path) -> None:
    # A bundle with no plugin-manifest.toml installs but won't hot-load.
    plugins_dir = tmp_path / "plugins"
    d = plugins_dir / "jsononly"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{}", encoding="utf-8")
    registry = PluginRegistry.from_roots([])

    out = await sync_registry(registry, plugins_dir, {"jsononly"})
    assert out["loaded"] == []
    assert registry.is_empty()
