"""Unit tests for the extracted atomic config writer (modularization Phase 1)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from corlinman_server.gateway.core.config_mutation import write_config_atomic


def test_write_config_atomic_roundtrips_toml(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "config.toml"
    result = write_config_atomic(target, {"server": {"public_url": "https://x"}})

    assert result is None  # success short-circuits with None
    assert target.exists()
    assert tomllib.loads(target.read_text()) == {"server": {"public_url": "https://x"}}
    # the sibling temp file is renamed away, not left behind
    assert not target.with_suffix(target.suffix + ".new").exists()


def test_write_config_atomic_reports_serialise_failure(tmp_path: Path) -> None:
    # A set is not TOML-serialisable -> 500 JSONResponse, not an exception.
    result = write_config_atomic(tmp_path / "config.toml", {"bad": {1, 2, 3}})

    assert result is not None
    assert result.status_code == 500


def test_onboard_reexport_shim_points_at_the_same_callable() -> None:
    # Old import path must keep working for backward compatibility.
    from corlinman_server.gateway.routes_admin_b.onboard import _write_config_atomic

    assert _write_config_atomic is write_config_atomic
