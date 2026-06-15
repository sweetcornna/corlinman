from __future__ import annotations

import json
from importlib import import_module
from typing import Any

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


def test_load_config_reads_explicit_path_over_env(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / "env-py-config.json"
    explicit_path = tmp_path / "explicit-py-config.json"
    env_path.write_text(
        json.dumps({"providers": [], "aliases": {}, "subagent": {"max_depth": 1}}),
        encoding="utf-8",
    )
    explicit_path.write_text(
        json.dumps({"providers": [], "aliases": {}, "subagent": {"max_depth": 4}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(env_path))

    _specs, _aliases, subagent = server_main._load_config(str(explicit_path))

    assert subagent == {"max_depth": 4}


def test_reloading_provider_resolver_forwards_provider_hint() -> None:
    class _Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any], str | None]] = []

        def resolve(
            self,
            *,
            alias_or_model: str,
            aliases: dict[str, Any],
            provider_hint: str | None = None,
        ) -> tuple[Any, str, dict[str, Any]]:
            self.calls.append((alias_or_model, aliases, provider_hint))
            return object(), alias_or_model, {}

    resolver = server_main._ReloadingProviderResolver(None)
    registry = _Registry()
    resolver._registry = registry
    resolver._aliases = {"chat-default": object()}

    resolver("gpt-5.5", provider_hint="relay")

    assert registry.calls == [
        ("gpt-5.5", {"chat-default": resolver._aliases["chat-default"]}, "relay")
    ]
