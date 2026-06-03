"""SQLite-backed marketplace *plugin* registry.

The marketplace can install three extension kinds (skills, MCP servers,
plugins). Skills materialise as on-disk bundles tracked by their sidecar;
MCP servers and plugins additionally carry an *index row* so the admin UI
can render an installed-plugins list (slug, version, enabled toggle,
provenance) without crawling the filesystem on every page load.

This module owns the plugin index table. It deliberately mirrors the
MCP/profile store pattern used elsewhere in the codebase:

* **Sync sqlite3, not aiosqlite.** Plugin mutations are infrequent
  (operator UI clicks) and tiny (a single row). Keeping the API sync
  matches :mod:`corlinman_server.profiles.store`; FastAPI handlers call
  in directly from ``async def`` since the underlying work is microseconds.
* **One connection + ``threading.Lock``.** SQLite serialises writes via
  its own file lock; the Python-level lock keeps concurrent callers on the
  shared ``check_same_thread=False`` connection from tripping
  ``database is locked`` on the read side.
* **WAL + foreign_keys ON.** Mirrors the rest of corlinman's SQLite stores.

The on-disk plugin bundle itself (``<plugins_dir>/<slug>/``) is owned by
:mod:`corlinman_server.system.marketplace.plugin_installer`; this store is
purely the index.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

import structlog

from corlinman_server.system.marketplace._sqlite_store import PersistentSqliteStore

logger = structlog.get_logger(__name__)

__all__ = [
    "InstalledPluginRow",
    "PluginInvalid",
    "PluginNotFound",
    "PluginStore",
    "PluginStoreError",
]


_SCHEMA_SQL: str = r"""
CREATE TABLE IF NOT EXISTS plugins (
    slug          TEXT PRIMARY KEY,
    version       TEXT,
    source        TEXT,
    enabled       INTEGER NOT NULL DEFAULT 0,
    installed_at  TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plugins_installed_at
    ON plugins(installed_at);
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PluginStoreError(Exception):
    """Base class for plugin-store domain errors.

    Route layer maps these to HTTP status codes; CLI / tests pattern-match
    on the subclass.
    """


class PluginNotFound(PluginStoreError):  # noqa: N818 - public API uses domain names.
    """Raised when an operation references a non-existent slug. Maps to
    HTTP 404."""


class PluginInvalid(PluginStoreError):  # noqa: N818 - public API uses domain names.
    """Raised when a slug fails validation. Maps to HTTP 422."""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstalledPluginRow:
    """One row from the ``plugins`` table.

    Frozen + slots so callers can stash returns without worrying about
    aliasing. ``installed_at`` / ``updated_at`` are ISO-8601 ``Z``-suffixed
    UTC strings (mirroring the rest of the corlinman codebase).
    """

    slug: str
    version: str
    source: str
    enabled: bool
    installed_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Time helpers (module-level so tests can freeze via monkeypatch)
# ---------------------------------------------------------------------------


def _utc_now() -> _dt.datetime:
    """Wall-clock UTC. Pulled out so tests can freeze time via monkeypatch
    of ``...plugin_store._utc_now``."""
    return _dt.datetime.now(_dt.UTC)


def _iso(dt: _dt.datetime) -> str:
    """Render a tz-aware datetime as RFC-3339 / ISO-8601 with ``Z`` suffix.

    Matches the format used by the profile store so the wire vocabulary is
    consistent across admin routes.
    """
    return dt.astimezone(_dt.UTC).isoformat().replace("+00:00", "Z")


def _validate_slug(slug: str) -> None:
    """Refuse anything that isn't a single, traversal-free path segment.

    A plugin slug doubles as its on-disk directory name, so the same
    rejection rules the installer applies to tar member names apply here:
    non-empty, not pure dots, no path separator, no NUL byte. Raises
    :class:`PluginInvalid`.
    """
    if not isinstance(slug, str) or not slug or slug in (".", ".."):
        raise PluginInvalid(f"invalid plugin slug: {slug!r}")
    if "/" in slug or "\\" in slug or "\x00" in slug:
        raise PluginInvalid(f"invalid plugin slug: {slug!r}")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class PluginStore(PersistentSqliteStore):
    """CRUD wrapper over the ``plugins`` table.

    Construct with the SQLite file path the index should live at. The
    constructor opens the connection eagerly and applies the schema; tests
    can pass any writable path (including ``:memory:`` is *not*
    recommended because the parent-dir ``mkdir`` would choke — pass a real
    ``tmp_path`` file instead).

    Thread safety: holds a single :class:`sqlite3.Connection` with
    ``check_same_thread=False`` guarded by a :class:`threading.Lock`,
    matching :class:`corlinman_server.profiles.store.ProfileStore`.
    """

    _SCHEMA_SQL = _SCHEMA_SQL

    # ---- internal helpers ---------------------------------------------------

    def _row_to_plugin(self, row: tuple[Any, ...]) -> InstalledPluginRow:
        slug, version, source, enabled, installed_at, updated_at = row
        return InstalledPluginRow(
            slug=str(slug),
            version=str(version) if version is not None else "",
            source=str(source) if source is not None else "",
            enabled=bool(enabled),
            installed_at=str(installed_at),
            updated_at=str(updated_at),
        )

    def _get_row(self, slug: str) -> tuple[Any, ...] | None:
        cursor = self._conn.execute(
            "SELECT slug, version, source, enabled, installed_at, updated_at "
            "FROM plugins WHERE slug = ?",
            (slug,),
        )
        row: tuple[Any, ...] | None = cursor.fetchone()
        return row

    # ---- CRUD ---------------------------------------------------------------

    def get(self, slug: str) -> InstalledPluginRow | None:
        """Fetch one plugin row by slug, or ``None`` when missing."""
        with self._lock:
            row = self._get_row(slug)
        return self._row_to_plugin(row) if row is not None else None

    def list(self) -> list[InstalledPluginRow]:
        """All installed plugins, ordered by ``installed_at ASC`` then slug
        so the listing is stable across calls."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT slug, version, source, enabled, installed_at, updated_at "
                "FROM plugins "
                "ORDER BY installed_at ASC, slug ASC"
            )
            rows = cursor.fetchall()
        return [self._row_to_plugin(r) for r in rows]

    def upsert(
        self,
        slug: str,
        *,
        version: str,
        source: str,
        enabled: bool = False,
    ) -> InstalledPluginRow:
        """Insert or update the index row for ``slug``.

        On first install the row is created with ``installed_at`` =
        ``updated_at`` = now. A subsequent upsert (a version bump / reinstall)
        preserves the original ``installed_at`` and only bumps
        ``updated_at`` + the mutable columns. ``enabled`` defaults to
        ``False`` so a freshly-installed plugin is inert until an operator
        toggles it on.

        Raises :class:`PluginInvalid` when ``slug`` is not a single safe
        path component.
        """
        _validate_slug(slug)
        now_iso = _iso(_utc_now())
        with self._lock:
            existing = self._get_row(slug)
            installed_at = str(existing[4]) if existing is not None else now_iso
            self._conn.execute(
                "INSERT INTO plugins "
                "(slug, version, source, enabled, installed_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET "
                "  version = excluded.version, "
                "  source = excluded.source, "
                "  enabled = excluded.enabled, "
                "  updated_at = excluded.updated_at",
                (
                    slug,
                    version,
                    source,
                    1 if enabled else 0,
                    installed_at,
                    now_iso,
                ),
            )
            row = self._get_row(slug)
        assert row is not None  # we just inserted/updated it
        logger.info(
            "marketplace.plugin.upserted",
            slug=slug,
            version=version,
            source=source,
            enabled=enabled,
        )
        return self._row_to_plugin(row)

    def set_enabled(self, slug: str, enabled: bool) -> InstalledPluginRow:
        """Flip the ``enabled`` flag for ``slug`` and bump ``updated_at``.

        Raises :class:`PluginNotFound` when the slug isn't registered.
        """
        _validate_slug(slug)
        now_iso = _iso(_utc_now())
        with self._lock:
            row = self._get_row(slug)
            if row is None:
                raise PluginNotFound(f"plugin {slug!r} is not installed")
            self._conn.execute(
                "UPDATE plugins SET enabled = ?, updated_at = ? WHERE slug = ?",
                (1 if enabled else 0, now_iso, slug),
            )
            row = self._get_row(slug)
        assert row is not None
        logger.info("marketplace.plugin.set_enabled", slug=slug, enabled=enabled)
        return self._row_to_plugin(row)

    def delete(self, slug: str) -> bool:
        """Remove the index row for ``slug``.

        Returns ``True`` when a row was deleted, ``False`` when the slug
        wasn't registered (idempotent). Does **not** touch the on-disk
        bundle — that's the installer's job; this is purely the index.

        Raises :class:`PluginInvalid` when ``slug`` is not a single safe
        path component (defence in depth, even though delete is read-mostly).
        """
        _validate_slug(slug)
        with self._lock:
            row = self._get_row(slug)
            if row is None:
                return False
            self._conn.execute("DELETE FROM plugins WHERE slug = ?", (slug,))
        logger.info("marketplace.plugin.deleted", slug=slug)
        return True
