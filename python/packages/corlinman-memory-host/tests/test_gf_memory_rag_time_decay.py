"""gap-fill lane-memory-rag: opt-in query-time exponential time-decay re-rank.

Covers the additive ``MemoryQuery.time_decay_half_life_s`` knob on
:class:`~corlinman_memory_host.local_sqlite.LocalSqliteHost`:

- decay OFF (default) → legacy BM25 ordering, byte-for-byte unchanged;
- decay ON → among equally BM25-relevant chunks, the more recent one
  outranks the older one;
- the wire shape round-trips the new fields.

The upsert path stamps ``files.mtime`` with the creation second; the
tests pin the decay reference instant via ``time_decay_now_s`` and rewrite
the synthetic file mtimes directly so ages are deterministic (no sleeps).
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from corlinman_memory_host import (
    LocalSqliteHost,
    MemoryDoc,
    MemoryQuery,
)


@pytest.fixture
async def host(tmp_path: Path) -> AsyncIterator[tuple[LocalSqliteHost, Path]]:
    db_path = tmp_path / "kb.sqlite"
    h = await LocalSqliteHost.open("local-kb", db_path)
    try:
        yield h, db_path
    finally:
        await h.close()


def _force_mtime(db_path: Path, chunk_id: int, mtime_unix: int) -> None:
    """Pin the synthetic file row's mtime for ``chunk_id`` (offline poke).

    The host stamps a real creation second on upsert; tests overwrite it
    so chunk ages are deterministic without wall-clock sleeps.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE files SET mtime = ? WHERE id = "
            "(SELECT file_id FROM chunks WHERE id = ?)",
            (mtime_unix, chunk_id),
        )
        conn.commit()
    finally:
        conn.close()


async def test_decay_off_preserves_legacy_bm25_order(
    host: tuple[LocalSqliteHost, Path],
) -> None:
    h, db_path = host
    # Two equally-matching docs; the second is far older.
    old_id = await h.upsert(MemoryDoc(content="banana split recipe"))
    new_id = await h.upsert(MemoryDoc(content="banana split recipe"))
    _force_mtime(db_path, int(old_id), 1_000)
    _force_mtime(db_path, int(new_id), 2_000_000_000)

    # No decay knob → ranking is whatever BM25 produced; both present.
    hits = await h.query(MemoryQuery(text="banana", top_k=10))
    ids = {hit.id for hit in hits}
    assert ids == {old_id, new_id}
    # Identical content → identical BM25 score; decay OFF must not reorder
    # based on age (scores stay equal).
    by_id = {hit.id: hit.score for hit in hits}
    assert by_id[old_id] == by_id[new_id]


async def test_decay_on_ranks_recent_chunk_higher(
    host: tuple[LocalSqliteHost, Path],
) -> None:
    h, db_path = host
    old_id = await h.upsert(MemoryDoc(content="banana split recipe"))
    new_id = await h.upsert(MemoryDoc(content="banana split recipe"))

    now = 2_000_000_000
    # old chunk: 10 half-lives in the past; new chunk: brand new.
    half_life = 86_400.0  # one day
    _force_mtime(db_path, int(old_id), int(now - 10 * half_life))
    _force_mtime(db_path, int(new_id), now)

    hits = await h.query(
        MemoryQuery(
            text="banana",
            top_k=10,
            time_decay_half_life_s=half_life,
            time_decay_now_s=float(now),
        )
    )
    assert next(hit.id for hit in hits) == new_id, (
        "with decay on, the recent chunk must rank first"
    )
    by_id = {hit.id: hit.score for hit in hits}
    assert by_id[new_id] > by_id[old_id]


async def test_decay_zero_half_life_is_noop(
    host: tuple[LocalSqliteHost, Path],
) -> None:
    h, db_path = host
    old_id = await h.upsert(MemoryDoc(content="banana split recipe"))
    new_id = await h.upsert(MemoryDoc(content="banana split recipe"))
    _force_mtime(db_path, int(old_id), 1_000)
    _force_mtime(db_path, int(new_id), 2_000_000_000)

    # Non-positive half-life must be treated as "decay disabled".
    hits = await h.query(
        MemoryQuery(text="banana", top_k=10, time_decay_half_life_s=0.0)
    )
    by_id = {hit.id: hit.score for hit in hits}
    assert by_id[old_id] == by_id[new_id]


async def test_decay_handles_unknown_age_gracefully(
    host: tuple[LocalSqliteHost, Path],
) -> None:
    h, db_path = host
    # A chunk whose file mtime is 0 (pre-decay row) keeps its raw score.
    stale_id = await h.upsert(MemoryDoc(content="banana split recipe"))
    fresh_id = await h.upsert(MemoryDoc(content="banana split recipe"))
    _force_mtime(db_path, int(stale_id), 0)  # unknown age
    _force_mtime(db_path, int(fresh_id), 2_000_000_000)

    hits = await h.query(
        MemoryQuery(
            text="banana",
            top_k=10,
            time_decay_half_life_s=86_400.0,
            time_decay_now_s=2_000_000_000.0,
        )
    )
    # The fresh chunk (weight 1.0) and the unknown-age chunk (raw score,
    # also effectively weight 1.0) both survive; no crash, both returned.
    assert {hit.id for hit in hits} == {stale_id, fresh_id}


def test_query_wire_shape_roundtrips_decay_fields() -> None:
    q = MemoryQuery(
        text="banana",
        top_k=5,
        time_decay_half_life_s=3600.0,
        time_decay_now_s=123.0,
    )
    wire = q.to_json()
    assert wire["time_decay_half_life_s"] == 3600.0
    assert wire["time_decay_now_s"] == 123.0
    back = MemoryQuery.from_json(wire)
    assert back.time_decay_half_life_s == 3600.0
    assert back.time_decay_now_s == 123.0


def test_query_wire_shape_omits_decay_when_unset() -> None:
    q = MemoryQuery(text="banana", top_k=5)
    wire = q.to_json()
    # Legacy/Rust query shape: the decay keys must be absent entirely.
    assert "time_decay_half_life_s" not in wire
    assert "time_decay_now_s" not in wire
    back = MemoryQuery.from_json(wire)
    assert back.time_decay_half_life_s is None
    assert back.time_decay_now_s is None
