"""R8 PASSIVE (L3) — scheduler ``evolution.shadow_test`` builtin tests.

Surfaces:

* Registry — importing the builtins package registers
  ``evolution.shadow_test`` so the scheduler-runtime ``run_tool`` dispatch
  resolves it by name.
* Action — running the builtin against a real ``evolution.sqlite`` with a
  seeded PENDING medium/high-risk ``memory_op`` proposal + an on-disk eval
  set transitions the proposal ``pending → shadow_done`` (the whole point
  of L3 — pending proposals get sandboxed without ever touching a live
  target).
* No-eval-set safe behaviour — a pending proposal whose kind has no eval
  set still completes (``no-eval-set`` marker) and stays gated.
* Degradation branches the scheduler tick loop relies on
  (``data_dir_unavailable`` — never raise out of a builtin).
* Default-off config gate — :func:`_register_default_evolution_shadow_job`
  only registers the cron job when ``[evolution.shadow] enabled = true``,
  honours an explicit operator job, and is idempotent on double-call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from corlinman_evolution_store import (
    EvolutionKind,
    EvolutionProposal,
    EvolutionRisk,
    EvolutionStatus,
    EvolutionStore,
    ProposalId,
    ProposalsRepo,
)
from corlinman_server.gateway.lifecycle.scheduler_integration import (
    DEFAULT_EVOLUTION_SHADOW_JOB_NAME,
    _register_default_evolution_shadow_job,
)
from corlinman_server.scheduler.builtins import (
    BUILTIN_ACTIONS,
    EVOLUTION_SHADOW_TEST_BUILTIN_NAME,
    BuiltinContext,
    _evolution_shadow_test_action,
    run_builtin,
)

# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------


def test_evolution_shadow_test_is_registered_by_name() -> None:
    """Importing the builtins package registers ``evolution.shadow_test`` so
    the scheduler-runtime hook can resolve it without re-importing every
    callsite."""
    assert EVOLUTION_SHADOW_TEST_BUILTIN_NAME == "evolution.shadow_test"
    assert EVOLUTION_SHADOW_TEST_BUILTIN_NAME in BUILTIN_ACTIONS
    assert (
        BUILTIN_ACTIONS[EVOLUTION_SHADOW_TEST_BUILTIN_NAME]
        is _evolution_shadow_test_action
    )


# ---------------------------------------------------------------------------
# Degradation branches — builtins must NEVER raise.
# ---------------------------------------------------------------------------


async def test_data_dir_unavailable_returns_typed_envelope() -> None:
    """``app_state`` with no ``data_dir`` slot → typed envelope, no raise."""
    context = BuiltinContext(app_state=SimpleNamespace())
    out = await _evolution_shadow_test_action(context)
    assert out == {"ok": False, "reason": "data_dir_unavailable"}


async def test_none_app_state_returns_data_dir_unavailable() -> None:
    """Degraded boot before the state bundle attaches — same short-circuit."""
    context = BuiltinContext(app_state=None)
    out = await _evolution_shadow_test_action(context)
    assert out == {"ok": False, "reason": "data_dir_unavailable"}


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_pending_memory_op(
    evolution_path: Path,
    *,
    proposal_id: str = "evol-2026-06-04-001",
    target: str = "merge_chunks:1,2",
    risk: EvolutionRisk = EvolutionRisk.HIGH,
) -> None:
    """Insert one PENDING ``memory_op`` proposal so the shadow runner has a
    row to claim. High risk so it falls inside the default shadow risk
    filter."""
    async with EvolutionStore(evolution_path) as store:
        repo = ProposalsRepo(store.conn)
        await repo.insert(
            EvolutionProposal(
                id=ProposalId(proposal_id),
                kind=EvolutionKind.MEMORY_OP,
                target=target,
                diff="",
                reasoning="seeded for shadow test",
                risk=risk,
                budget_cost=0,
                status=EvolutionStatus.PENDING,
                created_at=1,
            )
        )


def _write_memory_op_eval_set(eval_set_dir: Path) -> None:
    """Write one memory_op eval case under
    ``<eval_set_dir>/memory_op/<name>.yaml``. The case seeds two chunks and
    expects them merged into the lower id."""
    case_dir = eval_set_dir / "memory_op"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "merge.yaml").write_text(
        """
description: merge two near-duplicate chunks
proposal:
  target: "merge_chunks:1,2"
  reasoning: near-duplicate
  risk: high
expected:
  outcome: merged
  rows_merged: 1
  surviving_chunk_id: 1
kb_seed:
  - "INSERT INTO chunks (id, file_id, chunk_index, content) VALUES (1, 1, 0, 'alpha beta gamma')"
  - "INSERT INTO chunks (id, file_id, chunk_index, content) VALUES (2, 1, 1, 'alpha beta gamma!')"
""".strip(),
        encoding="utf-8",
    )


def _proposal_status(evolution_path: Path, proposal_id: str) -> str:
    conn = sqlite3.connect(evolution_path)
    try:
        row = conn.execute(
            "SELECT status FROM evolution_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


# ---------------------------------------------------------------------------
# Happy path — a seeded pending proposal transitions to shadow_done.
# ---------------------------------------------------------------------------


async def test_shadow_transitions_seeded_pending_proposal(tmp_path) -> None:
    """A seeded PENDING high-risk memory_op proposal + an on-disk eval set →
    the proposal reaches ``shadow_done`` after the builtin runs. The kb is
    materialised in a tempdir per case; the prod kb is never touched."""
    await _seed_pending_memory_op(tmp_path / "evolution.sqlite")
    _write_memory_op_eval_set(tmp_path / "evolution" / "eval_sets")

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await _evolution_shadow_test_action(context)

    assert out["ok"] is True
    assert out["proposals_claimed"] == 1
    assert out["proposals_completed"] == 1
    assert out["proposals_failed"] == 0
    assert out["cases_run"] == 1

    status = _proposal_status(tmp_path / "evolution.sqlite", "evol-2026-06-04-001")
    assert status == "shadow_done"


async def test_no_eval_set_still_completes_and_stays_gated(tmp_path) -> None:
    """No eval set on disk → the runner records the ``no-eval-set`` marker
    and the proposal still reaches ``shadow_done`` (gated, never
    auto-approved). Acceptable safe behaviour per the L3 contract."""
    await _seed_pending_memory_op(tmp_path / "evolution.sqlite")
    # Intentionally do NOT write any eval set dir.

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await _evolution_shadow_test_action(context)

    assert out["ok"] is True
    assert out["proposals_claimed"] == 1
    assert out["proposals_completed"] == 1
    assert out["cases_run"] == 0

    status = _proposal_status(tmp_path / "evolution.sqlite", "evol-2026-06-04-001")
    assert status == "shadow_done"


async def test_no_pending_proposals_is_ok_noop(tmp_path) -> None:
    """Empty store (no pending proposals) → ok=True, zero claims."""
    async with EvolutionStore(tmp_path / "evolution.sqlite"):
        pass

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await _evolution_shadow_test_action(context)

    assert out["ok"] is True
    assert out["proposals_claimed"] == 0
    assert out["proposals_completed"] == 0


async def test_eval_set_dir_override_from_config(tmp_path) -> None:
    """``[evolution.shadow] eval_set_dir`` on the live config redirects the
    runner to a custom eval-set root."""
    await _seed_pending_memory_op(tmp_path / "evolution.sqlite")
    custom_root = tmp_path / "custom_evals"
    _write_memory_op_eval_set(custom_root)

    cfg = {"evolution": {"shadow": {"eval_set_dir": str(custom_root)}}}
    context = BuiltinContext(
        app_state=SimpleNamespace(data_dir=tmp_path, config=cfg)
    )
    out = await _evolution_shadow_test_action(context)

    assert out["ok"] is True
    assert out["eval_set_dir"] == str(custom_root)
    assert out["cases_run"] == 1
    status = _proposal_status(tmp_path / "evolution.sqlite", "evol-2026-06-04-001")
    assert status == "shadow_done"


async def test_run_builtin_dispatches_evolution_shadow_test(tmp_path) -> None:
    """End-to-end via the public ``run_builtin`` entry point — the registry
    indirection is transparent and the pass still claims the proposal."""
    await _seed_pending_memory_op(tmp_path / "evolution.sqlite")
    _write_memory_op_eval_set(tmp_path / "evolution" / "eval_sets")

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await run_builtin(EVOLUTION_SHADOW_TEST_BUILTIN_NAME, context)

    assert out["ok"] is True
    assert out["proposals_claimed"] == 1


# ---------------------------------------------------------------------------
# Default-off config gate
# ---------------------------------------------------------------------------


def _make_app() -> SimpleNamespace:
    app = SimpleNamespace()
    app.state = SimpleNamespace()
    return app


def test_shadow_job_not_registered_when_flag_absent() -> None:
    """Default config (no [evolution.shadow] section) → job NOT added."""
    app = _make_app()
    _register_default_evolution_shadow_job(app, cfg=None)
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names = [getattr(j, "name", None) for j in jobs]
    assert DEFAULT_EVOLUTION_SHADOW_JOB_NAME not in names


def test_shadow_job_not_registered_when_enabled_false() -> None:
    """``[evolution.shadow] enabled = false`` → job NOT added."""
    app = _make_app()
    cfg = {"evolution": {"shadow": {"enabled": False}}}
    _register_default_evolution_shadow_job(app, cfg=cfg)
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names = [getattr(j, "name", None) for j in jobs]
    assert DEFAULT_EVOLUTION_SHADOW_JOB_NAME not in names


def test_shadow_job_registered_when_enabled_true() -> None:
    """``[evolution.shadow] enabled = true`` → job IS added once;
    idempotent double-call doesn't duplicate."""
    app = _make_app()
    cfg = {"evolution": {"shadow": {"enabled": True}}}
    _register_default_evolution_shadow_job(app, cfg=cfg)
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names = [getattr(j, "name", None) for j in jobs]
    assert DEFAULT_EVOLUTION_SHADOW_JOB_NAME in names

    _register_default_evolution_shadow_job(app, cfg=cfg)
    jobs2 = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names2 = [getattr(j, "name", None) for j in jobs2]
    assert names2.count(DEFAULT_EVOLUTION_SHADOW_JOB_NAME) == 1


def test_shadow_job_skipped_when_explicit_config_job() -> None:
    """Operator already declares an explicit job → helper is a no-op."""
    app = _make_app()
    cfg = {
        "evolution": {"shadow": {"enabled": True}},
        "scheduler": {
            "jobs": [
                {"name": DEFAULT_EVOLUTION_SHADOW_JOB_NAME, "cron": "0 45 4 * * * *"}
            ]
        },
    }
    _register_default_evolution_shadow_job(app, cfg=cfg)
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names = [getattr(j, "name", None) for j in jobs]
    assert DEFAULT_EVOLUTION_SHADOW_JOB_NAME not in names
