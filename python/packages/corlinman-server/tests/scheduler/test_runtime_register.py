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


# ---------------------------------------------------------------------------
# B4 (4d): publish-time jitter — JobSpec.jitter_secs + tick-loop sampling
# ---------------------------------------------------------------------------


def test_runtime_job_spec_carries_jitter_secs() -> None:
    """``jitter_secs`` rides onto the spec; default is 0 and negatives floor
    to 0 (no jitter)."""
    spec = runtime_job_spec("rtj", "0 9 * * *", "qzone.daily_publish", jitter_secs=90)
    assert spec is not None and spec.jitter_secs == 90
    # Default off.
    spec_def = runtime_job_spec("rtj", "0 9 * * *", "qzone.daily_publish")
    assert spec_def is not None and spec_def.jitter_secs == 0
    # Negative floored to 0.
    spec_neg = runtime_job_spec(
        "rtj", "0 9 * * *", "qzone.daily_publish", jitter_secs=-5
    )
    assert spec_neg is not None and spec_neg.jitter_secs == 0


async def test_run_job_loop_samples_jitter_within_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tick loop tacks on ``random.uniform(0, jitter_secs)`` after the
    computed wait. Patch ``random.uniform`` to capture its bounds (and prove
    the sample is drawn from ``[0, jitter_secs]``), then short-circuit the
    sleep so the loop exits after one wait-calc without firing."""
    from corlinman_server.scheduler import runner as runner_mod

    spec = runtime_job_spec("rtj", "* * * * *", "test.noop", jitter_secs=120)
    assert spec is not None and spec.jitter_secs == 120

    seen: list[tuple[float, float]] = []

    def _fake_uniform(a: float, b: float) -> float:
        seen.append((a, b))
        return (a + b) / 2.0  # deterministic mid-point, within [a, b]

    monkeypatch.setattr(runner_mod.random, "uniform", _fake_uniform)

    async def _fake_sleep_until(
        deadline: float,
        cancel: asyncio.Event,
        extra_cancel: asyncio.Event | None = None,
    ) -> bool:
        return True  # report cancelled so the loop exits after the wait-calc

    monkeypatch.setattr(runner_mod, "_sleep_until", _fake_sleep_until)

    bus = HookBus(16)
    cancel = asyncio.Event()
    await runner_mod._run_job_loop(spec, bus, cancel, None, catch_up=False)

    # uniform() called exactly once with the [0, jitter_secs] bounds.
    assert seen == [(0.0, 120.0)]
    lo, hi = seen[0]
    assert 0.0 <= (lo + hi) / 2.0 <= 120.0


async def test_run_job_loop_no_jitter_call_when_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job with ``jitter_secs == 0`` (config jobs / default) never touches
    ``random.uniform`` — the timing stays byte-identical to pre-B4."""
    from corlinman_server.scheduler import runner as runner_mod

    spec = runtime_job_spec("rtj", "* * * * *", "test.noop")
    assert spec is not None and spec.jitter_secs == 0

    called = False

    def _fake_uniform(a: float, b: float) -> float:  # pragma: no cover
        nonlocal called
        called = True
        return 0.0

    monkeypatch.setattr(runner_mod.random, "uniform", _fake_uniform)

    async def _fake_sleep_until(
        deadline: float,
        cancel: asyncio.Event,
        extra_cancel: asyncio.Event | None = None,
    ) -> bool:
        return True

    monkeypatch.setattr(runner_mod, "_sleep_until", _fake_sleep_until)

    bus = HookBus(16)
    await runner_mod._run_job_loop(spec, bus, asyncio.Event(), None, catch_up=False)
    assert called is False


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
