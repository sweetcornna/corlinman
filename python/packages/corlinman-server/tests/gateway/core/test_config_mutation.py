"""Unit tests for the extracted atomic config writer (modularization Phase 1)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_server.gateway.core.config_mutation import (
    publish_config_mutation,
    write_config_atomic,
)


def test_write_config_atomic_roundtrips_toml(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "config.toml"
    result = write_config_atomic(target, {"server": {"public_url": "https://x"}})

    assert result is None  # success short-circuits with None
    assert target.exists()
    assert tomllib.loads(target.read_text()) == {"server": {"public_url": "https://x"}}
    # the sibling temp file is renamed away, not left behind
    assert not target.with_suffix(target.suffix + ".new").exists()


def test_write_config_atomic_reports_serialise_failure(tmp_path: Path) -> None:
    # A set is not TOML-serialisable -> 500 JSONResponse, not an exception.
    result = write_config_atomic(tmp_path / "config.toml", {"bad": {1, 2, 3}})

    assert result is not None
    assert result.status_code == 500


def test_onboard_reexport_shim_points_at_the_same_callable() -> None:
    # Old import path must keep working for backward compatibility.
    from corlinman_server.gateway.routes_admin_b.config_admin.onboard import _write_config_atomic

    assert _write_config_atomic is write_config_atomic


@pytest.mark.asyncio
async def test_publish_config_mutation_treats_py_config_write_as_best_effort(
    tmp_path: Path,
) -> None:
    cfg = {"providers": {"relay": {"kind": "openai_compatible"}}}
    swapped: list[dict] = []

    async def swap_fn(next_cfg: dict) -> None:
        swapped.append(next_cfg)

    async def failing_py_config_writer(next_cfg: dict, path: Path) -> None:
        raise OSError("sidecar path is unwritable")

    state = SimpleNamespace(
        extras={"config_swap_fn": swap_fn},
        py_config_path=tmp_path / "py-config.json",
    )

    await publish_config_mutation(
        state,
        cfg,
        py_config_writer=failing_py_config_writer,
    )

    assert swapped == [cfg]


@pytest.mark.asyncio
async def test_publish_config_mutation_refreshes_provider_registry(
    tmp_path: Path,
) -> None:
    from corlinman_providers.registry import ProviderRegistry
    from corlinman_server.gateway.providers import (
        RegistryModelSource,
        build_registry,
    )

    original_cfg: dict[str, Any] = {"providers": {}}
    next_cfg: dict[str, Any] = {
        "providers": {"mock": {"kind": "mock", "enabled": True}},
        "models": {
            "aliases": {
                "mock-chat": {
                    "provider": "mock",
                    "model": "mock",
                    "params": {},
                }
            }
        },
    }

    def swap_fn(next_config: dict[str, Any]) -> None:
        state.config = next_config

    state = SimpleNamespace(
        config=original_cfg,
        data_dir=tmp_path,
        extras={"config_swap_fn": swap_fn},
        provider_registry=build_registry(original_cfg, data_dir=tmp_path),
    )
    old_registry = state.provider_registry

    await publish_config_mutation(state, next_cfg)

    assert isinstance(state.provider_registry, ProviderRegistry)
    assert state.provider_registry is not old_registry
    assert {spec.name for spec in state.provider_registry.list_specs()} == {"mock"}
    source = state.extras.get("models_source")
    assert isinstance(source, RegistryModelSource)
    assert [entry.id for entry in source.list_models()] == ["mock-chat", "mock"]
