"""``[subagent] max_concurrent_per_tenant`` is threaded into the dispatcher.

Regression for the real no-effect bug: ``build_app`` constructed the
:class:`AsyncSubagentDispatcher` without ``max_concurrent_per_tenant``, so
it silently fell back to the hardcoded
``DEFAULT_MAX_CONCURRENT_PER_TENANT`` (15) and any value an operator set in
``[subagent]`` had no effect on the per-tenant ceiling.

These tests boot the gateway with a config file and assert the dispatcher
published on ``app.state`` carries the configured ceiling.
"""

from __future__ import annotations

from pathlib import Path

from corlinman_server.gateway.lifecycle.entrypoint import build_app
from corlinman_server.system.subagent.dispatcher import (
    DEFAULT_MAX_CONCURRENT_PER_TENANT,
)
from fastapi.testclient import TestClient


def _write_cfg(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_dispatcher_uses_configured_per_tenant_ceiling(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        "[subagent]\nmax_concurrent_per_tenant = 3\n",
    )
    app = build_app(config_path=cfg, data_dir=tmp_path / "data")

    # The dispatcher is wired inside the lifespan, which fires on enter.
    with TestClient(app):
        dispatcher = getattr(
            app.state, "corlinman_subagent_dispatcher", None
        )
        assert dispatcher is not None, "dispatcher never wired"
        assert dispatcher.max_concurrent_per_tenant == 3


def test_dispatcher_falls_back_to_default_without_subagent_section(
    tmp_path: Path,
) -> None:
    # No [subagent] section at all → keep the hardcoded default.
    cfg = _write_cfg(tmp_path, "[server]\nport = 8080\n")
    app = build_app(config_path=cfg, data_dir=tmp_path / "data")

    with TestClient(app):
        dispatcher = getattr(
            app.state, "corlinman_subagent_dispatcher", None
        )
        assert dispatcher is not None, "dispatcher never wired"
        assert (
            dispatcher.max_concurrent_per_tenant
            == DEFAULT_MAX_CONCURRENT_PER_TENANT
        )


def test_dispatcher_ignores_nonpositive_config_value(tmp_path: Path) -> None:
    # A 0 / negative value is nonsensical for a ceiling — fall back to the
    # default rather than admitting a deadlocking "0 concurrent" policy.
    cfg = _write_cfg(
        tmp_path,
        "[subagent]\nmax_concurrent_per_tenant = 0\n",
    )
    app = build_app(config_path=cfg, data_dir=tmp_path / "data")

    with TestClient(app):
        dispatcher = getattr(
            app.state, "corlinman_subagent_dispatcher", None
        )
        assert dispatcher is not None, "dispatcher never wired"
        assert (
            dispatcher.max_concurrent_per_tenant
            == DEFAULT_MAX_CONCURRENT_PER_TENANT
        )
