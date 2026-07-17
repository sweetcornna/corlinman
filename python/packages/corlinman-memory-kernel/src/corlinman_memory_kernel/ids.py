"""ULID-ish sortable ids for kernel rows.

Same encoding as ``corlinman_episodes.store.new_episode_id`` (10-char
Crockford ms-timestamp prefix + 16-char random suffix, 26 chars total)
so ``ORDER BY id`` mirrors creation order; duplicated rather than
imported to keep this package dependency-free.
"""

from __future__ import annotations

import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(*, ts_ms: int | None = None) -> str:
    ts = ts_ms if ts_ms is not None else now_ms()
    out = []
    for _ in range(10):
        out.append(_CROCKFORD[ts & 0x1F])
        ts >>= 5
    out.reverse()
    rand_int = int.from_bytes(secrets.token_bytes(10), "big")
    rand_chars = []
    for _ in range(16):
        rand_chars.append(_CROCKFORD[rand_int & 0x1F])
        rand_int >>= 5
    rand_chars.reverse()
    return "".join(out) + "".join(rand_chars)
