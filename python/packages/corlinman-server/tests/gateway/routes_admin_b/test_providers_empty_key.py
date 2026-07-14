"""Regression: an empty / valueless ``api_key`` must not masquerade as a
configured literal key.

Prod incident (1.28.0): a provider whose config carried an empty
``[providers.<name>.api_key]`` table was shown in the UI as key source
"value" (字面量), so the operator believed a key was set — but requests
went out unauthenticated and the upstream rejected them ("blocked").

Two fixes are covered here:
- ``_view_from_entry`` reports ``api_key_source="unset"`` for an empty /
  valueless key table (not "value").
- ``POST /admin/providers`` refuses to persist a valueless key table, so
  an accidental ``{}`` / ``{"value": ""}`` can never create that state.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from corlinman_server.gateway.routes_admin_b.config_admin import (
    providers as providers_routes,
)
from corlinman_server.gateway.routes_admin_b.config_admin._providers_lib import (
    _view_from_entry,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth


def test_view_from_entry_empty_key_is_unset() -> None:
    for raw in ({}, {"value": ""}, {"env": ""}):
        view = _view_from_entry(
            "p", {"kind": "openai_compatible", "api_key": raw}
        )
        assert view.api_key_source == "unset", raw


def test_view_from_entry_usable_key_reports_its_source() -> None:
    v_val = _view_from_entry(
        "p", {"kind": "openai_compatible", "api_key": {"value": "sk-x"}}
    )
    assert v_val.api_key_source == "value"
    v_env = _view_from_entry(
        "p", {"kind": "openai_compatible", "api_key": {"env": "OPENAI_API_KEY"}}
    )
    assert v_env.api_key_source == "env"
    assert v_env.api_key_env_name == "OPENAI_API_KEY"


def _make_state(config_path: Path) -> AdminState:
    snapshot: dict[str, Any] = {}

    def _loader() -> dict[str, Any]:
        return dict(snapshot)

    state = AdminState(config_loader=_loader, config_path=config_path)
    configure_admin_auth(state)
    state.extras["snapshot"] = snapshot
    return state


def _with_state(config_path: Path) -> Iterator[tuple[AdminState, TestClient]]:
    state = _make_state(config_path)
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(providers_routes.router())
        yield state, authenticated_test_client(app)
    finally:
        set_admin_state(None)


def _sync_snapshot(state: AdminState) -> None:
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.clear()
    assert state.config_path is not None
    raw = state.config_path.read_text(encoding="utf-8")
    if raw.strip():
        snapshot.update(tomllib.loads(raw))


def test_upsert_refuses_to_persist_a_valueless_key(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")

    for state, client in _with_state(config_path):
        # Empty {} and {value:""} must both NOT create a stored key.
        for junk in ({}, {"value": ""}):
            resp = client.post(
                "/admin/providers",
                json={
                    "name": "cornna",
                    "kind": "openai_compatible",
                    "base_url": "https://api.example.test/",
                    "enabled": True,
                    "api_key": junk,
                },
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["provider"]["api_key_source"] == "unset"

            _sync_snapshot(state)
            on_disk = tomllib.loads(
                config_path.read_text(encoding="utf-8")
            )
            entry = on_disk["providers"]["cornna"]
            # No stored key at all (not an empty {} table).
            assert "api_key" not in entry or entry["api_key"] in ({}, None)
            row = next(
                r
                for r in client.get("/admin/providers").json()["providers"]
                if r["name"] == "cornna"
            )
            assert row["api_key_source"] == "unset"


def test_upsert_persists_a_real_value_key(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")

    for state, client in _with_state(config_path):
        resp = client.post(
            "/admin/providers",
            json={
                "name": "cornna",
                "kind": "openai_compatible",
                "base_url": "https://api.example.test/",
                "enabled": True,
                "api_key": {"value": "sk-real-123"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["provider"]["api_key_source"] == "value"

        on_disk = tomllib.loads(config_path.read_text(encoding="utf-8"))
        assert on_disk["providers"]["cornna"]["api_key"] == {
            "value": "sk-real-123"
        }
