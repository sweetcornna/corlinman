"""Tests for :class:`corlinman_server.system.marketplace.clawhub_source.ClawHubSource`.

A :class:`ClawHubClient` is built with an :class:`httpx.MockTransport`
serving the clawhub.ai read API (``/skills``, ``/search``,
``/skills/{slug}``, ``/download``) and wrapped in
:class:`ClawHubSource`. We assert the summary/detail/download mapping to
the marketplace vocabulary and the **skills-only** contract: a non-skill
``kind`` returns ``[]`` for list/search and raises
:class:`MarketplaceError` for detail/download.
"""

from __future__ import annotations

import httpx
import pytest
from corlinman_server.system.marketplace.clawhub_source import ClawHubSource
from corlinman_server.system.marketplace.source import (
    MarketplaceError,
    MarketplaceItem,
)
from corlinman_server.system.skill_hub.client import ClawHubClient

DOWNLOAD_BYTES = b"\x1f\x8b\x08\x00clawhub-tarball"


def _summary(slug: str = "web-search") -> dict:
    return {
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "description": "Search the live web.",
        "emoji": "🔎",
        "version": "1.0.0",
        "stars": 42,
        "downloads": 1024,
        "updatedAt": "2026-05-20T12:00:00Z",
    }


def _detail(slug: str = "web-search") -> dict:
    payload = _summary(slug)
    payload.update(
        {
            "homepage": "https://example.com",
            "readme": "# web-search\n\nDoes the thing.",
            "moderation": {"scanSummary": "no_findings"},
            "versions": ["1.0.0", "0.9.0"],
        }
    )
    return payload


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    # base_url is .../api/v1, so paths arrive as /api/v1/skills etc.
    if path.endswith("/download"):
        return httpx.Response(
            200,
            content=DOWNLOAD_BYTES,
            headers={"X-Content-Hash": "sha256:abc123"},
        )
    if path.endswith("/search"):
        return httpx.Response(200, json={"results": [_summary("web-search")]})
    if "/skills/" in path:
        slug = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=_detail(slug))
    if path.endswith("/skills"):
        return httpx.Response(
            200,
            json={
                "skills": [_summary("web-search"), _summary("web-fetch")],
                "nextCursor": "cursor-xyz",
            },
        )
    return httpx.Response(404, json={"error": "not found"})


def _source() -> ClawHubSource:
    client = ClawHubClient(transport=httpx.MockTransport(_handler))
    return ClawHubSource(client)


# ---------------------------------------------------------------------------
# Mapping to MarketplaceItem
# ---------------------------------------------------------------------------


async def test_name_is_clawhub() -> None:
    src = _source()
    try:
        assert src.name == "clawhub"
    finally:
        await src.aclose()


async def test_list_items_maps_summaries() -> None:
    src = _source()
    try:
        rows, cursor = await src.list_items("skill")
    finally:
        await src.aclose()

    assert all(isinstance(r, MarketplaceItem) for r in rows)
    assert [r.slug for r in rows] == ["web-search", "web-fetch"]
    first = rows[0]
    assert first.kind == "skill"
    assert first.name == "Web Search"
    assert first.stars == 42
    assert first.downloads == 1024
    # clawhub downloads by slug → content_ref carries it.
    assert first.content_ref == "web-search"
    assert cursor == "cursor-xyz"


async def test_search_maps_summaries() -> None:
    src = _source()
    try:
        rows = await src.search("skill", "web")
    finally:
        await src.aclose()

    assert [r.slug for r in rows] == ["web-search"]
    assert rows[0].kind == "skill"


async def test_detail_maps_detail_fields() -> None:
    src = _source()
    try:
        item = await src.detail("skill", "web-search")
    finally:
        await src.aclose()

    assert item.kind == "skill"
    assert item.slug == "web-search"
    assert item.homepage == "https://example.com"
    assert item.scan_summary == "no_findings"
    assert item.versions == ("1.0.0", "0.9.0")
    assert item.readme_excerpt.startswith("# web-search")
    assert item.content_ref == "web-search"


async def test_download_maps_content_and_hash() -> None:
    src = _source()
    try:
        dl = await src.download("skill", "web-search")
    finally:
        await src.aclose()

    assert dl.kind == "skill"
    assert dl.media == "tarball"
    assert dl.content == DOWNLOAD_BYTES
    assert dl.sha256 == "sha256:abc123"


# ---------------------------------------------------------------------------
# Skills-only contract — non-skill kinds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
async def test_list_items_non_skill_returns_empty(kind: str) -> None:
    src = _source()
    try:
        rows, cursor = await src.list_items(kind)  # type: ignore[arg-type]
    finally:
        await src.aclose()

    assert rows == []
    assert cursor is None


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
async def test_search_non_skill_returns_empty(kind: str) -> None:
    src = _source()
    try:
        rows = await src.search(kind, "anything")  # type: ignore[arg-type]
    finally:
        await src.aclose()

    assert rows == []


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
async def test_detail_non_skill_raises(kind: str) -> None:
    src = _source()
    try:
        with pytest.raises(MarketplaceError):
            await src.detail(kind, "web-search")  # type: ignore[arg-type]
    finally:
        await src.aclose()


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
async def test_download_non_skill_raises(kind: str) -> None:
    src = _source()
    try:
        with pytest.raises(MarketplaceError):
            await src.download(kind, "web-search")  # type: ignore[arg-type]
    finally:
        await src.aclose()
