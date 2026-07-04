"""GitHub-releases update checker with ETag-aware persistent cache.

W1.1 of ``docs/PLAN_AUTO_UPDATE.md`` §2 Wave 1.

Design contract
---------------

* :meth:`UpdateChecker.poll` **MUST never raise**. On any failure (network,
  rate-limit, malformed payload) it returns a degraded
  :class:`UpdateStatus` with ``latest=None, available=False`` and logs a
  structured warning. The reasoning: this runs from a background cron
  later (W2.2) and from a 1-rpm admin POST; neither should be able to
  crash the gateway because GitHub is down.

* The cache file (``$DATA_DIR/.update_check.json``) is the single source
  of truth between polls. ``poll()`` reads it on every call; with
  ``force=False`` and ``last_checked_at`` younger than
  ``interval_hours`` we short-circuit *before* opening a socket.

* When we *do* go out to GitHub we attach the previous ETag in
  ``If-None-Match``. 304 responses don't count against the unauthenticated
  rate limit (60/hr/IP), so a 6-hour cron + a 1-rpm manual refresh
  comfortably fits even without a token.

* Version comparison uses :class:`packaging.version.Version`. GitHub
  release tags conventionally lead with ``v`` (``v1.1.2``); we strip it
  before parsing so ``v1.2.0`` compares correctly against an installed
  ``1.1.1``. Prerelease / draft entries are filtered unless
  ``include_prereleases=True`` — but we *do* remember them in
  ``prerelease_seen`` so the future opt-in channel can show what's
  available without round-tripping again.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import structlog
from packaging.version import InvalidVersion, Version

logger = structlog.get_logger(__name__)


__all__ = [
    "SystemUpdateCheckConfig",
    "UpdateChecker",
    "UpdateStatus",
]


# Version resolution (current vs release-space comparison) lives in
# :mod:`corlinman_server.system.app_version`; ``current_version()`` below
# delegates to it so every reader agrees on the running version.

# Default cache TTL between polls; mirrored by the config dataclass.
_DEFAULT_INTERVAL_HOURS = 6

# Request timeout. Short enough that the lifespan/cron tick doesn't stall
# the gateway when GitHub is degraded.
_HTTP_TIMEOUT_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class UpdateStatus:
    """Wire shape returned by :meth:`UpdateChecker.poll`.

    All time fields are unix milliseconds (matches the JS UI's
    ``Date.now()``). ``last_checked_at`` is *always* populated — even on a
    degraded return where we couldn't reach GitHub.
    """

    current: str
    latest: str | None
    available: bool
    release_url: str | None
    release_notes_md: str | None
    published_at: int | None
    last_checked_at: int
    prerelease_seen: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """Pydantic-free serialiser the admin routes can pass through."""
        return asdict(self)


@dataclass(slots=True)
class SystemUpdateCheckConfig:
    """Subset of the ``[system.update_check]`` TOML stanza.

    ``github_token`` is optional; when present we attach an
    ``Authorization: Bearer <token>`` header which raises the rate limit
    to 5000/hr (operationally meaningful for multi-instance ops, not for
    a single home-lab deploy).
    """

    enabled: bool = True
    interval_hours: int = _DEFAULT_INTERVAL_HOURS
    include_prereleases: bool = False
    # The repo was transferred ymylive → sweetcornna in 2026-05.
    # ``follow_redirects=True`` on the httpx client handles requests sent
    # to the old owner, but defaulting to the canonical owner saves one
    # redirect on every poll and works even on hosts whose corporate
    # proxy strips 3xx.
    repo: str = "sweetcornna/corlinman"
    github_token: str | None = None

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> SystemUpdateCheckConfig:
        """Hydrate a config dataclass from the dict-shaped runtime config.

        The gateway carries config as plain dicts (see
        ``gateway.core.config`` module docstring) — this helper centralises
        the key-by-key extraction with sane defaults so callers don't
        re-implement it. Unknown keys are ignored silently.
        """
        if not isinstance(raw, dict):
            return cls()
        kwargs: dict[str, Any] = {}
        if "enabled" in raw and isinstance(raw["enabled"], bool):
            kwargs["enabled"] = raw["enabled"]
        if "interval_hours" in raw:
            try:
                kwargs["interval_hours"] = max(1, int(raw["interval_hours"]))
            except (TypeError, ValueError):
                pass
        if "include_prereleases" in raw and isinstance(
            raw["include_prereleases"], bool
        ):
            kwargs["include_prereleases"] = raw["include_prereleases"]
        if "repo" in raw and isinstance(raw["repo"], str) and raw["repo"]:
            kwargs["repo"] = raw["repo"]
        # github_token may arrive as a literal string (env-ref already
        # resolved by ``gateway.core.config.load_from_path``) or be
        # absent. We tolerate ``None`` / empty string as "no token".
        if "github_token" in raw:
            tok = raw["github_token"]
            if isinstance(tok, str) and tok:
                kwargs["github_token"] = tok
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Cache file shape
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _strip_v(tag: str) -> str:
    """Strip the conventional leading ``v`` from a GitHub release tag.

    ``"v1.2.0"`` → ``"1.2.0"``, ``"1.2.0"`` → ``"1.2.0"``. Returns the
    original string for empty / single-character inputs.
    """
    if len(tag) >= 2 and (tag[0] == "v" or tag[0] == "V"):
        return tag[1:]
    return tag


def _compare_versions(current: str, latest_tag: str | None) -> bool:
    """``True`` when ``latest_tag`` (after stripping leading ``v``) is
    strictly greater than ``current`` per PEP 440 semantics.

    Falls back to ``False`` on any parse error — we'd rather *not* flag
    an update than crash, and a malformed tag is most likely a fluke we
    should ignore.
    """
    if latest_tag is None:
        return False
    try:
        return Version(_strip_v(latest_tag)) > Version(_strip_v(current))
    except InvalidVersion:
        logger.warning(
            "update_check.version_parse_failed",
            current=current,
            latest_tag=latest_tag,
        )
        return False


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


class UpdateChecker:
    """GitHub releases poller + persistent cache.

    Instances are cheap; the gateway lifecycle constructs one per process
    and shares it through :class:`AdminState`.
    """

    def __init__(
        self,
        config: SystemUpdateCheckConfig,
        cache_path: Path,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._cache_path = cache_path
        # The injected client is owned by the caller — we never close it.
        # Lazy-constructed clients (``self._http_client is None`` at init
        # time) live for the process lifetime; FastAPI lifespan closes
        # them at shutdown when the lifecycle teardown calls
        # :meth:`aclose`.
        self._http_client = http_client
        self._owns_client = http_client is None

    @property
    def config(self) -> SystemUpdateCheckConfig:
        """Read-only view of the live config for inspection."""
        return self._config

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying client iff we own it."""
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            # ``follow_redirects=True`` is non-negotiable: GitHub serves a
            # 301 from ``/repos/{old_owner}/...`` to
            # ``/repositories/{numeric_id}/...`` after a transfer/rename
            # (this repo went ymylive → sweetcornna in 2026-05). Without
            # it the poll always returns 301 → ``unexpected_status`` →
            # cache is never refreshed → UI freezes on whatever
            # ``latest_tag`` was current at the moment of the rename.
            self._http_client = httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
        return self._http_client

    # ------------------------------------------------------------------
    # Version resolution
    # ------------------------------------------------------------------

    def current_version(self) -> str:
        """Resolve the currently-installed gateway version.

        Delegates to :func:`corlinman_server.system.app_version.resolve_app_version`
        so the updater compares against the same release-spaced version
        that ``/healthz``, telemetry and MCP report. Reading the
        ``corlinman-server`` sub-package metadata directly (the old
        behaviour) drifted from the root release version and left the
        checker permanently on "update available"; see that module for
        the full precedence chain.
        """
        from corlinman_server.system.app_version import resolve_app_version

        return resolve_app_version()

    # ------------------------------------------------------------------
    # Cache I/O
    # ------------------------------------------------------------------

    def _load_cache(self) -> dict[str, Any]:
        try:
            raw = self._cache_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning(
                "update_check.cache_read_failed",
                path=str(self._cache_path),
                error=str(exc),
            )
            return {}
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            logger.warning(
                "update_check.cache_parse_failed",
                path=str(self._cache_path),
                error=str(exc),
            )
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_cache(self, payload: dict[str, Any]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".new")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._cache_path)
        except OSError as exc:
            logger.warning(
                "update_check.cache_write_failed",
                path=str(self._cache_path),
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Status construction
    # ------------------------------------------------------------------

    def _status_from_cache(
        self,
        cache: dict[str, Any],
        *,
        now_ms: int,
        last_checked_override: int | None = None,
    ) -> UpdateStatus:
        """Build an :class:`UpdateStatus` from a (possibly empty) cache.

        ``last_checked_override`` lets the 304 path stamp "yes we just
        checked, nothing changed" without rewriting the rest of the
        cache.
        """
        current = self.current_version()
        latest_tag = cache.get("latest_tag")
        latest = _strip_v(latest_tag) if isinstance(latest_tag, str) else None
        available = _compare_versions(current, latest_tag) if latest_tag else False
        last_checked = (
            last_checked_override
            if last_checked_override is not None
            else int(cache.get("last_checked_at") or now_ms)
        )
        prerelease_seen = cache.get("prerelease_seen") or []
        if not isinstance(prerelease_seen, list):
            prerelease_seen = []
        return UpdateStatus(
            current=current,
            latest=latest,
            available=available,
            release_url=cache.get("release_url"),
            release_notes_md=cache.get("release_notes_md"),
            published_at=cache.get("published_at"),
            last_checked_at=last_checked,
            prerelease_seen=list(prerelease_seen),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def poll(self, *, force: bool = False) -> UpdateStatus:
        """Refresh the cached release status.

        Resilient: returns a best-effort :class:`UpdateStatus` even on
        network / API failure — never raises.
        """
        now_ms = _now_ms()
        cache = self._load_cache()

        # TTL fast-path — skip the network entirely when the cache is
        # fresh enough and the caller didn't force.
        if not force:
            last_checked = cache.get("last_checked_at")
            if isinstance(last_checked, int):
                interval_ms = self._config.interval_hours * 3600 * 1000
                if now_ms - last_checked < interval_ms:
                    return self._status_from_cache(cache, now_ms=now_ms)

        url = f"https://api.github.com/repos/{self._config.repo}/releases/latest"
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"corlinman/{self.current_version()}",
        }
        cached_etag = cache.get("etag")
        if isinstance(cached_etag, str) and cached_etag:
            headers["If-None-Match"] = cached_etag
        if self._config.github_token:
            headers["Authorization"] = f"Bearer {self._config.github_token}"

        try:
            resp = await self._client().get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "update_check.network_error",
                repo=self._config.repo,
                error=str(exc),
            )
            # Stale cache + stamp "we tried" so the UI's last-checked
            # widget keeps ticking.
            cache["last_checked_at"] = now_ms
            self._save_cache(cache)
            return self._status_from_cache(
                cache, now_ms=now_ms, last_checked_override=now_ms
            )

        # 304 → cache is current; rate-limit not consumed.
        if resp.status_code == 304:
            cache["last_checked_at"] = now_ms
            self._save_cache(cache)
            return self._status_from_cache(
                cache, now_ms=now_ms, last_checked_override=now_ms
            )

        # 403 / 429 → typically rate-limit. Return stale + log.
        if resp.status_code in (403, 429):
            logger.warning(
                "update_check.rate_limited",
                repo=self._config.repo,
                status=resp.status_code,
                remaining=resp.headers.get("X-RateLimit-Remaining"),
            )
            cache["last_checked_at"] = now_ms
            self._save_cache(cache)
            return self._status_from_cache(
                cache, now_ms=now_ms, last_checked_override=now_ms
            )

        if resp.status_code != 200:
            logger.warning(
                "update_check.unexpected_status",
                repo=self._config.repo,
                status=resp.status_code,
            )
            cache["last_checked_at"] = now_ms
            self._save_cache(cache)
            return self._status_from_cache(
                cache, now_ms=now_ms, last_checked_override=now_ms
            )

        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "update_check.parse_failed",
                repo=self._config.repo,
                error=str(exc),
            )
            cache["last_checked_at"] = now_ms
            self._save_cache(cache)
            return self._status_from_cache(
                cache, now_ms=now_ms, last_checked_override=now_ms
            )

        if not isinstance(body, dict):
            cache["last_checked_at"] = now_ms
            self._save_cache(cache)
            return self._status_from_cache(
                cache, now_ms=now_ms, last_checked_override=now_ms
            )

        tag_name = body.get("tag_name")
        is_prerelease = bool(body.get("prerelease"))
        is_draft = bool(body.get("draft"))
        prerelease_seen: list[str] = list(cache.get("prerelease_seen") or [])
        if (
            isinstance(tag_name, str)
            and is_prerelease
            and tag_name not in prerelease_seen
        ):
            prerelease_seen.append(tag_name)
            # Trim to a sane size; we never want to balloon the cache.
            if len(prerelease_seen) > 20:
                prerelease_seen = prerelease_seen[-20:]

        # Skip prerelease/draft unless explicitly opted in.
        if (is_prerelease and not self._config.include_prereleases) or is_draft:
            # Persist the discovery (etag + prerelease_seen) but DO NOT
            # update latest_tag — we want the next "release" release to
            # remain the canonical "latest" target.
            new_cache: dict[str, Any] = dict(cache)
            new_cache["last_checked_at"] = now_ms
            new_cache["prerelease_seen"] = prerelease_seen
            etag = resp.headers.get("ETag")
            if etag:
                new_cache["etag"] = etag
            self._save_cache(new_cache)
            return self._status_from_cache(
                new_cache, now_ms=now_ms, last_checked_override=now_ms
            )

        # 200 OK + acceptable release → refresh everything.
        new_cache = {
            "etag": resp.headers.get("ETag") or cache.get("etag"),
            "last_checked_at": now_ms,
            "latest_tag": tag_name if isinstance(tag_name, str) else None,
            "release_notes_md": body.get("body")
            if isinstance(body.get("body"), str)
            else None,
            "release_url": body.get("html_url")
            if isinstance(body.get("html_url"), str)
            else None,
            "published_at": _parse_published_at(body.get("published_at")),
            "prerelease_seen": prerelease_seen,
        }
        self._save_cache(new_cache)
        return self._status_from_cache(new_cache, now_ms=now_ms)


def _parse_published_at(raw: Any) -> int | None:
    """Convert GitHub's ISO-8601 ``published_at`` to unix milliseconds.

    Returns ``None`` on missing / malformed input.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # GitHub emits ``2024-05-21T12:34:56Z`` — ``fromisoformat``
        # accepts ``+00:00`` but not ``Z`` until 3.11+. We're on 3.12 so
        # the ``Z`` swap is just defensive.
        from datetime import datetime

        normalised = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalised)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None
