"""f32 codec + brute-force cosine top-k."""

from __future__ import annotations

from corlinman_memory_kernel import cosine, cosine_topk, decode_f32, encode_f32


def test_f32_roundtrip() -> None:
    vec = [0.25, -1.5, 3.0]
    assert decode_f32(encode_f32(vec)) == vec
    assert decode_f32(b"") == []


def test_cosine_edges() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero norm


def test_cosine_topk_ranks_and_skips_mismatched_dims() -> None:
    query = [1.0, 0.0]
    candidates = [
        ("orthogonal", encode_f32([0.0, 1.0])),
        ("close", encode_f32([0.9, 0.1])),
        ("exact", encode_f32([2.0, 0.0])),  # scale-invariant
        ("wrong-dim", encode_f32([1.0, 0.0, 0.0])),
    ]
    ranked = cosine_topk(query, candidates, top_k=3)
    assert [item_id for item_id, _ in ranked] == ["exact", "close"]
    assert cosine_topk(query, candidates, top_k=0) == []
    assert cosine_topk([], candidates, top_k=3) == []
