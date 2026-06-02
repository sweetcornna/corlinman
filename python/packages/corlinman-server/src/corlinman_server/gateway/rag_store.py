"""``RagStore`` — local FTS5 RAG corpus store backing ``/admin/rag*``.

Python port of the slice of ``corlinman_vector::SqliteStore`` the Rust
gateway opened via ``open_rag_store`` (``<data_dir>/kb.sqlite``) and
handed to the admin RAG routes. The Rust ``corlinman-vector`` crate was
not ported wholesale; this module ships just the surface the two admin
route modules consume:

``routes_admin_b/rag.py``:
  * :meth:`count_files`  — total ``files`` rows.
  * :meth:`count_chunks` — total ``chunks`` rows.
  * :meth:`count_tags`   — distinct tag count.
  * :meth:`search_bm25`  — FTS5 BM25 search → ``[(chunk_id, score)]``.
  * :meth:`query_chunks_by_ids` — hydrate ``[(id, content)]`` chunk rows.
  * :meth:`rebuild_fts`  — rebuild the ``chunks_fts`` virtual table.

``routes_admin_b/memory.py``:
  * :meth:`reset_chunk_decay`     — force ``decay_score`` back to 1.0,
    returning the affected-row count (0 = unknown chunk → 404).
  * :meth:`get_chunk_decay_state` — read a chunk's current ``decay_score``.

The schema is a deliberate subset of the ``corlinman-vector`` ``SCHEMA_SQL``
(it carries the columns these routes read/write and the FTS5 triggers that
keep BM25 in sync) plus a normalized ``tags`` table for ``count_tags``.
The store is *read-mostly* from the gateway's point of view: a fresh
``kb.sqlite`` simply reports zero counts and empty searches — the corpus
itself is populated out-of-band by the ingest tooling.

Construction is via :meth:`open` so the aiosqlite schema bootstrap is
awaited cleanly (mirrors :class:`corlinman_memory_host.LocalSqliteHost`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)


# Subset of the ``corlinman-vector`` SCHEMA_SQL: the columns these admin
# routes read/write plus the FTS5 BM25 table + sync triggers. ``decay_score``
# is the column the ``/admin/memory/decay/reset`` route forces back to 1.0;
# ``tags`` is the normalized table ``count_tags`` aggregates over. ``IF NOT
# EXISTS`` everywhere so opening a pre-existing corpus is a no-op.
_RAG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    diary_name TEXT NOT NULL DEFAULT '',
    checksum TEXT NOT NULL DEFAULT '',
    mtime INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    namespace TEXT NOT NULL DEFAULT 'general',
    decay_score REAL NOT NULL DEFAULT 1.0,
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    content='chunks',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class _ChunkRow:
    """The two fields ``rag.py`` reads off a hydrated chunk.

    Plain object so the route's ``getattr(c, "id", ...)`` /
    ``getattr(c, "content", ...)`` access works identically to the
    memory-host ``_ChunkRow``.
    """

    __slots__ = ("content", "id")

    def __init__(self, *, id: int, content: str) -> None:  # noqa: A002
        self.id = id
        self.content = content


class RagStore:
    """aiosqlite-backed FTS5 RAG corpus store (``<data_dir>/kb.sqlite``).

    Single connection — the admin routes are low-QPS operator surfaces, so
    no connection pool is warranted. Open via :meth:`open`; close via
    :meth:`close` in the gateway lifespan teardown so the WAL is
    checkpointed.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def open(cls, path: str | Path) -> RagStore:
        """Open (or create) the RAG SQLite DB at ``path`` and wrap it.

        The schema bootstrap is idempotent, so opening an existing corpus
        only adds the ``tags`` table / ``decay_score`` column when they are
        absent (via the ``IF NOT EXISTS`` guards). Creating ``decay_score``
        on a legacy ``chunks`` table that predates it is handled by
        :meth:`_ensure_decay_column`.
        """
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(_RAG_SCHEMA_SQL)
        await conn.commit()
        store = cls(conn)
        await store._ensure_decay_column()
        return store

    async def close(self) -> None:
        await self._conn.close()

    async def _ensure_decay_column(self) -> None:
        """Add ``chunks.decay_score`` to a pre-existing corpus that lacks it.

        ``CREATE TABLE IF NOT EXISTS`` does not alter the shape of an
        already-present ``chunks`` table, so a corpus ingested before this
        column existed would 500 the decay-reset route. Probe the column
        set and ``ALTER TABLE`` it in when missing. Best-effort — a failure
        leaves the route to surface its own ``storage_error``.
        """
        try:
            async with self._conn.execute("PRAGMA table_info(chunks)") as cur:
                cols = {str(r["name"]) for r in await cur.fetchall()}
            if "decay_score" not in cols:
                await self._conn.execute(
                    "ALTER TABLE chunks ADD COLUMN decay_score REAL NOT NULL DEFAULT 1.0"
                )
                await self._conn.commit()
        except aiosqlite.Error as exc:  # pragma: no cover — defensive
            logger.warning("rag_store.ensure_decay_column_failed", error=str(exc))

    # ---- /admin/rag/stats -------------------------------------------------

    async def _count(self, sql: str) -> int:
        async with self._conn.execute(sql) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row is not None else 0

    async def count_files(self) -> int:
        return await self._count("SELECT COUNT(*) FROM files")

    async def count_chunks(self) -> int:
        return await self._count("SELECT COUNT(*) FROM chunks")

    async def count_tags(self) -> int:
        return await self._count("SELECT COUNT(DISTINCT name) FROM tags")

    # ---- /admin/rag/query -------------------------------------------------

    async def search_bm25(self, q: str, k: int) -> list[tuple[int, float]]:
        """Run an FTS5 BM25 search; return ``(chunk_id, score)`` pairs.

        FTS5's ``bm25()`` is "lower = more relevant" (a negative log
        score); we negate it so larger = better, matching the orientation
        ``rag.py`` and the memory-host adapter both assume. A query that
        does not tokenise (stray operators, all stop-words) yields an empty
        result rather than raising — same forgiving handling as the
        memory-host store.
        """
        if k <= 0 or not q.strip():
            return []
        sql = (
            "SELECT rowid, bm25(chunks_fts) AS score "
            "FROM chunks_fts WHERE chunks_fts MATCH ? "
            "ORDER BY score ASC LIMIT ?"
        )
        try:
            async with self._conn.execute(sql, (q, k)) as cur:
                rows = await cur.fetchall()
        except aiosqlite.OperationalError as exc:
            # FTS5 raises on a query that doesn't tokenise cleanly (stray
            # operators, an unbalanced quote, all stop-words). For an
            # operator debug-search the right answer is "no hits", not a
            # 500 — mirror the memory-host store's forgiving handling but
            # cover the wider set of parse-error messages sqlite emits
            # (``fts5: syntax error``, ``malformed MATCH``, ``unterminated
            # string``).
            msg = str(exc).lower()
            if any(
                token in msg
                for token in ("fts5", "malformed", "unterminated", "syntax error")
            ):
                return []
            raise
        return [(int(r["rowid"]), -float(r["score"])) for r in rows]

    async def query_chunks_by_ids(self, ids: list[int]) -> list[_ChunkRow]:
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        sql = f"SELECT id, content FROM chunks WHERE id IN ({placeholders})"
        async with self._conn.execute(sql, ids) as cur:
            rows = await cur.fetchall()
        return [_ChunkRow(id=int(r["id"]), content=str(r["content"])) for r in rows]

    # ---- /admin/rag/rebuild ----------------------------------------------

    async def rebuild_fts(self) -> None:
        """Rebuild the ``chunks_fts`` virtual table from ``chunks``.

        FTS5's special ``'rebuild'`` command re-derives the index from the
        content table — the right primitive when the corpus was written
        directly (bypassing the triggers) or the index drifted.
        """
        await self._conn.execute(
            "INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')"
        )
        await self._conn.commit()

    # ---- /admin/memory/decay/reset ---------------------------------------

    async def reset_chunk_decay(self, chunk_id: int) -> int:
        """Force a chunk's ``decay_score`` back to 1.0.

        Returns the number of affected rows: 0 means the chunk doesn't
        exist (the route maps that to a 404).
        """
        cur = await self._conn.execute(
            "UPDATE chunks SET decay_score = 1.0 WHERE id = ?", (chunk_id,)
        )
        await self._conn.commit()
        return int(cur.rowcount or 0)

    async def get_chunk_decay_state(self, chunk_id: int) -> dict[str, Any] | None:
        """Read a chunk's current ``decay_score`` (or ``None`` if absent)."""
        async with self._conn.execute(
            "SELECT id, decay_score FROM chunks WHERE id = ?", (chunk_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {"chunk_id": int(row["id"]), "decay_score": float(row["decay_score"])}


__all__ = ["RagStore"]
