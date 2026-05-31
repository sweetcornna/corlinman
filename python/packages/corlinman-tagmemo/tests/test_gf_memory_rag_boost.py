"""gap-fill lane-memory-rag: chunk_epa-driven recall boost.

Pins the wiring that lets a query read a candidate chunk's stored
``chunk_epa`` stats (``logic_depth`` / ``entropy``) and turn them into a
:func:`~corlinman_tagmemo.boost.dynamic_boost` multiplier, optionally
modulated by the query's residual-pyramid resonance.
"""

from __future__ import annotations

import numpy as np
from corlinman_tagmemo.boost import (
    ChunkEpa,
    chunk_epa_boost,
    project_to_chunk_epa,
    query_pyramid_for_basis,
)
from corlinman_tagmemo.epa import fit_basis, project


def _basis_from_clusters() -> tuple[object, np.ndarray]:
    rng = np.random.default_rng(seed=7)
    centers = rng.normal(size=(4, 12)) * 3.0
    samples = np.vstack(
        [c + rng.normal(scale=0.4, size=(25, 12)) for c in centers]
    )
    basis = fit_basis(samples, weights=None, k=4)
    # A query vector aligned with the first centroid for a strong signal.
    query = centers[0] + rng.normal(scale=0.1, size=12)
    return basis, query


def test_higher_logic_depth_boosts_more() -> None:
    low = chunk_epa_boost(ChunkEpa(logic_depth=0.1))
    high = chunk_epa_boost(ChunkEpa(logic_depth=0.9))
    assert high > low


def test_chunk_epa_boost_within_range() -> None:
    boost = chunk_epa_boost(ChunkEpa(logic_depth=1.0))
    assert 0.5 <= boost <= 2.5
    # logic_depth 0 with no resonance → clipped to the floor.
    assert chunk_epa_boost(ChunkEpa(logic_depth=0.0)) == 0.5


def test_query_pyramid_resonance_raises_boost() -> None:
    basis, query = _basis_from_clusters()
    pyramid = query_pyramid_for_basis(basis, query)
    chunk = ChunkEpa(logic_depth=0.5, entropy=0.2)

    without = chunk_epa_boost(chunk)
    with_resonance = chunk_epa_boost(chunk, query_pyramid=pyramid)
    # Adding a non-negative resonance term cannot lower the boost; with a
    # genuine pyramid activation it should lift it.
    assert with_resonance >= without


def test_entropy_penalty_lowers_boost() -> None:
    calm = chunk_epa_boost(ChunkEpa(logic_depth=0.8, entropy=0.0))
    noisy = chunk_epa_boost(ChunkEpa(logic_depth=0.8, entropy=1.0))
    assert noisy <= calm


def test_project_to_chunk_epa_roundtrip() -> None:
    basis, query = _basis_from_clusters()
    proj = project(basis, query)
    chunk = project_to_chunk_epa(proj)
    assert chunk.logic_depth == proj.logic_depth
    assert chunk.entropy == proj.entropy
    assert chunk.projections is not None
    # The boost computed from a projected chunk is a finite, ranged value.
    boost = chunk_epa_boost(chunk)
    assert np.isfinite(boost)
    assert 0.5 <= boost <= 2.5
