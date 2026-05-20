"""Tests for the W-B1 custom-provider CRUD surface.

See ``docs/PLAN_PROVIDER_AUTH.md`` §2 W-B1. The endpoints under test:

* ``GET    /admin/providers/kinds``           — protocol-selector dropdown
* ``GET    /admin/providers/custom``          — list user-added providers
* ``POST   /admin/providers/custom``          — create one
* ``PATCH  /admin/providers/custom/{slug}``   — partial update
* ``DELETE /admin/providers/custom/{slug}``   — remove

The hard contract is that ``params.custom = true`` is the load-bearing
marker that separates user-added providers from built-in slots; the
endpoint writes it on POST and refuses to PATCH/DELETE blocks that lack
it. Built-in slugs (``anthropic``, ``openai``, ``google``, ``mock``)
are reserved and collide with 409.

Fixture pattern mirrors ``test_credentials.py`` — mount the router with a
temp config file and refresh the in-process snapshot between writes.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_providers.specs import ProviderKind, list_supported_kinds
from corlinman_server.gateway.routes_admin_b import providers as providers_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_config_path(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text("", encoding="utf-8")
    return cfg


@pytest.fixture()
def admin_state(temp_config_path: Path) -> Iterator[AdminState]:
    snapshot: dict[str, Any] = {}

    def _loader() -> dict[str, Any]:
        return dict(snapshot)

    state = AdminState(
        config_loader=_loader,
        config_path=temp_config_path,
    )
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
    app.include_router(providers_routes.router())
    return authenticated_test_client(app)


def _reload(state: AdminState) -> None:
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.clear()
    assert state.config_path is not None
    raw = state.config_path.read_text(encoding="utf-8")
    if raw.strip():
        snapshot.update(tomllib.loads(raw))


def _on_disk(state: AdminState) -> dict[str, Any]:
    assert state.config_path is not None
    raw = state.config_path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    return tomllib.loads(raw)


# ---------------------------------------------------------------------------
# GET /admin/providers/kinds
# ---------------------------------------------------------------------------


def test_kinds_discovery_returns_every_provider_kind(client: TestClient) -> None:
    """Dropdown source must mirror ProviderKind exactly (alphabetised)."""
    resp = client.get("/admin/providers/kinds")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload == {"kinds": list_supported_kinds()}
    # Spot-check a few enum values are present so a future enum
    # rename surfaces here, not just in the helper.
    assert "anthropic" in payload["kinds"]
    assert "openai_compatible" in payload["kinds"]
    assert "mock" in payload["kinds"]
    # And the ordering is stable / sorted.
    assert payload["kinds"] == sorted(payload["kinds"])
    # Length matches the enum cardinality.
    assert len(payload["kinds"]) == len(list(ProviderKind))


# ---------------------------------------------------------------------------
# POST → GET → DELETE round-trip
# ---------------------------------------------------------------------------


def test_create_list_delete_round_trip(
    client: TestClient, admin_state: AdminState
) -> None:
    """Happy-path: create one custom provider, see it, delete it."""
    body = {
        "slug": "my-vllm",
        "kind": "openai_compatible",
        "base_url": "https://vllm.internal/v1",
        "api_key": {"value": "sk-vllm-secret"},
        "params": {"timeout_seconds": 30},
    }
    resp = client.post("/admin/providers/custom", json=body)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["slug"] == "my-vllm"
    assert created["kind"] == "openai_compatible"
    assert created["base_url"] == "https://vllm.internal/v1"
    assert created["has_api_key"] is True
    # The marker is written + surfaced on the read shape.
    assert created["params"]["custom"] is True
    assert created["params"]["timeout_seconds"] == 30

    # On-disk persists with the custom marker.
    on_disk = _on_disk(admin_state)
    block = on_disk["providers"]["my-vllm"]
    assert block["kind"] == "openai_compatible"
    assert block["enabled"] is True
    assert block["base_url"] == "https://vllm.internal/v1"
    assert block["api_key"] == {"value": "sk-vllm-secret"}
    assert block["params"]["custom"] is True

    _reload(admin_state)
    listed = client.get("/admin/providers/custom").json()
    assert listed == {"providers": [created]}

    # DELETE → 204 + no longer listed.
    resp = client.delete("/admin/providers/custom/my-vllm")
    assert resp.status_code == 204
    assert resp.content == b""

    _reload(admin_state)
    listed = client.get("/admin/providers/custom").json()
    assert listed == {"providers": []}
    on_disk = _on_disk(admin_state)
    assert "my-vllm" not in (on_disk.get("providers") or {})


def test_list_excludes_blocks_without_custom_marker(
    client: TestClient, admin_state: AdminState
) -> None:
    """Built-in slots without the marker must NOT show up in /custom."""
    snapshot: dict[str, Any] = admin_state.extras["snapshot"]
    snapshot["providers"] = {
        "anthropic": {
            "kind": "anthropic",
            "enabled": True,
            "api_key": "sk-builtin",
        },
        "my-local": {
            "kind": "openai_compatible",
            "enabled": True,
            "base_url": "http://localhost:8080",
            "params": {"custom": True},
        },
    }

    listed = client.get("/admin/providers/custom").json()
    slugs = [p["slug"] for p in listed["providers"]]
    assert slugs == ["my-local"]


# ---------------------------------------------------------------------------
# Validation — slug regex
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_slug",
    [
        "",
        "-leading-dash",
        "UPPERCASE",
        "with space",
        "with.dot",
        "way-too-long-slug-that-exceeds-the-thirty-two-char-cap",
        "_leading_underscore",
        "trailing!",
    ],
)
def test_slug_regex_rejects_invalid_inputs(
    client: TestClient, bad_slug: str
) -> None:
    """Any slug failing the regex must 400 cleanly."""
    resp = client.post(
        "/admin/providers/custom",
        json={"slug": bad_slug, "kind": "openai_compatible"},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body.get("error") == "invalid_slug"


@pytest.mark.parametrize(
    "good_slug",
    [
        "a",
        "openai-clone",
        "my_provider_1",
        "0abc",
        "a" * 32,
    ],
)
def test_slug_regex_accepts_valid_inputs(
    client: TestClient, good_slug: str, admin_state: AdminState
) -> None:
    """Valid slugs are accepted and round-trip cleanly."""
    resp = client.post(
        "/admin/providers/custom",
        json={"slug": good_slug, "kind": "openai_compatible"},
    )
    assert resp.status_code == 201, resp.text
    _reload(admin_state)


# ---------------------------------------------------------------------------
# Validation — built-in slot collision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "builtin", ["anthropic", "openai", "google", "mock"]
)
def test_create_rejects_builtin_slug_with_409(
    client: TestClient, builtin: str
) -> None:
    """Reserved slugs collide with built-in slots — 409, not 400."""
    resp = client.post(
        "/admin/providers/custom",
        json={"slug": builtin, "kind": "openai_compatible"},
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body.get("error") == "builtin_slot"
    assert body.get("slug") == builtin


def test_create_rejects_invalid_kind(client: TestClient) -> None:
    """Unknown protocol values must 400 — we never write a junk kind."""
    resp = client.post(
        "/admin/providers/custom",
        json={"slug": "junk", "kind": "not-a-real-kind"},
    )
    assert resp.status_code == 400
    assert resp.json().get("error") == "invalid_kind"


def test_create_rejects_duplicate_existing_slug(
    client: TestClient, admin_state: AdminState
) -> None:
    """Second POST on the same slug must 409 — operator must DELETE first."""
    body = {"slug": "twice", "kind": "openai_compatible"}
    first = client.post("/admin/providers/custom", json=body)
    assert first.status_code == 201
    _reload(admin_state)

    second = client.post("/admin/providers/custom", json=body)
    assert second.status_code == 409
    assert second.json().get("error") == "slug_exists"


# ---------------------------------------------------------------------------
# DELETE — 404 paths
# ---------------------------------------------------------------------------


def test_delete_missing_slug_returns_404(client: TestClient) -> None:
    """Never-written slug → 404."""
    resp = client.delete("/admin/providers/custom/nope-not-here")
    assert resp.status_code == 404
    assert resp.json().get("error") == "not_found"


def test_delete_refuses_block_without_custom_marker(
    client: TestClient, admin_state: AdminState
) -> None:
    """A block lacking ``params.custom=true`` is owned by another surface."""
    snapshot: dict[str, Any] = admin_state.extras["snapshot"]
    snapshot["providers"] = {
        "anthropic": {
            "kind": "anthropic",
            "enabled": True,
            "api_key": "sk-builtin",
        },
    }

    resp = client.delete("/admin/providers/custom/anthropic")
    assert resp.status_code == 404
    body = resp.json()
    assert body.get("error") == "not_custom"


# ---------------------------------------------------------------------------
# PATCH — partial update + marker preservation
# ---------------------------------------------------------------------------


def test_patch_partial_update_preserves_other_fields(
    client: TestClient, admin_state: AdminState
) -> None:
    """PATCH base_url only — kind / api_key / params survive untouched."""
    create_body = {
        "slug": "mutable",
        "kind": "openai_compatible",
        "base_url": "http://old/v1",
        "api_key": {"value": "sk-old"},
        "params": {"hint": "before"},
    }
    assert client.post("/admin/providers/custom", json=create_body).status_code == 201
    _reload(admin_state)

    resp = client.patch(
        "/admin/providers/custom/mutable",
        json={"base_url": "http://new/v1"},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["base_url"] == "http://new/v1"
    assert updated["kind"] == "openai_compatible"
    assert updated["has_api_key"] is True
    # Marker survives even though caller didn't echo params.
    assert updated["params"]["custom"] is True
    assert updated["params"].get("hint") == "before"


def test_patch_marker_survives_caller_replacing_params(
    client: TestClient, admin_state: AdminState
) -> None:
    """Caller replaces ``params`` wholesale → marker is re-injected."""
    assert (
        client.post(
            "/admin/providers/custom",
            json={"slug": "marker", "kind": "openai_compatible"},
        ).status_code
        == 201
    )
    _reload(admin_state)

    resp = client.patch(
        "/admin/providers/custom/marker",
        json={"params": {"only": "this"}},
    )
    assert resp.status_code == 200
    assert resp.json()["params"] == {"only": "this", "custom": True}

    _reload(admin_state)
    on_disk = _on_disk(admin_state)
    assert on_disk["providers"]["marker"]["params"]["custom"] is True
    assert on_disk["providers"]["marker"]["params"]["only"] == "this"


def test_patch_rejects_unknown_kind(
    client: TestClient, admin_state: AdminState
) -> None:
    """Invalid kind on PATCH must 400 — never write a junk kind."""
    assert (
        client.post(
            "/admin/providers/custom",
            json={"slug": "kindcheck", "kind": "openai_compatible"},
        ).status_code
        == 201
    )
    _reload(admin_state)

    resp = client.patch(
        "/admin/providers/custom/kindcheck",
        json={"kind": "garbage-kind"},
    )
    assert resp.status_code == 400
    assert resp.json().get("error") == "invalid_kind"


def test_patch_missing_slug_returns_404(client: TestClient) -> None:
    resp = client.patch(
        "/admin/providers/custom/ghost",
        json={"kind": "openai_compatible"},
    )
    assert resp.status_code == 404
    assert resp.json().get("error") == "not_found"


def test_patch_refuses_block_without_custom_marker(
    client: TestClient, admin_state: AdminState
) -> None:
    """A built-in block is not paintable as custom via PATCH."""
    snapshot: dict[str, Any] = admin_state.extras["snapshot"]
    snapshot["providers"] = {
        "anthropic": {"kind": "anthropic", "enabled": True},
    }

    resp = client.patch(
        "/admin/providers/custom/anthropic",
        json={"base_url": "https://injected"},
    )
    assert resp.status_code == 404
    assert resp.json().get("error") == "not_custom"


# ---------------------------------------------------------------------------
# Marker round-trip via raw read
# ---------------------------------------------------------------------------


def test_params_custom_marker_round_trips_through_get(
    client: TestClient, admin_state: AdminState
) -> None:
    """POST → on-disk → snapshot → GET all preserve ``params.custom=true``."""
    create = client.post(
        "/admin/providers/custom",
        json={"slug": "marker-trip", "kind": "openai_compatible"},
    )
    assert create.status_code == 201
    assert create.json()["params"]["custom"] is True

    _reload(admin_state)
    on_disk = _on_disk(admin_state)
    assert on_disk["providers"]["marker-trip"]["params"]["custom"] is True

    listed = client.get("/admin/providers/custom").json()
    assert listed["providers"][0]["slug"] == "marker-trip"
    assert listed["providers"][0]["params"]["custom"] is True


def test_has_api_key_reflects_storage_shape(
    client: TestClient, admin_state: AdminState
) -> None:
    """``has_api_key`` is True for {value=…}, {env=…}, and bare strings."""
    snapshot: dict[str, Any] = admin_state.extras["snapshot"]
    snapshot["providers"] = {
        "plain": {
            "kind": "openai_compatible",
            "enabled": True,
            "api_key": "sk-literal",
            "params": {"custom": True},
        },
        "env-shaped": {
            "kind": "openai_compatible",
            "enabled": True,
            "api_key": {"env": "MY_KEY"},
            "params": {"custom": True},
        },
        "value-shaped": {
            "kind": "openai_compatible",
            "enabled": True,
            "api_key": {"value": "sk-val"},
            "params": {"custom": True},
        },
        "keyless": {
            "kind": "openai_compatible",
            "enabled": True,
            "params": {"custom": True},
        },
    }

    listed = client.get("/admin/providers/custom").json()
    by_slug = {p["slug"]: p for p in listed["providers"]}
    assert by_slug["plain"]["has_api_key"] is True
    assert by_slug["env-shaped"]["has_api_key"] is True
    assert by_slug["value-shaped"]["has_api_key"] is True
    assert by_slug["keyless"]["has_api_key"] is False
