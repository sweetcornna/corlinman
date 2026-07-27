"""G2 phase 2: pure-Python HNSW index correctness.

Recall is measured against the exact brute-force cosine ranking on a
deterministic random vector set (seeded ``random.Random``), so the tests
are exactly reproducible — a recall regression is a real algorithmic
regression, never RNG noise.
"""

from __future__ import annotations

import random

from corlinman_memory_host.dense import cosine_similarity
from corlinman_memory_host.hnsw import HnswIndex


def _random_vectors(n: int, dim: int, seed: int) -> list[list[float]]:
    rng = random.Random(seed)
    return [[rng.uniform(-1.0, 1.0) for _ in range(dim)] for _ in range(n)]


def _brute_top_k(
    vectors: list[list[float]], query: list[float], k: int
) -> list[tuple[int, float]]:
    scored = [(i, cosine_similarity(query, v)) for i, v in enumerate(vectors)]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[:k]


def _build(vectors: list[list[float]], dim: int) -> HnswIndex:
    index = HnswIndex(dim)
    for i, v in enumerate(vectors):
        assert index.add(i, v)
    return index


# ---------------------------------------------------------------------------
# Recall vs brute force (the acceptance-criterion test)
# ---------------------------------------------------------------------------


def test_recall_at_10_vs_brute_force() -> None:
    """recall@10 >= 0.9 on a deterministic random corpus. Uniform random
    vectors are the WORST case for a navigable small-world graph (no
    cluster structure to exploit), so passing here is a strong floor."""
    dim = 16
    corpus = _random_vectors(800, dim, seed=42)
    index = _build(corpus, dim)
    queries = _random_vectors(40, dim, seed=7)
    hits = 0
    total = 0
    for q in queries:
        expected = {i for i, _ in _brute_top_k(corpus, q, 10)}
        got = {cid for cid, _ in index.search(q, 10)}
        hits += len(expected & got)
        total += len(expected)
    assert total == 400
    assert hits / total >= 0.9


def test_reported_scores_are_true_cosine() -> None:
    dim = 8
    corpus = _random_vectors(64, dim, seed=3)
    index = _build(corpus, dim)
    query = corpus[10]
    results = index.search(query, 5)
    assert results
    # Best hit must be the vector itself at similarity ~1.0.
    assert results[0][0] == 10
    assert abs(results[0][1] - 1.0) < 1e-6
    for cid, score in results:
        assert abs(score - cosine_similarity(query, corpus[cid])) < 1e-6


def test_exhaustive_on_tiny_index_matches_brute_exactly() -> None:
    """With ef_search >> n the beam covers the whole (connected) graph, so
    the result must equal the exact brute ranking including tie-breaks."""
    dim = 8
    corpus = _random_vectors(30, dim, seed=11)
    index = _build(corpus, dim)
    for q in _random_vectors(10, dim, seed=12):
        expected = _brute_top_k(corpus, q, 10)
        got = index.search(q, 10)
        assert [cid for cid, _ in got] == [cid for cid, _ in expected]
        for (_, got_s), (_, exp_s) in zip(got, expected, strict=True):
            assert abs(got_s - exp_s) < 1e-6


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_insert_sequence_is_fully_deterministic() -> None:
    dim = 8
    corpus = _random_vectors(100, dim, seed=5)
    a = _build(corpus, dim)
    b = _build(corpus, dim)
    for q in _random_vectors(5, dim, seed=6):
        assert a.search(q, 10) == b.search(q, 10)


# ---------------------------------------------------------------------------
# Degenerate inputs (add/search must be total functions)
# ---------------------------------------------------------------------------


def test_add_rejects_bad_items_without_raising() -> None:
    index = HnswIndex(3)
    assert index.add(1, [1.0, 0.0, 0.0]) is True
    assert index.add(1, [0.0, 1.0, 0.0]) is False  # duplicate id
    assert index.add(2, [1.0, 0.0]) is False  # dimension mismatch
    assert index.add(3, [0.0, 0.0, 0.0]) is False  # zero norm
    assert index.add(4, [float("nan"), 0.0, 0.0]) is False  # non-finite
    assert len(index) == 1
    assert 1 in index
    assert 2 not in index


def test_search_edge_cases() -> None:
    index = HnswIndex(3)
    assert index.search([1.0, 0.0, 0.0], 5) == []  # empty index
    index.add(7, [1.0, 0.0, 0.0])
    assert index.search([1.0, 0.0], 5) == []  # dimension mismatch
    assert index.search([0.0, 0.0, 0.0], 5) == []  # zero-norm query
    assert index.search([1.0, 0.0, 0.0], 0) == []  # k <= 0
    # k > len(index) returns what exists.
    assert index.search([1.0, 0.0, 0.0], 5) == [(7, 1.0)]


def test_incremental_adds_are_immediately_searchable() -> None:
    dim = 4
    index = HnswIndex(dim)
    for i, v in enumerate(_random_vectors(50, dim, seed=9)):
        index.add(i, v)
    assert index.add(999, [0.0, 0.0, 0.0, 1.0]) is True
    results = index.search([0.0, 0.0, 0.0, 1.0], 1)
    assert results and results[0][0] == 999
