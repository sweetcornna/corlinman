"""Dynamic boost formula for tag-memo activation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from corlinman_tagmemo.epa import EpaBasis, EpaProjection
from corlinman_tagmemo.pyramid import PyramidResult, build_pyramid


def dynamic_boost(
    logic_depth: float,
    resonance_boost: float = 0.0,
    entropy_penalty: float = 0.0,
    base_tag_boost: float = 1.0,
    boost_range: tuple[float, float] = (0.5, 2.5),
) -> float:
    """Combine logic depth + external signals into a single multiplicative boost.

    Inputs are clamped to their expected ranges so pathological callers
    (e.g. `entropy_penalty = -2`) cannot produce a division by zero or NaN.
    """
    ld = float(np.clip(logic_depth, 0.0, 1.0))
    rb = float(np.clip(resonance_boost, 0.0, 1.0))
    ep = float(np.clip(entropy_penalty, 0.0, 1.0))

    denom = 1.0 + ep * 0.5  # ep in [0,1] => denom in [1, 1.5], never zero.
    factor = ld * (1.0 + rb) / denom
    lo, hi = boost_range
    return float(np.clip(base_tag_boost * factor, lo, hi))


@dataclass(frozen=True)
class ChunkEpa:
    """The per-chunk EPA stats persisted in the ``chunk_epa`` table.

    Mirrors the columns the offline ``EpaBackfiller`` writes
    (``projections`` / ``entropy`` / ``logic_depth``); the
    ``logic_depth`` field is the one the boost formula consumes. Decoded
    by callers from the SQLite row so this module never touches the DB.
    """

    logic_depth: float
    entropy: float = 0.0
    projections: np.ndarray | None = None


def chunk_epa_boost(
    chunk_epa: ChunkEpa,
    *,
    query_pyramid: PyramidResult | None = None,
    base_tag_boost: float = 1.0,
    boost_range: tuple[float, float] = (0.5, 2.5),
) -> float:
    """Turn a chunk's stored EPA stats into a recall boost multiplier.

    This is the query-time read of ``chunk_epa``: a candidate chunk's
    ``logic_depth`` drives the base :func:`dynamic_boost`, and when the
    query's residual-pyramid features are supplied they feed the
    ``resonance_boost`` (how strongly the query aligns with the tag-memo
    basis) and ``entropy_penalty`` (the chunk's own diffuseness) terms.

    The pyramid is optional so a caller that only has the chunk's
    ``logic_depth`` (the minimum the backfill writes) still gets a sane
    boost — exactly the Rust ``EpaBoost::prepare`` contract, which keys
    its cache on ``logic_depth`` alone.
    """
    resonance = (
        float(query_pyramid.features.tag_memo_activation)
        if query_pyramid is not None
        else 0.0
    )
    return dynamic_boost(
        logic_depth=chunk_epa.logic_depth,
        resonance_boost=resonance,
        entropy_penalty=chunk_epa.entropy,
        base_tag_boost=base_tag_boost,
        boost_range=boost_range,
    )


def query_pyramid_for_basis(
    basis: EpaBasis,
    query_vec: np.ndarray,
    *,
    target_explained: float = 0.90,
) -> PyramidResult:
    """Build the residual pyramid for a query against a fitted EPA basis.

    Thin convenience wrapper so the recall path can derive the query's
    :class:`~corlinman_tagmemo.pyramid.PyramidResult` (and thence the
    ``tag_memo_activation`` resonance signal) in one call, without the
    boost module's callers having to import :mod:`pyramid` directly.
    """
    return build_pyramid(basis, query_vec, target_explained=target_explained)


def project_to_chunk_epa(projection: EpaProjection) -> ChunkEpa:
    """Adapt an :class:`EpaProjection` into a :class:`ChunkEpa`.

    Lets a caller that just projected a chunk (e.g. inline during a
    re-embed) build the boost input without round-tripping through the
    ``chunk_epa`` table — the same ``(logic_depth, entropy, projections)``
    triple the offline backfill persists.
    """
    return ChunkEpa(
        logic_depth=float(projection.logic_depth),
        entropy=float(projection.entropy),
        projections=np.asarray(projection.projections, dtype=np.float64),
    )
