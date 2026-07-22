"""Embedded-brain py-config bootstrap — standalone TOML → drop rendering."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.console.embedded import EmbeddedBrain, _ensure_py_config_env

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


async def test_embedded_agent_wires_live_tencent_policy_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drop = tmp_path / "py-config.json"
    drop.write_text('{"tencent_safety":{"enabled":false}}', encoding="utf-8")
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(drop))

    captured: dict[str, Any] = {}

    class _FakeServicer:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    class _FakeServer:
        def add_insecure_port(self, _bind: str) -> int:
            return 1

        async def start(self) -> None:
            return None

        async def stop(self, *, grace: float) -> None:
            return None

    class _FakeChannel:
        async def channel_ready(self) -> None:
            return None

    monkeypatch.setattr(
        "corlinman_server.agent_servicer.CorlinmanAgentServicer", _FakeServicer
    )
    monkeypatch.setattr("grpc.aio.server", lambda options=None: _FakeServer())
    monkeypatch.setattr(
        "corlinman_grpc.agent_pb2_grpc.add_AgentServicer_to_server",
        lambda servicer, server: None,
    )
    monkeypatch.setattr(
        "corlinman_grpc.agent_client.connect_channel", lambda bind: _FakeChannel()
    )
    monkeypatch.setattr(
        "corlinman_grpc.agent_client.AgentClient", lambda channel: object()
    )
    monkeypatch.setattr(
        "corlinman_server.console.embedded._build_plugin_tool_executor",
        lambda *args, **kwargs: _async_value((None, b"", None)),
    )

    brain = EmbeddedBrain()
    brain._config = {}
    await brain._start_agent(tmp_path)
    try:
        assert captured["tencent_policy_resolver"]() is False
        drop.write_text('{"tencent_safety":{"enabled":true}}', encoding="utf-8")
        assert captured["tencent_policy_resolver"]() is True
    finally:
        await brain.aclose()


async def _async_value(value: Any) -> Any:
    return value
