"""MCP protocol-revision registry and handshake negotiation.

corlinman's MCP *client* used to hard-code ``2024-11-05`` — the oldest
released revision — as its ``initialize`` offer. Modern servers accept that
verbatim (the reference SDK echoes any requested version it still supports),
so the handshake "succeeded" while every connection silently locked itself to
the 2024 feature set: no ``structuredContent``, no ``resource_link`` blocks,
no ``_meta`` passthrough, no audio content. The bug was invisible because
nothing ever failed — the server just served less.

This module makes the version an explicit, negotiated, *recorded* value:

* :data:`CLIENT_PROTOCOL_VERSION` — what we offer (the newest revision
  reachable through the ``initialize`` handshake).
* :func:`negotiate_version` — fold the server's counter-offer into the
  version this connection actually speaks, conservatively.
* :func:`is_version_at_least` / the ``*_SINCE`` constants — feature gates, so
  callers ask "does this peer do structured content?" instead of hard-coding
  a date comparison.

Two eras
--------

Revisions through ``2025-11-25`` are reachable via the ``initialize``
handshake. ``2026-07-28`` introduced a different, stateless per-request
envelope discovered through a ``server/discover`` probe rather than a
handshake — a separate transport-level dialect this client does not speak
yet. It is listed in :data:`KNOWN_PROTOCOL_VERSIONS` (so ordering
comparisons stay correct) but deliberately excluded from
:data:`HANDSHAKE_PROTOCOL_VERSIONS`, and a server that answers our
``initialize`` with it is treated as *not* understood.

Ordering
--------

Revision identifiers happen to be dates and happen to sort
lexicographically, but they are an enumerated set, not a scalar: a future
identifier need not be date-shaped, and an unrecognised peer string must
compare conservatively rather than accidentally (``"zzz" > "2025-11-25"``
is true for strings and meaningless for protocols). Every ordering question
therefore goes through :data:`KNOWN_PROTOCOL_VERSIONS`.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "AUDIO_CONTENT_SINCE",
    "CLIENT_PROTOCOL_VERSION",
    "ELICITATION_SINCE",
    "HANDSHAKE_PROTOCOL_VERSIONS",
    "KNOWN_PROTOCOL_VERSIONS",
    "LATEST_HANDSHAKE_VERSION",
    "MODERN_PROTOCOL_VERSIONS",
    "OLDEST_SUPPORTED_VERSION",
    "RESOURCE_LINK_SINCE",
    "STRUCTURED_CONTENT_SINCE",
    "TOOL_ANNOTATIONS_SINCE",
    "is_version_at_least",
    "negotiate_version",
]

KNOWN_PROTOCOL_VERSIONS: Final[tuple[str, ...]] = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
    "2026-07-28",
)
"""Every released MCP revision, oldest to newest."""

HANDSHAKE_PROTOCOL_VERSIONS: Final[tuple[str, ...]] = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
)
"""Revisions reachable through the ``initialize`` handshake — the dialect
this client speaks."""

MODERN_PROTOCOL_VERSIONS: Final[tuple[str, ...]] = ("2026-07-28",)
"""Revisions using the stateless per-request envelope (``server/discover``).
Not spoken by this client yet; listed so ordering stays honest."""

LATEST_HANDSHAKE_VERSION: Final[str] = HANDSHAKE_PROTOCOL_VERSIONS[-1]
OLDEST_SUPPORTED_VERSION: Final[str] = HANDSHAKE_PROTOCOL_VERSIONS[0]

CLIENT_PROTOCOL_VERSION: Final[str] = LATEST_HANDSHAKE_VERSION
"""The version this client offers in ``initialize``.

Offering the newest revision we understand is what lets a modern server hand
back its richer payloads; the server downgrades us to whatever *it* supports
and we record that (see :func:`negotiate_version`).
"""

# ── Feature gates ────────────────────────────────────────────────────
# Named so call sites read as intent ("does this peer do structured
# content?") rather than as a date literal buried in a comparison.

TOOL_ANNOTATIONS_SINCE: Final[str] = "2025-03-26"
"""``ToolAnnotations`` (readOnlyHint / destructiveHint / …)."""

AUDIO_CONTENT_SINCE: Final[str] = "2025-03-26"
"""``AudioContent`` blocks in tool results and prompt messages."""

STRUCTURED_CONTENT_SINCE: Final[str] = "2025-06-18"
"""``tools/call`` results carrying ``structuredContent`` + a tool
``outputSchema``."""

RESOURCE_LINK_SINCE: Final[str] = "2025-06-18"
"""``resource_link`` content blocks (a URI reference instead of an inlined
resource body)."""

ELICITATION_SINCE: Final[str] = "2025-06-18"
"""Server-initiated ``elicitation/create`` requests. Declared here for
feature-gating; this client does not advertise the capability yet."""


def is_version_at_least(version: str, minimum: str) -> bool:
    """True when ``version`` is a known revision no older than ``minimum``.

    An unknown ``version`` returns ``False`` — an unrecognised peer is
    treated as *not* having the feature, which degrades to the older wire
    shape rather than sending a payload the peer may reject.

    ``minimum`` must itself be a known revision; anything else is a
    programming error and raises.
    """
    if minimum not in KNOWN_PROTOCOL_VERSIONS:
        raise ValueError(
            f"minimum must be a known protocol version, got {minimum!r}"
        )
    if version not in KNOWN_PROTOCOL_VERSIONS:
        return False
    return KNOWN_PROTOCOL_VERSIONS.index(version) >= KNOWN_PROTOCOL_VERSIONS.index(
        minimum
    )


def negotiate_version(reply: object) -> tuple[str, str | None]:
    """Resolve the version a connection speaks from the server's reply.

    ``reply`` is the raw ``protocolVersion`` member of the ``initialize``
    result — deliberately typed ``object`` because it arrives straight off
    the wire and may be missing, null, or the wrong type entirely.

    Returns ``(version, warning)``. ``warning`` is ``None`` on a clean
    negotiation and otherwise a human-readable reason the caller should log;
    it never means "abort". Every ambiguous case resolves to
    :data:`OLDEST_SUPPORTED_VERSION`, because assuming the *oldest* feature
    set can only cost us richness, whereas assuming a newer one would have us
    send payloads the peer never agreed to.
    """
    if not isinstance(reply, str) or not reply:
        return (
            OLDEST_SUPPORTED_VERSION,
            f"server omitted protocolVersion; assuming {OLDEST_SUPPORTED_VERSION}",
        )
    if reply in HANDSHAKE_PROTOCOL_VERSIONS:
        return reply, None
    if reply in MODERN_PROTOCOL_VERSIONS:
        # The reference server never answers `initialize` with a modern
        # revision, so this means the peer put us in an envelope dialect we
        # cannot encode. Stay connected at the floor rather than dropping a
        # server that may still answer plain tools/call.
        return (
            OLDEST_SUPPORTED_VERSION,
            (
                f"server answered initialize with {reply!r}, which uses the "
                "stateless per-request envelope this client does not speak; "
                f"assuming {OLDEST_SUPPORTED_VERSION}"
            ),
        )
    return (
        OLDEST_SUPPORTED_VERSION,
        (
            f"server replied with unknown protocolVersion {reply!r}; "
            f"assuming {OLDEST_SUPPORTED_VERSION}"
        ),
    )
