"""R4-F1 — the scheduler *runtime* is actually spawned by the gateway lifespan.

Round-4 audit finding F1 (CRITICAL): rounds 1-3 fixed ``dispatch()`` routing
(R3-002) but **nothing ever calls ``scheduler.runner.spawn()``**, so the
per-job tick loops are never created and the default cron jobs
(``system.update_check`` / ``evolution.darwin_curate``) never fire. The prior
FINAL_REPORT claimed "default scheduled jobs actually run" — they did not.

Two independent gaps are covered here:

1. ``spawn`` / ``_run_job_loop`` never threaded ``app_state`` into
   ``dispatch`` — so even if spawned, every ``run_tool`` builtin would
   degrade to ``checker_unavailable``.
2. The gateway lifespan never spawns the runtime at all.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_server.gateway.lifecycle.entrypoint import (  # noqa: E402
    DEFAULT_UPDATE_CHECK_JOB_NAME,
    build_app,
)
from corlinman_server.scheduler import (  # noqa: E402
    JobAction,
    SchedulerConfig,
    SchedulerJob,
    spawn,
)
from corlinman_server.scheduler import runner as _runner  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


async def test_spawn_threads_app_state_into_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``spawn(..., app_state=X)`` must propagate ``X`` all the way down to
    ``dispatch`` so ``run_tool`` builtins read a live ``app.state``.

    Deterministic: we neutralise the real sleep so the loop fires once
    immediately, capture the ``dispatch`` call, then flip cancel so the
    loop exits.
    """
    from corlinman_hooks import HookBus

    sentinel = object()
    captured: dict[str, object] = {}
    cancel = asyncio.Event()

    async def _fake_dispatch(spec, bus, app_state=None):  # type: ignore[no-untyped-def]
        captured["app_state"] = app_state
        cancel.set()  # exit the loop after one fire

    async def _no_sleep(deadline, cancel_evt, extra_cancel=None):  # type: ignore[no-untyped-def]
        return False  # never "cancelled while sleeping" → proceed to dispatch

    monkeypatch.setattr(_runner, "dispatch", _fake_dispatch)
    monkeypatch.setattr(_runner, "_sleep_until", _no_sleep)

    cfg = SchedulerConfig(
        jobs=(
            SchedulerJob(
                name="tick",
                cron="* * * * * *",  # every second (6-field)
                action=JobAction.subprocess(command="true"),
            ),
        )
    )
    handle = spawn(cfg, HookBus(16), cancel, app_state=sentinel)
    await asyncio.wait_for(handle.join_all(), timeout=5.0)

    assert captured.get("app_state") is sentinel


def _monkey_loaded_config(monkeypatch: pytest.MonkeyPatch, cfg: dict | None) -> None:
    monkeypatch.setattr(
        "corlinman_server.gateway.lifecycle.entrypoint._load_config",
        lambda path: cfg,
    )


def test_lifespan_spawns_scheduler_runtime_for_default_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Booting the gateway with the default update-check config must spawn a
    live scheduler runtime: ``app.state.corlinman_scheduler_handle`` carries
    a running ``scheduler-system.update_check`` tick task."""
    _monkey_loaded_config(
        monkeypatch,
        {"system": {"update_check": {"enabled": True, "interval_hours": 6}}},
    )
    fake_cfg_path = tmp_path / "config.toml"
    fake_cfg_path.write_text("# stubbed", encoding="utf-8")

    app = build_app(config_path=fake_cfg_path, data_dir=tmp_path / "data")

    with TestClient(app):
        handle = getattr(app.state, "corlinman_scheduler_handle", None)
        assert handle is not None, "scheduler runtime was never spawned"
        task_names = {t.get_name() for t in handle.tasks}
        assert f"scheduler-{DEFAULT_UPDATE_CHECK_JOB_NAME}" in task_names
        # The tick task must be live (not already finished/cancelled).
        live = [t for t in handle.tasks if not t.done()]
        assert live, "scheduler tick task is not running"


def test_effective_scheduler_config_merges_config_and_default_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config-defined ``[[scheduler.jobs]]`` entry (not one of the
    auto-registered defaults) must also be spawned — config jobs were
    display-only in the Python port before F1."""
    _monkey_loaded_config(
        monkeypatch,
        {
            "system": {"update_check": {"enabled": True, "interval_hours": 6}},
            "scheduler": {
                "jobs": [
                    {
                        "name": "nightly-brief",
                        "cron": "0 0 3 * * * *",
                        "action": {"type": "subprocess", "command": "true"},
                    }
                ]
            },
        },
    )
    fake_cfg_path = tmp_path / "config.toml"
    fake_cfg_path.write_text("# stubbed", encoding="utf-8")

    app = build_app(config_path=fake_cfg_path, data_dir=tmp_path / "data")

    with TestClient(app):
        handle = getattr(app.state, "corlinman_scheduler_handle", None)
        assert handle is not None
        task_names = {t.get_name() for t in handle.tasks}
        # Both the config-defined job AND the auto-registered default fire.
        assert "scheduler-nightly-brief" in task_names
        assert f"scheduler-{DEFAULT_UPDATE_CHECK_JOB_NAME}" in task_names
