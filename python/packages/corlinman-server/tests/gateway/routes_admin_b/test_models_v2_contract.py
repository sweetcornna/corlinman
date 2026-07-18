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


def test_bulk_alias_save_preserves_provider_and_params(tmp_path: Path) -> None:
    # Regression: the Models page "Save all" button posts a flat {name: target}
    # string map. That shape cannot carry an alias's provider/params, so the old
    # wholesale replace stripped the provider binding off every alias (e.g. the
    # ones OAuth login provisioned) and the resolver then silently dropped them —
    # chat fell through to the wrong upstream (the reported 401 + "—" provider).
    # The bulk save must MERGE: keep the existing provider+params for any alias
    # name still present, update only its target model, drop omitted names, and
    # store genuinely-new names as plain shorthands.
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
                "models": {
                    "default": "chat",
                    "aliases": {
                        "chat": {
                            "provider": "relay",
                            "model": "gpt-5.5",
                            "params": {"reasoning_effort": "high"},
                        },
                        "stale": {
                            "provider": "relay",
                            "model": "gpt-4o",
                            "params": {},
                        },
                    },
                },
            }
        )

        resp = client.post(
            "/admin/models/aliases",
            json={
                # Flat string map exactly as the toolbar Save sends. "chat" keeps
                # its target, "newone" is brand new, "stale" is omitted (deleted).
                "aliases": {"chat": "gpt-5.5", "newone": "gpt-4o"},
                "default": "chat",
            },
        )

    assert resp.status_code == 200, resp.text
    aliases = resp.json()["aliases"]
    # Existing alias keeps its provider binding AND params through the bulk save.
    assert aliases["chat"] == {
        "provider": "relay",
        "model": "gpt-5.5",
        "params": {"reasoning_effort": "high"},
    }
    # Genuinely-new name is stored as a plain shorthand.
    assert aliases["newone"] == "gpt-4o"
    # Omitted alias is dropped (row deletion is honoured, not silently restored).
    assert "stale" not in aliases


def test_default_only_write_updates_default_and_preserves_aliases(
    tmp_path: Path,
) -> None:
    # PR5 provider-setup flow: the last wizard step posts ``{"default": ...}``
    # with NO ``aliases`` key. Before the default-only branch existed that
    # shape 400'd as invalid_body, and the "obvious" workaround (bulk write
    # with ``aliases: {}``) would drop every alias — a routing-table wipe.
    # This test locks in the non-destructive contract: default moves, every
    # alias (including its provider binding + params) survives verbatim.
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
                "models": {
                    "default": "chat",
                    "aliases": {
                        "chat": {
                            "provider": "relay",
                            "model": "gpt-5.5",
                            "params": {"reasoning_effort": "high"},
                        },
                        "legacy": "gpt-4o",
                    },
                },
            }
        )

        resp = client.post("/admin/models/aliases", json={"default": "legacy"})

        # Empty default is rejected without touching anything.
        bad = client.post("/admin/models/aliases", json={"default": ""})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default"] == "legacy"
    # Aliases pass through VERBATIM — full dict entry incl. provider binding
    # and params, plain-string shorthand untouched, nothing dropped.
    assert body["aliases"] == {
        "chat": {
            "provider": "relay",
            "model": "gpt-5.5",
            "params": {"reasoning_effort": "high"},
        },
        "legacy": "gpt-4o",
    }

    # The atomic write landed on disk with the same preserved alias table.
    import tomllib

    on_disk = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["models"]["default"] == "legacy"
    assert on_disk["models"]["aliases"]["chat"]["provider"] == "relay"
    assert on_disk["models"]["aliases"]["legacy"] == "gpt-4o"

    assert bad.status_code == 400, bad.text
    assert bad.json()["error"] == "invalid_default"


def test_bulk_shape_with_default_still_replaces_alias_table(tmp_path: Path) -> None:
    # Guard: adding the default-only branch must NOT change how a body that
    # DOES carry ``aliases`` behaves — the Models page "Save all" contract
    # (merge provider bindings, drop omitted names, honour ``default``).
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
                "models": {
                    "default": "chat",
                    "aliases": {
                        "chat": {
                            "provider": "relay",
                            "model": "gpt-5.5",
                            "params": {},
                        },
                        "gone": "gpt-4o",
                    },
                },
            }
        )

        resp = client.post(
            "/admin/models/aliases",
            json={"aliases": {"chat": "gpt-5.5"}, "default": "chat"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default"] == "chat"
    # Bulk semantics unchanged: kept name preserves its binding, omitted
    # name is dropped.
    assert body["aliases"]["chat"]["provider"] == "relay"
    assert "gone" not in body["aliases"]


def test_alias_rows_carry_reasoning_tiers(tmp_path: Path) -> None:
    """Each alias advertises the *resolved* model's effort ladder so the
    composer renders real options ("cornna" alias → gpt-5.6 → six tiers)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    for _state, snapshot, client in _with_models_client(config_path):
        snapshot.update(
            {
                "providers": {
                    "relay": {"kind": "openai_compatible", "enabled": True},
                },
                "models": {
                    "default": "cornna",
                    "aliases": {
                        "cornna": {"provider": "relay", "model": "gpt-5.6-sol"},
                        "grok": {"provider": "relay", "model": "grok-4"},
                        "mystery": {"provider": "relay", "model": "sol-pro-x"},
                    },
                },
            }
        )
        resp = client.get("/admin/models")

    assert resp.status_code == 200, resp.text
    rows = {a["name"]: a for a in resp.json()["aliases"]}
    assert rows["cornna"]["reasoning_tiers"] == [
        "none", "low", "medium", "high", "xhigh", "max",
    ]
    assert rows["cornna"]["reasoning_default"] == "medium"
    # Known no-knob family → [] (hide the picker)
    assert rows["grok"]["reasoning_tiers"] == []
    # Unknown family → null (client heuristics)
    assert rows["mystery"]["reasoning_tiers"] is None
