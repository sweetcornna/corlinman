"""gap-fill lane-memory-rag: EPA backfill on a vector-less corpus.

The live memory-host write path stores ``chunks.vector = NULL`` (dense
recall is deferred), so the offline backfill legitimately finds nothing
to project. These tests pin that it (a) stays a safe no-op that writes
zero ``chunk_epa`` rows, (b) classifies the result so an operator can
tell "chunks exist but carry no vector yet" apart from a genuinely empty
DB, and (c) still backfills correctly once vectors are present.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from corlinman_agent.rag.epa_backfill import BackfillConfig, EpaBackfiller

_V6_SCHEMA = """
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
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chunk_epa (
    chunk_id     INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    projections  BLOB    NOT NULL,
    entropy      REAL    NOT NULL,
    logic_depth  REAL    NOT NULL,
    computed_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""


def _new_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V6_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _insert_vectorless_chunk(path: Path, content: str) -> None:
    conn = sqlite3.connect(path)
    try:
        file_id = conn.execute(
            "INSERT INTO files(path, diary_name, checksum, mtime, size) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"memory-host://{content}", "memory-host", "", 0, 0),
        ).lastrowid
        # vector = NULL: exactly what LocalSqliteHost.upsert writes today.
        conn.execute(
            "INSERT INTO chunks(file_id, chunk_index, content, vector, namespace) "
            "VALUES (?, ?, ?, NULL, ?)",
            (file_id, 0, content, "general"),
        )
        conn.commit()
    finally:
        conn.close()


def _count_epa(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM chunk_epa").fetchone()[0])
    finally:
        conn.close()


async def test_backfill_genuinely_empty_db(tmp_path: Path) -> None:
    db = tmp_path / "kb.sqlite"
    _new_db(db)
    stats = await EpaBackfiller(db, BackfillConfig(k=4)).run()
    assert stats.chunks_processed == 0
    assert stats.basis_axes == 0
    assert _count_epa(db) == 0


async def test_backfill_no_vectors_is_safe_noop(tmp_path: Path) -> None:
    """Chunks exist but none carry a dense vector — the live state.

    The backfill must not raise, must write zero EPA rows, and must
    count the vector-less chunks as skipped.
    """
    db = tmp_path / "kb.sqlite"
    _new_db(db)
    _insert_vectorless_chunk(db, "first memory")
    _insert_vectorless_chunk(db, "second memory")

    stats = await EpaBackfiller(db, BackfillConfig(k=4)).run()
    assert stats.chunks_processed == 0
    assert stats.chunks_skipped == 2
    assert _count_epa(db) == 0


async def test_backfill_populates_once_vectors_present(tmp_path: Path) -> None:
    """Sanity: when vectors DO exist the backfill still writes rows.

    Guards that the empty-path diagnostic added by this lane didn't
    short-circuit the populated happy path.
    """
    import numpy as np

    db = tmp_path / "kb.sqlite"
    _new_db(db)
    conn = sqlite3.connect(db)
    try:
        file_id = conn.execute(
            "INSERT INTO files(path, diary_name, checksum, mtime, size) "
            "VALUES (?, ?, ?, ?, ?)",
            ("notes/a.md", "notes", "h", 0, 0),
        ).lastrowid
        rng = np.random.default_rng(seed=99)
        for i in range(12):
            vec = rng.standard_normal(8).astype("<f4")
            conn.execute(
                "INSERT INTO chunks(file_id, chunk_index, content, vector, namespace) "
                "VALUES (?, ?, ?, ?, ?)",
                (file_id, i, f"chunk {i}", np.ascontiguousarray(vec).tobytes(), "general"),
            )
        conn.commit()
    finally:
        conn.close()

    stats = await EpaBackfiller(db, BackfillConfig(k=4)).run()
    assert stats.chunks_processed == 12
    assert stats.basis_axes >= 1
    assert _count_epa(db) == 12
