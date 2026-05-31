"""Gateway config hot-reload integration — Parcel P11.

Drives :func:`build_app` with a real ``config.toml`` and runs the
lifespan via :class:`TestClient`. Asserts that the ConfigWatcher armed
in the lifespan:

* updates ``AppState.config`` (the live snapshot) on an edit;
* rebuilds ``AppState.provider_registry`` when ``[providers]`` changes
  so a newly-added provider appears live without a restart;
* keeps the previous good snapshot when the reloaded file is malformed
  — the gateway stays up.

The watcher's debounce / fs-observer paths are exercised indirectly:
the deterministic seam every reload source funnels through is
``ConfigWatcher.trigger_reload``, so the test edits the file then drives
that directly off ``AppState.config_watcher``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from corlinman_server.gateway.lifecycle.entrypoint import build_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_ONE_PROVIDER = """\
[server]
bind = "127.0.0.1:6005"

[providers.openai]
kind = "openai"
api_key = "sk-original"
"""

_TWO_PROVIDERS = """\
[server]
bind = "127.0.0.1:6005"

[providers.openai]
kind = "openai"
api_key = "sk-original"

[providers.myproxy]
kind = "openai_compatible"
base_url = "http://localhost:9999/v1"
"""

_RESTART_SECTION = """\
[server]
bind = "0.0.0.0:7777"

[providers.openai]
kind = "openai"
api_key = "sk-original"
"""

_MALFORMED = "[providers.openai\nkind = broken"


@pytest.fixture(autouse=True)
def _enable_hot_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live fs-watch hot-reload is opt-in (default OFF) since gap-fill v1.15
    — a real fs observer per boot accumulates OS watch handles across the
    suite. These integration tests explicitly opt in so the ConfigWatcher
    arms in the lifespan; the env var is reverted automatically after each
    test, so no other test boots a watcher."""
    monkeypatch.setenv("CORLINMAN_CONFIG_HOT_RELOAD", "1")


def _provider_names(state) -> set[str]:
    registry = state.provider_registry
    assert registry is not None
    return {spec.name for spec in registry.list_specs()}


def test_watcher_armed_and_snapshot_updates(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_ONE_PROVIDER, encoding="utf-8")

    app = build_app(config_path=cfg_path, data_dir=tmp_path / "data")

    with TestClient(app) as client:  # runs the lifespan
        client.get("/health")
        state = app.state.corlinman

        # The watcher was armed and exposed on the live AppState.
        watcher = state.config_watcher
        assert watcher is not None
        assert state.extras.get("config_watcher") is watcher

        # Edit the file: rotate the api_key.
        cfg_path.write_text(
            _ONE_PROVIDER.replace("sk-original", "sk-rotated"),
            encoding="utf-8",
        )
        report = client.portal.call(watcher.trigger_reload)

        assert report.errors == []
        assert "providers" in report.changed_sections
        # The live AppState snapshot reflects the edit.
        assert state.config["providers"]["openai"]["api_key"] == "sk-rotated"


def test_adding_provider_rebuilds_registry(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_ONE_PROVIDER, encoding="utf-8")

    app = build_app(config_path=cfg_path, data_dir=tmp_path / "data")

    with TestClient(app) as client:
        client.get("/health")
        state = app.state.corlinman

        # Boot-time registry: just the one provider (strip Codex if
        # ~/.codex/auth.json exists on the dev machine).
        assert _provider_names(state) - {"codex"} == {"openai"}
        first_registry = state.provider_registry

        # Add a second provider to the file, then reload.
        cfg_path.write_text(_TWO_PROVIDERS, encoding="utf-8")
        report = client.portal.call(state.config_watcher.trigger_reload)

        assert "providers" in report.changed_sections
        # The registry was rebuilt and the new provider is live.
        assert _provider_names(state) - {"codex"} == {"openai", "myproxy"}
        # bootstrap replaced the handle, not mutated it in place.
        assert state.provider_registry is not first_registry


def test_malformed_reload_keeps_previous_snapshot(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_TWO_PROVIDERS, encoding="utf-8")

    app = build_app(config_path=cfg_path, data_dir=tmp_path / "data")

    with TestClient(app) as client:
        client.get("/health")
        state = app.state.corlinman
        assert _provider_names(state) - {"codex"} == {"openai", "myproxy"}
        good_registry = state.provider_registry

        # Corrupt the file, then reload.
        cfg_path.write_text(_MALFORMED, encoding="utf-8")
        report = client.portal.call(state.config_watcher.trigger_reload)

        assert report.errors  # parse failure recorded
        assert report.changed_sections == []
        # Previous good snapshot + registry retained — gateway stays up.
        assert state.config["providers"]["myproxy"]["kind"] == "openai_compatible"
        assert state.provider_registry is good_registry
        assert _provider_names(state) - {"codex"} == {"openai", "myproxy"}

        # Gateway still serves requests after a failed reload.
        assert client.get("/health").status_code == 200


def test_restart_required_section_does_not_crash(tmp_path: Path) -> None:
    """A change to a restart-required section (``[server]``) is reloaded
    into the snapshot but logged as needing a restart — never a crash."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_ONE_PROVIDER, encoding="utf-8")

    app = build_app(config_path=cfg_path, data_dir=tmp_path / "data")

    with TestClient(app) as client:
        client.get("/health")
        state = app.state.corlinman

        cfg_path.write_text(_RESTART_SECTION, encoding="utf-8")
        report = client.portal.call(state.config_watcher.trigger_reload)

        assert "server" in report.changed_sections
        # The snapshot still picked up the new value.
        assert state.config["server"]["bind"] == "0.0.0.0:7777"
        # Gateway is still alive.
        assert client.get("/health").status_code == 200
