"""corlinman-memory-kernel — unified scoped bi-temporal memory layer."""

from corlinman_memory_kernel.affect import (
    Affect,
    AffectAnchors,
    affect_from_embedding,
    build_anchors,
    resonance,
)
from corlinman_memory_kernel.ids import new_id, now_ms
from corlinman_memory_kernel.kernel import MemoryKernel, kernel_mode
from corlinman_memory_kernel.types import (
    KernelScope,
    LedgerEntry,
    MemoryItem,
    Observation,
    scope_namespace,
    user_namespace_prefix,
)
from corlinman_memory_kernel.vector import (
    cosine,
    cosine_topk,
    decode_f32,
    encode_f32,
)

__all__ = [
    "Affect",
    "AffectAnchors",
    "KernelScope",
    "LedgerEntry",
    "MemoryItem",
    "MemoryKernel",
    "Observation",
    "affect_from_embedding",
    "build_anchors",
    "cosine",
    "cosine_topk",
    "decode_f32",
    "encode_f32",
    "kernel_mode",
    "new_id",
    "now_ms",
    "resonance",
    "scope_namespace",
    "user_namespace_prefix",
]
