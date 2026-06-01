"""Tests for :class:`corlinman_server.system.marketplace.github_source.GitHubSource`.

A :class:`httpx.MockTransport` stands in for ``raw.githubusercontent.com``.
The handler serves a fixture ``index.json`` (one skill + one mcp + one
plugin) plus the referenced blob URLs, routing on ``request.url.path``.
The accelerator defaults to ``off`` so the source always GETs the raw
GitHub host (no rewrite), which keeps routing trivial.

Coverage:

* ``list_items`` filters by kind, sorts (downloads desc), and paginates
  via the opaque cursor.
* ``search`` substring-matches name/description/tags.
* ``detail`` returns the fully-populated row.
* ``download`` verifies the declared sha256 (ok), raises
  :class:`MarketplaceIntegrityError` on a tampered byte, returns an mcp
  ``manifest`` download (validating JSON), and maps a 429 →
  :class:`MarketplaceRateLimitedError` / 500 →
  :class:`MarketplaceUnavailableError`.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from corlinman_server.system.marketplace.github_source import GitHubSource
from corlinman_server.system.marketplace.source import (
    MarketplaceIntegrityError,
    MarketplaceRateLimitedError,
    MarketplaceUnavailableError,
)

# --- fixture blobs ---------------------------------------------------------

SKILL_TARBALL = b"\x1f\x8b\x08\x00skill-tarball-bytes-here"
PLUGIN_TARBALL = b"\x1f\x8b\x08\x00plugin-tarball-bytes-here"
MCP_MANIFEST = json.dumps(
    {"name": "weather", "transport": "stdio", "command": "weather-mcp"}
).encode("utf-8")

SKILL_SHA = hashlib.sha256(SKILL_TARBALL).hexdigest()
PLUGIN_SHA = hashlib.sha256(PLUGIN_TARBALL).hexdigest()

# Repo-relative content paths the index points at.
SKILL_PATH = "blobs/web-search-1.0.0.tar.gz"
PLUGIN_PATH = "blobs/auto-deploy-2.0.0.tar.gz"
MCP_PATH = "manifests/weather.json"


def _index() -> dict:
    return {
        "items": [
            {
                "kind": "skill",
                "slug": "web-search",
                "name": "Web Search",
                "description": "Search the live web.",
                "version": "1.0.0",
                "stars": 42,
                "downloads": 1000,
                "tags": ["search", "web"],
                "tarball": SKILL_PATH,
                "sha256": SKILL_SHA,
            },
            {
                "kind": "skill",
                "slug": "image-gen",
                "name": "Image Generator",
                "description": "Make pictures from prompts.",
                "version": "0.9.0",
                "stars": 10,
                "downloads": 5000,
                "tags": ["image"],
                "tarball": "blobs/image-gen.tar.gz",
                "sha256": hashlib.sha256(b"x").hexdigest(),
            },
            {
                "kind": "mcp",
                "slug": "weather",
                "name": "Weather MCP",
                "description": "Forecasts over stdio.",
                "version": "3.1.0",
                "transport": "stdio",
                "downloads": 7,
                "manifest": MCP_PATH,
            },
            {
                "kind": "plugin",
                "slug": "auto-deploy",
                "name": "Auto Deploy",
                "description": "Ship on green.",
                "version": "2.0.0",
                "downloads": 3,
                "tarball": PLUGIN_PATH,
                "sha256": PLUGIN_SHA,
            },
        ]
    }


# --- handler factory -------------------------------------------------------


def _make_handler(
    *,
    skill_body: bytes = SKILL_TARBALL,
    index_payload: dict | None = None,
):
    """Build a MockTransport handler routing on request path.

    ``skill_body`` lets a test serve a *tampered* skill tarball while the
    index still declares the pristine hash.
    """

    payload = index_payload if index_payload is not None else _index()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/index.json"):
            return httpx.Response(200, json=payload)
        if path.endswith("/" + SKILL_PATH):
            return httpx.Response(200, content=skill_body)
        if path.endswith("/" + PLUGIN_PATH):
            return httpx.Response(200, content=PLUGIN_TARBALL)
        if path.endswith("/" + MCP_PATH):
            return httpx.Response(200, content=MCP_MANIFEST)
        return httpx.Response(404, content=b"not found")

    return handler


def _source(handler) -> GitHubSource:
    return GitHubSource(
        repo="o/r",
        ref="main",
        transport=httpx.MockTransport(handler),
    )


# ---------------------------------------------------------------------------
# list_items — filter / sort / paginate
# ---------------------------------------------------------------------------


async def test_list_items_filters_by_kind() -> None:
    src = _source(_make_handler())
    try:
        skills, cursor = await src.list_items("skill")
        mcps, _ = await src.list_items("mcp")
        plugins, _ = await src.list_items("plugin")
    finally:
        await src.aclose()

    assert {i.slug for i in skills} == {"web-search", "image-gen"}
    assert [i.slug for i in mcps] == ["weather"]
    assert [i.slug for i in plugins] == ["auto-deploy"]
    assert cursor is None


async def test_list_items_sorts_by_downloads_desc() -> None:
    src = _source(_make_handler())
    try:
        skills, _ = await src.list_items("skill", sort="trending")
    finally:
        await src.aclose()

    # image-gen (5000) outranks web-search (1000) by downloads.
    assert [i.slug for i in skills] == ["image-gen", "web-search"]


async def test_list_items_paginates_via_cursor() -> None:
    src = _source(_make_handler())
    try:
        page1, cursor1 = await src.list_items("skill", limit=1)
        assert cursor1 is not None
        page2, cursor2 = await src.list_items("skill", limit=1, cursor=cursor1)
    finally:
        await src.aclose()

    assert [i.slug for i in page1] == ["image-gen"]  # highest downloads first
    assert [i.slug for i in page2] == ["web-search"]
    assert cursor2 is None


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def test_search_substring_match() -> None:
    src = _source(_make_handler())
    try:
        hits = await src.search("skill", "search")
        none = await src.search("skill", "nonexistent-needle")
        empty = await src.search("skill", "   ")
    finally:
        await src.aclose()

    assert [i.slug for i in hits] == ["web-search"]
    assert none == []
    assert empty == []


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


async def test_detail_returns_populated_item() -> None:
    src = _source(_make_handler())
    try:
        item = await src.detail("skill", "web-search")
    finally:
        await src.aclose()

    assert item.kind == "skill"
    assert item.slug == "web-search"
    assert item.name == "Web Search"
    assert item.latest_version == "1.0.0"
    assert item.tags == ("search", "web")


async def test_detail_missing_slug_raises_unavailable() -> None:
    src = _source(_make_handler())
    try:
        with pytest.raises(MarketplaceUnavailableError):
            await src.detail("skill", "does-not-exist")
    finally:
        await src.aclose()


# ---------------------------------------------------------------------------
# download — skill (sha256 verify ok + tamper)
# ---------------------------------------------------------------------------


async def test_download_skill_verifies_sha256() -> None:
    src = _source(_make_handler())
    try:
        dl = await src.download("skill", "web-search")
    finally:
        await src.aclose()

    assert dl.kind == "skill"
    assert dl.media == "tarball"
    assert dl.content == SKILL_TARBALL
    assert dl.version == "1.0.0"
    assert dl.sha256 == SKILL_SHA


async def test_download_skill_tampered_byte_raises_integrity() -> None:
    # Serve a body that differs from the declared hash by one byte.
    tampered = SKILL_TARBALL[:-1] + bytes([SKILL_TARBALL[-1] ^ 0xFF])
    assert tampered != SKILL_TARBALL
    src = _source(_make_handler(skill_body=tampered))
    try:
        with pytest.raises(MarketplaceIntegrityError):
            await src.download("skill", "web-search")
    finally:
        await src.aclose()


# ---------------------------------------------------------------------------
# download — mcp manifest
# ---------------------------------------------------------------------------


async def test_download_mcp_returns_manifest_media() -> None:
    src = _source(_make_handler())
    try:
        dl = await src.download("mcp", "weather")
    finally:
        await src.aclose()

    assert dl.media == "manifest"
    assert dl.content == MCP_MANIFEST
    # The source validated it parses; double-check it round-trips here.
    assert json.loads(dl.content.decode("utf-8"))["name"] == "weather"


async def test_download_mcp_invalid_json_raises_unavailable() -> None:
    handler = _make_handler()

    def bad_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/" + MCP_PATH):
            return httpx.Response(200, content=b"{not valid json")
        return handler(request)

    src = _source(bad_handler)
    try:
        with pytest.raises(MarketplaceUnavailableError):
            await src.download("mcp", "weather")
    finally:
        await src.aclose()


# ---------------------------------------------------------------------------
# Error mapping — 429 / 500
# ---------------------------------------------------------------------------


async def test_429_on_index_raises_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error": "rate_limited"}, headers={"Retry-After": "45"}
        )

    src = _source(handler)
    try:
        with pytest.raises(MarketplaceRateLimitedError) as excinfo:
            await src.list_items("skill")
    finally:
        await src.aclose()

    assert excinfo.value.retry_after_seconds == 45


async def test_500_on_index_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    src = _source(handler)
    try:
        with pytest.raises(MarketplaceUnavailableError):
            await src.list_items("skill")
    finally:
        await src.aclose()
