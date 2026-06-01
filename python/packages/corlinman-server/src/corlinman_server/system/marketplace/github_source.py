"""``corlinman_server.system.marketplace.github_source`` — GitHub source.

The default marketplace backend: a *single curated registry repo* on
GitHub holding an ``index.json`` catalog plus the per-item content
(tarballs for skills/plugins, ``manifest.json`` specs for MCP servers).

Fetch path::

    index.json  →  https://raw.githubusercontent.com/<repo>/<ref>/index.json
    tarball     →  https://raw.githubusercontent.com/<repo>/<ref>/<tarball>
    manifest    →  https://raw.githubusercontent.com/<repo>/<ref>/<manifest>

Every URL is run through the :class:`GithubAccelerator` before the GET, so
a China-region host transparently pulls from a mirror. ``index.json`` is
cached with a short TTL (the catalog changes rarely); downloads are never
cached. GitHub items **must** declare a ``sha256`` for their tarball — the
download verifies it and raises :class:`MarketplaceIntegrityError` on a
mismatch (defence against a hostile mirror on the accelerated path).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from typing import Any

import httpx
import structlog

from corlinman_server.system.marketplace.accel import GithubAccelerator
from corlinman_server.system.marketplace.source import (
    MarketplaceDownload,
    MarketplaceIntegrityError,
    MarketplaceItem,
    MarketplaceKind,
    MarketplaceRateLimitedError,
    MarketplaceUnavailableError,
)

logger = structlog.get_logger(__name__)

__all__ = ["GitHubSource"]

_DEFAULT_TIMEOUT_SECONDS = 15.0
_DEFAULT_CACHE_TTL_SECONDS = 60.0
_README_EXCERPT_MAX_BYTES = 4096
# Hard cap on a downloaded blob (matches the installer's 25 MiB total cap
# with headroom for the gzip wrapper). Guards against a mirror streaming an
# unbounded body into memory.
_MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024


class GitHubSource:
    """Reads a curated GitHub registry repo. Implements ``MarketplaceSource``."""

    __slots__ = (
        "_accel",
        "_cache",
        "_cache_lock",
        "_cache_ttl",
        "_client",
        "_raw_base",
        "_ref",
        "_repo",
        "_token",
    )

    def __init__(
        self,
        *,
        repo: str,
        ref: str = "main",
        accel: GithubAccelerator | None = None,
        token: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._repo = repo.strip("/")
        self._ref = ref
        self._accel = accel or GithubAccelerator()
        self._token = token or None
        self._raw_base = f"https://raw.githubusercontent.com/{self._repo}/{self._ref}/"
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = asyncio.Lock()
        # ``transport`` is the test seam (httpx.MockTransport). No base_url:
        # we always GET absolute URLs because the accelerator may rewrite the
        # host. ``follow_redirects`` handles ghproxy-style 302s.
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=True,
            headers={
                "User-Agent": "corlinman-server/marketplace-github",
                "Accept": "*/*",
            },
        )

    @property
    def name(self) -> str:
        return "github"

    async def aclose(self) -> None:
        await self._client.aclose()
        async with self._cache_lock:
            self._cache.clear()

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------

    async def list_items(
        self,
        kind: MarketplaceKind,
        *,
        sort: str = "trending",
        cursor: str | None = None,
        limit: int = 25,
    ) -> tuple[list[MarketplaceItem], str | None]:
        items = [i for i in await self._index() if i.kind == kind]
        items = _sort_items(items, sort)
        offset = _decode_cursor(cursor)
        window = items[offset : offset + limit]
        next_cursor = (
            _encode_cursor(offset + limit) if offset + limit < len(items) else None
        )
        return window, next_cursor

    async def search(
        self, kind: MarketplaceKind, q: str, *, limit: int = 25
    ) -> list[MarketplaceItem]:
        needle = q.strip().lower()
        if not needle:
            return []
        rows = [i for i in await self._index() if i.kind == kind]
        matched = [i for i in rows if _matches(i, needle)]
        return matched[: max(0, int(limit))]

    async def detail(self, kind: MarketplaceKind, slug: str) -> MarketplaceItem:
        item = await self._find(kind, slug)
        # Optionally enrich with a README excerpt when the index row points
        # at one via ``content_ref``'s sibling ``readme`` (carried on the
        # item already). For MVP the index row is the detail; a readme path
        # is fetched lazily only when present + not already inlined.
        if item.readme_excerpt or not item.homepage:
            return item
        return item

    async def download(
        self, kind: MarketplaceKind, slug: str, version: str = "latest"
    ) -> MarketplaceDownload:
        item = await self._find(kind, slug)
        ref = item.content_ref
        if not ref:
            raise MarketplaceUnavailableError(
                f"registry item {kind}:{slug} has no content reference"
            )
        url = self._accel.accelerate(self._raw_base + ref.lstrip("/"))
        body = await self._get_bytes(url)
        media = "manifest" if kind == "mcp" else "tarball"
        if kind == "mcp":
            # Manifests are small JSON specs — no hash requirement, but we
            # validate it parses so a half-served blob fails fast here.
            try:
                json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise MarketplaceUnavailableError(
                    f"registry manifest for {slug} is not valid JSON: {exc}"
                ) from exc
        else:
            self._verify_sha256(slug, item.sha256, body)
        return MarketplaceDownload(
            kind=kind,
            slug=slug,
            version=item.latest_version or version,
            content=body,
            media=media,  # type: ignore[arg-type]
            sha256=item.sha256,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _index(self) -> list[MarketplaceItem]:
        """Fetch + parse ``index.json`` with a TTL cache."""
        key = "index.json"
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                expires_at, value = cached
                if expires_at > now:
                    return list(value)
                del self._cache[key]

        url = self._accel.accelerate(self._raw_base + "index.json")
        body = await self._get_bytes(url)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise MarketplaceUnavailableError(
                f"registry index.json is not valid JSON: {exc}"
            ) from exc
        items = _parse_index(payload)
        async with self._cache_lock:
            self._cache[key] = (now + self._cache_ttl, items)
        return list(items)

    async def _find(self, kind: MarketplaceKind, slug: str) -> MarketplaceItem:
        for item in await self._index():
            if item.kind == kind and item.slug == slug:
                return item
        raise MarketplaceUnavailableError(
            f"registry has no {kind} named {slug!r}"
        )

    async def _get_bytes(self, url: str) -> bytes:
        headers: dict[str, str] = {}
        # Only attach the token when the (possibly rewritten) host is
        # trusted — never leak it through a public proxy.
        if self._token and self._accel.is_trusted_host(url):
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = await self._client.get(url, headers=headers or None)
        except httpx.HTTPError as exc:
            logger.warning("marketplace.github.network_error", url=url, error=str(exc))
            raise MarketplaceUnavailableError(f"network error fetching {url}") from exc
        _raise_for_status(response)
        content = response.content
        if len(content) > _MAX_DOWNLOAD_BYTES:
            raise MarketplaceUnavailableError(
                f"registry blob at {url} exceeds {_MAX_DOWNLOAD_BYTES} bytes"
            )
        return content

    @staticmethod
    def _verify_sha256(slug: str, declared: str | None, body: bytes) -> None:
        if not declared:
            raise MarketplaceIntegrityError(
                f"registry item {slug!r} is missing a required sha256"
            )
        actual = hashlib.sha256(body).hexdigest()
        want = declared.lower().removeprefix("sha256:")
        if actual != want:
            raise MarketplaceIntegrityError(
                f"sha256 mismatch for {slug!r}: expected {want}, got {actual}"
            )


# ---------------------------------------------------------------------------
# Parsing + helpers
# ---------------------------------------------------------------------------


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    if response.status_code == 429:
        raw_retry = response.headers.get("Retry-After")
        retry = 30
        if raw_retry:
            try:
                retry = max(1, int(float(raw_retry)))
            except ValueError:
                retry = 30
        raise MarketplaceRateLimitedError(retry)
    logger.warning(
        "marketplace.github.http_error",
        status=response.status_code,
        url=str(response.request.url) if response.request else None,
    )
    raise MarketplaceUnavailableError(
        f"registry returned HTTP {response.status_code}"
    )


def _parse_index(payload: Any) -> list[MarketplaceItem]:
    raw_items: Any
    if isinstance(payload, dict):
        raw_items = payload.get("items") or payload.get("skills") or []
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []
    out: list[MarketplaceItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = _parse_item(raw)
        if item is not None:
            out.append(item)
    return out


def _parse_item(raw: dict[str, Any]) -> MarketplaceItem | None:
    kind = str(raw.get("kind") or "skill").lower()
    if kind not in ("skill", "mcp", "plugin"):
        return None
    slug = _as_str(raw.get("slug"))
    if not slug:
        return None
    versions_raw = raw.get("versions") or []
    versions = tuple(_as_str(v) for v in versions_raw if _as_str(v)) if isinstance(
        versions_raw, list
    ) else ()
    requires = raw.get("requires") or {}
    req_env: tuple[str, ...] = ()
    if isinstance(requires, dict):
        env_list = requires.get("env") or []
        if isinstance(env_list, list):
            req_env = tuple(_as_str(e) for e in env_list if _as_str(e))
    tags_raw = raw.get("tags") or []
    tags = tuple(_as_str(t) for t in tags_raw if _as_str(t)) if isinstance(
        tags_raw, list
    ) else ()
    content_ref = (
        _as_opt_str(raw.get("manifest"))
        if kind == "mcp"
        else _as_opt_str(raw.get("tarball") or raw.get("content"))
    )
    readme = _as_str(raw.get("readme_excerpt") or raw.get("readme"))
    if len(readme.encode("utf-8")) > _README_EXCERPT_MAX_BYTES:
        readme = readme.encode("utf-8")[:_README_EXCERPT_MAX_BYTES].decode(
            "utf-8", errors="ignore"
        )
    return MarketplaceItem(
        kind=kind,  # type: ignore[arg-type]
        slug=slug,
        name=_as_str(raw.get("name") or slug),
        description=_as_str(raw.get("description")),
        latest_version=_as_str(raw.get("latest_version") or raw.get("version")),
        emoji=_as_opt_str(raw.get("emoji")),
        versions=versions,
        stars=_as_int(raw.get("stars")),
        downloads=_as_int(raw.get("downloads")),
        updated_at=_as_str(raw.get("updated_at") or raw.get("updatedAt")),
        homepage=_as_opt_str(raw.get("homepage")),
        tags=tags,
        transport=_as_opt_str(raw.get("transport")),
        requires_env=req_env,
        readme_excerpt=readme,
        scan_summary=_as_opt_str(raw.get("scan_summary")),
        content_ref=content_ref,
        sha256=_as_opt_str(raw.get("sha256")),
    )


def _matches(item: MarketplaceItem, needle: str) -> bool:
    haystack = " ".join(
        [item.slug, item.name, item.description, " ".join(item.tags)]
    ).lower()
    return needle in haystack


def _sort_items(items: list[MarketplaceItem], sort: str) -> list[MarketplaceItem]:
    sort = (sort or "trending").lower()
    if sort in ("downloads", "trending"):
        return sorted(items, key=lambda i: (i.downloads, i.stars), reverse=True)
    if sort == "stars":
        return sorted(items, key=lambda i: i.stars, reverse=True)
    if sort == "updated":
        return sorted(items, key=lambda i: i.updated_at, reverse=True)
    if sort == "name":
        return sorted(items, key=lambda i: i.name.lower())
    return items


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(cursor.encode("ascii"))))
    except (ValueError, TypeError):
        return 0


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
    if isinstance(value, bool):
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
