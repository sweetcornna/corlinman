"""Unit tests for the shared version resolver.

Covers the precedence chain in
:func:`corlinman_server.system.app_version.resolve_app_version` and its
helpers. The resolver is the single source of truth every version reader
(updater, ``/healthz``, telemetry, MCP) routes through, so a regression
here is exactly the "updater stuck on the old version" bug.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest
from corlinman_server.system import app_version
from corlinman_server.system.app_version import (
    DEV_FALLBACK_VERSION,
    _looks_like_version,
    _normalize,
    _read_root_version_from,
    resolve_app_version,
)


def _write_pyproject(path: Path, *, name: str, version: str) -> None:
    path.write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n',
        encoding="utf-8",
    )


class TestNormalizeAndLooksLikeVersion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("v1.27.0", "1.27.0"), ("V2.0", "2.0"), ("1.2.3", "1.2.3"), (" 1.0 ", "1.0")],
    )
    def test_normalize_strips_single_leading_v(self, raw: str, expected: str) -> None:
        assert _normalize(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "ok"),
        [
            ("1.27.0", True),
            ("v1.27.0", True),
            ("1.28.0rc1", True),
            ("main", False),
            ("", False),
            ("abc123", False),
            ("release-2", False),
            # Digit-leading but NOT PEP 440-parseable — a commit sha or a
            # branch ref must never beat the baked pyproject version
            # (accepting one broke _compare_versions → available=False
            # forever; Codex #121 review).
            ("1234abcdef", False),
            ("1.x-fixes", False),
        ],
    )
    def test_looks_like_version(self, raw: str, ok: bool) -> None:
        assert _looks_like_version(raw) is ok


class TestReadRootVersion:
    def test_finds_root_pyproject_walking_up(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path / "pyproject.toml", name="corlinman", version="9.9.9")
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        assert _read_root_version_from(deep) == "9.9.9"

    def test_skips_non_root_pyproject(self, tmp_path: Path) -> None:
        # Root workspace above; a sub-package pyproject in between must be
        # skipped (it is NOT name == "corlinman").
        _write_pyproject(tmp_path / "pyproject.toml", name="corlinman", version="9.9.9")
        sub = tmp_path / "packages" / "corlinman-server"
        sub.mkdir(parents=True)
        _write_pyproject(sub / "pyproject.toml", name="corlinman-server", version="1.2.3")
        assert _read_root_version_from(sub) == "9.9.9"

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        empty = tmp_path / "nothing"
        empty.mkdir()
        assert _read_root_version_from(empty) is None


class TestResolvePrecedence:
    def test_env_wins_when_versionlike(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORLINMAN_VERSION", "v3.4.5")
        assert resolve_app_version() == "3.4.5"

    def test_env_ignored_when_git_ref(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A ref like "main" must not leak in; fall through to the root read.
        monkeypatch.setenv("CORLINMAN_VERSION", "main")
        monkeypatch.setattr(app_version, "_read_root_version_from", lambda _p: "7.0.0")
        assert resolve_app_version() == "7.0.0"

    def test_falls_back_to_root_pyproject(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CORLINMAN_VERSION", raising=False)
        monkeypatch.setattr(app_version, "_read_root_version_from", lambda _p: "7.0.0")
        assert resolve_app_version() == "7.0.0"

    def test_falls_back_to_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CORLINMAN_VERSION", raising=False)
        monkeypatch.setattr(app_version, "_read_root_version_from", lambda _p: None)
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "5.5.5")
        assert resolve_app_version() == "5.5.5"

    def test_dev_fallback_last(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CORLINMAN_VERSION", raising=False)
        monkeypatch.setattr(app_version, "_read_root_version_from", lambda _p: None)

        def _raise(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", _raise)
        assert resolve_app_version() == DEV_FALLBACK_VERSION
