"""R8 PASSIVE (L2) — scheduler ``evolution.engine_run_once`` builtin tests.

Surfaces:

* Registry — importing the builtins package registers
  ``evolution.engine_run_once`` so the scheduler-runtime ``run_tool``
  dispatch resolves it by name.
* Action — running the builtin against a real ``evolution.sqlite`` +
  ``kb.sqlite`` mints a PENDING ``memory_op`` proposal from a clustered
  signal load + near-duplicate kb chunks (the whole point of L2 — without
  the scheduled pass clustered signals never become proposals).
* Degradation branches the scheduler tick loop relies on
  (``data_dir_unavailable`` — never raise out of a builtin).
* Default-off config gate — :func:`_register_default_evolution_engine_job`
  only registers the cron job when ``[evolution.engine] enabled = true``,
  honours an explicit operator job, and is idempotent on double-call.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from corlinman_evolution_store import (
    EvolutionStore,
    SignalSeverity,
    SignalsRepo,
)
from corlinman_evolution_store.types import EvolutionSignal
from corlinman_server.gateway.lifecycle.scheduler_integration import (
    DEFAULT_EVOLUTION_ENGINE_JOB_NAME,
    _register_default_evolution_engine_job,
)
from corlinman_server.scheduler.builtins import (
    BUILTIN_ACTIONS,
    EVOLUTION_ENGINE_RUN_ONCE_BUILTIN_NAME,
    BuiltinContext,
    _evolution_engine_run_once_action,
    run_builtin,
)

# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------


def test_evolution_engine_run_once_is_registered_by_name() -> None:
    """Importing the builtins package registers ``evolution.engine_run_once``
    so the scheduler-runtime hook can resolve it without re-importing every
    callsite."""
    assert EVOLUTION_ENGINE_RUN_ONCE_BUILTIN_NAME == "evolution.engine_run_once"
    assert EVOLUTION_ENGINE_RUN_ONCE_BUILTIN_NAME in BUILTIN_ACTIONS
    assert (
        BUILTIN_ACTIONS[EVOLUTION_ENGINE_RUN_ONCE_BUILTIN_NAME]
        is _evolution_engine_run_once_action
    )


# ---------------------------------------------------------------------------
# Degradation branches — builtins must NEVER raise.
# ---------------------------------------------------------------------------


async def test_data_dir_unavailable_returns_typed_envelope() -> None:
    """``app_state`` with no ``data_dir`` slot → typed envelope, no raise."""
    context = BuiltinContext(app_state=SimpleNamespace())
    out = await _evolution_engine_run_once_action(context)
    assert out == {"ok": False, "reason": "data_dir_unavailable"}


async def test_none_app_state_returns_data_dir_unavailable() -> None:
    """Degraded boot before the state bundle attaches — same short-circuit."""
    context = BuiltinContext(app_state=None)
    out = await _evolution_engine_run_once_action(context)
    assert out == {"ok": False, "reason": "data_dir_unavailable"}


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


_KB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    diary_name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    mtime INTEGER NOT NULL,
    size INTEGER NOT NULL,
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    vector BLOB,
    namespace TEXT NOT NULL DEFAULT 'general',
    decay_score REAL NOT NULL DEFAULT 1.0,
    consolidated_at INTEGER,
    last_recalled_at INTEGER
);
"""


def _seed_kb_with_near_duplicates(kb_path: Path) -> None:
    """Two near-identical chunks (>95% token overlap) + one unrelated."""
    conn = sqlite3.connect(kb_path)
    try:
        conn.executescript(_KB_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO files (id, path, diary_name, checksum, mtime, size) "
            "VALUES (1, '/tmp/f.md', 'test', 'cafef00d', 0, 0)"
        )
        for idx, content in enumerate(
            (
                "alpha beta gamma delta epsilon zeta eta theta",
                "alpha beta gamma delta epsilon zeta eta theta!",
                "totally different text about machine learning systems",
            )
        ):
            conn.execute(
                "INSERT INTO chunks (file_id, chunk_index, content, namespace) "
                "VALUES (1, ?, ?, 'general')",
                (idx, content),
            )
        conn.commit()
    finally:
        conn.close()


async def _seed_signal_cluster(evolution_path: Path, *, count: int = 5) -> None:
    """``count`` ``tool.call.failed`` signals on the same target — enough to
    clear the default ``min_cluster_size`` so the engine fires its
    handlers."""
    now_ms = int(time.time() * 1000)
    async with EvolutionStore(evolution_path) as store:
        repo = SignalsRepo(store.conn)
        for i in range(count):
            await repo.insert(
                EvolutionSignal(
                    event_kind="tool.call.failed",
                    target="web_search",
                    severity=SignalSeverity.ERROR,
                    payload_json={"reason": "timeout"},
                    observed_at=now_ms - 60_000 + i,
                    trace_id=f"trace-{i}",
                    session_id="sess-1",
                )
            )


def _pending_proposals(evolution_path: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(evolution_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, kind, target, status FROM evolution_proposals "
            "ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Happy path — the run mints a pending proposal.
# ---------------------------------------------------------------------------


async def test_run_once_mints_pending_proposal_from_seeded_signals(
    tmp_path,
) -> None:
    """A clustered signal load + near-duplicate kb chunks → one PENDING
    ``memory_op`` proposal in ``evolution.sqlite`` after the builtin runs."""
    await _seed_signal_cluster(tmp_path / "evolution.sqlite", count=5)
    _seed_kb_with_near_duplicates(tmp_path / "kb.sqlite")

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await _evolution_engine_run_once_action(context)

    assert out["ok"] is True
    assert out["signals_loaded"] == 5
    assert out["clusters_found"] == 1
    assert out["proposals_written"] == 1
    assert out["proposals_by_kind"].get("memory_op") == 1

    proposals = _pending_proposals(tmp_path / "evolution.sqlite")
    assert len(proposals) == 1
    p = proposals[0]
    assert p["kind"] == "memory_op"
    assert p["target"] == "merge_chunks:1,2"
    # PASSIVE: the proposal lands as PENDING — never applied.
    assert p["status"] == "pending"


async def test_run_once_no_signals_writes_no_proposal(tmp_path) -> None:
    """kb duplicates but no signal cluster → no trigger, ok=True, zero
    proposals (the engine only fires when a cluster exists)."""
    # Create an empty evolution store (schema only, no signals).
    async with EvolutionStore(tmp_path / "evolution.sqlite"):
        pass
    _seed_kb_with_near_duplicates(tmp_path / "kb.sqlite")

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await _evolution_engine_run_once_action(context)

    assert out["ok"] is True
    assert out["clusters_found"] == 0
    assert out["proposals_written"] == 0
    assert _pending_proposals(tmp_path / "evolution.sqlite") == []


async def test_run_builtin_dispatches_evolution_engine_run_once(tmp_path) -> None:
    """End-to-end via the public ``run_builtin`` entry point — the registry
    indirection is transparent and the run still mints a proposal."""
    await _seed_signal_cluster(tmp_path / "evolution.sqlite", count=5)
    _seed_kb_with_near_duplicates(tmp_path / "kb.sqlite")

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await run_builtin(EVOLUTION_ENGINE_RUN_ONCE_BUILTIN_NAME, context)

    assert out["ok"] is True
    assert out["proposals_written"] == 1


async def test_budget_config_read_off_app_state(tmp_path) -> None:
    """``[evolution.budget]`` on the live config gate-keeps the run; a
    weekly_total of 0 with enabled=true drops every proposal by budget."""
    await _seed_signal_cluster(tmp_path / "evolution.sqlite", count=5)
    _seed_kb_with_near_duplicates(tmp_path / "kb.sqlite")

    cfg = {"evolution": {"budget": {"enabled": True, "weekly_total": 0}}}
    context = BuiltinContext(
        app_state=SimpleNamespace(data_dir=tmp_path, config=cfg)
    )
    out = await _evolution_engine_run_once_action(context)

    assert out["ok"] is True
    # Cluster found, but the budget cap of 0 drops the proposal.
    assert out["clusters_found"] == 1
    assert out["proposals_written"] == 0
    assert out["proposals_skipped_budget"] >= 1


# ---------------------------------------------------------------------------
# Default-off config gate
# ---------------------------------------------------------------------------


def _make_app() -> SimpleNamespace:
    app = SimpleNamespace()
    app.state = SimpleNamespace()
    return app


def test_engine_job_not_registered_when_flag_absent() -> None:
    """Default config (no [evolution.engine] section) → job NOT added."""
    app = _make_app()
    _register_default_evolution_engine_job(app, cfg=None)
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names = [getattr(j, "name", None) for j in jobs]
    assert DEFAULT_EVOLUTION_ENGINE_JOB_NAME not in names


def test_engine_job_not_registered_when_enabled_false() -> None:
    """``[evolution.engine] enabled = false`` → job NOT added."""
    app = _make_app()
    cfg = {"evolution": {"engine": {"enabled": False}}}
    _register_default_evolution_engine_job(app, cfg=cfg)
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names = [getattr(j, "name", None) for j in jobs]
    assert DEFAULT_EVOLUTION_ENGINE_JOB_NAME not in names


def test_engine_job_registered_when_enabled_true() -> None:
    """``[evolution.engine] enabled = true`` → job IS added once;
    idempotent double-call doesn't duplicate."""
    app = _make_app()
    cfg = {"evolution": {"engine": {"enabled": True}}}
    _register_default_evolution_engine_job(app, cfg=cfg)
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names = [getattr(j, "name", None) for j in jobs]
    assert DEFAULT_EVOLUTION_ENGINE_JOB_NAME in names

    _register_default_evolution_engine_job(app, cfg=cfg)
    jobs2 = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names2 = [getattr(j, "name", None) for j in jobs2]
    assert names2.count(DEFAULT_EVOLUTION_ENGINE_JOB_NAME) == 1


def test_engine_job_skipped_when_explicit_config_job() -> None:
    """Operator already declares an explicit job → helper is a no-op."""
    app = _make_app()
    cfg = {
        "evolution": {"engine": {"enabled": True}},
        "scheduler": {
            "jobs": [
                {"name": DEFAULT_EVOLUTION_ENGINE_JOB_NAME, "cron": "0 0 6 * * * *"}
            ]
        },
    }
    _register_default_evolution_engine_job(app, cfg=cfg)
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names = [getattr(j, "name", None) for j in jobs]
    assert DEFAULT_EVOLUTION_ENGINE_JOB_NAME not in names
