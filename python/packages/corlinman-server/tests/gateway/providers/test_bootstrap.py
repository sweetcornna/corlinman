"""Tests for ``gateway.providers`` — Parcel P1 provider-registry wiring.

Covers :func:`bootstrap`, :func:`build_registry`, :class:`RegistryModelSource`
and :func:`model_source_for` against the resolved-dict config shape the
P0 loader produces (see ``docs/contracts/runtime-wiring.md`` §1.1).
"""

from __future__ import annotations

from corlinman_providers.registry import ProviderRegistry

from corlinman_server.gateway.core.state import AppState
from corlinman_server.gateway.providers import (
    RegistryModelSource,
    bootstrap,
    build_registry,
    model_source_for,
)
from corlinman_server.gateway.routes.models import ModelEntry, router

# A config shaped like ``load_from_path`` output: env-refs already
# resolved, dict access throughout.
_FULL_CONFIG = {
    "providers": {
        "openai": {"kind": "openai", "api_key": "sk-test"},
        "myproxy": {
            "kind": "openai_compatible",
            "base_url": "http://localhost:9999/v1",
            "api_key": None,
        },
    },
    "models": {
        "default": "gpt-4o-mini",
        "aliases": {
            "gpt-4o-mini": {"provider": "openai", "model": "gpt-4o-mini"},
            "gpt-4o": {"provider": "openai", "model": "gpt-4o"},
        },
    },
}


# ---------------------------------------------------------------------------
# build_registry
# ---------------------------------------------------------------------------


def test_build_registry_from_full_config() -> None:
    registry = build_registry(_FULL_CONFIG)
    assert isinstance(registry, ProviderRegistry)
    names = {s.name for s in registry.list_specs()}
    assert names == {"openai", "myproxy"}


def test_build_registry_skips_invalid_kind() -> None:
    """A bad ``kind`` is logged + skipped; the rest of the registry survives."""
    cfg = {
        "providers": {
            "openai": {"kind": "openai", "api_key": "sk-test"},
            "bogus": {"kind": "gemini"},  # not a valid ProviderKind
        }
    }
    registry = build_registry(cfg)
    names = {s.name for s in registry.list_specs()}
    assert names == {"openai"}


def test_build_registry_empty_section() -> None:
    """No ``[providers]`` -> a specs-less registry (legacy-prefix fallback)."""
    registry = build_registry({})
    assert isinstance(registry, ProviderRegistry)
    assert registry.list_specs() == []


def test_build_registry_none_config() -> None:
    registry = build_registry(None)
    assert isinstance(registry, ProviderRegistry)
    assert registry.list_specs() == []


def test_build_registry_malformed_section() -> None:
    """A non-table ``[providers]`` value degrades to an empty registry."""
    registry = build_registry({"providers": "not-a-table"})
    assert registry.list_specs() == []


def test_build_registry_skips_non_table_entry() -> None:
    cfg = {
        "providers": {
            "openai": {"kind": "openai", "api_key": "sk-test"},
            "weird": "not-a-table",
        }
    }
    registry = build_registry(cfg)
    assert {s.name for s in registry.list_specs()} == {"openai"}


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_attaches_registry_and_source() -> None:
    state = AppState(config=_FULL_CONFIG)
    result = bootstrap(state)
    assert result is None  # no background tasks
    assert isinstance(state.provider_registry, ProviderRegistry)
    assert isinstance(state.extras.get("models_source"), RegistryModelSource)


def test_bootstrap_degraded_when_no_config() -> None:
    """A ``None`` config still yields a (specs-less) registry, not a crash."""
    state = AppState(config=None)
    bootstrap(state)
    assert isinstance(state.provider_registry, ProviderRegistry)
    assert state.provider_registry.list_specs() == []


# ---------------------------------------------------------------------------
# RegistryModelSource / model_source_for
# ---------------------------------------------------------------------------


def test_model_source_lists_aliases_and_providers() -> None:
    state = AppState(config=_FULL_CONFIG)
    bootstrap(state)
    source = model_source_for(state)
    assert source is not None
    ids = [e.id for e in source.list_models() if e.id != "codex"]
    # aliases first (including o4-mini when Codex is auto-detected), then slot names
    assert ids == ["gpt-4o-mini", "gpt-4o", "openai", "myproxy"]
    entries = list(source.list_models())
    assert all(isinstance(e, ModelEntry) for e in entries)
    by_id = {e.id: e for e in entries}
    assert by_id["gpt-4o-mini"].owned_by == "openai"
    assert by_id["myproxy"].owned_by == "myproxy"


def test_model_source_dedups() -> None:
    """An alias that collides with a provider slot name is listed once."""
    cfg = {
        "providers": {"openai": {"kind": "openai", "api_key": "sk-test"}},
        "models": {"aliases": {"openai": {"provider": "openai", "model": "gpt-4o"}}},
    }
    registry = build_registry(cfg)
    source = RegistryModelSource(registry, cfg)
    ids = [e.id for e in source.list_models()]
    assert ids.count("openai") == 1


def test_model_source_for_none_when_no_registry() -> None:
    state = AppState(config=_FULL_CONFIG)  # bootstrap NOT called
    assert model_source_for(state) is None


def test_model_source_no_aliases_lists_provider_slots() -> None:
    cfg = {"providers": {"openai": {"kind": "openai", "api_key": "sk-test"}}}
    registry = build_registry(cfg)
    source = RegistryModelSource(registry, cfg)
    assert [e.id for e in source.list_models()] == ["openai"]


# ---------------------------------------------------------------------------
# /v1/models would return 200 once the source is wired in
# ---------------------------------------------------------------------------


def test_v1_models_route_200_with_registry_source() -> None:
    """End-to-end: the route built with a RegistryModelSource returns 200."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    state = AppState(config=_FULL_CONFIG)
    bootstrap(state)
    source = model_source_for(state)

    app = FastAPI()
    app.include_router(router(source))
    client = TestClient(app)

    resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    ids = {row["id"] for row in body["data"]}
    assert {"gpt-4o-mini", "gpt-4o", "openai", "myproxy"} <= ids


def test_v1_models_route_501_without_source() -> None:
    """Sanity: with no source the route keeps the typed 501 envelope."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router(None))
    resp = TestClient(app).get("/v1/models")
    assert resp.status_code == 501
    assert resp.json()["error"] == "not_implemented"
