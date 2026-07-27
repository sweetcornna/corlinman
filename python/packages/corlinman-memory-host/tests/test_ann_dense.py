"""G2 phase 2: HNSW ANN wired into ``search_dense`` — lazy in-memory
build, small-corpus brute fallback, incremental inserts, coarse staleness
invalidation, and the never-crash degradation posture.

Vectors are written straight through ``_SqliteStore`` (the same code path
host ingest uses) so tests control ids and dimensions deterministically.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest
from corlinman_memory_host import LocalSqliteHost, MemoryDoc, MemoryQuery
from corlinman_memory_host.dense import cosine_similarity, pack_vector
from corlinman_memory_host.local_sqlite import _SqliteStore

_DIM = 8


def _random_vectors(n: int, dim: int, seed: int) -> list[list[float]]:
    rng = random.Random(seed)
    return [[rng.uniform(-1.0, 1.0) for _ in range(dim)] for _ in range(n)]


async def _open_store(tmp_path: Path, name: str) -> _SqliteStore:
    return await _SqliteStore.open(tmp_path / f"{name}.sqlite")


async def _seed_chunks(
    store: _SqliteStore,
    vectors: list[list[float]],
    *,
    namespace: str = "general",
) -> list[int]:
    file_id = await store.insert_file("seed://ann", "memory-host", "", 0, 0)
    ids: list[int] = []
    for i, vector in enumerate(vectors):
        ids.append(
            await store.insert_chunk(
                file_id, i, f"chunk {i}", pack_vector(vector), namespace
            )
        )
    return ids


# ---------------------------------------------------------------------------
# Small-corpus fallback: ann on == ann off, byte-identical
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_below_min_chunks_ann_is_byte_identical_to_brute(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path, "small")
    try:
        await _seed_chunks(store, _random_vectors(30, _DIM, seed=1))
        query = _random_vectors(1, _DIM, seed=2)[0]
        brute = await store.search_dense(query, 10)
        ann = await store.search_dense(
            query, 10, ann_enabled=True, ann_min_chunks=1000
        )
        assert ann == brute
        # Below the threshold no index is ever built (lazy posture).
        assert store._ann_scopes == {}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ann_candidates_match_brute_on_modest_corpus(tmp_path: Path) -> None:
    store = await _open_store(tmp_path, "modest")
    try:
        vectors = _random_vectors(120, _DIM, seed=3)
        ids = await _seed_chunks(store, vectors)
        query = _random_vectors(1, _DIM, seed=4)[0]
        brute = await store.search_dense(query, 10)
        ann = await store.search_dense(
            query, 10, ann_enabled=True, ann_min_chunks=1
        )
        # An index was built for the unscoped (None) scope.
        assert None in store._ann_scopes
        assert len(store._ann_scopes[None].index) == len(ids)
        # ANN re-scores candidates against the live BLOBs, so any id both
        # paths return carries the identical exact cosine score; on this
        # corpus (ef_search covers most of the graph) recall is near-total.
        brute_scores = dict(brute)
        overlap = [cid for cid, _ in ann if cid in brute_scores]
        assert len(overlap) >= 9
        for cid, score in ann:
            if cid in brute_scores:
                assert score == pytest.approx(brute_scores[cid])
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Incremental insert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_insert_searchable_without_rebuild(tmp_path: Path) -> None:
    store = await _open_store(tmp_path, "incremental")
    try:
        await _seed_chunks(store, _random_vectors(60, _DIM, seed=5))
        probe = [0.0] * _DIM
        probe[0] = 1.0
        await store.search_dense(probe, 5, ann_enabled=True, ann_min_chunks=1)
        scope = store._ann_scopes[None]
        built_index = scope.index
        size_before = len(built_index)
        # Ingest a new chunk pointing exactly along the probe axis.
        file_id = await store.insert_file("seed://ann-new", "memory-host", "", 0, 0)
        new_id = await store.insert_chunk(
            file_id, 0, "new probe chunk", pack_vector(probe), "general"
        )
        hits = await store.search_dense(probe, 1, ann_enabled=True, ann_min_chunks=1)
        assert hits and hits[0][0] == new_id
        assert hits[0][1] == pytest.approx(1.0)
        # Same index object grew by one — incremental insert, no rebuild.
        assert store._ann_scopes[None].index is built_index
        assert len(built_index) == size_before + 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_backfill_stamp_becomes_incremental_insert(tmp_path: Path) -> None:
    """``update_chunk_vector`` on a NULL row (the backfill path) inserts
    into an existing index instead of counting as a stale write."""
    store = await _open_store(tmp_path, "backfill")
    try:
        await _seed_chunks(store, _random_vectors(40, _DIM, seed=6))
        file_id = await store.insert_file("seed://null", "memory-host", "", 0, 0)
        null_id = await store.insert_chunk(file_id, 0, "vectorless", None, "general")
        probe = [0.0] * _DIM
        probe[-1] = 1.0
        await store.search_dense(probe, 5, ann_enabled=True, ann_min_chunks=1)
        scope = store._ann_scopes[None]
        assert null_id not in scope.index
        await store.update_chunk_vector(null_id, pack_vector(probe))
        assert null_id in scope.index
        assert scope.stale_writes == 0
        hits = await store.search_dense(probe, 1, ann_enabled=True, ann_min_chunks=1)
        assert hits and hits[0][0] == null_id
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Staleness: deletes, re-embeds, dimension change, threshold rebuild
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleted_chunk_never_surfaces_from_stale_index(tmp_path: Path) -> None:
    store = await _open_store(tmp_path, "deleted")
    try:
        probe = [0.0] * _DIM
        probe[0] = 1.0
        vectors = _random_vectors(50, _DIM, seed=7)
        vectors[10] = probe
        ids = await _seed_chunks(store, vectors)
        await store.search_dense(probe, 5, ann_enabled=True, ann_min_chunks=1)
        await store.delete_chunk_by_id(ids[10])
        assert store._ann_scopes[None].stale_writes == 1
        hits = await store.search_dense(probe, 5, ann_enabled=True, ann_min_chunks=1)
        assert all(cid != ids[10] for cid, _ in hits)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reembedded_chunk_scores_by_live_vector(tmp_path: Path) -> None:
    store = await _open_store(tmp_path, "reembed")
    try:
        probe = [0.0] * _DIM
        probe[0] = 1.0
        vectors = _random_vectors(50, _DIM, seed=8)
        vectors[5] = probe
        ids = await _seed_chunks(store, vectors)
        await store.search_dense(probe, 5, ann_enabled=True, ann_min_chunks=1)
        # Re-embed chunk 5 to a new direction; the graph keeps the old one.
        new_vector = [0.0] * _DIM
        new_vector[1] = 1.0
        await store.update_chunk_vector(ids[5], pack_vector(new_vector))
        scope = store._ann_scopes[None]
        assert scope.stale_writes == 1
        hits = await store.search_dense(probe, 50, ann_enabled=True, ann_min_chunks=1)
        scores = dict(hits)
        # The stale graph entry may still nominate the id, but the score
        # is re-computed from the LIVE vector, never the stale one.
        if ids[5] in scores:
            assert scores[ids[5]] == pytest.approx(
                cosine_similarity(probe, new_vector)
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_query_dimension_change_rebuilds_index(tmp_path: Path) -> None:
    store = await _open_store(tmp_path, "dimswap")
    try:
        ids = await _seed_chunks(store, _random_vectors(40, _DIM, seed=9))
        old_probe = [0.0] * _DIM
        old_probe[0] = 1.0
        await store.search_dense(old_probe, 5, ann_enabled=True, ann_min_chunks=1)
        assert store._ann_scopes[None].dim == _DIM
        # Model swap: re-stamp every chunk at dim 4, then query at dim 4.
        new_dim = 4
        for i, cid in enumerate(ids):
            vec = [0.0] * new_dim
            vec[i % new_dim] = 1.0
            await store.update_chunk_vector(cid, pack_vector(vec))
        new_probe = [1.0, 0.0, 0.0, 0.0]
        hits = await store.search_dense(
            new_probe, 5, ann_enabled=True, ann_min_chunks=1
        )
        scope = store._ann_scopes[None]
        assert scope.dim == new_dim
        assert hits and hits[0][1] == pytest.approx(1.0)
        brute = await store.search_dense(new_probe, 5)
        assert hits == brute
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stale_write_threshold_triggers_rebuild(tmp_path: Path) -> None:
    store = await _open_store(tmp_path, "threshold")
    try:
        ids = await _seed_chunks(store, _random_vectors(80, _DIM, seed=10))
        probe = [0.0] * _DIM
        probe[0] = 1.0
        await store.search_dense(probe, 5, ann_enabled=True, ann_min_chunks=1)
        first_index = store._ann_scopes[None].index
        # Threshold is max(64, len//10) = 64: 65 deletes decay past it.
        for cid in ids[:65]:
            await store.delete_chunk_by_id(cid)
        assert store._ann_scopes[None].stale_writes == 65
        await store.search_dense(probe, 5, ann_enabled=True, ann_min_chunks=1)
        scope = store._ann_scopes[None]
        assert scope.index is not first_index  # rebuilt
        assert len(scope.index) == len(ids) - 65
        assert scope.stale_writes == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_namespace_rename_drops_all_scopes(tmp_path: Path) -> None:
    store = await _open_store(tmp_path, "rename")
    try:
        await store.ensure_memory_host_metadata_schema()
        await _seed_chunks(store, _random_vectors(40, _DIM, seed=11), namespace="ns/a")
        probe = [0.0] * _DIM
        probe[0] = 1.0
        await store.search_dense(
            probe, 5, "ns/a", ann_enabled=True, ann_min_chunks=1
        )
        assert "ns/a" in store._ann_scopes
        await store.rename_namespace_prefix("ns/a", "ns/b")
        assert store._ann_scopes == {}
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Never-crash degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_build_failure_degrades_to_brute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await _open_store(tmp_path, "buildfail")
    try:
        await _seed_chunks(store, _random_vectors(40, _DIM, seed=12))
        query = _random_vectors(1, _DIM, seed=13)[0]
        brute = await store.search_dense(query, 10)

        class _ExplodingIndex:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("graph construction blew up")

        import corlinman_memory_host.local_sqlite as local_sqlite_module

        monkeypatch.setattr(local_sqlite_module, "HnswIndex", _ExplodingIndex)
        ann = await store.search_dense(
            query, 10, ann_enabled=True, ann_min_chunks=1
        )
        assert ann == brute  # degraded, not crashed
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Host-level wiring: [rag] knobs reach search_dense through the fusion leg
# ---------------------------------------------------------------------------


class _ToyEmbedder:
    """cat/dog toy space, mirroring test_dense_vectors.py."""

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            cat = 1.0 if "cat" in lowered or "feline" in lowered else 0.0
            dog = 1.0 if "dog" in lowered or "canine" in lowered else 0.0
            out.append([cat, dog, 0.1])
        return out


@pytest.mark.asyncio
async def test_host_query_identical_with_ann_on_small_corpus(
    tmp_path: Path,
) -> None:
    """RRF-fused query results are identical with ann on vs off below the
    ann_min_chunks threshold — the acceptance-(b) contract."""
    host = await LocalSqliteHost.open("local", tmp_path / "host.sqlite")
    cfg: dict[str, Any] = {"dense_enabled": True}
    host.configure_dense(embed_many=_ToyEmbedder(), config_getter=lambda: cfg)
    try:
        await host.upsert(MemoryDoc(content="the feline purrs on the mat"))
        await host.upsert(MemoryDoc(content="a canine barks at the gate"))
        await host.upsert(MemoryDoc(content="quarterly report of the office"))
        query = MemoryQuery(text="cat sound", top_k=5)
        baseline = await host.query(query)
        cfg = {"dense_enabled": True, "ann_enabled": True, "ann_min_chunks": 1000}
        host.configure_dense(embed_many=_ToyEmbedder(), config_getter=lambda: cfg)
        with_ann = await host.query(query)
        assert with_ann == baseline
        assert host.store._ann_scopes == {}  # brute fallback: nothing built
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_ann_settings_parse_defensively(tmp_path: Path) -> None:
    host = await LocalSqliteHost.open("local", tmp_path / "cfg.sqlite")
    try:
        # Unconfigured → off with defaults.
        assert host._ann_settings() == (False, 1000)
        # Literal True only; integral floats accepted for the int knob.
        cfg: dict[str, Any] = {"ann_enabled": True, "ann_min_chunks": 5.0}
        host.configure_dense(embed_many=_ToyEmbedder(), config_getter=lambda: cfg)
        assert host._ann_settings() == (True, 5)
        # Truthy non-bools do NOT enable; bools where ints are expected
        # fall back to the default (isinstance bool before int).
        cfg = {"ann_enabled": 1, "ann_min_chunks": True}
        assert host._ann_settings() == (False, 1000)
        # A raising getter degrades to off.
        def _boom() -> Any:
            raise RuntimeError("config store down")

        host.configure_dense(embed_many=_ToyEmbedder(), config_getter=_boom)
        assert host._ann_settings() == (False, 1000)
    finally:
        await host.close()
