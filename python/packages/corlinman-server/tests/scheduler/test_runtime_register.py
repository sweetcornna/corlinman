"""``SchedulerHandle.register`` / ``.unregister`` + ``runtime_job_spec``.

These back the admin scheduler routes' hot create / edit / pause / resume:
a runtime job's tick loop can be (re)attached to a *running* scheduler
without a full restart, and torn down again when the job is paused.

The integration angle (does the registered loop actually fire?) is
covered indirectly: we register a per-second job, assert the loop emits
on the bus, then unregister and assert it stops.
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_hooks import HookBus, HookPriority
from corlinman_server.scheduler import (
    SchedulerConfig,
    register_builtin,
    runtime_job_spec,
    spawn,
)


def test_runtime_job_spec_maps_action_type_to_run_tool() -> None:
    spec = runtime_job_spec("rt.daily", "0 9 * * *", "qzone.daily_publish")
    assert spec is not None
    assert spec.name == "rt.daily"
    assert spec.action.kind == "run_tool"
    assert spec.action.plugin == "qzone"
    assert spec.action.tool == "daily_publish"


def test_runtime_job_spec_rejects_bad_cron() -> None:
    assert runtime_job_spec("x", "not-a-cron", "qzone.daily_publish") is None


def test_runtime_job_spec_rejects_actionless_slug() -> None:
    assert runtime_job_spec("x", "0 9 * * *", "noplugin") is None


async def test_register_into_empty_handle_adds_task() -> None:
    bus = HookBus(16)
    handle = spawn(SchedulerConfig(jobs=()), bus)
    assert handle.tasks == []
    spec = runtime_job_spec("rt", "0 9 * * *", "qzone.daily_publish")
    assert spec is not None
    assert handle.register(spec) is True
    assert handle.has_job("rt")
    assert len(handle.tasks) == 1
    handle.unregister("rt")
    assert not handle.has_job("rt")
    handle.cancel()
    await handle.join_all()


@pytest.mark.slow
async def test_registered_loop_fires_then_stops_on_unregister() -> None:
    """End-to-end: register a per-second builtin job, see it fire, then
    unregister and confirm it goes quiet."""
    fired: list[str] = []

    async def _builtin(ctx: object) -> dict[str, bool]:
        fired.append("hit")
        return {"ok": True}

    register_builtin("test.persecond", _builtin)
    bus = HookBus(64)
    sub = bus.subscribe(HookPriority.NORMAL)
    handle = spawn(SchedulerConfig(jobs=()), bus)
    try:
        spec = runtime_job_spec("rt", "* * * * * *", "test.persecond")
        assert spec is not None
        assert handle.register(spec) is True

        # Drain a couple of completions to confirm the loop is live.
        async def _wait_one(timeout: float) -> bool:
            try:
                await asyncio.wait_for(sub.recv(), timeout=timeout)
                return True
            except TimeoutError:
                return False

        assert await _wait_one(3.0)
        before = len(fired)
        assert before >= 1

        handle.unregister("rt")
        # Give the cancelled loop a beat to stop, then confirm no further
        # firings accumulate over a 2s window.
        await asyncio.sleep(0.2)
        steady = len(fired)
        await asyncio.sleep(2.0)
        assert len(fired) <= steady + 1  # at most one in-flight straggler
    finally:
        handle.cancel()
        await handle.join_all()
