"""Vector primitives — f32 blob codec + brute-force cosine top-k.

Same little-endian float32 encoding as ``corlinman_episodes.embed`` so
blobs are interchangeable across stores. Brute force is deliberate: a
per-scope candidate set is hundreds-to-thousands of rows on a single
server, where a linear scan is <10ms and needs zero native deps. The
function boundary lets sqlite-vec (or numpy) slot in later without
touching callers.
"""

from __future__ import annotations

import math
import struct


def encode_f32(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def decode_f32(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob[: n * 4]))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def cosine_topk(
    query: list[float],
    candidates: list[tuple[str, bytes]],
    top_k: int,
) -> list[tuple[str, float]]:
    """Rank ``(id, f32-blob)`` candidates by cosine similarity to ``query``.

    Blobs whose dimension differs from the query score 0 (skipped from
    the result) rather than raising — mixed-dimension stores happen when
    the embedding provider changes between waves.
    """
    if top_k <= 0 or not query:
        return []
    scored: list[tuple[str, float]] = []
    for item_id, blob in candidates:
        vec = decode_f32(blob)
        if len(vec) != len(query):
            continue
        score = cosine(query, vec)
        if score > 0.0:
            scored.append((item_id, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_k]
