"""#108 item 5 — ``_resolve_public_base_url`` stat-keyed disk-read cache.

``_register_tool_media`` calls ``_resolve_public_base_url`` once per
media-bearing tool result. With ``CORLINMAN_PUBLIC_URL`` unset it otherwise
re-reads BOTH backing files (the ``$CORLINMAN_PY_CONFIG`` drop and the
learned ``public_origin``) on every call. These tests lock in the cache:
a hit skips the reads, a mtime/size change — or either file appearing or
disappearing — invalidates, and the env short-circuit is unaffected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from corlinman_server import agent_servicer


@pytest.fixture(autouse=True)
def _reset_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolate each test: fresh module cache, tmp data dir, no env / drop."""
    monkeypatch.setattr(agent_servicer, "_public_url_cache", None)
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CORLINMAN_PUBLIC_URL", raising=False)
    monkeypatch.delenv("CORLINMAN_PY_CONFIG", raising=False)


def test_cache_hit_avoids_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second call with both backing files unchanged serves the cached
    value without re-invoking either disk reader."""
    calls = {"py_config": 0, "origin": 0}

    def _fake_py_config() -> str:
        calls["py_config"] += 1
        return ""

    def _fake_origin() -> str:
        calls["origin"] += 1
        return ""

    monkeypatch.setattr(
        agent_servicer, "_read_public_url_from_py_config", _fake_py_config
    )
    monkeypatch.setattr(
        agent_servicer, "_read_learned_public_origin", _fake_origin
    )

    assert agent_servicer._resolve_public_base_url() == ""
    assert calls == {"py_config": 1, "origin": 1}
    # Second call — signature unchanged (both files absent) → cache hit.
    assert agent_servicer._resolve_public_base_url() == ""
    assert calls == {"py_config": 1, "origin": 1}


def test_mtime_bump_invalidates(tmp_path: Path) -> None:
    """Rewriting the py-config drop (mtime change) re-reads it — the cached
    value does not go stale."""
    drop = tmp_path / "py-config.json"
    drop.write_text(json.dumps({"public_url": "https://a.example"}), "utf-8")
    assert agent_servicer._resolve_public_base_url() == "https://a.example"

    # Same byte length, but bump mtime so the (mtime, size) signature moves.
    drop.write_text(json.dumps({"public_url": "https://b.example"}), "utf-8")
    os.utime(drop, (drop.stat().st_atime + 100, drop.stat().st_mtime + 100))
    assert agent_servicer._resolve_public_base_url() == "https://b.example"


def test_appear_and_disappear_invalidate(tmp_path: Path) -> None:
    """A backing file appearing or disappearing invalidates the cache."""
    drop = tmp_path / "py-config.json"
    # Absent → empty, cached.
    assert agent_servicer._resolve_public_base_url() == ""
    # Appears → invalidated, picked up.
    drop.write_text(json.dumps({"public_url": "https://c.example"}), "utf-8")
    assert agent_servicer._resolve_public_base_url() == "https://c.example"
    # Disappears → invalidated, back to empty.
    drop.unlink()
    assert agent_servicer._resolve_public_base_url() == ""


def test_env_wins_and_short_circuits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``CORLINMAN_PUBLIC_URL`` wins over the drop and short-circuits before
    either disk reader runs."""
    # A drop that would otherwise resolve — the env must beat it.
    (tmp_path / "py-config.json").write_text(
        json.dumps({"public_url": "https://drop.example"}), "utf-8"
    )
    monkeypatch.setenv("CORLINMAN_PUBLIC_URL", "https://env.example/")

    def _boom() -> str:  # pragma: no cover — must never run
        raise AssertionError("disk reader called despite env short-circuit")

    monkeypatch.setattr(
        agent_servicer, "_read_public_url_from_py_config", _boom
    )
    monkeypatch.setattr(agent_servicer, "_read_learned_public_origin", _boom)

    assert agent_servicer._resolve_public_base_url() == "https://env.example"
