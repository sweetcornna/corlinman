"""Storage backends for :class:`corlinman_server.agent_journal.AgentJournal`.

The journal historically lived as a single concrete class talking to an
``aiosqlite`` connection at ``<data_dir>/agent_journal.sqlite``. To unlock
multi-gateway HA (where two ``corlinman-server`` processes share a
journal so per-turn resume survives a single gateway dying), this module
introduces a backend Protocol that the public ``AgentJournal`` facade
delegates to.

Concrete backends:

- :class:`SqliteJournalBackend` — current behavior, single-process file.
  Default. No deployment change, no migration risk.
- :class:`~corlinman_server.agent_journal_postgres.PostgresJournalBackend`
  — multi-gateway HA. Lets N gateways behind a load balancer share one
  journal via ``CORLINMAN_JOURNAL_POSTGRES_DSN``. Lives in
  ``agent_journal_postgres.py`` so the asyncpg import stays optional.
- :class:`RedisJournalBackend` — stub, raises ``NotImplementedError``.
  Lower-latency alternative for ephemeral resume state via
  ``CORLINMAN_JOURNAL_REDIS_URL``.

Selection happens in :meth:`AgentJournal.open_from_env`; the
``CORLINMAN_JOURNAL_BACKEND`` env var picks one (``sqlite`` by default).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import aiosqlite
import structlog

if TYPE_CHECKING:
    # Imported only for static type-checkers so the symbol exists in
    # ``__all__`` without forcing the real (optional, asyncpg-bearing)
    # module to load on every gateway boot. The runtime path goes
    # through ``__getattr__`` below.
    from corlinman_server.agent_journal_postgres import (
        PostgresJournalBackend as PostgresJournalBackend,
    )

logger = structlog.get_logger(__name__)


# Status enum for the ``turns`` table.
TURN_IN_PROGRESS = "in_progress"
TURN_COMPLETED = "completed"
TURN_ERRORED = "errored"

# A turn is "fresh" for resume purposes only if it started within the
# last 5 minutes. Older interrupted turns are abandoned — the user has
# moved on; treat the new message as a new task.
RESUME_MAX_AGE_MS = 5 * 60 * 1000

# Env var contract — keep these names stable; ops/runbooks reference them.
ENV_BACKEND = "CORLINMAN_JOURNAL_BACKEND"
ENV_POSTGRES_DSN = "CORLINMAN_JOURNAL_POSTGRES_DSN"
ENV_REDIS_URL = "CORLINMAN_JOURNAL_REDIS_URL"


@dataclass(frozen=True)
class ResumeData:
    """The bits the chat handler needs to resume an interrupted turn."""

    turn_id: int
    started_at_ms: int
    messages: list[dict[str, Any]]
    """Replay buffer: user + assistant(tool_calls) + tool(result) rows
    in the order they landed, ready to be prepended to ``start.messages``.
    The user turn that *started* the interrupted work is included; the
    caller should NOT also re-append the freshly-arrived user message
    when this is non-None — the resume IS the continuation."""


@runtime_checkable
class JournalBackend(Protocol):
    """The contract every storage backend must satisfy.

    Mirrors the public surface of the original ``AgentJournal``. Every
    method is async because all real backends (sqlite, postgres, redis)
    talk over async I/O.

    Lifecycle: ``open()`` (or backend-specific factory) → many calls →
    ``close()``. The journal facade owns lifecycle; backends should not
    self-open in ``__init__``.
    """

    async def close(self) -> None:
        """Release any underlying connections. Idempotent."""
        ...

    async def begin_turn(
        self,
        session_key: str,
        user_text: str,
        *,
        user_id: str | None = None,
    ) -> int | None:
        """Insert an in-progress row; return the new turn_id.

        ``user_id`` (S4) scopes the row to a specific channel sender so a
        group-chat replay attack — Mallory parroting Alice's user_text on
        the same session_key — can't pick up Alice's in-progress turn.
        ``None`` keeps the legacy "no sender" semantics for HTTP callers.

        May return ``None`` when a concurrent ``begin_turn`` for the same
        (session_key, user_text, user_id) already opened a row — the
        caller should re-run ``find_resumable_turn`` to grab the winner.
        Backends that cannot detect this race return the new id unchanged.
        """
        ...

    async def complete_turn(self, turn_id: int) -> None:
        """Stamp ``turn_id`` as completed if still in_progress."""
        ...

    async def error_turn(self, turn_id: int, error: str) -> None:
        """Stamp ``turn_id`` as errored with a truncated message."""
        ...

    async def append_message(
        self,
        turn_id: int,
        role: str,
        content: str,
        *,
        tool_call_id: str | None = None,
        tool_calls: Any | None = None,
    ) -> None:
        """Append a single message to the turn's replay buffer."""
        ...

    async def find_resumable_turn(
        self,
        session_key: str,
        user_text: str,
        *,
        user_id: str | None = None,
    ) -> ResumeData | None:
        """Return the most-recent in-progress turn matching session+text.

        ``user_id`` (S4): when set, only rows journaled under the same
        ``user_id`` are considered — Mallory cannot resume Alice's turn
        in a group chat by replaying her text. ``None`` falls back to
        the legacy user_text-only match for backwards-compatible HTTP
        callers that don't carry a channel sender.
        """
        ...

    async def recent_errored_turns(
        self, session_key: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Return the most recent errored turns for diagnostics."""
        ...

    async def mark_stale_in_progress_as_errored(self) -> int:
        """Sweep abandoned in-progress turns past the resume window."""
        ...

    async def load_messages(self, turn_id: int) -> list[dict[str, Any]]:
        """Load every message under ``turn_id`` in seq order.

        Public on the backend so it can be tested in isolation; callers
        normally read ``ResumeData.messages``.
        """
        ...


# ---------------------------------------------------------------------------
# SQLite backend — the default, drop-in replacement for the original impl.
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    turn_id        INTEGER PRIMARY KEY,
    session_key    TEXT    NOT NULL,
    status         TEXT    NOT NULL
                          CHECK (status IN ('in_progress', 'completed', 'errored')),
    started_at_ms  INTEGER NOT NULL,
    ended_at_ms    INTEGER,
    user_text      TEXT,
    user_id        TEXT,
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_turns_session_status
    ON turns(session_key, status, started_at_ms);

CREATE INDEX IF NOT EXISTS idx_turns_session_started
    ON turns(session_key, started_at_ms DESC);

CREATE TABLE IF NOT EXISTS turn_messages (
    turn_id          INTEGER NOT NULL,
    seq              INTEGER NOT NULL,
    role             TEXT    NOT NULL,
    content          TEXT    NOT NULL,
    tool_call_id     TEXT,
    tool_calls_json  TEXT,
    PRIMARY KEY (turn_id, seq),
    FOREIGN KEY (turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);
"""


# Pre-S4 deployments have a ``turns`` table without ``user_id``. The
# additive ``ALTER TABLE`` is idempotent under our gate (we only fire
# it when the column is absent) so re-running on a fresh DB is a no-op.
_USER_ID_MIGRATION = "ALTER TABLE turns ADD COLUMN user_id TEXT"


class SqliteJournalBackend:
    """Single-process SQLite backend over ``aiosqlite``.

    Schema is auto-created on open. WAL mode + ``synchronous = NORMAL``
    so concurrent sessions read+write without serializing on the writer.
    """

    __slots__ = ("_path", "_conn")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open(cls, path: Path) -> SqliteJournalBackend:
        backend = cls(path)
        await backend._open()
        return backend

    async def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA busy_timeout = 5000")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        # S4: additive ``user_id TEXT`` migration on pre-existing
        # journals. ``PRAGMA table_info`` is the documented way to
        # check column presence in SQLite — adding the column only
        # when missing keeps the open path idempotent.
        try:
            cur = await conn.execute("PRAGMA table_info(turns)")
            rows = await cur.fetchall()
            await cur.close()
            existing = {str(r[1]) for r in rows}
            if "user_id" not in existing:
                await conn.execute(_USER_ID_MIGRATION)
                await conn.commit()
                logger.info("agent.journal.migrated", migration="user_id_column")
        except aiosqlite.Error as exc:  # pragma: no cover — defensive
            logger.warning("agent.journal.migrate_failed", error=str(exc))
        self._conn = conn

    async def close(self) -> None:
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
                "SqliteJournalBackend not opened — call open() first"
            )
        return self._conn

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    async def begin_turn(
        self,
        session_key: str,
        user_text: str,
        *,
        user_id: str | None = None,
    ) -> int | None:
        """Insert an in-progress row; return the new ``turn_id``.

        ``turn_id`` is wall-clock ms — uniqueness across one process is
        good enough for a chat-turn store. Two inserts in the same ms
        collide on the PK; we retry with ms+1 on
        :class:`aiosqlite.IntegrityError`.

        L5: the retry only catches ``IntegrityError`` — narrowed from
        the previous broad ``aiosqlite.Error`` so a corrupted DB or I/O
        error surfaces as a real exception instead of looping silently.

        S4 — ``user_id`` is stored so a later
        ``find_resumable_turn`` can scope its match by sender. ``None``
        keeps the legacy behaviour for HTTP-only callers (the column
        stays NULL).
        """
        conn = self._c
        ts = int(time.time() * 1000)
        for offset in range(0, 20):
            tid = ts + offset
            try:
                await conn.execute(
                    "INSERT INTO turns (turn_id, session_key, status, "
                    "started_at_ms, user_text, user_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        tid,
                        session_key or "",
                        TURN_IN_PROGRESS,
                        ts,
                        user_text,
                        user_id,
                    ),
                )
                await conn.commit()
                return tid
            except aiosqlite.IntegrityError:
                # PK collision — the next ms is almost certainly free.
                # ``commit`` is not reached on the failed INSERT, so
                # sqlite auto-aborts the statement and no rollback is
                # needed.
                continue
        # Vanishingly unlikely; fall through with a tagged turn_id.
        return ts

    async def complete_turn(self, turn_id: int) -> None:
        try:
            await self._c.execute(
                "UPDATE turns SET status = ?, ended_at_ms = ? "
                "WHERE turn_id = ? AND status = ?",
                (TURN_COMPLETED, int(time.time() * 1000), turn_id, TURN_IN_PROGRESS),
            )
            await self._c.commit()
        except aiosqlite.Error as exc:
            logger.warning("agent.journal.complete_failed", error=str(exc))

    async def error_turn(self, turn_id: int, error: str) -> None:
        try:
            await self._c.execute(
                "UPDATE turns SET status = ?, ended_at_ms = ?, error = ? "
                "WHERE turn_id = ? AND status = ?",
                (
                    TURN_ERRORED,
                    int(time.time() * 1000),
                    error[:1000],
                    turn_id,
                    TURN_IN_PROGRESS,
                ),
            )
            await self._c.commit()
        except aiosqlite.Error as exc:
            logger.warning("agent.journal.error_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Message append
    # ------------------------------------------------------------------

    async def append_message(
        self,
        turn_id: int,
        role: str,
        content: str,
        *,
        tool_call_id: str | None = None,
        tool_calls: Any | None = None,
    ) -> None:
        """Append one message to the turn.

        ``seq`` is computed inside a ``BEGIN IMMEDIATE`` / ``COMMIT``
        envelope so a SELECT-then-INSERT pair on the same turn_id can't
        observe a stale ``MAX(seq)``.

        L5: the prior implementation caught the broad ``aiosqlite.Error``
        on every step and issued a blind ``ROLLBACK`` — which raised a
        secondary "no transaction is active" error when the failure
        happened pre-BEGIN, was then swallowed by the inner
        ``except aiosqlite.Error: pass``, and left the connection in
        an undefined state where subsequent ``complete_turn`` writes
        silently no-op'd. The fix:

        - rely on ``conn.in_transaction`` instead of blindly emitting
          ``ROLLBACK`` (sqlite raises if there is no live tx);
        - keep the outer catch as the diagnostic boundary, but never
          mask its inner rollback failure.
        """
        conn = self._c
        tool_calls_text: str | None = None
        if tool_calls is not None:
            try:
                tool_calls_text = json.dumps(tool_calls)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "agent.journal.append_serialize_failed", error=str(exc)
                )
                return
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM turn_messages "
                "WHERE turn_id = ?",
                (turn_id,),
            )
            row = await cur.fetchone()
            await cur.close()
            next_seq = int(row[0]) if row is not None else 0
            await conn.execute(
                "INSERT INTO turn_messages (turn_id, seq, role, content, "
                "tool_call_id, tool_calls_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    turn_id,
                    next_seq,
                    role,
                    content,
                    tool_call_id,
                    tool_calls_text,
                ),
            )
            await conn.commit()
        except aiosqlite.Error as exc:
            logger.warning("agent.journal.append_failed", error=str(exc))
            # L5: only roll back when sqlite still has the tx open;
            # blindly issuing ROLLBACK on a connection that already
            # auto-aborted raises and corrupts subsequent writes.
            if conn.in_transaction:
                try:
                    await conn.rollback()
                except aiosqlite.Error as rb_exc:
                    logger.warning(
                        "agent.journal.append_rollback_failed",
                        error=str(rb_exc),
                    )

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------

    async def find_resumable_turn(
        self,
        session_key: str,
        user_text: str,
        *,
        user_id: str | None = None,
    ) -> ResumeData | None:
        """Return the most-recent in-progress turn for ``session_key``
        whose ``user_text`` matches and that is younger than the resume
        window. The caller decides whether to actually resume — this
        method only finds the candidate.

        S4 — when ``user_id`` is non-None, the candidate row's
        ``user_id`` must match exactly (or be NULL for legacy rows
        journaled before the scoping was added). ``user_id=None``
        preserves the legacy user_text-only match for HTTP callers.
        """
        if not session_key or not user_text:
            return None
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - RESUME_MAX_AGE_MS
        try:
            if user_id is not None:
                # S4: only resume turns started by the same sender, but
                # tolerate NULL (rows from before the user_id column
                # existed, or HTTP turns that began without a sender).
                cur = await self._c.execute(
                    "SELECT turn_id, started_at_ms FROM turns "
                    "WHERE session_key = ? AND status = ? AND user_text = ? "
                    "AND started_at_ms >= ? "
                    "AND (user_id = ? OR user_id IS NULL) "
                    "ORDER BY started_at_ms DESC LIMIT 1",
                    (
                        session_key,
                        TURN_IN_PROGRESS,
                        user_text,
                        cutoff,
                        user_id,
                    ),
                )
            else:
                cur = await self._c.execute(
                    "SELECT turn_id, started_at_ms FROM turns "
                    "WHERE session_key = ? AND status = ? AND user_text = ? "
                    "AND started_at_ms >= ? "
                    "ORDER BY started_at_ms DESC LIMIT 1",
                    (session_key, TURN_IN_PROGRESS, user_text, cutoff),
                )
            row = await cur.fetchone()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("agent.journal.find_resumable_failed", error=str(exc))
            return None
        if row is None:
            return None
        turn_id = int(row[0])
        started_at_ms = int(row[1])
        messages = await self.load_messages(turn_id)
        return ResumeData(
            turn_id=turn_id,
            started_at_ms=started_at_ms,
            messages=messages,
        )

    async def load_messages(self, turn_id: int) -> list[dict[str, Any]]:
        """Load every message stored under ``turn_id`` in seq order.

        Reconstructs the canonical chat-shape dicts the reasoning loop
        expects: ``{"role": ..., "content": ..., ...}`` with
        ``tool_calls`` re-deserialized as a list, ``tool_call_id``
        present on tool rows.
        """
        try:
            cur = await self._c.execute(
                "SELECT seq, role, content, tool_call_id, tool_calls_json "
                "FROM turn_messages WHERE turn_id = ? ORDER BY seq ASC",
                (turn_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("agent.journal.load_messages_failed", error=str(exc))
            return []
        out: list[dict[str, Any]] = []
        for _, role, content, tool_call_id, tool_calls_json in rows:
            msg: dict[str, Any] = {"role": role, "content": content}
            if tool_call_id is not None:
                msg["tool_call_id"] = tool_call_id
            if tool_calls_json is not None:
                try:
                    msg["tool_calls"] = json.loads(tool_calls_json)
                except json.JSONDecodeError:
                    pass
            out.append(msg)
        return out

    # ------------------------------------------------------------------
    # T4.4 — Error breadcrumbs
    # ------------------------------------------------------------------

    async def recent_errored_turns(
        self, session_key: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Return the most recent errored turns for ``session_key`` so an
        operator (or a future self-heal hook) can see what failed."""
        try:
            cur = await self._c.execute(
                "SELECT turn_id, started_at_ms, ended_at_ms, user_text, error "
                "FROM turns WHERE session_key = ? AND status = ? "
                "ORDER BY started_at_ms DESC LIMIT ?",
                (session_key, TURN_ERRORED, max(1, int(limit))),
            )
            rows = await cur.fetchall()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning(
                "agent.journal.recent_errored_failed", error=str(exc)
            )
            return []
        return [
            {
                "turn_id": int(r[0]),
                "started_at_ms": int(r[1]),
                "ended_at_ms": int(r[2]) if r[2] is not None else None,
                "user_text": r[3],
                "error": r[4],
            }
            for r in rows
        ]

    async def mark_stale_in_progress_as_errored(self) -> int:
        """Sweep stale in-progress turns (older than the resume window)
        and stamp them errored — called once on gateway boot so a
        previously-crashed process doesn't leave the table littered
        with phantom in-progress rows. Returns the count flipped."""
        cutoff = int(time.time() * 1000) - RESUME_MAX_AGE_MS
        try:
            cur = await self._c.execute(
                "UPDATE turns SET status = ?, ended_at_ms = ?, "
                "error = COALESCE(error, ?) "
                "WHERE status = ? AND started_at_ms < ?",
                (
                    TURN_ERRORED,
                    int(time.time() * 1000),
                    "abandoned: gateway restart left turn in_progress",
                    TURN_IN_PROGRESS,
                    cutoff,
                ),
            )
            await self._c.commit()
            n = cur.rowcount or 0
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("agent.journal.sweep_failed", error=str(exc))
            return 0
        if n:
            logger.info("agent.journal.swept_stale", count=n)
        return int(n)


# ---------------------------------------------------------------------------
# Postgres backend lives in ``agent_journal_postgres.py`` so the optional
# asyncpg dependency stays out of the import path until the env actually
# selects it. We re-export the class here for back-compat with callers
# that historically imported ``PostgresJournalBackend`` from this module.
# ---------------------------------------------------------------------------


def _load_postgres_backend_cls() -> type[Any]:
    """Lazy importer for :class:`PostgresJournalBackend`.

    Centralised so the env dispatcher and the module-level re-export use
    exactly the same code path. Raises a friendly ``RuntimeError`` if
    asyncpg is missing.
    """
    try:
        from corlinman_server.agent_journal_postgres import (
            PostgresJournalBackend as _Postgres,
        )
    except ImportError as exc:  # pragma: no cover — defensive
        raise RuntimeError(
            "postgres backend selected but asyncpg is not installed; "
            "pip install corlinman-server[postgres]"
        ) from exc
    return _Postgres


def __getattr__(name: str) -> Any:
    """Module-level lazy attribute hook.

    Keeps ``from corlinman_server.agent_journal_backend import
    PostgresJournalBackend`` working without forcing the asyncpg import
    at module load time. Anything else still raises AttributeError as
    usual.
    """
    if name == "PostgresJournalBackend":
        return _load_postgres_backend_cls()
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}"
    )


# ---------------------------------------------------------------------------
# Stubs for future HA backends — intentionally non-functional so a
# misconfigured deployment fails loudly (NotImplementedError) instead of
# silently falling back to a local file.
# ---------------------------------------------------------------------------


class RedisJournalBackend:
    """Stub. A future implementation will use Redis hashes + sorted sets
    for low-latency resume state shared across gateways. Until then this
    class refuses to open so ops can't accidentally rely on it.
    """

    def __init__(self, url: str) -> None:
        self._url = url

    @classmethod
    async def open(cls, url: str) -> RedisJournalBackend:
        raise NotImplementedError(
            "redis journal backend not yet implemented; "
            "set CORLINMAN_JOURNAL_BACKEND=sqlite (the default) "
            "or track the HA journal issue"
        )


# ---------------------------------------------------------------------------
# Backend selector — used by AgentJournal.open_from_env().
# ---------------------------------------------------------------------------


async def open_backend_from_env(
    sqlite_path: Path,
    env: dict[str, str] | None = None,
) -> JournalBackend:
    """Pick a backend based on ``CORLINMAN_JOURNAL_BACKEND``.

    Defaults to SQLite at ``sqlite_path`` so existing deployments need
    no env-var change. ``env`` is injectable for tests; production
    callers pass ``None`` (reads ``os.environ``).

    The ``postgres`` backend is implemented in
    :mod:`corlinman_server.agent_journal_postgres`; the ``redis`` backend
    is still a stub that raises ``NotImplementedError`` — that's
    intentional, so a misconfigured deployment fails loudly at startup
    rather than silently writing to a local file that other gateways
    can't read.
    """
    e = env if env is not None else os.environ
    kind = (e.get(ENV_BACKEND) or "sqlite").strip().lower()

    if kind in ("", "sqlite"):
        return await SqliteJournalBackend.open(sqlite_path)
    if kind == "postgres":
        dsn = e.get(ENV_POSTGRES_DSN, "").strip()
        if not dsn:
            raise RuntimeError(
                f"{ENV_BACKEND}=postgres requires {ENV_POSTGRES_DSN} to be set"
            )
        postgres_cls = _load_postgres_backend_cls()
        return await postgres_cls.open(dsn)
    if kind == "redis":
        url = e.get(ENV_REDIS_URL, "").strip()
        if not url:
            raise RuntimeError(
                f"{ENV_BACKEND}=redis requires {ENV_REDIS_URL} to be set"
            )
        return await RedisJournalBackend.open(url)
    raise RuntimeError(
        f"unknown {ENV_BACKEND}={kind!r}; expected one of: sqlite, postgres, redis"
    )


__all__ = [
    "ENV_BACKEND",
    "ENV_POSTGRES_DSN",
    "ENV_REDIS_URL",
    "JournalBackend",
    "PostgresJournalBackend",
    "RedisJournalBackend",
    "RESUME_MAX_AGE_MS",
    "ResumeData",
    "SqliteJournalBackend",
    "TURN_COMPLETED",
    "TURN_ERRORED",
    "TURN_IN_PROGRESS",
    "open_backend_from_env",
]
