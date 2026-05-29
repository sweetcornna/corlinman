"""Regression tests for R2-005 — canvas session id entropy.

The session id returned by :func:`_new_session_id` is the only
credential gating ``GET /v1/canvas/session/{id}/events`` (SSE event
stream) and ``POST /v1/canvas/frame`` (push frames into another
session). Per NIST SP 800-63B unguessable session identifiers MUST
carry at least 128 bits of entropy; the pre-fix Rust-parity id
("cs_" + 8 hex chars) was 32 bits and brute-forceable in minutes.
"""

from __future__ import annotations

from corlinman_server.gateway.routes.canvas import _new_session_id


def test_session_id_carries_at_least_128_bits_of_entropy() -> None:
    """At least 32 chars (post-prefix) so the encoded body holds
    >=128 bits — concretely ``secrets.token_urlsafe(24)`` => 192
    bits, base64url ~ 32 chars => total >= 35 with the ``cs_``
    prefix."""
    sid = _new_session_id()
    assert sid.startswith("cs_")
    # Need >=32 chars total to safely encode 128+ bits.
    assert len(sid) >= 32, f"session id too short: {sid!r} ({len(sid)} chars)"


def test_session_ids_are_unique_across_10k_draws() -> None:
    """Collisions in a 10k sample would imply <<128 bits effective
    entropy. With 192 bits the collision probability is ~0."""
    ids = {_new_session_id() for _ in range(10_000)}
    assert len(ids) == 10_000
