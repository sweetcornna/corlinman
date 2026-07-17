"""gap-fill v1.15 — CONTRACT C2 wiring-spine tests (wire-B).

Covers the boot-lifespan wiring the wire-B lane owns:

* :class:`AppState` carries the six C2 attribute slots.
* :func:`_wire_c2_handles` populates ``memory_host`` / ``persona_resolver``
  / ``identity_store`` / ``agent_runner_fn`` / ``hook_runner`` against a
  temp data dir, and stamps the identity store onto the AdminState so the
  ``/admin/identity*`` routes un-503.
* The identity-route disabled gate flips from 503 → store once the store
  is assigned.
* The ergonomic ``every <N><unit>`` cron grammar.
* The scheduler missed-run catch-up store resolution.

These avoid spinning a full FastAPI app — they exercise the wiring helper
+ the route's ``_require_store`` gate directly, which is the load-bearing
contract.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_server.gateway.core.state import AppState

# NOTE: every test that calls ``_wire_c2_handles`` must close through
# the shared ``close_c2_handles`` conftest fixture — a leaked aiosqlite
# connection parks a non-daemon worker thread that blocks interpreter
# exit AFTER the test summary (the historical "py-test hangs to the CI
# cap" failure mode).

# ---------------------------------------------------------------------------
# AppState C2 slots
# ---------------------------------------------------------------------------


def test_appstate_has_c2_slots_defaulting_none() -> None:
    s = AppState()
    for attr in (
        "memory_host",
        "persona_resolver",
        "identity_store",
        "agent_runner_fn",
        "hook_runner",
        "config_watcher",
    ):
        assert hasattr(s, attr), f"AppState missing C2 slot {attr!r}"
        assert getattr(s, attr) is None


# ---------------------------------------------------------------------------
# _wire_c2_handles
# ---------------------------------------------------------------------------


async def test_wire_c2_handles_populates_all_slots(
    tmp_path: Path, close_c2_handles: Any
) -> None:
    from corlinman_server.gateway.lifecycle.entrypoint import _wire_c2_handles

    state = AppState()
    state.data_dir = tmp_path  # dynamic attr the gateway boot stamps
    admin_a = SimpleNamespace(identity_store=None, persona_resolver=None)
    app = SimpleNamespace(state=SimpleNamespace())

    await _wire_c2_handles(app, state, admin_a, tmp_path, cfg={})

    # memory_host — real LocalSqliteHost handle.
    assert state.memory_host is not None
    # persona_resolver — PersonaResolver over agent_state.sqlite.
    assert state.persona_resolver is not None
    assert hasattr(state.persona_resolver, "resolve")
    # identity_store — assigned + mirrored onto the AdminState (un-503).
    assert state.identity_store is not None
    assert admin_a.identity_store is state.identity_store
    # agent_runner_fn — async callable.
    assert state.agent_runner_fn is not None
    assert callable(state.agent_runner_fn)
    # hook_runner — HookRunner.
    assert state.hook_runner is not None
    assert hasattr(state.hook_runner, "run_pre_tool_async")

    # The store handles are stashed on app.state for the lifespan teardown.
    assert getattr(app.state, "corlinman_identity_store", None) is not None
    assert (
        getattr(app.state, "corlinman_persona_state_store", None) is not None
    )

    # Clean up EVERY sqlite handle this wiring call opened (the earlier
    # version missed memory_host/memory_kernel — leaked worker threads
    # blocked interpreter exit after the test summary).
    await close_c2_handles(state, app)


async def test_wire_c2_persona_resolver_reads_agent_state(
    tmp_path: Path, close_c2_handles: Any
) -> None:
    """The wired resolver reads the SAME agent_state.sqlite the persona
    life tools write to — a mood set on that row resolves through it."""
    from corlinman_persona import PersonaState
    from corlinman_persona.store import PersonaStore
    from corlinman_server.gateway.lifecycle.entrypoint import _wire_c2_handles

    # Seed a persona-state row first.
    store = await PersonaStore.open_or_create(tmp_path / "agent_state.sqlite")
    await store.upsert(PersonaState(agent_id="grantley", mood="嘚瑟"))
    await store.close()

    state = AppState()
    state.data_dir = tmp_path
    app = SimpleNamespace(state=SimpleNamespace())
    await _wire_c2_handles(app, state, None, tmp_path, cfg={})

    assert state.persona_resolver is not None
    mood = await state.persona_resolver.resolve("mood", "grantley")
    assert mood == "嘚瑟"

    await close_c2_handles(state, app)


# ---------------------------------------------------------------------------
# identity-route disabled gate flips once the store is assigned
# ---------------------------------------------------------------------------


def test_identity_route_503_until_store_assigned() -> None:
    from corlinman_server.gateway.routes_admin_a.state import AdminState
    from corlinman_server.gateway.routes_admin_a.tenancy.identity import _require_store
    from fastapi import HTTPException

    st = AdminState()
    assert st.identity_store is None
    with pytest.raises(HTTPException) as ei:
        _require_store(st)
    assert ei.value.status_code == 503
    assert ei.value.detail == {"error": "identity_disabled"}

    # Assign a store → the gate returns it (un-503).
    sentinel = object()
    st.identity_store = sentinel
    assert _require_store(st) is sentinel


# ---------------------------------------------------------------------------
# cron "every <N><unit>" grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("every 30m", "*/30 * * * *"),
        ("every 2h", "0 */2 * * *"),
        ("every 1d", "0 0 */1 * *"),
        ("30s", "*/30 * * * * *"),
        ("EVERY 15M", "*/15 * * * *"),
    ],
)
def test_cron_interval_grammar(expr: str, expected: str) -> None:
    from corlinman_server.scheduler.cron import normalise_interval_expr

    assert normalise_interval_expr(expr) == expected


def test_cron_interval_non_interval_passes_through() -> None:
    from corlinman_server.scheduler.cron import normalise_interval_expr

    assert normalise_interval_expr("0 0 3 * * * *") is None
    assert normalise_interval_expr("every 0m") is None
    assert normalise_interval_expr("garbage") is None


def test_cron_parse_accepts_interval_form() -> None:
    from datetime import UTC, datetime

    from corlinman_server.scheduler.cron import next_after, parse

    s = parse("every 30m")
    assert next_after(s, datetime.now(tz=UTC)) is not None


# ---------------------------------------------------------------------------
# scheduler missed-run catch-up store resolution
# ---------------------------------------------------------------------------


def test_scheduler_store_resolution_off_app_state() -> None:
    from corlinman_server.scheduler.runner import _resolve_scheduler_store

    assert _resolve_scheduler_store(None) is None
    assert _resolve_scheduler_store(SimpleNamespace()) is None

    sentinel = object()
    assert (
        _resolve_scheduler_store(SimpleNamespace(scheduler_store=sentinel))
        is sentinel
    )
    # Also resolves via a scheduler handle's .store attribute.
    handle = SimpleNamespace(store=sentinel)
    assert (
        _resolve_scheduler_store(
            SimpleNamespace(corlinman_scheduler_handle=handle)
        )
        is sentinel
    )


async def test_catch_up_fires_missed_run(tmp_path: Path) -> None:
    """A per-minute job with no recorded history fires once on startup
    via the catch-up path (the most-recent due firing is within grace)."""
    from corlinman_hooks import HookBus
    from corlinman_server.scheduler import SchedulerStore
    from corlinman_server.scheduler.cron import parse
    from corlinman_server.scheduler.runner import (
        ActionSpec,
        JobSpec,
        _maybe_catch_up,
    )

    store = await SchedulerStore.open(tmp_path / "scheduler.sqlite")
    app_state = SimpleNamespace(scheduler_store=store)

    fired: list[str] = []

    spec = JobSpec(
        name="catchup-test",
        cron=parse("* * * * *"),  # every minute → a due firing < 60s ago
        action=ActionSpec(kind="subprocess", command="/bin/true"),
    )

    # Drive the catch-up directly; record whether dispatch ran by checking
    # the run-history table grows (the subprocess records an outcome).
    bus = HookBus(capacity=16)
    await _maybe_catch_up(spec, bus, app_state)
    # The subprocess fired -> a history row recorded for the job.
    rows = await store.list_for_job("catchup-test", 10)
    fired = [r.job_name for r in rows]
    assert fired == ["catchup-test"], (
        "catch-up should fire the missed per-minute firing once"
    )

    # A second catch-up must NOT re-fire — the last recorded run now
    # covers the most-recent due firing.
    await _maybe_catch_up(spec, bus, app_state)
    rows2 = await store.list_for_job("catchup-test", 10)
    assert len(rows2) == 1, "catch-up must not replay an already-run firing"

    await store.close()
