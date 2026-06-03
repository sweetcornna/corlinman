"""SQLite-backed registry of marketplace-installed MCP servers.

When an operator installs an MCP server from the unified marketplace
(:mod:`corlinman_server.system.marketplace`), we persist a single row
recording the server's name, its full launch spec, and provenance
(source + version) so the catalogue survives restarts and the operator
UI can list / toggle / uninstall without re-downloading.

Design choices
--------------

* **One row per server.** ``name`` is the primary key — it doubles as
  the user-facing identifier and the key the runtime uses to look up a
  launch spec.
* **Spec stored as JSON text.** The launch spec is an opaque ``dict``
  (transport, command, args, env, headers, …) that the marketplace and
  runtime agree on; the store doesn't interpret it, it just round-trips
  it through a ``TEXT`` column via :func:`json.dumps` / :func:`json.loads`.
* **Sync sqlite3, not aiosqlite.** Install / enable / uninstall are
  infrequent operator UI clicks and tiny (a single row). The sync API
  keeps callers sync — same rationale as
  :mod:`corlinman_server.profiles.store`, which this module mirrors.
* **WAL + a Python-level lock.** Mirrors the rest of the corlinman
  SQLite stores: a single ``check_same_thread=False`` connection guarded
  by a :class:`threading.Lock` so concurrent callers on the same
  connection don't trip ``database is locked`` on the read side.
* **RFC-3339 UTC ``Z`` timestamps.** ``installed_at`` / ``updated_at``
  are tz-aware UTC; the SQLite columns store ISO-8601 strings with a
  ``Z`` suffix, matching the rest of the admin wire vocabulary.

On :meth:`McpServerStore.upsert`, an *update* (the row already exists)
preserves the original ``installed_at`` and bumps ``updated_at``; an
*insert* sets both to the same wall-clock instant.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from typing import Any

import structlog

from corlinman_server.system.marketplace._sqlite_store import PersistentSqliteStore

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL: str = r"""
CREATE TABLE IF NOT EXISTS mcp_servers (
    name          TEXT PRIMARY KEY,
    spec_json     TEXT NOT NULL,
    source        TEXT,
    version       TEXT,
    enabled       INTEGER NOT NULL DEFAULT 0,
    installed_at  TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_servers_installed_at
    ON mcp_servers(installed_at);
"""

_COLUMNS: str = (
    "name, spec_json, source, version, enabled, installed_at, updated_at"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class McpStoreError(Exception):
    """Base class for MCP-server-store domain errors.

    Route layer maps these to HTTP status codes; CLI / tests pattern-match
    on the subclass.
    """


class McpServerNotFound(McpStoreError):  # noqa: N818 - public API uses domain names.
    """Raised when an operation references a name that isn't installed.
    Maps to HTTP 404."""


class McpServerInvalid(McpStoreError):  # noqa: N818 - public API uses domain names.
    """Raised when a server name fails validation (empty, or containing a
    slash or NUL). Maps to HTTP 422."""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstalledMcpServer:
    """One row from the ``mcp_servers`` table.

    Frozen + slots so callers can stash returns without worrying about
    aliasing. ``installed_at`` / ``updated_at`` are timezone-aware UTC;
    the SQLite columns store ISO-8601 strings with a ``Z`` suffix.
    """

    name: str
    spec: dict[str, Any]
    source: str | None
    version: str | None
    enabled: bool
    installed_at: _dt.datetime
    updated_at: _dt.datetime


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _utc_now() -> _dt.datetime:
    """Wall-clock UTC. Pulled out so tests can freeze time via monkeypatch
    of ``corlinman_server.system.marketplace.mcp_store._utc_now``."""
    return _dt.datetime.now(_dt.UTC)


def _iso(dt: _dt.datetime) -> str:
    """Render a tz-aware datetime as RFC-3339 / ISO-8601 with ``Z`` suffix."""
    return dt.astimezone(_dt.UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> _dt.datetime:
    """Parse the ISO-8601 ``Z`` string written by :func:`_iso` back into a
    tz-aware datetime. Accepts the legacy ``+00:00`` suffix too."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(value)


def _validate_name(name: str) -> str:
    """Return ``name`` unchanged when valid, else raise
    :class:`McpServerInvalid`.

    A valid name is a non-empty string containing neither a path
    separator (``/``) nor a NUL byte — the two characters that would let
    a name escape its own row or corrupt the SQLite text column.
    """
    if not isinstance(name, str) or not name:
        raise McpServerInvalid("MCP server name must be a non-empty string")
    if "/" in name or "\x00" in name:
        raise McpServerInvalid(
            f"MCP server name {name!r} must not contain '/' or NUL"
        )
    return name


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class McpServerStore(PersistentSqliteStore):
    """CRUD wrapper over the ``mcp_servers`` table.

    Construct with the path to the SQLite file that should hold the
    registry. The constructor opens the connection eagerly and applies
    the schema (idempotent ``CREATE TABLE IF NOT EXISTS``); the parent
    directory is created on demand. Tests can pass any writable path.

    Thread safety: holds a single :class:`sqlite3.Connection` with
    ``check_same_thread=False``. SQLite serialises writes via its own
    file lock; we add a Python-level :class:`threading.Lock` so
    concurrent callers on the same connection don't trip
    ``sqlite3.OperationalError: database is locked`` on the read side.
    """

    _SCHEMA_SQL = _SCHEMA_SQL

    # ---- internal helpers ---------------------------------------------------

    def _row_to_server(self, row: tuple[Any, ...]) -> InstalledMcpServer:
        name, spec_json, source, version, enabled, installed_at, updated_at = row
        return InstalledMcpServer(
            name=str(name),
            spec=json.loads(str(spec_json)),
            source=(str(source) if source is not None else None),
            version=(str(version) if version is not None else None),
            enabled=bool(enabled),
            installed_at=_parse_iso(str(installed_at)),
            updated_at=_parse_iso(str(updated_at)),
        )

    def _get_row(self, name: str) -> tuple[Any, ...] | None:
        cursor = self._conn.execute(
            f"SELECT {_COLUMNS} FROM mcp_servers WHERE name = ?",
            (name,),
        )
        row: tuple[Any, ...] | None = cursor.fetchone()
        return row

    # ---- CRUD ---------------------------------------------------------------

    def get(self, name: str) -> InstalledMcpServer | None:
        """Fetch one installed server by name, or ``None`` when missing.

        Raises :class:`McpServerInvalid` when ``name`` is malformed.
        """
        _validate_name(name)
        with self._lock:
            row = self._get_row(name)
        return self._row_to_server(row) if row is not None else None

    def list(self) -> list[InstalledMcpServer]:
        """All installed servers, ordered by ``installed_at ASC`` (then
        ``name`` as a stable tie-breaker)."""
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT {_COLUMNS} FROM mcp_servers "
                "ORDER BY installed_at ASC, name ASC"
            )
            rows = cursor.fetchall()
        return [self._row_to_server(r) for r in rows]

    def upsert(
        self,
        name: str,
        spec: dict[str, Any],
        *,
        source: str | None = None,
        version: str | None = None,
        enabled: bool = False,
    ) -> InstalledMcpServer:
        """Insert a new server row, or replace the spec/provenance of an
        existing one.

        On *update* (the ``name`` already exists) the original
        ``installed_at`` is preserved and ``updated_at`` is bumped to now.
        On *insert* both stamps are set to the same wall-clock instant.

        Raises :class:`McpServerInvalid` when ``name`` is malformed.
        """
        _validate_name(name)
        spec_json = json.dumps(spec)
        now = _utc_now()
        now_iso = _iso(now)

        with self._lock:
            existing = self._get_row(name)
            if existing is None:
                installed_iso = now_iso
                self._conn.execute(
                    f"INSERT INTO mcp_servers ({_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        spec_json,
                        source,
                        version,
                        int(enabled),
                        installed_iso,
                        now_iso,
                    ),
                )
            else:
                # Preserve the original installed_at (column index 5).
                installed_iso = str(existing[5])
                self._conn.execute(
                    "UPDATE mcp_servers SET "
                    "spec_json = ?, source = ?, version = ?, enabled = ?, "
                    "updated_at = ? WHERE name = ?",
                    (
                        spec_json,
                        source,
                        version,
                        int(enabled),
                        now_iso,
                        name,
                    ),
                )
            row = self._get_row(name)

        assert row is not None  # we just inserted/updated it
        logger.info(
            "mcp_store.upserted",
            name=name,
            source=source,
            version=version,
            enabled=enabled,
            inserted=existing is None,
        )
        return self._row_to_server(row)

    def set_enabled(self, name: str, enabled: bool) -> InstalledMcpServer:
        """Toggle the ``enabled`` flag and bump ``updated_at``.

        Raises :class:`McpServerNotFound` when ``name`` isn't installed,
        :class:`McpServerInvalid` when ``name`` is malformed.
        """
        _validate_name(name)
        now_iso = _iso(_utc_now())
        with self._lock:
            if self._get_row(name) is None:
                raise McpServerNotFound(
                    f"MCP server {name!r} is not installed"
                )
            self._conn.execute(
                "UPDATE mcp_servers SET enabled = ?, updated_at = ? "
                "WHERE name = ?",
                (int(enabled), now_iso, name),
            )
            row = self._get_row(name)
        assert row is not None
        logger.info("mcp_store.set_enabled", name=name, enabled=enabled)
        return self._row_to_server(row)

    def delete(self, name: str) -> bool:
        """Remove the row. Returns ``True`` on success, ``False`` when the
        name wasn't installed (idempotent).

        Raises :class:`McpServerInvalid` when ``name`` is malformed.
        """
        _validate_name(name)
        with self._lock:
            if self._get_row(name) is None:
                return False
            self._conn.execute(
                "DELETE FROM mcp_servers WHERE name = ?",
                (name,),
            )
        logger.info("mcp_store.deleted", name=name)
        return True


__all__ = [
    "InstalledMcpServer",
    "McpServerInvalid",
    "McpServerNotFound",
    "McpServerStore",
    "McpStoreError",
]
