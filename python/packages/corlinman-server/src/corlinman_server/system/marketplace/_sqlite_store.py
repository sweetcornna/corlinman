"""Shared SQLite *plumbing* base for the marketplace sync-sqlite index stores.

Both :mod:`corlinman_server.system.marketplace.plugin_store` and
:mod:`corlinman_server.system.marketplace.mcp_store` open a single
``check_same_thread=False`` sqlite3 connection guarded by a
:class:`threading.Lock`, apply the same WAL / synchronous / foreign-keys
PRAGMAs, run a per-store schema script, and expose an idempotent
:meth:`close` plus a :attr:`db_path` property. That connection lifecycle is
pure plumbing and identical between the two stores, so it lives here.

Everything domain-specific (CRUD, row mapping, validation, time helpers,
error classes, loggers) stays in each subclass. Subclasses supply their
schema by overriding the :attr:`_SCHEMA_SQL` class attribute; the base runs
it (when non-empty) at construction time.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from pathlib import Path


class PersistentSqliteStore:
    """Connection-lifecycle base for the marketplace sync-sqlite index stores.

    Opens (or creates) the SQLite file at ``db_path`` eagerly, applies the
    standard PRAGMAs, and runs :attr:`_SCHEMA_SQL` (when set). Holds a single
    :class:`sqlite3.Connection` with ``check_same_thread=False`` guarded by a
    :class:`threading.Lock`, matching the rest of corlinman's SQLite stores.

    Subclasses override :attr:`_SCHEMA_SQL` with their ``CREATE TABLE IF NOT
    EXISTS`` script and keep all domain logic (CRUD, validation, row mapping)
    to themselves.
    """

    _SCHEMA_SQL: str = ""

    def __init__(self, db_path: Path) -> None:
        """Open (or create) the index DB at ``db_path``.

        The parent directory is created on demand so callers can point at a
        not-yet-materialised data dir.
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        # WAL + foreign-keys mirrors the rest of corlinman's sqlite stores.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        if self._SCHEMA_SQL:
            self._conn.executescript(self._SCHEMA_SQL)

    def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        with self._lock, contextlib.suppress(sqlite3.Error):
            self._conn.close()

    @property
    def db_path(self) -> Path:
        """Path to the SQLite file backing the store."""
        return self._db_path
