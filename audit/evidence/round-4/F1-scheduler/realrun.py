"""R4-F1 real-run verification — boot the ACTUAL gateway lifespan and prove
the scheduler runtime now spawns + fires.

Drives the production ``_lifespan`` via ``app.router.lifespan_context`` (the
same code uvicorn runs on startup/shutdown), on the same event loop the
scheduler tick tasks run on, so we can subscribe to the live hook bus and
watch real firings.

Proves:
  1. The lifespan spawns ``app.state.corlinman_scheduler_handle`` (was None).
  2. A per-second job actually FIRES repeatedly in the booted gateway
     (EngineRunCompleted on the live bus) — the CRITICAL "jobs never run".
  3. The default ``system.update_check`` job is wired as a tick task.
  4. ``app_state`` is the live ``app.state`` (threaded into dispatch).
  5. The admin "fire now" path: ``handle.trigger(name)`` dispatches.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import corlinman_server.gateway.lifecycle.entrypoint as ep
from corlinman_hooks import Closed, HookEvent, HookPriority, Lagged
from corlinman_server.gateway.lifecycle.entrypoint import (
    DEFAULT_UPDATE_CHECK_JOB_NAME,
    build_app,
)

CFG = {
    "system": {"update_check": {"enabled": True, "interval_hours": 6}},
    "scheduler": {
        "jobs": [
            {
                "name": "realrun-tick",
                "cron": "* * * * * *",  # every second
                "action": {"type": "subprocess", "command": "true"},
            }
        ]
    },
}


async def _count_fires(bus, job_name: str, seconds: float) -> int:
    sub = bus.subscribe(HookPriority.NORMAL)
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    count = 0
    while True:
        remaining = end - loop.time()
        if remaining <= 0:
            return count
        try:
            evt = await asyncio.wait_for(sub.recv(), timeout=min(0.5, remaining))
        except TimeoutError:
            continue
        except Lagged:
            continue
        except Closed:
            return count
        if isinstance(evt, HookEvent.EngineRunCompleted):
            count += 1


async def main() -> int:
    with TemporaryDirectory() as td:
        ep._load_config = lambda path: CFG  # type: ignore[assignment]
        cfg_path = Path(td) / "config.toml"
        cfg_path.write_text("# stubbed\n", encoding="utf-8")
        app = build_app(config_path=cfg_path, data_dir=Path(td) / "data")

        async with app.router.lifespan_context(app):
            handle = getattr(app.state, "corlinman_scheduler_handle", None)
            print(f"[1] scheduler_handle present: {handle is not None}")
            assert handle is not None, "FAIL: scheduler runtime not spawned"

            names = sorted(t.get_name() for t in handle.tasks)
            print(f"[3] spawned tick tasks: {names}")
            assert "scheduler-realrun-tick" in names
            assert f"scheduler-{DEFAULT_UPDATE_CHECK_JOB_NAME}" in names, (
                "FAIL: default update_check job not wired"
            )

            live = [t for t in handle.tasks if not t.done()]
            print(f"[1b] live (running) tick tasks: {len(live)}/{len(handle.tasks)}")
            assert len(live) == len(handle.tasks)

            # app_state threaded into the runtime (so run_tool builtins
            # read a live state instead of degrading to checker_unavailable).
            print(f"[4] handle app_state is app.state: {handle._app_state is app.state}")
            assert handle._app_state is app.state

            # Watch the per-second job actually fire in the booted gateway.
            bus = app.state.hook_bus
            fires = await _count_fires(bus, "realrun-tick", 2.6)
            print(f"[2] EngineRunCompleted firings observed in ~2.6s: {fires}")
            assert fires >= 2, f"FAIL: expected >=2 real firings, got {fires}"

            # Admin "fire now" path — manual out-of-band trigger.
            sub = bus.subscribe(HookPriority.NORMAL)
            await handle.trigger("realrun-tick")
            triggered = False
            try:
                for _ in range(20):
                    evt = await asyncio.wait_for(sub.recv(), timeout=0.5)
                    if isinstance(evt, HookEvent.EngineRunCompleted):
                        triggered = True
                        break
            except (TimeoutError, Closed):
                pass
            print(f"[5] handle.trigger('realrun-tick') dispatched: {triggered}")
            assert triggered, "FAIL: trigger() did not dispatch"

            # And the default job is triggerable by name (fire-now for
            # system.update_check); it may report ok or a degraded poll,
            # but it must NOT raise KeyError (job is registered).
            try:
                await asyncio.wait_for(
                    handle.trigger(DEFAULT_UPDATE_CHECK_JOB_NAME), timeout=10.0
                )
                print("[5b] handle.trigger('system.update_check') dispatched without KeyError")
            except KeyError:
                raise AssertionError("FAIL: default job not registered for trigger")
            except TimeoutError:
                print("[5b] update_check trigger timed out on network (acceptable)")

        print("\nALL REAL-RUN CHECKS PASSED ✅ (scheduler spawned + fires in real boot)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
