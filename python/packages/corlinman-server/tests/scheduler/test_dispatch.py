"""Port of ``corlinman-scheduler::runtime``'s dispatch + cancel unit tests.

Mirrors the Rust ``mod tests`` in ``src/runtime.rs``:

* ``dispatch_subprocess_success_emits_completed``
* ``dispatch_subprocess_failure_emits_failed_with_exit_code``
* ``dispatch_subprocess_timeout_emits_failed_timeout``
* ``dispatch_subprocess_missing_binary_emits_spawn_failed``
* ``unsupported_action_emits_failed``
* ``cancel_stops_job_loop_promptly``

Hook events flow through :mod:`corlinman_hooks` (the workspace's
Python port of ``corlinman-hooks``). Each test subscribes at
``HookPriority.NORMAL`` and asserts on the first event off the
subscription — the bus delivers in tier order so a single Normal
subscriber sees everything for these tests.
"""

from __future__ import annotations

import asyncio

from corlinman_hooks import HookBus, HookEvent, HookPriority, Lagged
from corlinman_server.scheduler import (
    JobAction,
    JobSpec,
    SchedulerConfig,
    SchedulerJob,
    dispatch,
    spawn,
)


def _spec_for(action: JobAction) -> JobSpec:
    """Build a :class:`JobSpec` directly — tests call :func:`dispatch`
    rather than going through the tick loop, so the cron expression is
    a placeholder. Mirrors the Rust ``spec_for`` helper."""
    cfg = SchedulerJob(name="unit", cron="0 0 0 * * * *", action=action)
    spec = JobSpec.from_config(cfg)
    assert spec is not None, "test cron should parse"
    return spec


async def _next_event(sub) -> object:
    """Receive the next event, with a small timeout so a hung dispatch
    doesn't deadlock the test binary. Mirrors the Rust helper of the
    same name."""

    async def _loop():
        while True:
            try:
                return await sub.recv()
            except Lagged:
                # Slow subscriber surfaced a Lagged exception — skip
                # forward, the bus has already advanced.
                continue

    return await asyncio.wait_for(_loop(), timeout=2.0)


async def test_dispatch_subprocess_success_emits_completed() -> None:
    """A successful firing emits :class:`HookEvent.EngineRunCompleted`."""
    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)
    spec = _spec_for(JobAction.subprocess(command="true", timeout_secs=5))
    await dispatch(spec, bus)
    evt = await _next_event(sub)
    assert isinstance(evt, HookEvent.EngineRunCompleted), f"got {evt!r}"


async def test_dispatch_subprocess_failure_emits_failed_with_exit_code() -> None:
    """``false`` → :class:`HookEvent.EngineRunFailed` with
    ``error_kind = "exit_code"`` and ``exit_code = 1``."""
    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)
    spec = _spec_for(JobAction.subprocess(command="false", timeout_secs=5))
    await dispatch(spec, bus)
    evt = await _next_event(sub)
    assert isinstance(evt, HookEvent.EngineRunFailed), f"got {evt!r}"
    assert evt.error_kind == "exit_code"
    assert evt.exit_code == 1


async def test_dispatch_subprocess_timeout_emits_failed_timeout() -> None:
    """``sleep 30`` with a 1s timeout → ``error_kind = "timeout"`` and
    ``exit_code = None``."""
    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)
    spec = _spec_for(JobAction.subprocess(command="sleep", args=("30",), timeout_secs=1))
    await dispatch(spec, bus)
    evt = await _next_event(sub)
    assert isinstance(evt, HookEvent.EngineRunFailed), f"got {evt!r}"
    assert evt.error_kind == "timeout"
    assert evt.exit_code is None


async def test_dispatch_subprocess_missing_binary_emits_spawn_failed() -> None:
    """A missing binary surfaces as ``error_kind = "spawn_failed"`` —
    the spawn fails before we ever have a child to inspect."""
    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)
    spec = _spec_for(
        JobAction.subprocess(command="/nonexistent/__corlinman_test_bin__", timeout_secs=5)
    )
    await dispatch(spec, bus)
    evt = await _next_event(sub)
    assert isinstance(evt, HookEvent.EngineRunFailed), f"got {evt!r}"
    assert evt.error_kind == "spawn_failed"


async def test_unsupported_action_emits_failed() -> None:
    """``RunAgent`` dispatch with no runner wired must surface
    ``error_kind = "runner_not_registered"`` on the bus — operators see the
    missing wiring instead of a silent drop."""
    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)
    spec = _spec_for(JobAction.run_agent(prompt="x"))
    await dispatch(spec, bus)
    evt = await _next_event(sub)
    assert isinstance(evt, HookEvent.EngineRunFailed), f"got {evt!r}"
    assert evt.error_kind == "runner_not_registered"


async def test_cancel_stops_job_loop_promptly() -> None:
    """A cron that fires only once a year would block forever without
    a working cancel path. Flipping the cancel event must let
    ``join_all`` return inside the 2-second timeout."""
    bus = HookBus(16)
    cancel = asyncio.Event()
    cfg = SchedulerConfig(
        jobs=(
            SchedulerJob(
                name="yearly",
                cron="0 0 0 1 1 * *",  # 00:00:00 on Jan 1, any year
                action=JobAction.subprocess(command="true", timeout_secs=5),
            ),
        )
    )
    handle = spawn(cfg, bus, cancel)
    # Let the loop park on `sleep_until` before flipping cancel — a
    # 50ms yield is enough on every CI host we've observed.
    await asyncio.sleep(0.05)
    cancel.set()
    await asyncio.wait_for(handle.join_all(), timeout=2.0)


async def test_unknown_run_tool_emits_unsupported_action() -> None:
    """``RunTool`` whose ``plugin.tool`` is not in :data:`BUILTIN_ACTIONS`
    must still fall through to ``unsupported_action`` — the bus surfaces
    a misconfigured cron rather than silently dropping it. Mirrors the
    Rust ``RunTool`` fallback for unknown registry keys."""
    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)
    spec = _spec_for(JobAction.run_tool(plugin="unknown_plugin", tool="ghost_tool"))
    await dispatch(spec, bus)
    evt = await _next_event(sub)
    assert isinstance(evt, HookEvent.EngineRunFailed)
    assert evt.error_kind == "unsupported_action"


async def test_dispatch_run_tool_routes_to_builtin_actions_registry() -> None:
    """R3-002 regression: ``JobAction.run_tool(plugin="system",
    tool="update_check")`` is the exact shape ``entrypoint.py`` registers
    for the default ``system.update_check`` cron job. Dispatch MUST route
    it to :data:`BUILTIN_ACTIONS` and emit :class:`HookEvent.EngineRunCompleted`,
    not :class:`HookEvent.EngineRunFailed(error_kind="unsupported_action")`.

    Before the fix this assertion failed because ``dispatch()`` lumped
    every ``run_tool`` firing into the unsupported-action branch — the
    nightly update-check + darwin-curate cron jobs were silently never
    running on production while the admin "fire now" route (which calls
    ``run_builtin()`` directly) masked the bug.
    """
    from corlinman_server.scheduler.builtins import (
        BUILTIN_ACTIONS,
        BuiltinContext,
    )

    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)

    call_count = 0
    captured_ctx: list[BuiltinContext] = []

    async def _stub_action(context: BuiltinContext) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        captured_ctx.append(context)
        return {"ok": True}

    # Monkeypatch the registry in-place; restore afterwards so other
    # tests in the suite see the real builtin. (The registry is module
    # global; ``register_builtin`` uses last-in-wins semantics.)
    previous = BUILTIN_ACTIONS.get("system.update_check")
    BUILTIN_ACTIONS["system.update_check"] = _stub_action
    try:
        spec = _spec_for(JobAction.run_tool(plugin="system", tool="update_check"))
        await dispatch(spec, bus)
        evt = await _next_event(sub)
    finally:
        if previous is None:
            BUILTIN_ACTIONS.pop("system.update_check", None)
        else:
            BUILTIN_ACTIONS["system.update_check"] = previous

    assert call_count == 1, (
        f"BUILTIN_ACTIONS['system.update_check'] should fire exactly once; got {call_count}"
    )
    assert not isinstance(evt, HookEvent.EngineRunFailed) or evt.error_kind != "unsupported_action", (
        f"run_tool dispatch must not emit unsupported_action when the builtin exists; got {evt!r}"
    )
    assert isinstance(evt, HookEvent.EngineRunCompleted), (
        f"successful run_tool dispatch should emit EngineRunCompleted; got {evt!r}"
    )


async def test_dispatch_run_tool_evolution_darwin_curate_routes_to_builtin() -> None:
    """Second R3-002 regression: the ``evolution.darwin_curate`` default
    job (entrypoint.py:590) must also reach the registry. Parametrising
    over both default jobs would hide which one regresses, so they get
    distinct tests."""
    from corlinman_server.scheduler.builtins import (
        BUILTIN_ACTIONS,
        BuiltinContext,
    )

    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)

    call_count = 0

    async def _stub_action(context: BuiltinContext) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        return {"ok": True}

    previous = BUILTIN_ACTIONS.get("evolution.darwin_curate")
    BUILTIN_ACTIONS["evolution.darwin_curate"] = _stub_action
    try:
        spec = _spec_for(
            JobAction.run_tool(plugin="evolution", tool="darwin_curate")
        )
        await dispatch(spec, bus)
        evt = await _next_event(sub)
    finally:
        if previous is None:
            BUILTIN_ACTIONS.pop("evolution.darwin_curate", None)
        else:
            BUILTIN_ACTIONS["evolution.darwin_curate"] = previous

    assert call_count == 1
    assert isinstance(evt, HookEvent.EngineRunCompleted), (
        f"evolution.darwin_curate dispatch should emit EngineRunCompleted; got {evt!r}"
    )


async def test_dispatch_run_tool_builtin_returning_not_ok_emits_failed() -> None:
    """When the registered builtin returns ``{"ok": False, ...}`` the
    bus should see ``EngineRunFailed`` (not Completed) so operators
    notice the degraded run. Mirrors the admin "fire now" route's own
    ok-vs-error handling."""
    from corlinman_server.scheduler.builtins import (
        BUILTIN_ACTIONS,
        BuiltinContext,
    )

    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)

    async def _stub_action(context: BuiltinContext) -> dict[str, object]:
        return {"ok": False, "reason": "checker_unavailable"}

    previous = BUILTIN_ACTIONS.get("system.update_check")
    BUILTIN_ACTIONS["system.update_check"] = _stub_action
    try:
        spec = _spec_for(JobAction.run_tool(plugin="system", tool="update_check"))
        await dispatch(spec, bus)
        evt = await _next_event(sub)
    finally:
        if previous is None:
            BUILTIN_ACTIONS.pop("system.update_check", None)
        else:
            BUILTIN_ACTIONS["system.update_check"] = previous

    assert isinstance(evt, HookEvent.EngineRunFailed)
    # error_kind reflects "builtin reported non-ok" rather than a
    # transport/unknown failure — distinct from unsupported_action.
    assert evt.error_kind != "unsupported_action"
