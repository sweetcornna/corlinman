"""Tests for :class:`corlinman_server.system.UpdateChecker` (W1.1).

Covers the documented behaviour in
``docs/PLAN_AUTO_UPDATE.md`` §2 Wave 1/W1.1:

* 200 OK with newer/equal/older tag — ``available`` flag flips correctly
* 304 Not Modified — cache returned, ``last_checked_at`` stamped fresh
* 403 rate-limit + network error — stale cache returned, no raise
* Prerelease filter — drafts/prereleases skipped unless opted in
* ``force=True`` bypasses the TTL fast-path
* Leading ``v`` stripped during version compare

Uses ``respx`` to mock the GitHub API and an injected ``httpx.AsyncClient``
so the tests are hermetic + deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from corlinman_server.system import (
    SystemUpdateCheckConfig,
    UpdateChecker,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Releases *list* endpoint (the checker fetches ``?per_page=10``; respx
# treats pattern query params as "contains", so the bare path matches).
_RELEASES_URL = "https://api.github.com/repos/ymylive/corlinman/releases"


def _make_checker(
    tmp_path: Path,
    *,
    include_prereleases: bool = False,
    interval_hours: int = 6,
    cache_seed: dict | None = None,
    repo: str = "ymylive/corlinman",
) -> UpdateChecker:
    """Construct a checker pointed at a temp cache file.

    The HTTP client is left to lazy-construct so respx can patch it.
    """
    cache_path = tmp_path / ".update_check.json"
    if cache_seed is not None:
        cache_path.write_text(json.dumps(cache_seed), encoding="utf-8")
    config = SystemUpdateCheckConfig(
        enabled=True,
        interval_hours=interval_hours,
        include_prereleases=include_prereleases,
        repo=repo,
    )
    return UpdateChecker(config=config, cache_path=cache_path)


def _release_entry(
    *,
    tag: str = "v1.1.2",
    body: str = "## What's new\n\n- thing",
    html_url: str = "https://github.com/ymylive/corlinman/releases/tag/v1.1.2",
    published_at: str = "2026-05-20T12:00:00Z",
    prerelease: bool = False,
    draft: bool = False,
) -> dict:
    return {
        "tag_name": tag,
        "body": body,
        "html_url": html_url,
        "published_at": published_at,
        "prerelease": prerelease,
        "draft": draft,
    }


def _release_body(**kwargs) -> list[dict]:
    """Single-entry releases-list payload (the wire shape of
    ``GET /releases?per_page=10``)."""
    return [_release_entry(**kwargs)]


@pytest.fixture(autouse=True)
def _pin_current_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``current_version`` to a known value across every test.

    The runner has ``corlinman-server == 1.1.1`` installed once
    ``uv sync`` runs, but pinning via env var makes the assertions
    deterministic regardless of editable-install state.
    """
    monkeypatch.setenv("CORLINMAN_VERSION", "1.1.1")
    # Make sure importlib metadata path doesn't beat the env var.
    import importlib.metadata as md

    original = md.version

    def _fake_version(name: str) -> str:
        if name == "corlinman-server":
            raise md.PackageNotFoundError(name)
        return original(name)

    monkeypatch.setattr(md, "version", _fake_version)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_200_newer_tag_marks_available(tmp_path: Path) -> None:
    checker = _make_checker(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_RELEASES_URL).respond(
            200,
            json=_release_body(tag="v1.1.2"),
            headers={"ETag": 'W/"abc"'},
        )
        status = await checker.poll(force=True)

    assert status.current == "1.1.1"
    assert status.latest == "1.1.2"
    assert status.available is True
    assert status.release_notes_md is not None
    assert "What's new" in status.release_notes_md
    assert status.release_url == "https://github.com/ymylive/corlinman/releases/tag/v1.1.2"
    assert status.published_at is not None and status.published_at > 0
    # Cache was written with the ETag
    on_disk = json.loads((tmp_path / ".update_check.json").read_text())
    assert on_disk["etag"] == 'W/"abc"'
    assert on_disk["latest_tag"] == "v1.1.2"


@pytest.mark.asyncio
async def test_poll_200_same_tag_marks_unavailable(tmp_path: Path) -> None:
    checker = _make_checker(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_RELEASES_URL).respond(
            200, json=_release_body(tag="v1.1.1")
        )
        status = await checker.poll(force=True)

    assert status.latest == "1.1.1"
    assert status.available is False


@pytest.mark.asyncio
async def test_poll_200_older_tag_marks_unavailable(tmp_path: Path) -> None:
    checker = _make_checker(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_RELEASES_URL).respond(
            200, json=_release_body(tag="v1.0.0")
        )
        status = await checker.poll(force=True)

    assert status.latest == "1.0.0"
    assert status.available is False


@pytest.mark.asyncio
async def test_poll_304_returns_cached_unchanged(tmp_path: Path) -> None:
    seed = {
        "etag": 'W/"old"',
        "last_checked_at": 1,  # Ancient so TTL is exhausted
        "latest_tag": "v1.2.0",
        "release_notes_md": "previous body",
        "release_url": "https://example.com/v1.2.0",
        "published_at": 1716000000000,
        "prerelease_seen": [],
    }
    checker = _make_checker(tmp_path, cache_seed=seed)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_RELEASES_URL).respond(304)
        status = await checker.poll(force=True)

    assert status.latest == "1.2.0"
    assert status.release_notes_md == "previous body"
    # ``last_checked_at`` was bumped, etag preserved.
    on_disk = json.loads((tmp_path / ".update_check.json").read_text())
    assert on_disk["etag"] == 'W/"old"'
    assert on_disk["last_checked_at"] > 1
    assert status.last_checked_at > 1
    # We *did* hit the network — exactly once
    assert route.called is True


@pytest.mark.asyncio
async def test_poll_403_returns_cached_stale(tmp_path: Path) -> None:
    seed = {
        "etag": 'W/"old"',
        "last_checked_at": 1,
        "latest_tag": "v1.2.0",
        "release_notes_md": "previous",
        "release_url": "https://example.com/v1.2.0",
        "published_at": 100,
        "prerelease_seen": [],
    }
    checker = _make_checker(tmp_path, cache_seed=seed)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_RELEASES_URL).respond(
            403, json={"message": "API rate limit exceeded"}
        )
        # MUST NOT raise
        status = await checker.poll(force=True)

    assert status.latest == "1.2.0"
    assert status.release_notes_md == "previous"


@pytest.mark.asyncio
async def test_poll_network_error_returns_cached(tmp_path: Path) -> None:
    seed = {
        "etag": 'W/"x"',
        "last_checked_at": 1,
        "latest_tag": "v1.2.0",
        "release_notes_md": "stuff",
        "release_url": "https://example.com",
        "published_at": 100,
        "prerelease_seen": [],
    }
    checker = _make_checker(tmp_path, cache_seed=seed)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_RELEASES_URL).mock(
            side_effect=httpx.ConnectError("boom")
        )
        # MUST NOT raise
        status = await checker.poll(force=True)

    assert status.latest == "1.2.0"
    assert status.release_notes_md == "stuff"


@pytest.mark.asyncio
async def test_poll_prerelease_skipped_by_default(tmp_path: Path) -> None:
    checker = _make_checker(tmp_path, include_prereleases=False)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_RELEASES_URL).respond(
            200,
            json=_release_body(tag="v1.2.0-rc.1", prerelease=True),
        )
        status = await checker.poll(force=True)

    # Prerelease skipped — no latest_tag persisted but the tag is
    # remembered for the future opt-in channel.
    assert status.latest is None
    assert status.available is False
    on_disk = json.loads((tmp_path / ".update_check.json").read_text())
    assert "v1.2.0-rc.1" in on_disk["prerelease_seen"]
    assert on_disk.get("latest_tag") in (None, "")  # not promoted


@pytest.mark.asyncio
async def test_poll_force_bypasses_ttl(tmp_path: Path) -> None:
    """When ``last_checked_at`` is fresh we normally skip the network;
    ``force=True`` must override that."""
    import time

    fresh_now_ms = int(time.time() * 1000)
    seed = {
        "etag": 'W/"old"',
        "last_checked_at": fresh_now_ms,
        "latest_tag": "v1.1.1",
        "release_notes_md": "old",
        "release_url": "https://example.com/v1.1.1",
        "published_at": 100,
        "prerelease_seen": [],
    }
    checker = _make_checker(tmp_path, cache_seed=seed)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_RELEASES_URL).respond(
            200, json=_release_body(tag="v1.2.5", body="forced")
        )
        # force=False on a fresh cache should NOT hit the network
        await checker.poll(force=False)
        assert route.call_count == 0

        # force=True hits the wire
        status = await checker.poll(force=True)
        assert route.call_count == 1
        assert status.latest == "1.2.5"
        assert status.release_notes_md == "forced"


@pytest.mark.asyncio
async def test_version_compare_strips_leading_v(tmp_path: Path) -> None:
    """``v1.2.0`` must compare as ``1.2.0`` against current ``1.1.2``."""

    # Override the pinned env var for this case so we can probe a
    # different current version.
    import os

    os.environ["CORLINMAN_VERSION"] = "1.1.2"
    try:
        checker = _make_checker(tmp_path)
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RELEASES_URL).respond(
                200, json=_release_body(tag="v1.2.0")
            )
            status = await checker.poll(force=True)
        assert status.current == "1.1.2"
        assert status.latest == "1.2.0"
        assert status.available is True
    finally:
        os.environ["CORLINMAN_VERSION"] = "1.1.1"


@pytest.mark.asyncio
async def test_poll_sends_if_none_match_when_etag_cached(tmp_path: Path) -> None:
    """Sanity check: cached ETag is forwarded as ``If-None-Match``."""
    seed = {
        "etag": 'W/"xyz"',
        "last_checked_at": 1,
        "latest_tag": "v1.1.2",
        "release_notes_md": None,
        "release_url": None,
        "published_at": None,
        "prerelease_seen": [],
    }
    checker = _make_checker(tmp_path, cache_seed=seed)
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["If-None-Match"] = request.headers.get("If-None-Match", "")
        captured["User-Agent"] = request.headers.get("User-Agent", "")
        return httpx.Response(304)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_RELEASES_URL).mock(side_effect=_capture)
        await checker.poll(force=True)

    assert captured["If-None-Match"] == 'W/"xyz"'
    assert captured["User-Agent"].startswith("corlinman/")


@pytest.mark.asyncio
async def test_poll_populates_recent_releases_newest_first(
    tmp_path: Path,
) -> None:
    """The list endpoint yields the rollback-picker history: drafts are
    dropped, prereleases kept (flagged), and ``latest`` is the first
    non-prerelease entry even when a prerelease sits above it."""
    checker = _make_checker(tmp_path)
    payload = [
        _release_entry(tag="v1.3.0-rc.1", prerelease=True),
        _release_entry(tag="v1.2.9-draft", draft=True),
        _release_entry(tag="v1.2.0", body="stable notes"),
        _release_entry(tag="v1.1.2"),
    ]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_RELEASES_URL).respond(200, json=payload)
        status = await checker.poll(force=True)

    assert status.latest == "1.2.0"
    assert status.available is True
    assert status.release_notes_md == "stable notes"
    tags = [entry["tag"] for entry in status.recent_releases]
    assert tags == ["v1.3.0-rc.1", "v1.2.0", "v1.1.2"]  # draft dropped
    assert status.recent_releases[0]["prerelease"] is True
    assert status.recent_releases[1]["prerelease"] is False
    # Persisted for the TTL fast-path.
    on_disk = json.loads((tmp_path / ".update_check.json").read_text())
    assert [e["tag"] for e in on_disk["recent_releases"]] == tags
    # And served back from cache without a network hit.
    cached = await checker.poll(force=False)
    assert [e["tag"] for e in cached.recent_releases] == tags


@pytest.mark.asyncio
async def test_proxy_url_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``proxy_url`` is configured the lazily-built client must be
    handed the proxy (httpx routes ALL traffic through it — no silent
    direct fallback), and a dead proxy degrades to the stale cache."""
    captured_kwargs: dict = {}
    real_async_client = httpx.AsyncClient

    def _capturing_client(**kwargs):
        captured_kwargs.update(kwargs)
        kwargs.pop("proxy", None)  # respx can't intercept through a proxy
        return real_async_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _capturing_client)

    seed = {
        "etag": 'W/"old"',
        "last_checked_at": 1,
        "latest_tag": "v1.2.0",
        "release_notes_md": "stale",
        "release_url": None,
        "published_at": 100,
        "prerelease_seen": [],
    }
    cache_path = tmp_path / ".update_check.json"
    cache_path.write_text(json.dumps(seed), encoding="utf-8")
    config = SystemUpdateCheckConfig(
        repo="ymylive/corlinman",
        proxy_url="http://127.0.0.1:59999",
    )
    checker = UpdateChecker(config=config, cache_path=cache_path)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_RELEASES_URL).mock(
            side_effect=httpx.ProxyError("proxy down")
        )
        status = await checker.poll(force=True)  # MUST NOT raise

    assert captured_kwargs.get("proxy") == "http://127.0.0.1:59999"
    assert status.latest == "1.2.0"
    assert status.release_notes_md == "stale"


def test_config_from_mapping_parses_proxy_url() -> None:
    cfg = SystemUpdateCheckConfig.from_mapping(
        {"proxy_url": "  socks5://127.0.0.1:1080  "}
    )
    assert cfg.proxy_url == "socks5://127.0.0.1:1080"
    assert SystemUpdateCheckConfig.from_mapping({"proxy_url": ""}).proxy_url is None
    assert SystemUpdateCheckConfig.from_mapping({}).proxy_url is None
