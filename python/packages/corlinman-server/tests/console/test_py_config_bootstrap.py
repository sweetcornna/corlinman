"""Embedded-brain py-config bootstrap — standalone TOML → drop rendering."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.console.embedded import _ensure_py_config_env

_CONFIG: dict[str, Any] = {
    "providers": {
        "my-vllm": {
            "kind": "openai_compatible",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "test-key",
        }
    },
    "models": {"default": "my-alias"},
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_PY_CONFIG", raising=False)


def test_generates_drop_from_toml_config(tmp_path: Path) -> None:
    _ensure_py_config_env(tmp_path, _CONFIG)
    drop = tmp_path / "py-config.json"
    assert drop.is_file()
    assert os.environ.get("CORLINMAN_PY_CONFIG") == str(drop)
    data = json.loads(drop.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_refreshes_stale_drop_when_toml_is_newer(tmp_path: Path) -> None:
    drop = tmp_path / "py-config.json"
    drop.write_text('{"providers": []}', encoding="utf-8")
    old_mtime = time.time() - 100
    os.utime(drop, (old_mtime, old_mtime))
    (tmp_path / "config.toml").write_text("# edited later", encoding="utf-8")

    _ensure_py_config_env(tmp_path, _CONFIG)

    data = json.loads(drop.read_text(encoding="utf-8"))
    assert data != {"providers": []}  # re-rendered from the supplied config


def test_keeps_fresh_drop_untouched(tmp_path: Path) -> None:
    """A drop newer than config.toml (e.g. a running gateway just wrote
    it after an admin mutation) must not be clobbered."""
    toml = tmp_path / "config.toml"
    toml.write_text("# old", encoding="utf-8")
    old_mtime = time.time() - 100
    os.utime(toml, (old_mtime, old_mtime))
    drop = tmp_path / "py-config.json"
    drop.write_text('{"providers": ["gateway-owned"]}', encoding="utf-8")

    _ensure_py_config_env(tmp_path, _CONFIG)

    assert json.loads(drop.read_text(encoding="utf-8")) == {
        "providers": ["gateway-owned"]
    }


def test_env_already_set_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", "/explicit/operator/path.json")
    _ensure_py_config_env(tmp_path, _CONFIG)
    assert os.environ["CORLINMAN_PY_CONFIG"] == "/explicit/operator/path.json"
    assert not (tmp_path / "py-config.json").exists()
