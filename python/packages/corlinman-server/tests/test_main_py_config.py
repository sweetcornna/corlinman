from __future__ import annotations

import json
from importlib import import_module

server_main = import_module("corlinman_server.main")


def test_load_config_returns_subagent_section(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "py-config.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": [],
                "aliases": {},
                "subagent": {
                    "max_concurrent_per_parent": 2,
                    "max_concurrent_per_tenant": 4,
                    "max_depth": 3,
                    "max_wall_seconds_ceiling": 120,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(config_path))

    _specs, _aliases, subagent = server_main._load_config()

    assert subagent == {
        "max_concurrent_per_parent": 2,
        "max_concurrent_per_tenant": 4,
        "max_depth": 3,
        "max_wall_seconds_ceiling": 120,
    }
