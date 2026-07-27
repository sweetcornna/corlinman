"""Dense-vector primitives for the local SQLite memory host (G2 phase 1).

Pure-Python on purpose: the workspace's memory-host package carries no
numpy dependency and phase 1 is a brute-force scan over at most a few
thousand chunk vectors — ``struct`` + ``math`` are plenty. ANN indexing
(HNSW) is explicitly phase 2.

Vector wire format: raw little-endian ``f32`` sequence (``<{n}f``) in the
existing ``chunks.vector`` BLOB column — the same layout the Rust
``corlinman-vector`` crate used, so a corpus ingested by the old gateway
stays readable. Dimension is implied by byte length; readers skip blobs
whose dimension doesn't match the query vector (a model swap leaves stale
vectors behind until backfill re-stamps them).
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence

# f32 little-endian — 4 bytes per component.
_F32_SIZE = 4


def pack_vector(values: Sequence[float]) -> bytes:
    """Encode ``values`` as a little-endian f32 BLOB."""
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes | None) -> list[float] | None:
    """Decode a BLOB written by :func:`pack_vector`.

    Returns ``None`` (never raises) for anything that can't be a valid
    f32 sequence — empty, ``None``, or a byte length that isn't a
    multiple of 4 — so a corrupt row degrades to "no vector".
    """
    if not blob:
        return None
    if len(blob) % _F32_SIZE != 0:
        return None
    count = len(blob) // _F32_SIZE
    return list(struct.unpack(f"<{count}f", blob))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]; 0.0 on dimension mismatch or a
    zero-norm operand (a degenerate vector must not rank anywhere)."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def rrf_fuse(
    rank_lists: Sequence[Sequence[int]],
    *,
    k: int = 60,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion over already-ranked id lists.

    Each list is best-first; an id at 0-based position ``r`` contributes
    ``1 / (k + r + 1)`` (the classic Cormack/Clarke formulation with
    1-based ranks). Ids absent from a list simply contribute nothing from
    it. Returns ``(id, fused_score)`` sorted by score descending with an
    ascending-id tie-break so the ordering is fully deterministic.
    """
    if k < 1:
        k = 1
    scores: dict[int, float] = {}
    for ranks in rank_lists:
        for position, item_id in enumerate(ranks):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + position + 1)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


__all__ = ["cosine_similarity", "pack_vector", "rrf_fuse", "unpack_vector"]
