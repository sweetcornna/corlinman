"""Protocol-revision registry and ``initialize`` version negotiation.

Guards the property the old client silently violated: the version a
connection ends up speaking must be something *both* sides named, and every
ambiguous answer must resolve **downwards** (to the oldest revision) rather
than optimistically.
"""

from __future__ import annotations

import pytest
from corlinman_mcp_server.protocol import (
    CLIENT_PROTOCOL_VERSION,
    HANDSHAKE_PROTOCOL_VERSIONS,
    KNOWN_PROTOCOL_VERSIONS,
    LATEST_HANDSHAKE_VERSION,
    MODERN_PROTOCOL_VERSIONS,
    OLDEST_SUPPORTED_VERSION,
    STRUCTURED_CONTENT_SINCE,
    is_version_at_least,
    negotiate_version,
)

# ─── registry shape ──────────────────────────────────────────────────


def test_registry_is_ordered_and_partitioned() -> None:
    """Handshake and modern revisions partition the known set, in order."""
    assert HANDSHAKE_PROTOCOL_VERSIONS + MODERN_PROTOCOL_VERSIONS == (
        KNOWN_PROTOCOL_VERSIONS
    )
    assert OLDEST_SUPPORTED_VERSION == KNOWN_PROTOCOL_VERSIONS[0]
    assert LATEST_HANDSHAKE_VERSION == HANDSHAKE_PROTOCOL_VERSIONS[-1]


def test_client_offers_the_newest_handshake_revision() -> None:
    """The whole point of this module: we offer the newest revision we
    understand, not the oldest one that happens to work."""
    assert CLIENT_PROTOCOL_VERSION == LATEST_HANDSHAKE_VERSION
    assert CLIENT_PROTOCOL_VERSION != OLDEST_SUPPORTED_VERSION


def test_modern_revisions_are_not_handshake_reachable() -> None:
    """2026-07-28 uses the stateless envelope; it must never be offered
    through ``initialize``."""
    assert CLIENT_PROTOCOL_VERSION not in MODERN_PROTOCOL_VERSIONS
    for version in MODERN_PROTOCOL_VERSIONS:
        assert version not in HANDSHAKE_PROTOCOL_VERSIONS


# ─── ordering ────────────────────────────────────────────────────────


def test_is_version_at_least_orders_known_revisions() -> None:
    assert is_version_at_least("2025-06-18", "2025-06-18")
    assert is_version_at_least("2025-11-25", "2025-06-18")
    assert not is_version_at_least("2025-03-26", "2025-06-18")


def test_unknown_version_is_never_at_least_anything() -> None:
    """An unrecognised peer is treated as lacking the feature — degrading
    to the older wire shape, never sending a payload it never agreed to.
    A naive string compare would say ``"zzz" >= "2025-06-18"``."""
    assert not is_version_at_least("zzz", "2025-06-18")
    assert not is_version_at_least("", "2024-11-05")
    assert not is_version_at_least("2099-01-01", STRUCTURED_CONTENT_SINCE)


def test_is_version_at_least_rejects_an_unknown_minimum() -> None:
    """A typo'd gate is a programming error, not a silent False."""
    with pytest.raises(ValueError, match="known protocol version"):
        is_version_at_least("2025-06-18", "2025-06-19")


# ─── negotiation ─────────────────────────────────────────────────────


@pytest.mark.parametrize("version", HANDSHAKE_PROTOCOL_VERSIONS)
def test_negotiate_accepts_any_handshake_revision(version: str) -> None:
    """A server may counter-offer any revision it supports — including one
    older than we asked for. That is a clean negotiation, not a warning."""
    assert negotiate_version(version) == (version, None)


def test_negotiate_missing_version_falls_to_the_floor() -> None:
    resolved, warning = negotiate_version(None)
    assert resolved == OLDEST_SUPPORTED_VERSION
    assert warning is not None and "omitted" in warning


@pytest.mark.parametrize("reply", ["", 20251125, {"v": "2025-11-25"}, []])
def test_negotiate_rejects_non_string_replies(reply: object) -> None:
    """The reply comes straight off the wire — a number or an object must
    not crash the handshake, just pin us to the floor."""
    resolved, warning = negotiate_version(reply)
    assert resolved == OLDEST_SUPPORTED_VERSION
    assert warning is not None


def test_negotiate_unknown_version_falls_to_the_floor() -> None:
    resolved, warning = negotiate_version("2099-01-01")
    assert resolved == OLDEST_SUPPORTED_VERSION
    assert warning is not None and "unknown" in warning


def test_negotiate_modern_version_is_not_adopted() -> None:
    """A modern revision over the handshake means the peer put us in an
    envelope dialect we cannot encode. Stay connected, but at the floor —
    and say why."""
    resolved, warning = negotiate_version(MODERN_PROTOCOL_VERSIONS[0])
    assert resolved == OLDEST_SUPPORTED_VERSION
    assert warning is not None and "envelope" in warning
