"""Tests for :class:`corlinman_server.system.skill_hub.ClawHubClient`.

W1.4 of ``docs/PLAN_SKILL_HUB.md``. Covers the public surface the admin
routes proxy:

* ``search(q, limit)`` — substring search.
* ``list_skills(sort, cursor, limit)`` — paginated browse.
* ``get_skill(slug)`` — detail with moderation scan summary.
* ``download(slug, version)`` — tarball + content-hash header.
* Error mapping: HTTP 429 → :class:`HubRateLimitedError`, HTTP 500 /
  ``httpx.ConnectError`` → :class:`HubUnavailableError`.
* TTL cache: repeated ``search()`` collapses to one HTTP call inside the
  window; advancing the clock past the TTL re-issues the request.
* ``CORLINMAN_SKILL_HUB_BASE_URL`` env override picked up at client
  construction.

All HTTP traffic is mocked with :mod:`respx` so the suite is hermetic.
Tests are gated behind a module-level :func:`pytest.importorskip` on
``corlinman_server.system.skill_hub`` so this file stays collectable
while the sibling agent (W1-CORE) is still landing
``system/skill_hub/client.py``.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx

# TODO(W1-CORE): once `system/skill_hub/client.py` lands the
# ``importorskip`` collapses to a regular import. Until then the whole
# module is skipped at collection time so the rest of the test suite
# stays green.
skill_hub = pytest.importorskip(
    "corlinman_server.system.skill_hub",
    reason=(
        "TODO(W1-CORE): waiting on system/skill_hub/client.py from the "
        "sibling agent before these tests can execute."
    ),
)

ClawHubClient = skill_hub.ClawHubClient
HubUnavailableError = skill_hub.HubUnavailableError
HubRateLimitedError = skill_hub.HubRateLimitedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


DEFAULT_BASE = "https://clawhub.ai/api/v1"


def _summary(slug: str = "web-search") -> dict:
    return {
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "description": "Search the live web.",
        "version": "1.0.0",
        "stars": 42,
        "downloads": 1024,
        "updatedAt": "2026-05-20T12:00:00Z",
    }


def _detail(slug: str = "web-search") -> dict:
    payload = _summary(slug)
    payload.update(
        {
            "readme": "# web-search\n\nDoes the thing.",
            "moderation": {
                "scanSummary": "no_findings",
                "scannedAt": "2026-05-20T12:00:00Z",
            },
            "versions": [
                {"version": "1.0.0", "publishedAt": "2026-05-20T12:00:00Z"},
            ],
        }
    )
    return payload


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_returns_parsed_summaries() -> None:
    with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as mock:
        mock.get("/search", params={"q": "web", "limit": 25}).respond(
            200,
            json={"results": [_summary("web-search"), _summary("web-fetch")]},
        )
        client = ClawHubClient()
        try:
            rows = await client.search("web")
        finally:
            await client.aclose()

    slugs = [r.slug for r in rows]
    assert slugs == ["web-search", "web-fetch"]
    # Sanity: the parsed objects expose the canonical fields the UI needs.
    first = rows[0]
    assert first.name
    assert first.description
    assert first.version == "1.0.0"


# ---------------------------------------------------------------------------
# list_skills() + nextCursor handling
# ---------------------------------------------------------------------------


async def test_list_skills_parses_next_cursor() -> None:
    with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as mock:
        mock.get("/skills").respond(
            200,
            json={
                "results": [_summary("a"), _summary("b")],
                "nextCursor": "cursor-abc",
            },
        )
        client = ClawHubClient()
        try:
            rows, cursor = await client.list_skills(sort="trending")
        finally:
            await client.aclose()

    assert [r.slug for r in rows] == ["a", "b"]
    assert cursor == "cursor-abc"


async def test_list_skills_returns_none_cursor_when_absent() -> None:
    with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as mock:
        mock.get("/skills").respond(
            200,
            json={"results": [_summary("a")]},
        )
        client = ClawHubClient()
        try:
            _, cursor = await client.list_skills(sort="trending")
        finally:
            await client.aclose()

    assert cursor is None


# ---------------------------------------------------------------------------
# get_skill() — moderation scan mapping
# ---------------------------------------------------------------------------


async def test_get_skill_maps_scan_summary() -> None:
    with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as mock:
        mock.get("/skills/web-search").respond(
            200,
            json=_detail("web-search"),
        )
        client = ClawHubClient()
        try:
            detail = await client.get_skill("web-search")
        finally:
            await client.aclose()

    assert detail.slug == "web-search"
    assert detail.scan_summary == "no_findings"


# ---------------------------------------------------------------------------
# download() — content hash extraction
# ---------------------------------------------------------------------------


async def test_download_extracts_content_hash() -> None:
    tar_bytes = b"\x1f\x8b\x08\x00not-a-real-tarball-but-bytes"
    with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as mock:
        mock.get("/download", params={"slug": "web-search", "version": "latest"}).respond(
            200,
            content=tar_bytes,
            headers={
                "X-Content-Hash": "sha256:deadbeef",
                "Content-Type": "application/gzip",
            },
        )
        client = ClawHubClient()
        try:
            dl = await client.download("web-search")
        finally:
            await client.aclose()

    assert dl.content == tar_bytes
    assert dl.content_hash == "sha256:deadbeef"


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


async def test_429_raises_rate_limited_with_retry_after() -> None:
    with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as mock:
        mock.get("/search").respond(
            429,
            json={"error": "rate_limited"},
            headers={"Retry-After": "60"},
        )
        client = ClawHubClient()
        try:
            with pytest.raises(HubRateLimitedError) as excinfo:
                await client.search("web")
        finally:
            await client.aclose()

    assert excinfo.value.retry_after_seconds == 60


async def test_500_raises_hub_unavailable() -> None:
    with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as mock:
        mock.get("/search").respond(500, json={"error": "internal"})
        client = ClawHubClient()
        try:
            with pytest.raises(HubUnavailableError):
                await client.search("web")
        finally:
            await client.aclose()


async def test_network_error_raises_hub_unavailable() -> None:
    with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as mock:
        mock.get("/search").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        client = ClawHubClient()
        try:
            with pytest.raises(HubUnavailableError):
                await client.search("web")
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


async def test_search_cache_collapses_repeat_calls() -> None:
    with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as mock:
        route = mock.get("/search").respond(
            200, json={"results": [_summary("web-search")]}
        )
        client = ClawHubClient()
        try:
            await client.search("web")
            await client.search("web")
        finally:
            await client.aclose()

    assert route.call_count == 1


async def test_search_cache_re_fetches_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Advance the monotonic clock past the cache TTL and assert the
    second call performs a fresh HTTP fetch."""
    # Build a real-time offset we can advance — patch
    # ``time.monotonic`` so any cache implementation that uses it sees
    # us "in the future" on the second call. Implementations that use
    # ``time.time`` instead are also handled.
    now = time.monotonic()
    fake_now = {"t": now}

    monkeypatch.setattr(
        "time.monotonic", lambda: fake_now["t"]
    )
    monkeypatch.setattr(
        "time.time", lambda: fake_now["t"]
    )

    with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as mock:
        route = mock.get("/search").respond(
            200, json={"results": [_summary("web-search")]}
        )
        client = ClawHubClient()
        try:
            await client.search("web")
            # Jump 10 minutes — well past the documented 60s list/search TTL.
            fake_now["t"] += 600.0
            await client.search("web")
        finally:
            await client.aclose()

    assert route.call_count == 2


# ---------------------------------------------------------------------------
# Env-var base URL override
# ---------------------------------------------------------------------------


async def test_base_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    custom_base = "https://hub.staging.openclaw.ai/api/v1"
    monkeypatch.setenv("CORLINMAN_SKILL_HUB_BASE_URL", custom_base)

    with respx.mock(base_url=custom_base, assert_all_called=False) as mock:
        route = mock.get("/search").respond(
            200, json={"results": [_summary("web-search")]}
        )
        client = ClawHubClient()
        try:
            rows = await client.search("web")
        finally:
            await client.aclose()

    assert route.call_count == 1
    assert [r.slug for r in rows] == ["web-search"]
