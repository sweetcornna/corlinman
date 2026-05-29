"""Iter 2 tests — distillation-run log + insert_episode CRUD.

Covers the unique-window guard, the stale-running sweeper, the
``latest_ok_run`` window-advancement helper, and an
``insert_episode`` round-trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_episodes import (
    RUN_STATUS_FAILED,
    RUN_STATUS_OK,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SKIPPED_EMPTY,
    Episode,
    EpisodeKind,
    EpisodesStore,
    RunWindowConflict,
    new_episode_id,
    select_window,
    window_too_small,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "episodes.sqlite"


# ---------------------------------------------------------------------------
# insert_episode + natural-key probe
# ---------------------------------------------------------------------------


async def test_insert_episode_round_trip(db_path: Path) -> None:
    """Round-trip every column on an episode row.

    Source-id JSON columns and the embedding BLOB are the load-bearing
    fidelity targets — the placeholder resolver depends on the
    decoder reproducing them faithfully.
    """
    eid = new_episode_id()
    async with EpisodesStore(db_path) as store:
        await store.insert_episode(
            Episode(
                id=eid,
                tenant_id="tenant-A",
                started_at=1_700_000_000_000,
                ended_at=1_700_000_010_000,
                kind=EpisodeKind.INCIDENT,
                summary_text="auto-rollback fired on engine_prompt:clustering",
                source_session_keys=["sess-1", "sess-2"],
                source_signal_ids=[12, 13],
                source_history_ids=[7],
                embedding=b"\x00\x01\x02\x03",
                embedding_dim=384,
                importance_score=0.91,
                distilled_by="default-summary",
                distilled_at=1_700_000_011_000,
            )
        )
        rt = await store.find_episode_by_natural_key(
            tenant_id="tenant-A",
            started_at=1_700_000_000_000,
            ended_at=1_700_000_010_000,
            kind=EpisodeKind.INCIDENT,
        )
    assert rt is not None
    assert rt.id == eid
    assert rt.kind == EpisodeKind.INCIDENT
    assert rt.source_session_keys == ["sess-1", "sess-2"]
    assert rt.source_signal_ids == [12, 13]
    assert rt.source_history_ids == [7]
    assert rt.embedding == b"\x00\x01\x02\x03"
    assert rt.embedding_dim == 384
    assert rt.importance_score == pytest.approx(0.91)


async def test_insert_episodes_batch_single_commit(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisting K episodes via :meth:`insert_episodes` issues ONE commit.

    PERF-008: the per-row ``insert_episode`` does one ``commit()`` (one
    fsync) per row. The batch path must wrap all K rows in a single
    transaction with a single commit so a busy distillation pass doesn't
    pay K fsyncs. Spy on ``conn.commit`` to pin the count.
    """
    episodes = [
        Episode(
            id=new_episode_id(),
            tenant_id="tenant-batch",
            started_at=1_700_000_000_000 + i,
            ended_at=1_700_000_000_500 + i,
            kind=EpisodeKind.CONVERSATION,
            summary_text=f"chat {i}",
            source_session_keys=[f"sess-{i}"],
            source_signal_ids=[i],
            source_history_ids=[],
            importance_score=0.5,
            distilled_by="default-summary",
            distilled_at=1_700_000_001_000 + i,
        )
        for i in range(5)
    ]

    async with EpisodesStore(db_path) as store:
        commit_calls = 0
        real_commit = store.conn.commit

        async def _counting_commit() -> None:
            nonlocal commit_calls
            commit_calls += 1
            await real_commit()

        monkeypatch.setattr(store.conn, "commit", _counting_commit)

        await store.insert_episodes(episodes)

        # Single transaction → single commit, regardless of K.
        assert commit_calls == 1

        cursor = await store.conn.execute("SELECT COUNT(*) FROM episodes")
        count_row = await cursor.fetchone()
        await cursor.close()
    assert count_row == (5,)


async def test_insert_episodes_batch_round_trips_all_rows(db_path: Path) -> None:
    """All K batched rows land with the right fields, in input order.

    Correctness companion to the single-commit test — pins row shape,
    ordering, and the JSON source-id encoding so the batch path stays
    behaviourally identical to the per-row insert.
    """
    episodes = [
        Episode(
            id=f"EID{i:023d}",  # 26-char ULID-ish width, sortable by index
            tenant_id="tenant-batch",
            started_at=10 + i,
            ended_at=20 + i,
            kind=EpisodeKind.INCIDENT if i % 2 else EpisodeKind.CONVERSATION,
            summary_text=f"summary {i}",
            source_session_keys=[f"sess-{i}", "shared"],
            source_signal_ids=[i, i + 100],
            source_history_ids=[i + 1000],
            embedding=bytes([i]) if i else None,
            embedding_dim=1 if i else None,
            importance_score=0.1 * i,
            distilled_by="provider-x",
            distilled_at=30 + i,
        )
        for i in range(4)
    ]

    async with EpisodesStore(db_path) as store:
        await store.insert_episodes(episodes)
        cursor = await store.conn.execute(
            "SELECT id FROM episodes ORDER BY id"
        )
        id_rows = await cursor.fetchall()
        await cursor.close()

        round_tripped = [
            await store.find_episode_by_natural_key(
                tenant_id="tenant-batch",
                started_at=ep.started_at,
                ended_at=ep.ended_at,
                kind=ep.kind,
            )
            for ep in episodes
        ]

    assert [r[0] for r in id_rows] == [ep.id for ep in episodes]
    for ep, rt in zip(episodes, round_tripped, strict=True):
        assert rt is not None
        assert rt.id == ep.id
        assert rt.kind == ep.kind
        assert rt.summary_text == ep.summary_text
        assert rt.source_session_keys == ep.source_session_keys
        assert rt.source_signal_ids == ep.source_signal_ids
        assert rt.source_history_ids == ep.source_history_ids
        assert rt.embedding == ep.embedding
        assert rt.embedding_dim == ep.embedding_dim
        assert rt.importance_score == pytest.approx(ep.importance_score)
        assert rt.distilled_by == ep.distilled_by
        assert rt.distilled_at == ep.distilled_at


async def test_insert_episodes_empty_list_is_noop(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty batch writes nothing and issues no commit/executemany.

    Guards against an empty distillation pass paying for an empty
    transaction or tripping ``executemany`` on a zero-row sequence.
    """
    async with EpisodesStore(db_path) as store:
        commit_calls = 0
        real_commit = store.conn.commit

        async def _counting_commit() -> None:
            nonlocal commit_calls
            commit_calls += 1
            await real_commit()

        monkeypatch.setattr(store.conn, "commit", _counting_commit)

        await store.insert_episodes([])

        assert commit_calls == 0
        cursor = await store.conn.execute("SELECT COUNT(*) FROM episodes")
        row = await cursor.fetchone()
        await cursor.close()
    assert row == (0,)


async def test_natural_key_returns_none_when_kind_differs(db_path: Path) -> None:
    """Same window but different kind → distinct episode.

    Pinned because the natural-key probe is the second-line defence
    against double-minting; if it ever broadens beyond the kind
    column, two episodes from a window with both an INCIDENT and a
    CONVERSATION pair would collapse.
    """
    async with EpisodesStore(db_path) as store:
        await store.insert_episode(
            Episode(
                id=new_episode_id(),
                tenant_id="t",
                started_at=10,
                ended_at=20,
                kind=EpisodeKind.CONVERSATION,
                summary_text="a chat",
                distilled_by="x",
                distilled_at=21,
            )
        )
        miss = await store.find_episode_by_natural_key(
            tenant_id="t",
            started_at=10,
            ended_at=20,
            kind=EpisodeKind.INCIDENT,
        )
        hit = await store.find_episode_by_natural_key(
            tenant_id="t",
            started_at=10,
            ended_at=20,
            kind=EpisodeKind.CONVERSATION,
        )
    assert miss is None
    assert hit is not None


# ---------------------------------------------------------------------------
# Distillation-run log
# ---------------------------------------------------------------------------


async def test_open_run_then_finish(db_path: Path) -> None:
    """Happy path: open → finish → status=ok, episodes_written stamped."""
    async with EpisodesStore(db_path) as store:
        run = await store.open_run(
            tenant_id="t",
            window_start=100,
            window_end=200,
            started_at=150,
        )
        assert run.status == RUN_STATUS_RUNNING
        assert run.finished_at is None

        await store.finish_run(
            run.run_id,
            status=RUN_STATUS_OK,
            episodes_written=3,
            finished_at=180,
        )

        rt = await store.find_run(
            tenant_id="t", window_start=100, window_end=200
        )
    assert rt is not None
    assert rt.run_id == run.run_id
    assert rt.status == RUN_STATUS_OK
    assert rt.episodes_written == 3
    assert rt.finished_at == 180


async def test_open_run_collision_raises(db_path: Path) -> None:
    """Two ``open_run`` calls on the same window → second raises.

    Acts as the load-bearing race guard described in the design doc:
    an idempotent re-run on the same window must not double-mint.
    """
    async with EpisodesStore(db_path) as store:
        await store.open_run(tenant_id="t", window_start=0, window_end=100)
        with pytest.raises(RunWindowConflict) as exc_info:
            await store.open_run(tenant_id="t", window_start=0, window_end=100)
        assert exc_info.value.tenant_id == "t"
        assert exc_info.value.window_start == 0
        assert exc_info.value.window_end == 100


async def test_open_run_collision_is_per_tenant(db_path: Path) -> None:
    """Different tenants can claim the same window concurrently."""
    async with EpisodesStore(db_path) as store:
        a = await store.open_run(tenant_id="a", window_start=0, window_end=100)
        b = await store.open_run(tenant_id="b", window_start=0, window_end=100)
    assert a.run_id != b.run_id


async def test_latest_ok_run_advances_window(db_path: Path) -> None:
    """``latest_ok_run`` returns the most recent OK or skipped-empty
    row; failed rows don't advance the window.
    """
    async with EpisodesStore(db_path) as store:
        r1 = await store.open_run(tenant_id="t", window_start=0, window_end=10)
        await store.finish_run(r1.run_id, status=RUN_STATUS_OK)

        r2 = await store.open_run(tenant_id="t", window_start=10, window_end=20)
        await store.finish_run(
            r2.run_id, status=RUN_STATUS_FAILED, error_message="boom"
        )

        r3 = await store.open_run(tenant_id="t", window_start=20, window_end=30)
        await store.finish_run(r3.run_id, status=RUN_STATUS_SKIPPED_EMPTY)

        latest = await store.latest_ok_run(tenant_id="t")
    assert latest is not None
    # Skipped-empty counts as ok-for-window-advancement, so r3 wins.
    assert latest.run_id == r3.run_id
    assert latest.window_end == 30


async def test_sweep_stale_runs_marks_failed(db_path: Path) -> None:
    """A ``running`` row older than the threshold is swept to failed.

    The sweeper is the crash-resume contract — without it, a runner
    that exited mid-pass would deadlock the next pass on the unique
    window guard.
    """
    async with EpisodesStore(db_path) as store:
        ghost = await store.open_run(
            tenant_id="t",
            window_start=0,
            window_end=10,
            started_at=1000,
        )

        swept = await store.sweep_stale_runs(now_ms=10_000, stale_after_secs=5)

        assert swept == [ghost.run_id]
        rt = await store.find_run(
            tenant_id="t", window_start=0, window_end=10
        )
    assert rt is not None
    assert rt.status == RUN_STATUS_FAILED
    assert rt.error_message and "stale running row" in rt.error_message
    # The previously-blocked window can now be opened by a fresh runner.
    async with EpisodesStore(db_path) as store2:
        with pytest.raises(RunWindowConflict):
            # The ghost row lingers (status=failed) so the unique
            # window still applies — distinct from the persona-style
            # PK overwrite. The runner's contract is to find_run +
            # decide whether to retry under a *different* window.
            await store2.open_run(tenant_id="t", window_start=0, window_end=10)


async def test_finish_run_rejects_running_status(db_path: Path) -> None:
    """``finish_run`` with status='running' is a coding error."""
    async with EpisodesStore(db_path) as store:
        run = await store.open_run(tenant_id="t", window_start=0, window_end=1)
        with pytest.raises(ValueError):
            await store.finish_run(run.run_id, status=RUN_STATUS_RUNNING)


# ---------------------------------------------------------------------------
# Window selection helpers
# ---------------------------------------------------------------------------


def test_select_window_first_run_uses_rolling_start() -> None:
    """No prior OK run → ``window_start = now - window_hours``."""
    start, end = select_window(
        now_ms=1_000_000_000,
        distillation_window_hours=1.0,
        last_ok_run_window_end_ms=None,
    )
    assert end == 1_000_000_000
    assert start == 1_000_000_000 - 3_600_000


def test_select_window_clamps_to_last_ok() -> None:
    """A recent prior run beats the rolling start.

    Without this clamp, a back-to-back cron tick would reprocess the
    same hour twice.
    """
    start, end = select_window(
        now_ms=10_000,
        distillation_window_hours=1.0,
        last_ok_run_window_end_ms=8_000,
    )
    # Rolling start would be 10_000 - 3_600_000 = -3_590_000 (well in
    # the past) — last-ok wins.
    assert (start, end) == (8_000, 10_000)


def test_window_too_small_uses_milliseconds() -> None:
    """Defensive check: ``min_window_secs`` is interpreted correctly."""
    assert window_too_small(
        window_start_ms=0, window_end_ms=2_000, min_window_secs=3
    )
    assert not window_too_small(
        window_start_ms=0, window_end_ms=4_000, min_window_secs=3
    )
