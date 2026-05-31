"""EPA backfill job for the `chunk_epa` cache.

The v5→v6 migration creates the `chunk_epa` table but leaves it empty —
the TagMemo engine only populates rows lazily as chunks are re-embedded.
For existing corpora we need a one-shot batch job that fits an EPA basis
over the whole corpus (or a sample), projects every chunk onto it, and
upserts `(projections, entropy, logic_depth)` rows.

This runs as offline maintenance, not in the query hot path, so it reads
and writes SQLite directly via `sqlite3` rather than going through the
Rust `SqliteStore`.

Binary format: `chunks.vector` and `chunk_epa.projections` are both
little-endian packed `f32[]` BLOBs — confirmed against
`corlinman-vector::f32_slice_to_blob` (rust/crates/corlinman-vector/src/lib.rs).
We use `numpy.ndarray.astype(np.float32).tobytes()`, which produces
little-endian bytes on every platform corlinman supports.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import structlog
from corlinman_tagmemo import EpaBasis, fit_basis, project

_log = structlog.get_logger(__name__)

# Sampling threshold — if a corpus is bigger than this we fit the basis on
# a random sample and still project every chunk against it. Keeps memory
# + KMeans runtime predictable on large corpora.
_FIT_SAMPLE_LIMIT = 10_000


@dataclass
class BackfillConfig:
    """Knobs for the EPA backfill run."""

    k: int = 8
    """Number of EPA axes to fit (clamped to n_chunks by `fit_basis`)."""

    target_namespace: str | None = None
    """Restrict the backfill to chunks in a single namespace. `None` = all."""

    batch_size: int = 1_000
    """Chunks per upsert batch (keeps transactions short)."""

    fit_sample_limit: int = _FIT_SAMPLE_LIMIT
    """Random-sample cap when fitting the basis on large corpora."""

    random_seed: int = 42
    """RNG seed for both sklearn KMeans (via fit_basis) and our sampling."""


@dataclass
class BackfillStats:
    """Summary of a completed backfill run."""

    chunks_processed: int = 0
    chunks_skipped: int = 0
    basis_axes: int = 0
    wall_clock_s: float = 0.0
    namespaces_touched: list[str] = field(default_factory=list)


def _blob_to_vec(blob: bytes | None) -> np.ndarray | None:
    """Decode a little-endian packed f32[] BLOB, mirroring Rust's
    `blob_to_f32_vec`. Returns None for NULL or malformed blobs."""
    if blob is None:
        return None
    if len(blob) % 4 != 0:
        return None
    arr = np.frombuffer(blob, dtype="<f4")
    # Copy so callers can own / mutate without worrying about the buffer.
    return np.asarray(arr, dtype=np.float32).copy()


def _vec_to_blob(vec: np.ndarray) -> bytes:
    """Encode an f32[] as a little-endian packed blob, matching Rust
    `f32_slice_to_blob`. We force `<f4` dtype so the output is
    endian-deterministic on every platform."""
    arr = np.ascontiguousarray(vec, dtype="<f4")
    return arr.tobytes()


class EpaBackfiller:
    """Populate `chunk_epa` rows for every chunk with a vector.

    Phases:
        1. Load `(id, namespace, vector)` for every chunk whose vector
           is non-NULL (optionally restricted to one namespace).
        2. Fit an EPA basis — either on the whole corpus, or on a
           random sample if the corpus exceeds `fit_sample_limit`.
        3. Project every chunk onto the fitted basis and upsert the
           resulting `(projections, entropy, logic_depth)` row.

    The backfill is idempotent: rerunning it on the same corpus yields
    the same basis (seeded KMeans) and therefore byte-identical BLOBs.
    """

    def __init__(self, db_path: Path, config: BackfillConfig | None = None):
        self.db_path = Path(db_path)
        self.config = config or BackfillConfig()

    # ------------------------------------------------------------------
    # public entrypoint

    async def run(self) -> BackfillStats:
        """Run the backfill. Returns stats even on empty corpora."""
        start = time.perf_counter()
        stats = BackfillStats()

        rows = self._load_chunks()
        if not rows:
            stats.wall_clock_s = time.perf_counter() - start
            # No chunk carries a dense vector. This is the *expected* state
            # on a memory-host-populated DB today: ``LocalSqliteHost.upsert``
            # writes ``vector = NULL`` (dense recall is deferred), so the
            # backfill has nothing to project. Distinguish "no vectors but
            # chunks exist" (the live case — actionable: embeddings not yet
            # populated) from "genuinely empty DB" so an operator running
            # this offline job isn't left guessing. Never the BM25 hot path.
            stats.chunks_skipped = self._count_skipped()
            total_chunks = self._count_total_chunks()
            status = (
                "no_vectors"
                if total_chunks > 0
                else "empty_corpus"
            )
            _log.info(
                "epa_backfill",
                chunks_processed=0,
                chunks_skipped=stats.chunks_skipped,
                total_chunks=total_chunks,
                basis_axes=0,
                wall_clock_s=stats.wall_clock_s,
                namespace=self.config.target_namespace,
                status=status,
            )
            return stats

        ids, namespaces, vectors = self._split_rows(rows)
        basis = self._fit_basis(vectors)
        stats.basis_axes = int(basis.ortho_basis.shape[0])
        stats.namespaces_touched = sorted(set(namespaces))

        processed = self._upsert_projections(ids, vectors, basis)
        stats.chunks_processed = processed
        # `rows` only contains chunks with non-NULL vectors, so everything
        # else (NULL vectors + out-of-namespace rows) is implicitly skipped.
        # We surface that as a separate count for observability.
        stats.chunks_skipped = self._count_skipped()
        stats.wall_clock_s = time.perf_counter() - start

        # Structured event — name is load-bearing for Grafana/Loki queries
        # (`event="epa_backfill"`). Fields match the B5-BE4 spec so a
        # single log query powers the backfill dashboard panel.
        _log.info(
            "epa_backfill",
            chunks_processed=stats.chunks_processed,
            chunks_skipped=stats.chunks_skipped,
            basis_axes=stats.basis_axes,
            wall_clock_s=stats.wall_clock_s,
            namespaces_touched=stats.namespaces_touched,
            namespace=self.config.target_namespace,
            status="ok",
        )
        return stats

    # ------------------------------------------------------------------
    # phase 1: load

    def _connect(self) -> sqlite3.Connection:
        # `detect_types=0` because we're dealing with BLOBs directly.
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _load_chunks(self) -> list[tuple[int, str, bytes]]:
        """Load `(id, namespace, vector_blob)` for chunks with vectors."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.config.target_namespace is None:
                cur.execute(
                    "SELECT id, namespace, vector FROM chunks "
                    "WHERE vector IS NOT NULL ORDER BY id ASC"
                )
            else:
                cur.execute(
                    "SELECT id, namespace, vector FROM chunks "
                    "WHERE vector IS NOT NULL AND namespace = ? ORDER BY id ASC",
                    (self.config.target_namespace,),
                )
            return cur.fetchall()
        finally:
            conn.close()

    def _count_skipped(self) -> int:
        """Count chunks excluded from the backfill (NULL vector or
        outside `target_namespace`). Never raises — returns 0 on error."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.config.target_namespace is None:
                cur.execute("SELECT COUNT(*) FROM chunks WHERE vector IS NULL")
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM chunks "
                    "WHERE vector IS NULL OR namespace != ?",
                    (self.config.target_namespace,),
                )
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def _count_total_chunks(self) -> int:
        """Count every chunk in scope, regardless of vector state.

        Used only to classify the empty-result case: ``> 0`` here while
        ``_load_chunks()`` returned nothing means "chunks exist but none
        carry a dense vector yet" (status ``no_vectors``), as opposed to a
        genuinely empty DB (``empty_corpus``). Never raises — returns 0 on
        a missing table or any operational error so the diagnostic can't
        itself fail the offline job."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.config.target_namespace is None:
                cur.execute("SELECT COUNT(*) FROM chunks")
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM chunks WHERE namespace = ?",
                    (self.config.target_namespace,),
                )
            row = cur.fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # phase 2: fit

    @staticmethod
    def _split_rows(
        rows: list[tuple[int, str, bytes]],
    ) -> tuple[np.ndarray, list[str], np.ndarray]:
        """Decode the raw SQLite rows into parallel arrays."""
        ids = np.empty(len(rows), dtype=np.int64)
        namespaces: list[str] = []
        decoded: list[np.ndarray] = []
        dim = None
        for chunk_id, ns, blob in rows:
            vec = _blob_to_vec(blob)
            if vec is None or vec.size == 0:
                # Shouldn't happen because we filtered WHERE vector IS NOT NULL,
                # but a zero-length blob would blow up dim inference. Treat
                # it as skipped by dropping it from the fit + projection sets.
                continue
            if dim is None:
                dim = vec.shape[0]
            elif vec.shape[0] != dim:
                # Mixed dimensions → can't fit a single basis. Skip the row
                # rather than crash the whole backfill.
                continue
            ids[len(decoded)] = chunk_id
            namespaces.append(ns)
            decoded.append(vec)

        if not decoded:
            return np.empty(0, dtype=np.int64), [], np.empty((0, 0), dtype=np.float64)

        ids = ids[: len(decoded)]
        vectors = np.stack(decoded, axis=0).astype(np.float64)
        return ids, namespaces, vectors

    def _fit_basis(self, vectors: np.ndarray) -> EpaBasis:
        """Fit an EpaBasis on the corpus (or a random sample)."""
        n = int(vectors.shape[0])
        if n > self.config.fit_sample_limit:
            rng = np.random.default_rng(self.config.random_seed)
            idx = rng.choice(n, size=self.config.fit_sample_limit, replace=False)
            idx.sort()
            fit_vectors = vectors[idx]
        else:
            fit_vectors = vectors
        return fit_basis(fit_vectors, weights=None, k=self.config.k)

    # ------------------------------------------------------------------
    # phase 3: project + upsert

    def _upsert_projections(
        self, ids: np.ndarray, vectors: np.ndarray, basis: EpaBasis
    ) -> int:
        """Project + upsert in batches. Returns the number of rows written."""
        if ids.size == 0:
            return 0
        conn = self._connect()
        try:
            now = int(time.time())
            written = 0
            batch: list[tuple[bytes, float, float, int, int]] = []
            for i in range(int(ids.size)):
                proj = project(basis, vectors[i])
                # NaN safety: `project` already clamps entropy to [0,1] and
                # guards against zero-probability logs, but we still belt-
                # and-brace to keep bad floats out of SQLite.
                entropy = _sanitise_float(proj.entropy)
                logic_depth = _sanitise_float(proj.logic_depth)
                projections_f32 = np.asarray(proj.projections, dtype=np.float32)
                if not np.all(np.isfinite(projections_f32)):
                    projections_f32 = np.nan_to_num(
                        projections_f32, nan=0.0, posinf=0.0, neginf=0.0
                    )
                blob = _vec_to_blob(projections_f32)
                batch.append(
                    (blob, float(entropy), float(logic_depth), now, int(ids[i]))
                )
                if len(batch) >= self.config.batch_size:
                    written += self._flush_batch(conn, batch)
                    batch.clear()
            if batch:
                written += self._flush_batch(conn, batch)
            return written
        finally:
            conn.close()

    @staticmethod
    def _flush_batch(
        conn: sqlite3.Connection,
        batch: list[tuple[bytes, float, float, int, int]],
    ) -> int:
        """Commit one batch of upserts. Returns the row count written."""
        # ON CONFLICT preserves idempotency: rerunning against the same
        # basis overwrites with byte-identical data (KMeans is seeded).
        conn.executemany(
            "INSERT INTO chunk_epa(projections, entropy, logic_depth, "
            "computed_at, chunk_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chunk_id) DO UPDATE SET "
            "  projections = excluded.projections, "
            "  entropy     = excluded.entropy, "
            "  logic_depth = excluded.logic_depth, "
            "  computed_at = excluded.computed_at",
            batch,
        )
        conn.commit()
        return len(batch)


def _sanitise_float(x: float) -> float:
    """Replace NaN / inf with 0.0 so SQLite REAL columns never see a bogus value."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not np.isfinite(f):
        return 0.0
    return f
