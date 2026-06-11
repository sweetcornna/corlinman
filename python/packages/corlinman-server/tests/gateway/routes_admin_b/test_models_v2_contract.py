"""v0.2 contract coverage for ``/admin/models``.

The Models admin page switches to its richer editor when ``aliases`` is an
array, then expects alias rows to carry an effective params schema and provider
rows to use the same view shape as ``/admin/providers``. Without those fields
the UI renders the "upgrade the gateway to 0.2.x" fallback.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from corlinman_server.gateway.routes_admin_b.config_admin import models
from corlinman_server.gateway.routes_admin_b.state import AdminState, set_admin_state
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth


def _with_models_client(
    config_path: Path,
) -> Iterator[tuple[AdminState, dict[str, Any], TestClient]]:
    snapshot: dict[str, Any] = {}

    def _loader() -> dict[str, Any]:
        return dict(snapshot)

    state = AdminState(config_loader=_loader, config_path=config_path)
    configure_admin_auth(state)
    set_admin_state(state)
    app = FastAPI()
    app.include_router(models.router())
    try:
        yield state, snapshot, authenticated_test_client(app)
    finally:
        set_admin_state(None)


def test_list_models_returns_alias_and_provider_v2_views(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")

    for _state, snapshot, client in _with_models_client(config_path):
        snapshot.update(
            {
                "providers": {
                    "relay": {
                        "kind": "openai_compatible",
                        "enabled": True,
                        "base_url": "https://relay.example/v1",
                        "api_key": {"env": "RELAY_API_KEY"},
                        "params": {"timeout": 30},
                    },
                    "anthropic": {
                        "kind": "anthropic",
                        "enabled": False,
                    },
                },
                "models": {
                    "default": "chat",
                    "aliases": {
                        "chat": {
                            "provider": "relay",
                            "model": "gpt-5.5",
                            "params": {"temperature": 0.4},
                        },
                        "legacy": "gpt-4o",
                    },
                },
            }
        )

        resp = client.get("/admin/models")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default"] == "chat"
    assert isinstance(body["aliases"], list)
    assert isinstance(body["providers"], list)

    relay = next(p for p in body["providers"] if p["name"] == "relay")
    assert relay["kind"] == "openai_compatible"
    assert relay["api_key_source"] == "env"
    assert relay["api_key_env_name"] == "RELAY_API_KEY"
    assert relay["params"] == {"timeout": 30}
    assert relay["params_schema"]["type"] == "object"
    assert relay["capabilities"]["chat"] is True
    assert relay["capabilities"]["embedding"] is True

    anthropic = next(p for p in body["providers"] if p["name"] == "anthropic")
    assert anthropic["capabilities"]["chat"] is True
    assert anthropic["capabilities"]["embedding"] is False

    chat = next(a for a in body["aliases"] if a["name"] == "chat")
    assert chat["provider"] == "relay"
    assert chat["model"] == "gpt-5.5"
    assert chat["params"] == {"temperature": 0.4}
    assert chat["effective_params_schema"]["type"] == "object"

    legacy = next(a for a in body["aliases"] if a["name"] == "legacy")
    assert legacy["provider"] == ""
    assert legacy["model"] == "gpt-4o"
    assert legacy["params"] == {}
    assert legacy["effective_params_schema"]["type"] == "object"


def test_single_alias_upsert_returns_alias_v2_view(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")

    for _state, snapshot, client in _with_models_client(config_path):
        snapshot.update(
            {
                "providers": {
                    "relay": {
                        "kind": "openai_compatible",
                        "enabled": True,
                        "api_key": {"value": "sk-test"},
                    },
                },
                "models": {"default": "chat", "aliases": {}},
            }
        )

        resp = client.post(
            "/admin/models/aliases",
            json={
                "name": "chat",
                "provider": "relay",
                "model": "gpt-5.5",
                "params": {"temperature": 0.2},
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "chat"
    assert body["provider"] == "relay"
    assert body["model"] == "gpt-5.5"
    assert body["params"] == {"temperature": 0.2}
    assert body["effective_params_schema"]["type"] == "object"
