"""Kind normalization coverage for ``/admin/providers*`` CRUD endpoints.

Ensures legacy kind spellings are accepted on write but canonicalized to
``openai_compatible`` in persisted config and read APIs.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from corlinman_server.gateway.routes_admin_b.config_admin import providers as providers_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth


def _on_disk(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    return tomllib.loads(raw)


def _set_snapshot_from_disk(state: AdminState) -> None:
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.clear()
    assert state.config_path is not None
    snapshot.update(_on_disk(state.config_path))


def _make_state(config_path: Path) -> AdminState:
    snapshot: dict[str, Any] = {}

    def _loader() -> dict[str, Any]:
        return dict(snapshot)

    state = AdminState(config_loader=_loader, config_path=config_path)
    configure_admin_auth(state)
    state.extras["snapshot"] = snapshot
    return state


def _make_client(state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(providers_routes.router())
    return authenticated_test_client(app)


def _with_state(config_path: Path) -> Iterator[tuple[AdminState, TestClient]]:
    state = _make_state(config_path)
    set_admin_state(state)
    try:
        yield state, _make_client(state)
    finally:
        set_admin_state(None)


def test_upsert_accepts_openai_compatible_alias_and_persists_canonical(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")

    for state, client in _with_state(config_path):
        resp = client.post(
            "/admin/providers",
            json={
                "name": "legacy-hyphen",
                "kind": "openai-compatible",
                "enabled": True,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider"]["kind"] == "openai_compatible"

        _set_snapshot_from_disk(state)
        listing = client.get("/admin/providers")
        assert listing.status_code == 200
        rows = listing.json()["providers"]
        row = next(r for r in rows if r["name"] == "legacy-hyphen")
        assert row["kind"] == "openai_compatible"

        on_disk = _on_disk(config_path)
        assert on_disk["providers"]["legacy-hyphen"]["kind"] == "openai_compatible"


def test_upsert_accepts_newapi_alias_and_persists_canonical(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")

    for state, client in _with_state(config_path):
        resp = client.post(
            "/admin/providers",
            json={
                "name": "legacy-newapi",
                "kind": "newapi",
                "enabled": True,
                "base_url": "https://pool.example/v1",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider"]["kind"] == "openai_compatible"

        _set_snapshot_from_disk(state)
        on_disk = _on_disk(config_path)
        assert on_disk["providers"]["legacy-newapi"]["kind"] == "openai_compatible"


def test_patch_normalizes_kind_alias(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[providers.patch-me]
kind = "openai"
enabled = true
        """.strip(),
        encoding="utf-8",
    )

    for state, client in _with_state(config_path):
        _set_snapshot_from_disk(state)
        resp = client.patch(
            "/admin/providers/patch-me",
            json={"kind": "openai-compatible"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["provider"]["kind"] == "openai_compatible"

        _set_snapshot_from_disk(state)
        on_disk = _on_disk(config_path)
        assert on_disk["providers"]["patch-me"]["kind"] == "openai_compatible"


def test_custom_create_accepts_kind_alias_and_persists_canonical(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")

    for state, client in _with_state(config_path):
        resp = client.post(
            "/admin/providers/custom",
            json={
                "slug": "custom-hyphen",
                "kind": "openai-compatible",
                "base_url": "https://custom.example/v1",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["kind"] == "openai_compatible"

        _set_snapshot_from_disk(state)
        on_disk = _on_disk(config_path)
        assert on_disk["providers"]["custom-hyphen"]["kind"] == "openai_compatible"

