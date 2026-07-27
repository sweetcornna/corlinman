"""G2 (c)+(d): dense-vector ingest, brute-force cosine retrieval, RRF
fusion, backfill — and the "off = byte-identical" guarantee.

The embedder is a deterministic toy over a 3-dim space (cat-axis,
dog-axis, bias) so semantic matches are fully predictable without a
provider. The bias component keeps every vector's norm non-zero.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from corlinman_memory_host import LocalSqliteHost, MemoryDoc, MemoryQuery
from corlinman_memory_host.dense import (
    cosine_similarity,
    pack_vector,
    rrf_fuse,
    unpack_vector,
)

_CAT_WORDS = ("cat", "feline", "purr", "purring", "whiskers")
_DOG_WORDS = ("dog", "canine", "bark", "barking", "kennel")


def _toy_vector(text: str) -> list[float]:
    lowered = text.lower()
    cat = 1.0 if any(w in lowered for w in _CAT_WORDS) else 0.0
    dog = 1.0 if any(w in lowered for w in _DOG_WORDS) else 0.0
    return [cat, dog, 0.1]


class _ToyEmbedder:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embedding backend down")
        return [_toy_vector(t) for t in texts]


def _dense_config(**overrides: Any) -> dict[str, Any]:
    cfg = {"dense_enabled": True, "rrf_k": 60, "dense_top_k": 20}
    cfg.update(overrides)
    return cfg


async def _open_host(tmp_path: Path, name: str) -> LocalSqliteHost:
    return await LocalSqliteHost.open("local", tmp_path / f"{name}.sqlite")


# ---------------------------------------------------------------------------
# Pure primitives
# ---------------------------------------------------------------------------


def test_vector_codec_roundtrip() -> None:
    values = [0.25, -1.5, 3.0]
    blob = pack_vector(values)
    assert unpack_vector(blob) == values


def test_vector_codec_rejects_garbage() -> None:
    assert unpack_vector(None) is None
    assert unpack_vector(b"") is None
    assert unpack_vector(b"\x00\x01\x02") is None  # not a multiple of 4


def test_cosine_similarity_basics() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    # Dimension mismatch and zero norms are 0.0, never an exception.
    assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_rrf_fuse_scores_and_ordering() -> None:
    # List A ranks 1,2,3 — list B ranks 3,4. With k=60:
    #   id 3: 1/63 + 1/61 ; id 1: 1/61 ; id 2: 1/62 ; id 4: 1/62.
    fused = rrf_fuse([[1, 2, 3], [3, 4]], k=60)
    ids = [cid for cid, _ in fused]
    scores = dict(fused)
    assert ids[0] == 3
    assert scores[3] == pytest.approx(1 / 63 + 1 / 61)
    assert scores[1] == pytest.approx(1 / 61)
    assert scores[2] == pytest.approx(1 / 62)
    assert scores[4] == pytest.approx(1 / 62)
    # id 2 and id 4 tie on score — ascending-id tie-break is pinned.
    assert ids == [3, 1, 2, 4]


def test_rrf_fuse_empty_lists() -> None:
    assert rrf_fuse([[], []], k=60) == []


# ---------------------------------------------------------------------------
# Ingest writes real vectors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_writes_embedding_into_vector_blob(tmp_path: Path) -> None:
    host = await _open_host(tmp_path, "ingest")
    embedder = _ToyEmbedder()
    host.configure_dense(embed_many=embedder, config_getter=_dense_config)
    try:
        chunk_id = int(await host.upsert(MemoryDoc(content="the feline purrs")))
        async with host.store._conn.execute(
            "SELECT vector FROM chunks WHERE id = ?", (chunk_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert unpack_vector(row["vector"]) == pytest.approx([1.0, 0.0, 0.1])
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_upsert_embed_failure_degrades_to_null_vector(tmp_path: Path) -> None:
    """红线: embedding 失败不能让 ingest 崩 — the chunk still lands."""
    host = await _open_host(tmp_path, "ingest-fail")
    host.configure_dense(embed_many=_ToyEmbedder(fail=True), config_getter=_dense_config)
    try:
        chunk_id = int(await host.upsert(MemoryDoc(content="the feline purrs")))
        async with host.store._conn.execute(
            "SELECT vector FROM chunks WHERE id = ?", (chunk_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None and row["vector"] is None
        # BM25 retrieval still works for the vectorless chunk.
        hits = await host.query(MemoryQuery(text="feline", top_k=5))
        assert [h.id for h in hits] == [str(chunk_id)]
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# End-to-end dense retrieval + fusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dense_retrieval_hits_semantic_match(tmp_path: Path) -> None:
    """A query sharing NO tokens with the document still hits via dense.

    'purring cat' vs a corpus of 'feline whiskers' / 'canine kennel':
    FTS5 (no stemming) finds nothing, the toy embedding space maps both
    the query and the cat doc onto the cat axis.
    """
    host = await _open_host(tmp_path, "semantic")
    embedder = _ToyEmbedder()
    host.configure_dense(embed_many=embedder, config_getter=_dense_config)
    try:
        cat_id = await host.upsert(MemoryDoc(content="feline whiskers everywhere"))
        await host.upsert(MemoryDoc(content="canine kennel routine"))

        hits = await host.query(MemoryQuery(text="purring cat", top_k=5))

        assert hits, "dense leg must surface the semantic match"
        # The cat doc wins; the dog doc scores lower on the cat axis
        # (only bias overlap).
        assert hits[0].id == cat_id
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_fusion_combines_bm25_and_dense_ranks(tmp_path: Path) -> None:
    """A doc found by BOTH legs outranks single-leg docs under RRF."""
    host = await _open_host(tmp_path, "fusion")
    embedder = _ToyEmbedder()
    host.configure_dense(embed_many=embedder, config_getter=_dense_config)
    try:
        both_id = await host.upsert(MemoryDoc(content="cat food brands"))
        await host.upsert(MemoryDoc(content="food safety rules"))  # BM25-only
        await host.upsert(MemoryDoc(content="feline naps"))  # dense-only

        hits = await host.query(MemoryQuery(text="cat food", top_k=5))

        assert hits, "fusion must produce hits"
        assert hits[0].id == both_id
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_dense_query_failure_degrades_to_bm25(tmp_path: Path) -> None:
    """红线: embedding 失败不能让检索崩 — falls back to plain BM25."""
    host = await _open_host(tmp_path, "quer-fail")
    host.configure_dense(embed_many=_ToyEmbedder(fail=True), config_getter=_dense_config)
    try:
        doc_id = await host.upsert(MemoryDoc(content="alpha beta gamma"))
        hits = await host.query(MemoryQuery(text="alpha", top_k=5))
        assert [h.id for h in hits] == [doc_id]
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# Off = byte-identical
# ---------------------------------------------------------------------------


def _hit_tuples(hits: list[Any]) -> list[tuple[Any, ...]]:
    return [(h.id, h.content, h.score, h.source, h.metadata) for h in hits]


@pytest.mark.asyncio
async def test_disabled_flag_is_byte_identical_to_unconfigured(
    tmp_path: Path,
) -> None:
    """With dense_enabled off, results equal a never-configured host and
    the embedder is NEVER invoked (embeddings cost money)."""
    corpus = [
        "alpha cat story",
        "beta dog story",
        "alpha beta appendix",
    ]
    queries = ["alpha", "beta story", "alpha beta"]

    baseline = await _open_host(tmp_path, "baseline")
    embedder = _ToyEmbedder()
    configured = await _open_host(tmp_path, "configured-off")
    configured.configure_dense(
        embed_many=embedder,
        config_getter=lambda: _dense_config(dense_enabled=False),
    )
    try:
        for text in corpus:
            await baseline.upsert(MemoryDoc(content=text))
            await configured.upsert(MemoryDoc(content=text))
        for q in queries:
            req = MemoryQuery(text=q, top_k=5)
            assert _hit_tuples(await configured.query(req)) == _hit_tuples(
                await baseline.query(req)
            )
        # Vectors were not written and the embedder never ran.
        async with configured.store._conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE vector IS NOT NULL"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None and int(row[0]) == 0
        assert embedder.calls == []
    finally:
        await baseline.close()
        await configured.close()


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_stamps_missing_vectors_then_dense_works(
    tmp_path: Path,
) -> None:
    host = await _open_host(tmp_path, "backfill")
    embedder = _ToyEmbedder()
    try:
        # Ingest BEFORE dense is configured — vectors are NULL.
        for text in ("feline chronicles", "canine adventures", "neutral notes"):
            await host.upsert(MemoryDoc(content=text))

        host.configure_dense(embed_many=embedder, config_getter=_dense_config)
        stamped = await host.backfill_vectors(batch_size=2)
        assert stamped == 3

        async with host.store._conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE vector IS NULL"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None and int(row[0]) == 0

        # Idempotent: nothing left to stamp.
        assert await host.backfill_vectors() == 0

        # Semantic retrieval now works over the backfilled corpus.
        hits = await host.query(MemoryQuery(text="purring cat", top_k=3))
        assert hits and hits[0].content == "feline chronicles"
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_backfill_without_embedder_is_a_noop(tmp_path: Path) -> None:
    host = await _open_host(tmp_path, "backfill-noop")
    try:
        await host.upsert(MemoryDoc(content="anything"))
        assert await host.backfill_vectors() == 0
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_backfill_stops_on_embed_failure_and_reports_progress(
    tmp_path: Path,
) -> None:
    host = await _open_host(tmp_path, "backfill-fail")

    class _FailsOnSecondBatch:
        def __init__(self) -> None:
            self.count = 0

        async def __call__(self, texts: list[str]) -> list[list[float]]:
            self.count += 1
            if self.count > 1:
                raise RuntimeError("backend died mid-backfill")
            return [_toy_vector(t) for t in texts]

    try:
        for i in range(4):
            await host.upsert(MemoryDoc(content=f"doc number {i} feline"))
        host.configure_dense(
            embed_many=_FailsOnSecondBatch(), config_getter=_dense_config
        )
        stamped = await host.backfill_vectors(batch_size=2)
        assert stamped == 2  # first batch landed, failure stopped the loop
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# Config shape parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dense_settings_parse_bools_before_ints(tmp_path: Path) -> None:
    host = await _open_host(tmp_path, "settings")
    try:
        host.configure_dense(
            embed_many=_ToyEmbedder(),
            # bools must NOT be accepted where ints are expected, and
            # dense_enabled must only honour a literal True.
            config_getter=lambda: {
                "dense_enabled": 1,
                "rrf_k": True,
                "dense_top_k": "7",
            },
        )
        enabled, rrf_k, dense_top_k = host._dense_settings()
        assert enabled is False  # 1 is not True
        assert rrf_k == 60  # bool rejected → default
        assert dense_top_k == 20  # str rejected → default

        host.configure_dense(
            embed_many=_ToyEmbedder(),
            config_getter=lambda: {
                "dense_enabled": True,
                "rrf_k": 10,
                "dense_top_k": 5,
            },
        )
        assert host._dense_settings() == (True, 10, 5)

        # A raising getter degrades to off, never crashes retrieval.
        def _boom() -> Any:
            raise RuntimeError("config store down")

        host.configure_dense(embed_many=_ToyEmbedder(), config_getter=_boom)
        assert host._dense_settings()[0] is False
    finally:
        await host.close()
