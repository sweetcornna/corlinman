"""Tests for :func:`corlinman_server.system.marketplace.plugin_installer`.

The installer materialises a downloaded plugin tarball (raw bytes, already
fetched by the route via a ``MarketplaceSource``) into
``<plugins_dir>/<slug>/`` with a ``.openclaw-meta.json`` provenance sidecar,
reusing the hardened tar extractor from the skill installer. We build real
``application/gzip`` tarballs in memory with :mod:`tarfile` and assert the
on-disk result, the force-overwrite path, the uninstall path, the
no-sidecar refusal, and the malicious-member rejection.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
from corlinman_server.system.marketplace.plugin_installer import (
    PluginAlreadyInstalledError,
    PluginInstallError,
    PluginInstallReport,
    UnsafeTarballError,
    install_plugin,
    uninstall_plugin,
)

# ---------------------------------------------------------------------------
# Tarball builders
# ---------------------------------------------------------------------------


def _build_tarball(members: list[tuple[str, bytes]]) -> bytes:
    """Return raw gzip bytes for a tarball containing the given members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _valid_plugin_tarball(
    *,
    plugin_name: str = "echo-plugin",
    manifest: bytes = b'{"name": "echo-plugin", "version": "1.0.0"}\n',
) -> bytes:
    return _build_tarball(
        [
            (f"{plugin_name}/manifest.json", manifest),
            (f"{plugin_name}/handler.py", b"def run():\n    return 'echo'\n"),
        ]
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_install_writes_bundle_and_sidecar(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    report = await install_plugin(
        plugins_dir=plugins_dir,
        content=_valid_plugin_tarball(),
        slug="echo-plugin",
        version="1.0.0",
        source="github",
    )

    assert isinstance(report, PluginInstallReport)
    assert report.slug == "echo-plugin"
    assert report.version == "1.0.0"
    assert report.files_written >= 1
    assert report.bytes_extracted > 0

    target = plugins_dir / "echo-plugin"
    assert target.is_dir()
    # Files land flat under <slug>/ (the wrapping dir was lifted up).
    assert (target / "manifest.json").is_file()
    assert (target / "handler.py").is_file()
    assert (target / ".openclaw-meta.json").is_file()
    assert report.target_path == target.resolve()


async def test_sidecar_records_provenance(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    await install_plugin(
        plugins_dir=plugins_dir,
        content=_valid_plugin_tarball(),
        slug="echo-plugin",
        version="2.1.0",
        source="clawhub",
    )
    sidecar = plugins_dir / "echo-plugin" / ".openclaw-meta.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["slug"] == "echo-plugin"
    assert payload["version"] == "2.1.0"
    assert payload["source"] == "clawhub"
    assert isinstance(payload["installed_at"], str)
    assert "T" in payload["installed_at"]


async def test_install_handles_flat_tarball(tmp_path: Path) -> None:
    """A tarball without the ``<slug>/`` wrapper still installs correctly."""
    plugins_dir = tmp_path / "plugins"
    payload = _build_tarball([("manifest.json", b'{"name": "flat"}\n')])
    await install_plugin(
        plugins_dir=plugins_dir,
        content=payload,
        slug="flat-plugin",
        version="1.0.0",
    )
    assert (plugins_dir / "flat-plugin" / "manifest.json").is_file()
    assert (plugins_dir / "flat-plugin" / ".openclaw-meta.json").is_file()


# ---------------------------------------------------------------------------
# Pre-existing target dir
# ---------------------------------------------------------------------------


async def test_install_refuses_existing_dir_without_force(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    target = plugins_dir / "echo-plugin"
    target.mkdir(parents=True)
    (target / "marker.txt").write_text("existing", encoding="utf-8")

    with pytest.raises(PluginAlreadyInstalledError):
        await install_plugin(
            plugins_dir=plugins_dir,
            content=_valid_plugin_tarball(),
            slug="echo-plugin",
        )
    # Pre-existing file untouched.
    assert (target / "marker.txt").read_text(encoding="utf-8") == "existing"


async def test_install_replaces_existing_dir_with_force(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    target = plugins_dir / "echo-plugin"
    target.mkdir(parents=True)
    (target / "marker.txt").write_text("existing", encoding="utf-8")

    await install_plugin(
        plugins_dir=plugins_dir,
        content=_valid_plugin_tarball(),
        slug="echo-plugin",
        force=True,
    )
    assert (target / "manifest.json").is_file()
    # Old marker gone after the force-replace.
    assert not (target / "marker.txt").exists()


# ---------------------------------------------------------------------------
# Tarball safety — reused hardened extractor rejects malicious members
# ---------------------------------------------------------------------------


async def test_install_rejects_path_traversal_member(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    payload = _build_tarball(
        [
            ("echo-plugin/manifest.json", b"{}\n"),
            ("../escape/evil", b"pwn"),
        ]
    )
    with pytest.raises(UnsafeTarballError):
        await install_plugin(
            plugins_dir=plugins_dir,
            content=payload,
            slug="echo-plugin",
        )
    # No partial extraction.
    assert not (plugins_dir / "echo-plugin").exists()
    assert not (tmp_path / "escape").exists()


async def test_install_rejects_absolute_path_member(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    payload = _build_tarball([("/etc/passwd", b"root:x:0:0\n")])
    with pytest.raises(UnsafeTarballError):
        await install_plugin(
            plugins_dir=plugins_dir,
            content=payload,
            slug="echo-plugin",
        )
    assert not (plugins_dir / "echo-plugin").exists()


@pytest.mark.parametrize("bad_slug", ["..", "foo/bar", "/abs"])
async def test_install_rejects_unsafe_slug(tmp_path: Path, bad_slug: str) -> None:
    plugins_dir = tmp_path / "plugins"
    with pytest.raises((ValueError, UnsafeTarballError)):
        await install_plugin(
            plugins_dir=plugins_dir,
            content=_valid_plugin_tarball(),
            slug=bad_slug,
        )


# ---------------------------------------------------------------------------
# uninstall_plugin
# ---------------------------------------------------------------------------


async def test_uninstall_removes_installed_plugin(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    await install_plugin(
        plugins_dir=plugins_dir,
        content=_valid_plugin_tarball(),
        slug="echo-plugin",
        version="1.0.0",
    )
    target = plugins_dir / "echo-plugin"
    assert target.is_dir()

    await uninstall_plugin(plugins_dir=plugins_dir, slug="echo-plugin")
    assert not target.exists()


async def test_uninstall_refuses_dir_without_sidecar(tmp_path: Path) -> None:
    """A plugin dir lacking the sidecar is treated as bundled / hand-placed
    and must not be ``rm -rf``'d."""
    plugins_dir = tmp_path / "plugins"
    target = plugins_dir / "bundled-plugin"
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(PluginInstallError):
        await uninstall_plugin(plugins_dir=plugins_dir, slug="bundled-plugin")
    # Directory still present.
    assert (target / "manifest.json").is_file()


async def test_uninstall_missing_plugin_raises(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir(parents=True)
    with pytest.raises(PluginInstallError):
        await uninstall_plugin(plugins_dir=plugins_dir, slug="ghost")
