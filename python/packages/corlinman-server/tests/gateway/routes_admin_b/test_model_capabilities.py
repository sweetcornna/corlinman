"""``/admin/models/capabilities`` — chat / image / speech bindings.

One place answering "which model runs when the agent chats, draws, or
speaks?". Chat and speech are read-through (their editors live in the
routing tab and on /voice); image is the one this endpoint owns, because
before it there was no global image binding at all.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_b.config_admin import models as model_routes
from corlinman_server.gateway.routes_admin_b.state import AdminState, set_admin_state
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth


@pytest.fixture()
def admin_state(tmp_path: Path) -> Iterator[AdminState]:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    snapshot: dict[str, Any] = {}
    state = AdminState(config_loader=lambda: dict(snapshot), config_path=cfg_path)
    configure_admin_auth(state)
    state.extras["snapshot"] = snapshot
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(admin_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(model_routes.router())
    return authenticated_test_client(app)


def _set(state: AdminState, cfg: dict[str, Any]) -> None:
    snap: dict[str, Any] = state.extras["snapshot"]
    snap.clear()
    snap.update(cfg)


def _reload(state: AdminState) -> None:
    snap: dict[str, Any] = state.extras["snapshot"]
    snap.clear()
    assert state.config_path is not None
    raw = state.config_path.read_text(encoding="utf-8")
    if raw.strip():
        snap.update(tomllib.loads(raw))


def test_composes_all_three_capabilities(
    client: TestClient, admin_state: AdminState
) -> None:
    _set(
        admin_state,
        {
            "models": {"default": "gpt-5.2", "aliases": {"fast": {"model": "x"}}},
            "providers": {
                "main": {"kind": "openai"},
                "img": {"kind": "openai", "image_capable": True},
            },
            "voice": {"backend": "gemini", "voice": "Kore", "model": "tts-x"},
        },
    )
    body = client.get("/admin/models/capabilities").json()
    assert body["text"]["model"] == "gpt-5.2"
    assert body["image"]["capable_providers"] == ["img"]
    assert body["voice"] == {
        "enabled": True,
        "backend": "gemini",
        "model": "tts-x",
        "voice": "Kore",
    }
    assert body["aliases"] == ["fast"]


def test_empty_config_reports_everything_unset(client: TestClient) -> None:
    body = client.get("/admin/models/capabilities").json()
    assert body["text"]["model"] == ""
    assert body["image"] == {"provider": "", "model": "", "capable_providers": []}
    assert body["voice"]["backend"] == ""


def test_disabled_providers_are_not_offered_as_image_candidates(
    client: TestClient, admin_state: AdminState
) -> None:
    _set(
        admin_state,
        {
            "providers": {
                "on": {"kind": "openai", "image_capable": True},
                "off": {"kind": "openai", "image_capable": True, "enabled": False},
            }
        },
    )
    body = client.get("/admin/models/capabilities").json()
    assert body["image"]["capable_providers"] == ["on"]


def test_put_image_binding_persists(client: TestClient, admin_state: AdminState) -> None:
    _set(admin_state, {"models": {"default": "gpt-5.2"}})
    resp = client.put(
        "/admin/models/capabilities/image",
        json={"provider": "img", "model": "gpt-image-2"},
    )
    assert resp.status_code == 200
    _reload(admin_state)
    on_disk = tomllib.loads(admin_state.config_path.read_text(encoding="utf-8"))
    assert on_disk["models"]["image_provider"] == "img"
    assert on_disk["models"]["image_model"] == "gpt-image-2"
    assert client.get("/admin/models/capabilities").json()["image"]["model"] == (
        "gpt-image-2"
    )


def test_blank_values_clear_the_binding(
    client: TestClient, admin_state: AdminState
) -> None:
    """Clearing must remove the keys, not write empty strings the agent
    would then treat as a configured-but-broken binding."""
    _set(admin_state, {"models": {"default": "gpt-5.2"}})
    client.put(
        "/admin/models/capabilities/image",
        json={"provider": "img", "model": "gpt-image-2"},
    )
    _reload(admin_state)
    client.put("/admin/models/capabilities/image", json={"provider": "", "model": ""})
    _reload(admin_state)
    models_cfg = tomllib.loads(admin_state.config_path.read_text(encoding="utf-8"))[
        "models"
    ]
    assert "image_provider" not in models_cfg
    assert "image_model" not in models_cfg


def test_image_write_preserves_aliases_and_default(
    client: TestClient, admin_state: AdminState
) -> None:
    _set(
        admin_state,
        {"models": {"default": "gpt-5.2", "aliases": {"fast": {"model": "x"}}}},
    )
    client.put(
        "/admin/models/capabilities/image", json={"provider": "", "model": "img-1"}
    )
    _reload(admin_state)
    models_cfg = tomllib.loads(admin_state.config_path.read_text(encoding="utf-8"))[
        "models"
    ]
    assert models_cfg["default"] == "gpt-5.2"
    assert models_cfg["aliases"] == {"fast": {"model": "x"}}
    assert models_cfg["image_model"] == "img-1"


def test_capabilities_requires_auth(admin_state: AdminState) -> None:
    app = FastAPI()
    app.include_router(model_routes.router())
    assert TestClient(app).get("/admin/models/capabilities").status_code == 401
