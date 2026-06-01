"""``corlinman_server.system.marketplace.source`` — source abstraction.

The marketplace serves three *kinds* of installable extension — skills,
MCP servers, and plugins — from one or more *sources*. A source is any
backend that can list / search / detail / download items. The default
source is :class:`~corlinman_server.system.marketplace.github_source.GitHubSource`
(a single curated GitHub registry repo); the legacy clawhub.ai skill hub
is retained behind
:class:`~corlinman_server.system.marketplace.clawhub_source.ClawHubSource`.

This module owns the *vocabulary* every source speaks:

* :class:`MarketplaceItem` — one catalog row (used for both list rows and
  the fully-populated detail view; detail-only fields default to empty so
  a single shape works everywhere, sidestepping the frozen-dataclass
  "non-default follows default" inheritance trap).
* :class:`MarketplaceDownload` — the bytes a source hands back for an
  install (a ``tarball`` for skills/plugins, a ``manifest`` JSON blob for
  MCP servers).
* :class:`MarketplaceSource` — the ``Protocol`` both backends satisfy.
* The typed error family (``MarketplaceUnavailableError`` /
  ``MarketplaceRateLimitedError`` / ``MarketplaceIntegrityError``) mirrors
  the clawhub client's so the route layer keeps a single offline-collapse
  path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "MarketplaceDownload",
    "MarketplaceError",
    "MarketplaceIntegrityError",
    "MarketplaceItem",
    "MarketplaceKind",
    "MarketplaceRateLimitedError",
    "MarketplaceSource",
    "MarketplaceUnavailableError",
]


#: The three installable extension kinds. Stored on every item + passed
#: to every source method so one source can serve all three (GitHub) or
#: just one (clawhub serves ``"skill"`` only).
MarketplaceKind = Literal["skill", "mcp", "plugin"]


# ---------------------------------------------------------------------------
# Errors — mirror skill_hub.client so the route layer maps a single family.
# ---------------------------------------------------------------------------


class MarketplaceError(RuntimeError):
    """Base class for every marketplace-source failure."""


class MarketplaceUnavailableError(MarketplaceError):
    """The source is unreachable / returned 5xx / a network error.

    The route layer maps this to an "offline — retry?" envelope, exactly
    as it already does for the clawhub :class:`HubUnavailableError`.
    """


class MarketplaceRateLimitedError(MarketplaceError):
    """Upstream returned 429.

    ``retry_after_seconds`` mirrors the ``Retry-After`` header (default
    30 s when missing / malformed).
    """

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            f"marketplace source rate-limited; retry after {retry_after_seconds}s"
        )
        self.retry_after_seconds = retry_after_seconds


class MarketplaceIntegrityError(MarketplaceError):
    """A downloaded blob failed its declared ``sha256`` check.

    Always fatal: an integrity mismatch on a GitHub/mirror download path
    is treated as hostile (a tampered mirror) and the install is refused.
    """


# ---------------------------------------------------------------------------
# Wire-shape dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketplaceItem:
    """One catalog row.

    Used for *both* the list/search rows and the detail view. The first
    block of fields is always present; the rest default to empty so a
    list row (no readme / content pointers) and a detail row (fully
    populated) share one frozen shape. ``content_ref`` + ``sha256`` are
    source-internal install pointers (a repo-relative tarball/manifest
    path for GitHub, the clawhub slug otherwise) — the route layer never
    surfaces them to the browser.
    """

    # --- core (always present) ---
    kind: MarketplaceKind
    slug: str
    name: str
    description: str
    latest_version: str

    # --- optional metadata (list + detail) ---
    emoji: str | None = None
    versions: tuple[str, ...] = ()
    stars: int = 0
    downloads: int = 0
    updated_at: str = ""  # ISO-8601
    homepage: str | None = None
    tags: tuple[str, ...] = ()

    # --- kind-specific ---
    transport: str | None = None  # mcp: "stdio" | "ws" | "http"
    requires_env: tuple[str, ...] = ()  # mcp/plugin: env/secret names to prompt

    # --- detail-only ---
    readme_excerpt: str = ""
    scan_summary: str | None = None  # "pass" | "warn" | "fail" | None

    # --- source-internal install pointers (not surfaced to the UI) ---
    content_ref: str | None = None
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class MarketplaceDownload:
    """The bytes a source hands back for one install.

    ``media`` distinguishes the two install shapes: a ``"tarball"`` (gzip
    bundle for skills/plugins, fed to the hardened extractor) versus a
    ``"manifest"`` (a JSON spec for an MCP server, validated + persisted
    rather than extracted). ``sha256`` is the *verified* hash when the
    source enforced one (GitHub items must declare one; clawhub's is
    best-effort).
    """

    kind: MarketplaceKind
    slug: str
    version: str
    content: bytes
    media: Literal["tarball", "manifest"] = "tarball"
    sha256: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MarketplaceSource(Protocol):
    """Backend contract for a marketplace catalog + download origin.

    Every method takes the :data:`MarketplaceKind` so one source can
    serve all three kinds. A source that only serves a subset (clawhub →
    skills) returns an empty list for the others and raises
    :class:`MarketplaceError` on a download of an unsupported kind.
    """

    @property
    def name(self) -> str:
        """Short stable identifier (``"github"`` / ``"clawhub"``)."""
        ...

    async def list_items(
        self,
        kind: MarketplaceKind,
        *,
        sort: str = "trending",
        cursor: str | None = None,
        limit: int = 25,
    ) -> tuple[list[MarketplaceItem], str | None]:
        """Return ``(rows, next_cursor)`` for ``kind``."""
        ...

    async def search(
        self, kind: MarketplaceKind, q: str, *, limit: int = 25
    ) -> list[MarketplaceItem]:
        """Substring search over ``kind`` rows."""
        ...

    async def detail(self, kind: MarketplaceKind, slug: str) -> MarketplaceItem:
        """Fully-populated item for ``slug`` (readme, versions, pointers)."""
        ...

    async def download(
        self, kind: MarketplaceKind, slug: str, version: str = "latest"
    ) -> MarketplaceDownload:
        """Fetch + integrity-check the install payload for ``slug@version``."""
        ...

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        ...
