"""Voice session persistence.

Direct Python port of
``rust/crates/corlinman-gateway/src/routes/voice/persistence.rs``.
Holds the data shapes the WebSocket session driver writes when a
session opens / closes, plus a path-resolution helper for retained
audio.

This Python iter ships:

* The :class:`VoiceEndReason` closed-set enum (mirroring the Rust
  variants exactly, including the snake_case string forms persisted
  into ``voice_sessions.end_reason``).
* The :class:`VoiceSessionStart` / :class:`VoiceSessionEnd` /
  :class:`VoiceSessionRow` dataclasses.
* The :class:`VoiceSessionStore` Protocol + two implementations: the
  in-memory :class:`MemoryVoiceSessionStore` (tests) and the
  durable :class:`SqliteVoiceSessionStore` (production), the latter
  mirroring the Rust ``SqliteVoiceSessionStore`` over an ``aiosqlite``
  connection. Both honour the same insert / update / fetch contract;
  the schema string :data:`VOICE_SCHEMA_SQL` is the shared DDL applied
  on first open so the same physical ``voice_sessions.sqlite`` file is
  interoperable across both gateways.
* :func:`audio_path_for` / :func:`tts_audio_path_for` for opt-in audio
  retention path resolution.
* :class:`VoiceTranscriptSink` Protocol + :class:`MemoryTranscriptSink`
  for the chat-session bridge that exposes voice turns to the agent
  loop.

Audio retention: default ``[voice] retain_audio = false`` means audio
is dropped at session end and ``voice_sessions.audio_path`` is NULL.
When ``retain_audio = true``, the gateway writes raw PCM-16 to
``<data_dir>/tenants/<tenant>/voice/<session_id>.pcm``.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Final, Protocol, runtime_checkable

import aiosqlite

VOICE_SCHEMA_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS voice_sessions (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    session_key     TEXT NOT NULL,
    agent_id        TEXT,
    provider_alias  TEXT NOT NULL,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    duration_secs   INTEGER,
    audio_path      TEXT,
    transcript_text TEXT,
    end_reason      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_voice_sessions_tenant_session
    ON voice_sessions(tenant_id, session_key, started_at);
"""
"""Schema applied on first open of the SQLite store. Idempotent via
``IF NOT EXISTS``. Mirrors the Rust ``VOICE_SCHEMA_SQL`` byte-for-byte
so the same physical sessions.sqlite file is interoperable across both
gateways."""


class VoiceEndReason(StrEnum):
    """Closed set of session-end reasons. The string value is what
    lands in the ``end_reason`` column; spelt out so a casual operator
    query like ``SELECT end_reason, COUNT(*) FROM voice_sessions GROUP
    BY end_reason`` shows readable buckets.
    """

    GRACEFUL = "graceful"
    BUDGET = "budget"
    MAX_SESSION = "max_session"
    PROVIDER_ERROR = "provider_error"
    CLIENT_DISCONNECT = "client_disconnect"
    START_FAILED = "start_failed"


@dataclass(frozen=True)
class VoiceSessionStart:
    """Insert-time payload — fields known when the session opens
    (before any audio flows). The row is updated in-place on session
    end with the duration / transcript / end_reason columns."""

    id: str
    tenant_id: str
    session_key: str
    agent_id: str | None
    provider_alias: str
    started_at: int  # Unix seconds


@dataclass(frozen=True)
class VoiceSessionEnd:
    """Update-time payload — fields known at session close."""

    id: str
    ended_at: int
    duration_secs: int
    audio_path: str | None
    transcript_text: str | None
    end_reason: VoiceEndReason


@dataclass(frozen=True)
class VoiceSessionRow:
    """Read shape — used by tests + (later) the admin UI's
    voice-session-history view. Keeps the column → field mapping in
    one place."""

    id: str
    tenant_id: str
    session_key: str
    agent_id: str | None
    provider_alias: str
    started_at: int
    ended_at: int | None
    duration_secs: int | None
    audio_path: str | None
    transcript_text: str | None
    end_reason: str


class VoiceStoreError(Exception):
    """Errors raised by :class:`VoiceSessionStore` implementations.

    The Rust enum splits these into ``Sql`` / ``RowMissing``; the
    Python side uses subclasses so callers can ``except RowMissing``.
    """

    __slots__ = ()


class VoiceStoreSqlError(VoiceStoreError):
    """Underlying SQL error."""


class VoiceStoreRowMissingError(VoiceStoreError):
    """Update target row not found — defends against
    double-finalisation."""

    __slots__ = ("row_id",)

    def __init__(self, row_id: str) -> None:
        super().__init__(f"voice store row missing: {row_id}")
        self.row_id = row_id


@runtime_checkable
class VoiceSessionStore(Protocol):
    """Trait surface so tests can drive a pure in-memory store and
    production uses the SQLite-backed adapter (TODO; see file
    docstring).
    """

    async def record_start(self, start: VoiceSessionStart) -> None: ...

    async def record_end(self, end: VoiceSessionEnd) -> None: ...

    async def fetch(self, id: str) -> VoiceSessionRow | None: ...

    async def list_for_session(
        self, tenant_id: str, session_key: str
    ) -> list[VoiceSessionRow]: ...


class MemoryVoiceSessionStore:
    """Pure in-memory :class:`VoiceSessionStore` for tests. Honours the
    same insert / update / fetch contract as the SQLite adapter."""

    def __init__(self) -> None:
        self._rows: dict[str, VoiceSessionRow] = {}
        self._lock = asyncio.Lock()

    async def record_start(self, start: VoiceSessionStart) -> None:
        async with self._lock:
            self._rows[start.id] = VoiceSessionRow(
                id=start.id,
                tenant_id=start.tenant_id,
                session_key=start.session_key,
                agent_id=start.agent_id,
                provider_alias=start.provider_alias,
                started_at=start.started_at,
                ended_at=None,
                duration_secs=None,
                audio_path=None,
                transcript_text=None,
                # Placeholder; overwritten by record_end. Using
                # "graceful" as the default so a row that's never
                # finalised (gateway crash) still has a valid
                # end_reason.
                end_reason=VoiceEndReason.GRACEFUL.value,
            )

    async def record_end(self, end: VoiceSessionEnd) -> None:
        async with self._lock:
            existing = self._rows.get(end.id)
            if existing is None:
                raise VoiceStoreRowMissingError(end.id)
            self._rows[end.id] = replace(
                existing,
                ended_at=end.ended_at,
                duration_secs=end.duration_secs,
                audio_path=end.audio_path,
                transcript_text=end.transcript_text,
                end_reason=end.end_reason.value,
            )

    async def fetch(self, id: str) -> VoiceSessionRow | None:
        async with self._lock:
            return self._rows.get(id)

    async def list_for_session(
        self, tenant_id: str, session_key: str
    ) -> list[VoiceSessionRow]:
        async with self._lock:
            matching = [
                row
                for row in self._rows.values()
                if row.tenant_id == tenant_id and row.session_key == session_key
            ]
        # Most-recent first to mirror the SQLite `ORDER BY started_at
        # DESC` semantics.
        matching.sort(key=lambda r: r.started_at, reverse=True)
        return matching


VOICE_SESSIONS_DB_FILENAME: Final[str] = "voice_sessions.sqlite"
"""Filename of the SQLite database under the gateway data dir. The store
opens ``<data_dir>/voice_sessions.sqlite`` — a sibling of the other
per-gateway stores (``agent_journal.sqlite`` / ``home_channels.sqlite``)."""


# Read-shape column order shared by every SELECT so the row projection in
# :meth:`SqliteVoiceSessionStore._row_from_record` stays a single source
# of truth. Matches the :class:`VoiceSessionRow` field order 1:1.
_VOICE_ROW_COLUMNS: Final[str] = (
    "id, tenant_id, session_key, agent_id, provider_alias, started_at, "
    "ended_at, duration_secs, audio_path, transcript_text, end_reason"
)


class SqliteVoiceSessionStore:
    """Durable :class:`VoiceSessionStore` over a dedicated ``aiosqlite``
    connection at ``<data_dir>/voice_sessions.sqlite``.

    This is the production swap for :class:`MemoryVoiceSessionStore`;
    it honours the same insert / update / fetch contract so
    :func:`~corlinman_server.gateway.routes_voice.mod.run_voice_session`
    can write a ``voice_sessions`` row on every connect without caring
    which backing store is wired.

    Concurrency (R5-B3 lesson)
    --------------------------
    The agent-journal backend learned the hard way that sharing ONE
    ``aiosqlite`` connection across concurrent sessions while mixing
    autocommit ``execute()`` + ``commit()`` against explicit
    ``BEGIN IMMEDIATE`` envelopes lets a bare ``commit()`` from one
    coroutine flush — and end — another coroutine's open transaction
    (``commit()`` is connection-*global*). This store sidesteps that
    entirely:

    * It owns its OWN dedicated connection (not shared with any other
      store), and
    * every write (``record_start`` / ``record_end``) is a single
      ``execute()`` + ``commit()`` guarded by an :class:`asyncio.Lock`,
      so no two writes can interleave a transaction on the connection.

    Reads (``fetch`` / ``list_for_session``) are left lock-free — they
    issue no ``commit()`` and so can't disturb an in-flight write.
    Crash-safety: each record commits before returning, so a gateway
    crash never loses an already-acknowledged row.
    """

    __slots__ = ("_conn", "_path", "_write_lock")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        # One lock guarding ALL writes on this store's dedicated
        # connection. See the class docstring (R5-B3): a single
        # connection + one write lock means no bare commit() can flush a
        # concurrent write's transaction. Reads stay lock-free.
        self._write_lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> SqliteVoiceSessionStore:
        """Open (and bootstrap) the store at ``path``.

        Mirrors :meth:`SqliteJournalBackend.open`: construct, then run
        the idempotent ``_open`` that creates the parent dir, applies the
        WAL / busy-timeout pragmas, and runs :data:`VOICE_SCHEMA_SQL`
        (``CREATE TABLE IF NOT EXISTS``) so re-opening an existing file is
        a no-op.
        """
        store = cls(path)
        await store._open()
        return store

    @classmethod
    async def open_under_data_dir(
        cls, data_dir: Path | str
    ) -> SqliteVoiceSessionStore:
        """Open the canonical ``<data_dir>/voice_sessions.sqlite`` store."""
        return await cls.open(Path(data_dir) / VOICE_SESSIONS_DB_FILENAME)

    async def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        try:
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.executescript(VOICE_SCHEMA_SQL)
            await conn.commit()
        except Exception:
            await conn.close()
            raise
        self._conn = conn

    async def close(self) -> None:
        """Release the underlying connection. Idempotent."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def path(self) -> Path:
        """Filesystem path of the backing SQLite file (test/debug only)."""
        return self._path

    @property
    def _c(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "SqliteVoiceSessionStore not opened — call open() first"
            )
        return self._conn

    async def record_start(self, start: VoiceSessionStart) -> None:
        """Insert the session-start row.

        The ``end_reason`` placeholder is ``graceful`` (matching
        :class:`MemoryVoiceSessionStore`) so a row that's never finalised
        — gateway crash between start and end — still carries a valid
        ``NOT NULL`` ``end_reason``. ``INSERT OR REPLACE`` keeps the open
        idempotent against a re-used session id.
        """
        async with self._write_lock:
            try:
                await self._c.execute(
                    "INSERT OR REPLACE INTO voice_sessions "
                    "(id, tenant_id, session_key, agent_id, provider_alias, "
                    "started_at, ended_at, duration_secs, audio_path, "
                    "transcript_text, end_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)",
                    (
                        start.id,
                        start.tenant_id,
                        start.session_key,
                        start.agent_id,
                        start.provider_alias,
                        start.started_at,
                        VoiceEndReason.GRACEFUL.value,
                    ),
                )
                await self._c.commit()
            except aiosqlite.Error as exc:  # pragma: no cover — defensive
                raise VoiceStoreSqlError(str(exc)) from exc

    async def record_end(self, end: VoiceSessionEnd) -> None:
        """UPDATE the start row in place with the session-end fields.

        Guard: if no row with ``end.id`` exists, raise
        :class:`VoiceStoreRowMissingError` — identical behaviour to
        :class:`MemoryVoiceSessionStore`, defending against
        double-finalisation / finalising a session that never started.
        """
        async with self._write_lock:
            try:
                cur = await self._c.execute(
                    "UPDATE voice_sessions SET "
                    "ended_at = ?, duration_secs = ?, audio_path = ?, "
                    "transcript_text = ?, end_reason = ? WHERE id = ?",
                    (
                        end.ended_at,
                        end.duration_secs,
                        end.audio_path,
                        end.transcript_text,
                        end.end_reason.value,
                        end.id,
                    ),
                )
                if cur.rowcount == 0:
                    # No row matched — roll back the (empty) UPDATE so the
                    # connection is left clean, then signal the miss.
                    await self._c.rollback()
                    raise VoiceStoreRowMissingError(end.id)
                await self._c.commit()
            except aiosqlite.Error as exc:  # pragma: no cover — defensive
                raise VoiceStoreSqlError(str(exc)) from exc

    async def fetch(self, id: str) -> VoiceSessionRow | None:
        """Read one row by id, or ``None`` when absent. Lock-free read."""
        cur = await self._c.execute(
            f"SELECT {_VOICE_ROW_COLUMNS} FROM voice_sessions WHERE id = ?",
            (id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return self._row_from_record(row)

    async def list_for_session(
        self, tenant_id: str, session_key: str
    ) -> list[VoiceSessionRow]:
        """Every row for a (tenant, session_key), most-recent first.

        ``ORDER BY started_at DESC`` matches the in-memory store's sort
        so callers see the same ordering regardless of backend.
        """
        cur = await self._c.execute(
            f"SELECT {_VOICE_ROW_COLUMNS} FROM voice_sessions "
            "WHERE tenant_id = ? AND session_key = ? "
            "ORDER BY started_at DESC",
            (tenant_id, session_key),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._row_from_record(r) for r in rows]

    @staticmethod
    def _row_from_record(record: Any) -> VoiceSessionRow:
        """Project a raw SQLite tuple (in :data:`_VOICE_ROW_COLUMNS`
        order) onto a :class:`VoiceSessionRow`."""
        return VoiceSessionRow(
            id=str(record[0]),
            tenant_id=str(record[1]),
            session_key=str(record[2]),
            agent_id=None if record[3] is None else str(record[3]),
            provider_alias=str(record[4]),
            started_at=int(record[5]),
            ended_at=None if record[6] is None else int(record[6]),
            duration_secs=None if record[7] is None else int(record[7]),
            audio_path=None if record[8] is None else str(record[8]),
            transcript_text=None if record[9] is None else str(record[9]),
            end_reason=str(record[10]),
        )


# ---------------------------------------------------------------------------
# Audio retention path helpers — pure, no I/O
# ---------------------------------------------------------------------------


class VoicePathError(ValueError):
    """Raised when a ``tenant_id`` / ``session_id`` carries a path
    component that would escape ``data_dir/tenants`` (``..`` / a path
    separator / an absolute or drive-anchored value). The audio-path
    helpers reject rather than silently slugify so two distinct unsafe
    inputs can never collapse onto the same on-disk path."""


def _validate_path_segment(name: str, value: str) -> str:
    """Validate a single user-supplied path segment.

    Rejects anything that could traverse out of the per-tenant tree:
    empty / whitespace-only values, ``.`` / ``..``, path separators
    (POSIX *or* Windows), and absolute / drive-anchored values. Returns
    the value unchanged on success so the resolved path is guaranteed to
    stay one directory deep under ``data_dir/tenants``.
    """
    if not value or not value.strip():
        raise VoicePathError(f"voice {name} must be a non-empty path segment")
    if value in (".", ".."):
        raise VoicePathError(f"voice {name} {value!r} is a path-traversal segment")
    # Reject every separator flavour plus the NUL byte regardless of host
    # OS — the on-disk layout must be identical across platforms and a
    # backslash is a separator on Windows.
    if any(sep and sep in value for sep in (os.sep, os.altsep, "/", "\\", "\x00")):
        raise VoicePathError(
            f"voice {name} {value!r} contains a path separator"
        )
    # Defence in depth: a single segment must not parse as anything with
    # parents / a drive / a root on either path flavour.
    for flavour in (PurePosixPath, PureWindowsPath):
        parsed = flavour(value)
        if parsed.is_absolute() or parsed.drive or parsed.root or len(parsed.parts) != 1:
            raise VoicePathError(
                f"voice {name} {value!r} is not a single path segment"
            )
    return value


def audio_path_for(data_dir: Path | str, tenant_id: str, session_id: str) -> Path:
    """Per-session inbound PCM-16 path under the per-tenant tree.

    Pure: returns a :class:`Path`. The caller (audio writer or
    retention sweeper) is responsible for ``mkdir`` and per-session
    file handles.

    ``tenant_id`` / ``session_id`` are validated as single path segments
    (see :func:`_validate_path_segment`) so a traversal value like
    ``../../etc`` can never escape ``data_dir/tenants``. Raises
    :class:`VoicePathError` on an unsafe segment.
    """
    tenant_id = _validate_path_segment("tenant_id", tenant_id)
    session_id = _validate_path_segment("session_id", session_id)
    return Path(data_dir) / "tenants" / tenant_id / "voice" / f"{session_id}.pcm"


def tts_audio_path_for(
    data_dir: Path | str, tenant_id: str, session_id: str
) -> Path:
    """TTS sibling path for retained assistant audio. Lives next to
    the inbound PCM under the same per-tenant tree so the retention
    sweeper can match both with one glob.

    Validates ``tenant_id`` / ``session_id`` identically to
    :func:`audio_path_for`; raises :class:`VoicePathError` on traversal.
    """
    tenant_id = _validate_path_segment("tenant_id", tenant_id)
    session_id = _validate_path_segment("session_id", session_id)
    return Path(data_dir) / "tenants" / tenant_id / "voice" / f"{session_id}.tts.pcm"


# ---------------------------------------------------------------------------
# Transcript bridge — voice turns → chat sessions table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptedTurn:
    """One voice turn the bridge has appended to a chat session.
    Surfaces what the in-memory sink captured so tests can assert
    ordering / content."""

    tenant_id: str
    session_key: str
    role: str
    text: str


@runtime_checkable
class VoiceTranscriptSink(Protocol):
    """Trait so a route-handler-side bridge (the real Python
    ``SessionStore`` adapter) can be wired without the voice route
    importing the agent loop. Tests use :class:`MemoryTranscriptSink`.
    """

    async def append_turn(
        self,
        tenant_id: str,
        session_key: str,
        role: str,
        text: str,
    ) -> None: ...


class MemoryTranscriptSink:
    """In-memory :class:`VoiceTranscriptSink` for tests + a default
    no-op deployment path while the production wiring lands.
    """

    def __init__(self) -> None:
        self._turns: list[TranscriptedTurn] = []
        self._lock = asyncio.Lock()

    async def append_turn(
        self,
        tenant_id: str,
        session_key: str,
        role: str,
        text: str,
    ) -> None:
        async with self._lock:
            self._turns.append(
                TranscriptedTurn(
                    tenant_id=tenant_id,
                    session_key=session_key,
                    role=role,
                    text=text,
                )
            )

    async def snapshot(self) -> list[TranscriptedTurn]:
        """Cloned snapshot of the appended turns. Cloned out so the
        caller doesn't hold a lock across awaits."""
        async with self._lock:
            return list(self._turns)


__all__ = [
    "VOICE_SCHEMA_SQL",
    "VOICE_SESSIONS_DB_FILENAME",
    "MemoryTranscriptSink",
    "MemoryVoiceSessionStore",
    "SqliteVoiceSessionStore",
    "TranscriptedTurn",
    "VoiceEndReason",
    "VoicePathError",
    "VoiceSessionEnd",
    "VoiceSessionRow",
    "VoiceSessionStart",
    "VoiceSessionStore",
    "VoiceStoreError",
    "VoiceStoreRowMissingError",
    "VoiceStoreSqlError",
    "VoiceTranscriptSink",
    "audio_path_for",
    "tts_audio_path_for",
]
