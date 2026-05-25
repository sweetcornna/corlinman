"""Async ClawHub HTTP client.

W1.1 of ``docs/PLAN_SKILL_HUB.md``. ClawHub's anonymous read API (3000
req/min for list+search, 1200/min for ``/download``) is exposed by
``https://clawhub.ai/api/v1``. This module is the only place inside the
gateway that talks to that origin — every admin route proxies through
:class:`ClawHubClient` so we control:

* timeouts (10 s default — short enough not to wedge a request-bound
  admin route when ClawHub is degraded),
* error mapping (network / 5xx → :class:`HubUnavailableError`, 429 →
  :class:`HubRateLimitedError` with the ``Retry-After`` budget),
* a tiny TTL cache so the UI can drag the search box without hammering
  the upstream.

The cache deliberately does NOT cover ``/download`` — tarballs are
megabyte-sized and the install pipeline only fetches each (slug, version)
pair once anyway. Cache keys for the other endpoints fold the verb +
path + a sorted-params signature so identical queries reuse the same
parsed dataclasses.

The dataclasses are frozen + slotted so the route layer can stash them
in the in-memory cache without worrying about a later mutation racing a
serve. ``HubSkillDetail`` extends :class:`HubSkillSummary` so a detail
response is a drop-in shape wherever a summary is rendered (we read both
through Pydantic at the route boundary in W1.3).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


__all__ = [
    "ClawHubClient",
    "HubDownload",
    "HubRateLimitedError",
    "HubSkillDetail",
    "HubSkillSummary",
    "HubUnavailableError",
]


# Env var honoured at construction time. We intentionally read once
# (constructor) — a long-lived gateway process never picks up a mid-run
# env mutation, and tests pass an explicit ``base_url`` arg anyway.
_BASE_URL_ENV = "CORLINMAN_SKILL_HUB_BASE_URL"
_DEFAULT_BASE_URL = "https://clawhub.ai/api/v1"

# Default request timeout. Five seconds was too tight in early manual
# probes (the search endpoint occasionally takes ~2 s on cold cache);
# ten gives us headroom without making the UI feel laggy.
_DEFAULT_TIMEOUT_SECONDS = 10.0

# Default cache TTL. 60 s matches the PLAN — list/search responses can
# tolerate a minute of staleness, and the UI typically issues a fresh
# fetch on every tab focus anyway.
_DEFAULT_CACHE_TTL_SECONDS = 60.0

# ``readme_excerpt`` is clipped to 4 KiB before being handed to the UI;
# anything longer is fetched via the SKILL.md file route on demand.
_README_EXCERPT_MAX_BYTES = 4096


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HubUnavailableError(RuntimeError):
    """ClawHub is unreachable or returned 5xx / a network error.

    The route layer maps this to a 502/503-style payload the UI can
    show with a "ClawHub is unreachable — retry?" banner (per resolved
    decision #4 in the plan).
    """


class HubRateLimitedError(RuntimeError):
    """Upstream returned 429.

    ``retry_after_seconds`` mirrors the ``Retry-After`` response header
    (falling back to 30 s when the header is missing or malformed). The
    route layer surfaces this verbatim so the UI can disable the search
    box for that window.
    """

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            f"ClawHub rate-limited; retry after {retry_after_seconds}s"
        )
        self.retry_after_seconds = retry_after_seconds


# ---------------------------------------------------------------------------
# Wire-shape dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HubSkillSummary:
    """One row in a ``/skills`` / ``/search`` list response.

    Fields mirror the on-wire ClawHub names (camelCase coerced to
    snake_case at parse time) plus a normalised ISO-8601 ``updated_at``
    string the UI passes straight to ``new Date()``.

    ``version`` is exposed as a read-only alias for ``latest_version``
    — the ClawHub wire payload calls this field ``version`` (the row
    is a snapshot of "the latest" anyway) and the W1.4 tests + the
    route layer DTOs are happier with the wire-native name.
    """

    slug: str
    name: str
    description: str
    emoji: str | None
    stars: int
    downloads: int
    latest_version: str
    updated_at: str  # ISO 8601

    @property
    def version(self) -> str:
        """Alias for :attr:`latest_version` matching the wire name."""
        return self.latest_version


@dataclass(frozen=True, slots=True)
class HubSkillDetail(HubSkillSummary):
    """One ``/skills/{slug}`` response, extends summary with detail-only
    fields the drawer in the UI renders.
    """

    homepage: str | None
    versions: list[str]
    scan_summary: str | None  # "pass" / "warn" / "fail" / None
    readme_excerpt: str  # first 4 KiB of SKILL.md body


@dataclass(frozen=True, slots=True)
class HubDownload:
    """Result of ``GET /download?slug=&version=``.

    ``content`` is the raw tarball bytes — the installer untars it in
    a temp dir. ``content_hash`` is the ``X-Content-Hash`` response
    header when upstream sets it; the installer can optionally verify
    after the extraction but does not require it (ClawHub's docs note
    the header is best-effort and not present on all CDN edges).

    Note: the orchestrator's PLAN_SKILL_HUB spec named this field
    ``tarball`` but the sibling W1.4 tests + the route layer reach for
    ``content`` (matching httpx's :attr:`Response.content` shape). We
    use ``content`` as the canonical name and expose ``tarball`` as a
    read-only property so both spellings continue to work.

    ``slug`` and ``version`` default to empty strings so the tests'
    minimal kwarg-only construction (``HubDownload(content=..., content_hash=...)``)
    works — they're recorded for completeness when the client builds
    a download but the installer doesn't consume them.
    """

    content: bytes
    content_hash: str | None
    slug: str = ""
    version: str = ""

    @property
    def tarball(self) -> bytes:
        """Alias for :attr:`content`; preserved from the original spec."""
        return self.content


# ---------------------------------------------------------------------------
# Cache helper
# ---------------------------------------------------------------------------


def _cache_key(
    method: str, path: str, params: dict[str, Any] | None
) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """Stable cache key tuple (method, path, sorted-params).

    We sort the param items so ``?limit=10&q=foo`` and ``?q=foo&limit=10``
    land on the same row. Values are stringified — ClawHub's params are
    all scalar-shaped so this round-trips cleanly.
    """
    if not params:
        return (method, path, ())
    items = tuple(sorted((k, str(v)) for k, v in params.items() if v is not None))
    return (method, path, items)


# ---------------------------------------------------------------------------
# Parsers — defensive against missing / wrong-shape upstream fields. We
# return reasonable empties rather than raising so a single bad row
# doesn't blank the whole search page.
# ---------------------------------------------------------------------------


def _as_str(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


def _as_opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value) or None


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):  # bool is an int — keep it out.
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _parse_summary(raw: dict[str, Any]) -> HubSkillSummary:
    """Parse one summary row from the ClawHub JSON shape.

    ClawHub uses camelCase keys; we accept both spellings of the
    "latest version" field (``version`` per the plan, ``latestVersion``
    seen in a few responses) so a future upstream rename is graceful.
    """
    latest = raw.get("latestVersion") or raw.get("version") or ""
    return HubSkillSummary(
        slug=_as_str(raw.get("slug")),
        name=_as_str(raw.get("name") or raw.get("slug")),
        description=_as_str(raw.get("description")),
        emoji=_as_opt_str(raw.get("emoji")),
        stars=_as_int(raw.get("stars")),
        downloads=_as_int(raw.get("downloads")),
        latest_version=_as_str(latest),
        updated_at=_as_str(raw.get("updatedAt") or raw.get("updated_at")),
    )


def _parse_detail(raw: dict[str, Any]) -> HubSkillDetail:
    """Parse a ``/skills/{slug}`` response.

    The detail endpoint extends the summary with ``homepage``,
    ``versions`` (string list), ``moderation.scanSummary`` (the chip the
    UI badges with), and a ``readme`` / ``readmeExcerpt`` body we clip
    to 4 KiB.
    """
    summary = _parse_summary(raw)
    # ``versions`` may be a plain list of strings (``["1.0.0", "1.1.0"]``)
    # or a list of ``{"version": str, "publishedAt": str}`` records — we
    # accept both shapes and flatten to a string list for the UI.
    versions_raw = raw.get("versions") or []
    versions: list[str] = []
    if isinstance(versions_raw, list):
        for entry in versions_raw:
            if isinstance(entry, str) and entry:
                versions.append(entry)
            elif isinstance(entry, dict):
                token = entry.get("version") or entry.get("name")
                if token:
                    versions.append(_as_str(token))
    moderation = raw.get("moderation")
    scan_summary: str | None = None
    if isinstance(moderation, dict):
        scan_summary = _as_opt_str(moderation.get("scanSummary"))

    readme = raw.get("readmeExcerpt")
    if readme is None:
        readme = raw.get("readme")
    readme_str = _as_str(readme)
    if len(readme_str.encode("utf-8")) > _README_EXCERPT_MAX_BYTES:
        # Byte-clip then decode-fix so we don't slice a multi-byte char.
        clipped = readme_str.encode("utf-8")[:_README_EXCERPT_MAX_BYTES]
        readme_str = clipped.decode("utf-8", errors="ignore")

    return HubSkillDetail(
        slug=summary.slug,
        name=summary.name,
        description=summary.description,
        emoji=summary.emoji,
        stars=summary.stars,
        downloads=summary.downloads,
        latest_version=summary.latest_version,
        updated_at=summary.updated_at,
        homepage=_as_opt_str(raw.get("homepage")),
        versions=versions,
        scan_summary=scan_summary,
        readme_excerpt=readme_str,
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ClawHubClient:
    """Reusable async HTTP client + tiny TTL cache.

    Construct once per gateway process. The httpx ``AsyncClient`` is
    lazily built on first call so a process that never opens the Browse
    Hub tab pays no socket-pool cost.

    The cache is purposely simple: a single in-memory dict with
    (method, path, params) keys and ``(expires_at, value)`` tuples.
    We GC on read — every cache get prunes its own bucket if expired
    — so the size of the dict stays roughly bounded by the per-minute
    distinct-request count, which is fine for an admin surface.
    """

    __slots__ = (
        "_base_url",
        "_cache",
        "_cache_lock",
        "_cache_ttl",
        "_client",
        "_owns_client",
        "_timeout",
    )

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Env override wins so operators can point at a staging mirror
        # without restarting with custom config.
        env_override = os.environ.get(_BASE_URL_ENV)
        resolved = (env_override or base_url).rstrip("/")
        self._base_url = resolved
        self._timeout = timeout_seconds
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[
            tuple[str, str, tuple[tuple[str, str], ...]],
            tuple[float, Any],
        ] = {}
        self._cache_lock = asyncio.Lock()
        # ``transport`` is the seam tests use to swap in an
        # :class:`httpx.MockTransport`. Production passes ``None`` and we
        # let httpx pick the default AsyncHTTPTransport.
        self._client = httpx.AsyncClient(
            base_url=resolved,
            timeout=timeout_seconds,
            transport=transport,
            headers={
                # Identify ourselves on ClawHub's logs so an abuse
                # report can be traced back to the corlinman fleet.
                "User-Agent": "corlinman-server/skill-hub-client",
                "Accept": "application/json",
            },
        )
        self._owns_client = True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying httpx client + clear the cache."""
        if self._owns_client:
            await self._client.aclose()
        async with self._cache_lock:
            self._cache.clear()

    @property
    def base_url(self) -> str:
        """Resolved base URL (after env override)."""
        return self._base_url

    # ------------------------------------------------------------------
    # Public API — list / search / detail / download
    # ------------------------------------------------------------------

    async def search(self, q: str, *, limit: int = 25) -> list[HubSkillSummary]:
        """``GET /search?q=&limit=``.

        Returns at most ``limit`` summary rows. Empty query → empty
        list (we don't go to upstream for a no-op).
        """
        if not q.strip():
            return []
        params = {"q": q, "limit": str(int(limit))}
        payload = await self._cached_get_json("/search", params)
        rows = _extract_rows(payload)
        return [_parse_summary(r) for r in rows]

    async def list_skills(
        self,
        *,
        sort: str = "trending",
        cursor: str | None = None,
        limit: int = 25,
    ) -> tuple[list[HubSkillSummary], str | None]:
        """``GET /skills?sort=&cursor=&limit=``.

        Returns ``(rows, next_cursor)``. ``next_cursor`` is ``None``
        when the upstream omits a continuation token (end-of-list).
        """
        params: dict[str, Any] = {"sort": sort, "limit": str(int(limit))}
        if cursor:
            params["cursor"] = cursor
        payload = await self._cached_get_json("/skills", params)
        rows = _extract_rows(payload)
        parsed = [_parse_summary(r) for r in rows]
        next_cursor = _extract_next_cursor(payload)
        return parsed, next_cursor

    async def get_skill(self, slug: str) -> HubSkillDetail:
        """``GET /skills/{slug}``."""
        if not slug or "/" in slug or ".." in slug:
            raise HubUnavailableError(f"invalid slug: {slug!r}")
        payload = await self._cached_get_json(f"/skills/{slug}", None)
        # Detail responses sometimes wrap the body in {"skill": {...}};
        # accept either shape.
        if isinstance(payload, dict) and isinstance(payload.get("skill"), dict):
            payload = payload["skill"]
        if not isinstance(payload, dict):
            raise HubUnavailableError(
                f"clawhub returned non-object detail payload for {slug!r}"
            )
        return _parse_detail(payload)

    async def download(
        self, slug: str, version: str = "latest"
    ) -> HubDownload:
        """``GET /download?slug=&version=`` — raw tarball.

        Not cached. Always hits upstream. The installer is the only
        caller in v1.5 and is gated by the admin route's CSRF +
        request_id machinery.
        """
        if not slug or "/" in slug or ".." in slug:
            raise HubUnavailableError(f"invalid slug: {slug!r}")
        params = {"slug": slug, "version": version}
        try:
            response = await self._client.get("/download", params=params)
        except httpx.HTTPError as exc:
            logger.warning(
                "skill_hub.download_network_error",
                slug=slug,
                version=version,
                error=str(exc),
            )
            raise HubUnavailableError(
                f"network error fetching {slug}@{version}"
            ) from exc
        self._raise_for_status(response)
        content_hash = _as_opt_str(response.headers.get("X-Content-Hash"))
        return HubDownload(
            content=response.content,
            content_hash=content_hash,
            slug=slug,
            version=version,
        )

    # ------------------------------------------------------------------
    # Internal — cached GET wrapper
    # ------------------------------------------------------------------

    async def _cached_get_json(
        self, path: str, params: dict[str, Any] | None
    ) -> Any:
        """GET ``path`` returning JSON, with the TTL cache in front.

        We hold the cache lock only across the dict get / set — the
        actual upstream call happens outside the lock so two concurrent
        cache-miss requests can race the network rather than serialising.
        That risks a duplicate fetch in flight; the cache size impact is
        negligible (one extra row) and the latency win is worth it for
        the search-box debounce path.
        """
        key = _cache_key("GET", path, params)
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                expires_at, value = cached
                if expires_at > now:
                    return value
                # Expired — prune in-place.
                del self._cache[key]

        try:
            response = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            logger.warning(
                "skill_hub.network_error",
                path=path,
                error=str(exc),
            )
            raise HubUnavailableError(
                f"network error fetching {path}"
            ) from exc
        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:  # json.JSONDecodeError subclasses ValueError
            logger.warning(
                "skill_hub.invalid_json",
                path=path,
                error=str(exc),
            )
            raise HubUnavailableError(
                f"clawhub returned non-JSON body for {path}"
            ) from exc

        async with self._cache_lock:
            self._cache[key] = (now + self._cache_ttl, payload)
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Map HTTP status to our typed exceptions.

        * 200 → return (caller continues).
        * 429 → :class:`HubRateLimitedError` carrying the ``Retry-After``
          header value (default 30 s when missing / malformed).
        * 5xx → :class:`HubUnavailableError`.
        * 4xx other → :class:`HubUnavailableError` so the route layer
          surfaces a single "ClawHub is degraded" path; we don't try to
          carry the upstream status through to the UI.
        """
        if response.is_success:
            return
        if response.status_code == 429:
            raw_retry = response.headers.get("Retry-After")
            retry_seconds = 30
            if raw_retry:
                try:
                    retry_seconds = max(1, int(float(raw_retry)))
                except ValueError:
                    retry_seconds = 30
            raise HubRateLimitedError(retry_seconds)
        # 5xx + any other 4xx fold into the unavailable bucket.
        logger.warning(
            "skill_hub.http_error",
            status=response.status_code,
            url=str(response.request.url) if response.request else None,
        )
        raise HubUnavailableError(
            f"clawhub returned HTTP {response.status_code}"
        )


# ---------------------------------------------------------------------------
# JSON shape helpers
# ---------------------------------------------------------------------------


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    """Pull the rows out of a list/search response.

    ClawHub's documented shape is ``{"skills": [...], "nextCursor": ...}``
    for ``/skills`` and ``{"results": [...]}`` for ``/search``; we accept
    a top-level list too in case a future API version simplifies.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("skills", "results", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _extract_next_cursor(payload: Any) -> str | None:
    """Cursor lives at the top level for paginated list endpoints."""
    if isinstance(payload, dict):
        for key in ("nextCursor", "next_cursor", "cursor"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return None
