"""Auto-bind coverage for the ``/admin/providers*`` CRUD endpoints.

Enabling a provider used to leave ``[models]`` empty, which made /chat
fall back to the legacy ``MODEL_PREFIX_DEFAULTS`` table and silently
route to api.openai.com with no key. The auto-bind path now writes
``models.aliases.<name> = {provider, model}`` and ``models.default =
<name>`` the first time an operator enables any provider, so the
in-app chat surface can talk to that provider out of the box.

These tests pin the contract:
* fresh upsert with no models.default → auto-bind kicks in.
* upsert when models.default is already set → existing default is
  preserved (no clobber).
* probe failure (network down, /v1/models 404) → fall back to the
  kind-specific default model id.
* PATCH that enables a previously-disabled provider → also auto-binds.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_b.config_admin import (
    _providers_lib,
)
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


def _stub_probe(monkeypatch: pytest.MonkeyPatch, models: list[str] | None) -> None:
    """Stub ``_query_provider_models`` so tests don't hit the network.

    ``models=None`` simulates a probe failure (``ok=False``); a list
    means the probe succeeds and returns those ids in sorted order.
    """

    async def _fake(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
        if models is None:
            return {"ok": False, "models": [], "latency_ms": 0, "error": "stubbed"}
        return {"ok": True, "models": list(models), "latency_ms": 1, "error": None}

    # ``_autobind_default_alias`` lives in ``_providers_lib`` and resolves
    # ``_query_provider_models`` from that module's globals, so patch it there.
    monkeypatch.setattr(_providers_lib, "_query_provider_models", _fake)


def test_upsert_enable_autobinds_default_from_probed_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    _stub_probe(monkeypatch, ["claude-3", "gpt-4o-mini", "z-model"])

    for _state, client in _with_state(config_path):
        resp = client.post(
            "/admin/providers",
            json={
                "name": "relay-a",
                "kind": "openai_compatible",
                "enabled": True,
                "base_url": "https://cdnapi.example/v1",
                "api_key": {"value": "sk-test"},
            },
        )
        assert resp.status_code == 200, resp.text

        on_disk = _on_disk(config_path)
        # Prefer a well-known id (gpt-4o-mini) over the alphabetical first.
        assert on_disk["models"]["default"] == "relay-a"
        assert on_disk["models"]["aliases"]["relay-a"] == {
            "provider": "relay-a",
            "model": "gpt-4o-mini",
            "params": {},
        }


def test_upsert_credentialed_provider_without_key_does_not_autobind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    _stub_probe(monkeypatch, None)

    for _state, client in _with_state(config_path):
        resp = client.post(
            "/admin/providers",
            json={
                "name": "claude",
                "kind": "anthropic",
                "enabled": True,
            },
        )
        assert resp.status_code == 200, resp.text

        on_disk = _on_disk(config_path)
        assert on_disk["providers"]["claude"]["enabled"] is True
        assert "api_key" not in on_disk["providers"]["claude"]
        assert "models" not in on_disk or not on_disk.get("models", {}).get("default")


def test_upsert_openai_without_key_autobinds_when_env_key_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    _stub_probe(monkeypatch, ["gpt-4o-mini"])
    # OpenAIProvider.build() falls back to OPENAI_API_KEY, so a keyless openai
    # slot is usable and must still autobind a default.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

    for _state, client in _with_state(config_path):
        resp = client.post(
            "/admin/providers",
            json={"name": "openai", "kind": "openai", "enabled": True},
        )
        assert resp.status_code == 200, resp.text

        on_disk = _on_disk(config_path)
        assert "api_key" not in on_disk["providers"]["openai"]
        assert on_disk["models"]["default"] == "openai"
        assert on_disk["models"]["aliases"]["openai"]["provider"] == "openai"


def test_upsert_openai_without_key_or_env_does_not_autobind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    _stub_probe(monkeypatch, ["gpt-4o-mini"])
    # No config key AND no env key → the slot can't authenticate, so no default
    # is bound (the env-var fallback is what makes the keyless case usable).
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    for _state, client in _with_state(config_path):
        resp = client.post(
            "/admin/providers",
            json={"name": "openai", "kind": "openai", "enabled": True},
        )
        assert resp.status_code == 200, resp.text

        on_disk = _on_disk(config_path)
        assert "models" not in on_disk or not on_disk.get("models", {}).get("default")


def test_upsert_does_not_clobber_existing_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[models]
default = "operator-pick"

[models.aliases.operator-pick]
provider = "first"
model = "claude-3-5-sonnet-latest"

[providers.first]
kind = "anthropic"
enabled = true
        """.strip(),
        encoding="utf-8",
    )
    _stub_probe(monkeypatch, ["gpt-4o-mini"])

    for state, client in _with_state(config_path):
        _set_snapshot_from_disk(state)
        resp = client.post(
            "/admin/providers",
            json={
                "name": "second",
                "kind": "openai_compatible",
                "enabled": True,
                "base_url": "https://other.example/v1",
                "api_key": {"value": "sk-other"},
            },
        )
        assert resp.status_code == 200, resp.text

        on_disk = _on_disk(config_path)
        # Operator's explicit default survives the second-provider enable.
        assert on_disk["models"]["default"] == "operator-pick"
        # No alias was auto-added for `second` because default was set.
        assert "second" not in on_disk["models"].get("aliases", {})


def test_upsert_probe_failure_falls_back_to_kind_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    _stub_probe(monkeypatch, None)  # probe fails → kind default kicks in

    for _state, client in _with_state(config_path):
        resp = client.post(
            "/admin/providers",
            json={
                "name": "ds",
                "kind": "deepseek",
                "enabled": True,
                "api_key": {"value": "sk-deepseek"},
            },
        )
        assert resp.status_code == 200, resp.text

        on_disk = _on_disk(config_path)
        assert on_disk["models"]["default"] == "ds"
        assert on_disk["models"]["aliases"]["ds"]["model"] == "deepseek-chat"


def test_patch_enable_triggers_autobind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[providers.sleeper]
kind = "openai_compatible"
enabled = false
base_url = "https://relay.example/v1"
        """.strip(),
        encoding="utf-8",
    )
    _stub_probe(monkeypatch, ["gpt-4o", "gpt-4o-mini"])

    for state, client in _with_state(config_path):
        _set_snapshot_from_disk(state)
        resp = client.patch(
            "/admin/providers/sleeper",
            json={"enabled": True},
        )
        assert resp.status_code == 200, resp.text

        on_disk = _on_disk(config_path)
        assert on_disk["models"]["default"] == "sleeper"
        assert on_disk["models"]["aliases"]["sleeper"]["provider"] == "sleeper"
        assert on_disk["models"]["aliases"]["sleeper"]["model"] == "gpt-4o-mini"


def test_patch_disable_clears_active_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[models]
default = "sleeper"

[models.aliases.sleeper]
provider = "sleeper"
model = "gpt-4o-mini"

[providers.sleeper]
kind = "openai_compatible"
enabled = true
base_url = "https://relay.example/v1"
api_key = "sk-relay"
        """.strip(),
        encoding="utf-8",
    )
    _stub_probe(monkeypatch, ["gpt-4o-mini"])

    for state, client in _with_state(config_path):
        _set_snapshot_from_disk(state)
        resp = client.patch(
            "/admin/providers/sleeper",
            json={"enabled": False},
        )
        assert resp.status_code == 200, resp.text

        on_disk = _on_disk(config_path)
        assert on_disk["providers"]["sleeper"]["enabled"] is False
        assert "sleeper" not in (on_disk.get("models") or {}).get("aliases", {})
        assert (on_disk.get("models") or {}).get("default") != "sleeper"


def test_upsert_disabled_does_not_autobind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    _stub_probe(monkeypatch, ["gpt-4o-mini"])

    for _state, client in _with_state(config_path):
        resp = client.post(
            "/admin/providers",
            json={
                "name": "draft",
                "kind": "openai_compatible",
                "enabled": False,
            },
        )
        assert resp.status_code == 200, resp.text

        on_disk = _on_disk(config_path)
        # Provider stored but no auto-bind because it's disabled.
        assert "draft" in on_disk["providers"]
        assert "models" not in on_disk or not on_disk.get("models", {}).get("default")
