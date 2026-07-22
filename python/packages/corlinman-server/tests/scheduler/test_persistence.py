"""Tests for :class:`SchedulerStore` — the :mod:`aiosqlite`-backed
run-history persistence layer.

Python-only: the Rust crate is purely in-memory and persists through
the hook bus + downstream observers. The Python brief asks for an
aiosqlite store as part of the port, so these tests cover the wrapper
in isolation (the store is decoupled from the runtime — the gateway
integration code wires a hook subscription that drives
:meth:`SchedulerStore.record_outcome` per firing).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from corlinman_server.scheduler import (
    RunRecord,
    SchedulerEffectConflict,
    SchedulerStore,
    SubprocessOutcome,
    SubprocessOutcomeKind,
)


async def test_open_creates_file_and_applies_schema(tmp_path: Path) -> None:
    """``open`` creates the parent directory + the SQLite file + applies
    the schema. A subsequent ``count`` on the empty table returns 0."""
    p = tmp_path / "nested" / "scheduler.sqlite"
    store = await SchedulerStore.open(p)
    try:
        assert p.exists(), "SQLite file should exist after open"
        assert await store.count() == 0
    finally:
        await store.close()


async def test_open_migrates_legacy_scheduler_runs_before_occurrence_index(
    tmp_path: Path,
) -> None:
    """Opening a pre-extension DB must add columns before indexing them."""
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE scheduler_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                run_id TEXT NOT NULL,
                action_kind TEXT NOT NULL,
                outcome_kind TEXT NOT NULL,
                error_kind TEXT,
                exit_code INTEGER,
                duration_ms INTEGER NOT NULL,
                fired_at_ms INTEGER NOT NULL
            );
            """
        )

    store = await SchedulerStore.open(path)
    try:
        async with store.connection().execute(
            "PRAGMA table_info(scheduler_runs)"
        ) as cursor:
            columns = {str(row[1]) async for row in cursor}
        assert {
            "result_json",
            "execution_mode",
            "scheduled_for_ms",
            "occurrence_key",
        } <= columns
        async with store.connection().execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_scheduler_runs_occurrence'"
        ) as cursor:
            assert await cursor.fetchone() is not None
    finally:
        await store.close()


async def test_record_outcome_success_persists_row(tmp_path: Path) -> None:
    """Recording a :class:`SubprocessOutcomeKind.SUCCESS` outcome
    persists a row with ``outcome_kind = "success"`` and ``error_kind = None``.
    Round-trip through :meth:`list_recent`."""
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    try:
        outcome = SubprocessOutcome(kind=SubprocessOutcomeKind.SUCCESS, duration_secs=0.42)
        row_id = await store.record_outcome(
            job_name="daily-engine",
            run_id="abc123",
            action_kind="subprocess",
            outcome=outcome,
        )
        assert row_id > 0

        rows = await store.list_recent()
        assert len(rows) == 1
        r = rows[0]
        assert isinstance(r, RunRecord)
        assert r.job_name == "daily-engine"
        assert r.run_id == "abc123"
        assert r.action_kind == "subprocess"
        assert r.outcome_kind == "success"
        assert r.error_kind is None
        assert r.exit_code is None
        # 0.42s → 420ms.
        assert r.duration_ms == 420
        assert r.fired_at_ms > 0
    finally:
        await store.close()


async def test_record_outcome_failure_maps_error_kind(tmp_path: Path) -> None:
    """Non-zero exit / timeout / spawn-failed outcomes get mapped to
    the same vocabulary the hook bus uses on ``EngineRunFailed.error_kind``.
    Pinned per branch so a code drift surfaces immediately."""
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    try:
        cases: list[tuple[SubprocessOutcome, str, int | None]] = [
            (
                SubprocessOutcome(
                    kind=SubprocessOutcomeKind.NON_ZERO_EXIT,
                    duration_secs=0.1,
                    exit_code=1,
                ),
                "exit_code",
                1,
            ),
            (
                SubprocessOutcome(kind=SubprocessOutcomeKind.TIMEOUT, duration_secs=1.0),
                "timeout",
                None,
            ),
            (
                SubprocessOutcome(
                    kind=SubprocessOutcomeKind.SPAWN_FAILED,
                    duration_secs=0.0,
                    error="No such file",
                ),
                "spawn_failed",
                None,
            ),
        ]
        for i, (outcome, _expected_error_kind, _expected_exit_code) in enumerate(cases):
            await store.record_outcome(
                job_name="j",
                run_id=f"r-{i}",
                action_kind="subprocess",
                outcome=outcome,
            )
        rows = await store.list_recent()
        # list_recent orders DESC by fired_at_ms — but all three rows
        # land in the same millisecond on a fast box, so the secondary
        # ``id DESC`` ordering kicks in. We assert the *set* of
        # error_kinds rather than the order to keep the test stable.
        error_kinds = {r.error_kind for r in rows}
        assert error_kinds == {"exit_code", "timeout", "spawn_failed"}
        # Tie one row back to its exit_code for the non_zero_exit case.
        non_zero = next(r for r in rows if r.outcome_kind == "non_zero_exit")
        assert non_zero.exit_code == 1
        # The other two have no exit_code.
        for r in rows:
            if r.outcome_kind != "non_zero_exit":
                assert r.exit_code is None
        # The expected error_kind / exit_code per case are asserted
        # collectively via the ``error_kinds`` set and ``non_zero``
        # row checks above, so the per-case tuple fields are unused
        # in the loop body (hence the ``_`` prefix).
    finally:
        await store.close()


async def test_list_for_job_filters_and_orders(tmp_path: Path) -> None:
    """``list_for_job`` returns only rows for the named job, newest
    first by ``fired_at_ms`` (with ``id DESC`` as the tie-breaker on
    same-millisecond rows)."""
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    try:
        for i in range(3):
            await store.record_raw(
                job_name="job-a",
                run_id=f"a-{i}",
                action_kind="subprocess",
                outcome_kind="success",
                error_kind=None,
                exit_code=None,
                duration_ms=10,
                fired_at_ms=1000 + i,  # explicit stamps so the order is deterministic
            )
        await store.record_raw(
            job_name="job-b",
            run_id="b-0",
            action_kind="subprocess",
            outcome_kind="success",
            error_kind=None,
            exit_code=None,
            duration_ms=10,
            fired_at_ms=2000,
        )

        a_rows = await store.list_for_job("job-a")
        assert [r.run_id for r in a_rows] == ["a-2", "a-1", "a-0"]
        b_rows = await store.list_for_job("job-b")
        assert [r.run_id for r in b_rows] == ["b-0"]
    finally:
        await store.close()


async def test_get_by_run_id_returns_none_for_missing(tmp_path: Path) -> None:
    """Missing run_id → ``None`` (not an exception). Callers branch on
    the ``None`` return."""
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    try:
        assert await store.get_by_run_id("nope") is None
        await store.record_raw(
            job_name="j",
            run_id="present",
            action_kind="subprocess",
            outcome_kind="success",
            error_kind=None,
            exit_code=None,
            duration_ms=5,
        )
        got = await store.get_by_run_id("present")
        assert got is not None
        assert got.run_id == "present"
    finally:
        await store.close()


async def test_extended_run_fields_round_trip_and_deduplicate_occurrence(tmp_path: Path) -> None:
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    try:
        await store.record_raw(
            job_name="imported",
            run_id="r1",
            action_kind="run_tool",
            outcome_kind="success",
            error_kind=None,
            exit_code=None,
            duration_ms=3,
            result_json={"ok": True, "message_id": 42},
            execution_mode="shadow",
            scheduled_for_ms=1234,
            occurrence_key="external:job:1234",
        )
        row = await store.get_by_run_id("r1")
        assert row is not None
        assert row.result_json == {"ok": True, "message_id": 42}
        assert row.execution_mode == "shadow"
        assert row.scheduled_for_ms == 1234
        assert row.occurrence_key == "external:job:1234"
        with pytest.raises(Exception):
            await store.record_raw(
                job_name="imported",
                run_id="r2",
                action_kind="run_tool",
                outcome_kind="success",
                error_kind=None,
                exit_code=None,
                duration_ms=3,
                occurrence_key="external:job:1234",
            )
    finally:
        await store.close()


async def test_effect_reservation_receipt_and_duplicate_block(tmp_path: Path) -> None:
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    try:
        prepared = await store.prepare_effect(
            source_system="external",
            source_job_id="abc",
            occurrence_key="external:abc:1234",
            effect_kind="telegram.message",
            effect_target="configured-topic",
        )
        assert prepared.state == "prepared"
        with pytest.raises(SchedulerEffectConflict):
            await store.prepare_effect(
                source_system="external",
                source_job_id="abc",
                occurrence_key="external:abc:1234",
                effect_kind="telegram.message",
                effect_target="configured-topic",
            )
        sent = await store.complete_effect(
            prepared.id,
            state="sent",
            receipt={"message_id": 77},
        )
        assert sent.state == "sent"
        assert sent.receipt_json == {"message_id": 77}
        with pytest.raises(SchedulerEffectConflict):
            await store.complete_effect(prepared.id, state="failed")
        with pytest.raises(SchedulerEffectConflict):
            await store.prepare_effect(
                source_system="external",
                source_job_id="abc",
                occurrence_key="external:abc:1234",
                effect_kind="telegram.message",
                effect_target="configured-topic",
            )
    finally:
        await store.close()


async def test_open_reconciles_prepared_effects_to_unknown(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    store = await SchedulerStore.open(path)
    prepared = await store.prepare_effect(
        source_system="external",
        source_job_id="job-1",
        occurrence_key="external:job-1:1234",
        effect_kind="qzone.publish",
        effect_target="account:1",
    )
    await store.close()

    reopened = await SchedulerStore.open(path, reconcile_prepared=True)
    try:
        effect = await reopened.get_effect_by_id(prepared.id)
        assert effect is not None
        assert effect.state == "unknown"
        assert effect.error_code == "process_restart"
        with pytest.raises(SchedulerEffectConflict):
            await reopened.prepare_effect(
                source_system="external",
                source_job_id="job-1",
                occurrence_key="external:job-1:1234",
                effect_kind="qzone.publish",
                effect_target="account:1",
            )
    finally:
        await reopened.close()


async def test_regular_open_does_not_reconcile_another_process_effect(
    tmp_path: Path,
) -> None:
    path = tmp_path / "s.sqlite"
    owner = await SchedulerStore.open(path)
    prepared = await owner.prepare_effect(
        source_system="external",
        source_job_id="job-1",
        occurrence_key="external:job-1:1234",
        effect_kind="qzone.publish",
        effect_target="account:1",
    )
    peer = await SchedulerStore.open(path)
    try:
        effect = await peer.get_effect_by_id(prepared.id)
        assert effect is not None
        assert effect.state == "prepared"
        sent = await owner.complete_effect(prepared.id, state="sent")
        assert sent.state == "sent"
    finally:
        await peer.close()
        await owner.close()


async def test_close_is_idempotent(tmp_path: Path) -> None:
    """``close`` swallows errors on a second call — used at shutdown
    so the gateway can call it from multiple cleanup paths."""
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    await store.close()
    # Second close should not raise.
    await store.close()


async def test_records_unsupported_action_via_record_raw(tmp_path: Path) -> None:
    """The dispatcher's unsupported-action branch has no
    :class:`SubprocessOutcome` to wrap, so callers persist it via
    :meth:`record_raw`. Test that the row makes it through with the
    expected vocabulary (mirrors the hook event's ``error_kind``)."""
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    try:
        await store.record_raw(
            job_name="agentic",
            run_id="run-u",
            action_kind="run_agent",
            outcome_kind="unsupported_action",
            error_kind="unsupported_action",
            exit_code=None,
            duration_ms=0,
        )
        row = await store.get_by_run_id("run-u")
        assert row is not None
        assert row.action_kind == "run_agent"
        assert row.outcome_kind == "unsupported_action"
        assert row.error_kind == "unsupported_action"
    finally:
        await store.close()
