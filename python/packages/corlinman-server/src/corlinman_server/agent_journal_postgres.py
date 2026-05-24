"""Postgres backend for :class:`corlinman_server.agent_journal.AgentJournal`.

Enables multi-gateway HA: two ``corlinman-server`` processes behind a
load balancer can share a single journal in Postgres, so a turn started
on gateway A and interrupted by A crashing can be resumed on gateway B
when the user resends the same message.

The :class:`PostgresJournalBackend` is a structural sibling of
``SqliteJournalBackend`` — both satisfy the
:class:`corlinman_server.agent_journal_backend.JournalBackend` Protocol.
Behaviour parity (turn lifecycle, message append, resume window) is
preserved, with one explicit caveat documented below.

**Concurrency note — single-writer-per-session is NOT enforced.**

The single-process SQLite backend pairs with the per-session asyncio
lock in :func:`corlinman_server.agent_servicer._lock_for` to guarantee
one turn at a time per ``session_key``. That lock is intra-process. With
two gateway processes pointed at the same Postgres, both can call
:meth:`begin_turn` for the same ``session_key`` concurrently and both
will get distinct turn rows back. By design today — operators are
expected to partition session traffic at the load-balancer layer
(e.g. consistent-hash by ``session_key``) so a given session lands on
exactly one gateway. A future revision could promote the per-session
lock to a Postgres advisory lock; that is out of scope for v1.

The schema lives in ``packages/corlinman-server/migrations/journal_postgres_v1.sql``.
:meth:`PostgresJournalBackend.open` runs it on first connect, so a fresh
deployment Just Works; existing deployments can pre-apply the .sql via
``psql`` if they prefer.

asyncpg is an optional extra; install with::

    pip install 'corlinman-server[postgres]'

If asyncpg is missing when :meth:`open` is called we raise a clear
``RuntimeError`` rather than letting the ``ImportError`` bubble out at
gateway startup with no useful context.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any

import structlog

from corlinman_server.agent_journal_backend import (
    RESUME_MAX_AGE_MS,
    TURN_COMPLETED,
    TURN_ERRORED,
    TURN_IN_PROGRESS,
    ResumeData,
)

logger = structlog.get_logger(__name__)


# DDL kept in sync with ``migrations/journal_postgres_v1.sql`` (base
# tables + indexes) and ``journal_postgres_v2.sql`` (S4 user_id column
# + C5 partial unique index). All statements are idempotent —
# ``CREATE ... IF NOT EXISTS`` / ``ADD COLUMN IF NOT EXISTS`` — so
# running this on a Postgres that already has the migrations applied is
# a no-op.
#
# S4: ``user_id`` column scopes ``find_resumable_turn`` by channel
# sender (defeats the group-chat replay attack). The column is nullable
# so HTTP callers without a sender (and legacy rows) keep working.
#
# C5: the partial unique index on
# ``(session_key, user_text, COALESCE(user_id, ''))`` where status is
# ``in_progress`` lets two gateways race ``begin_turn`` safely — the
# second loser hits ``ON CONFLICT DO NOTHING`` and the chat handler
# falls back to ``find_resumable_turn`` to grab the winner's row.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS journal_turns (
    turn_id       BIGSERIAL PRIMARY KEY,
    session_key   TEXT   NOT NULL,
    status        TEXT   NOT NULL,
    started_at_ms BIGINT NOT NULL,
    ended_at_ms   BIGINT,
    user_text     TEXT,
    user_id       TEXT,
    error         TEXT
);

ALTER TABLE journal_turns ADD COLUMN IF NOT EXISTS user_id TEXT;

CREATE INDEX IF NOT EXISTS journal_turns_session_status_idx
    ON journal_turns(session_key, status, started_at_ms DESC);

CREATE INDEX IF NOT EXISTS journal_turns_status_started_idx
    ON journal_turns(status, started_at_ms);

CREATE UNIQUE INDEX IF NOT EXISTS journal_turns_in_progress_uniq
    ON journal_turns (session_key, user_text, COALESCE(user_id, ''))
    WHERE status = 'in_progress';

CREATE TABLE IF NOT EXISTS journal_turn_messages (
    turn_id         BIGINT  NOT NULL REFERENCES journal_turns(turn_id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    role            TEXT    NOT NULL,
    content         TEXT,
    tool_call_id    TEXT,
    tool_calls_json TEXT,
    PRIMARY KEY (turn_id, seq)
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


class PostgresJournalBackend:
    """Postgres-backed journal for multi-gateway HA deployments.

    Implements the :class:`JournalBackend` Protocol verbatim — every
    method matches the SQLite backend's signature, return type, and
    exception-swallowing posture. Connections are taken from an
    ``asyncpg`` pool (default 1..8 connections) so concurrent sessions
    don't serialise on a single TCP socket.

    .. warning::
       The per-session asyncio lock in ``agent_servicer._lock_for`` is
       intra-process only. Two gateway processes operating on the same
       ``session_key`` will not serialise their turns through this
       backend. Partition traffic at the load balancer (consistent-hash
       on ``session_key``) until distributed locking ships.
    """

    __slots__ = ("_dsn", "_pool")

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        # Concrete type is ``asyncpg.Pool`` but we keep ``Any`` so the
        # module imports cleanly when asyncpg is not installed.
        self._pool: Any = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def open(cls, dsn: str) -> PostgresJournalBackend:
        """Connect to ``dsn`` and apply the v1 schema on the first call.

        Raises ``RuntimeError`` if asyncpg is not installed — the import
        is deferred to here so the module itself stays importable in
        environments that never select the Postgres backend.
        """
        try:
            import asyncpg  # noqa: F401 — proven importable; used below.
        except ImportError as exc:
            raise RuntimeError(
                "postgres backend selected but asyncpg is not installed; "
                "pip install corlinman-server[postgres]"
            ) from exc

        backend = cls(dsn)
        await backend._open()
        return backend

    async def _open(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=8
        )
        # Apply the schema once on open. ``CREATE ... IF NOT EXISTS`` is
        # idempotent, so re-running across gateway restarts is fine.
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA_SQL)

    async def close(self) -> None:
        """Close the underlying pool. Idempotent."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def _p(self) -> Any:
        if self._pool is None:
            raise RuntimeError(
                "PostgresJournalBackend not opened — call open() first"
            )
        return self._pool

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

        Unlike SQLite (which uses wall-clock ms as the id), Postgres
        uses a ``BIGSERIAL`` so two concurrent writers never collide on
        the primary key.

        C5: a partial unique index on
        ``(session_key, user_text, COALESCE(user_id, ''))`` where
        ``status = 'in_progress'`` makes the INSERT race-safe across HA
        gateways. The query uses ``ON CONFLICT DO NOTHING RETURNING
        turn_id``; when two gateways open the same (session_key,
        user_text, user_id) tuple concurrently the loser gets back zero
        rows and we return ``None``. The chat handler then re-runs
        ``find_resumable_turn`` to grab the winner's row and join it.

        S4: ``user_id`` is journaled so ``find_resumable_turn`` can
        scope its match by channel sender. ``None`` is preserved
        verbatim (the column is nullable) for HTTP turns that have no
        sender id.
        """
        ts = _now_ms()
        async with self._p.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO journal_turns "
                "(session_key, status, started_at_ms, user_text, user_id) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT DO NOTHING RETURNING turn_id",
                session_key or "",
                TURN_IN_PROGRESS,
                ts,
                user_text,
                user_id,
            )
        if row is None:
            # C5 — another gateway raced us and won the partial unique
            # index. The servicer falls back to ``find_resumable_turn``
            # so the caller continues on the winner's row instead of
            # creating a duplicate.
            logger.info(
                "agent.journal.begin_turn_conflict",
                session_key=session_key,
                user_id=user_id,
            )
            return None
        return int(row["turn_id"])

    async def complete_turn(self, turn_id: int) -> None:
        try:
            async with self._p.acquire() as conn:
                await conn.execute(
                    "UPDATE journal_turns SET status = $1, ended_at_ms = $2 "
                    "WHERE turn_id = $3 AND status = $4",
                    TURN_COMPLETED,
                    _now_ms(),
                    int(turn_id),
                    TURN_IN_PROGRESS,
                )
        except Exception as exc:
            logger.warning("agent.journal.complete_failed", error=str(exc))

    async def error_turn(self, turn_id: int, error: str) -> None:
        try:
            async with self._p.acquire() as conn:
                await conn.execute(
                    "UPDATE journal_turns "
                    "SET status = $1, ended_at_ms = $2, error = $3 "
                    "WHERE turn_id = $4 AND status = $5",
                    TURN_ERRORED,
                    _now_ms(),
                    error[:1000],
                    int(turn_id),
                    TURN_IN_PROGRESS,
                )
        except Exception as exc:
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
        """Append one message to ``turn_id`` at the next ``seq`` slot.

        Wrapped in a transaction with ``SELECT ... FOR UPDATE`` on the
        parent turn row so two appenders racing on the same ``turn_id``
        cannot pick the same ``seq``. The agent servicer is single-task
        per session within a single process, but two HA gateways on the
        same session_key (see class-level warning) could race; this
        defends the invariant.
        """
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
            async with self._p.acquire() as conn, conn.transaction():
                # Lock the parent turn row to serialise concurrent
                # appenders against the same turn_id.
                await conn.fetchval(
                    "SELECT turn_id FROM journal_turns "
                    "WHERE turn_id = $1 FOR UPDATE",
                    int(turn_id),
                )
                next_seq_row = await conn.fetchrow(
                    "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq "
                    "FROM journal_turn_messages WHERE turn_id = $1",
                    int(turn_id),
                )
                next_seq = int(next_seq_row["next_seq"]) if next_seq_row else 0
                await conn.execute(
                    "INSERT INTO journal_turn_messages "
                    "(turn_id, seq, role, content, tool_call_id, tool_calls_json) "
                    "VALUES ($1, $2, $3, $4, $5, $6)",
                    int(turn_id),
                    next_seq,
                    role,
                    content,
                    tool_call_id,
                    tool_calls_text,
                )
        except Exception as exc:
            logger.warning("agent.journal.append_failed", error=str(exc))

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
        whose ``user_text`` matches and that is younger than
        :data:`RESUME_MAX_AGE_MS`. Mirrors the SQLite backend's
        candidate-only semantics — the caller decides whether to resume.

        S4 — when ``user_id`` is non-None, the candidate row's
        ``user_id`` must match (or be NULL for legacy rows journaled
        before the column existed). ``user_id=None`` keeps the legacy
        user_text-only match for HTTP turns.
        """
        if not session_key or not user_text:
            return None
        cutoff = _now_ms() - RESUME_MAX_AGE_MS
        try:
            async with self._p.acquire() as conn:
                if user_id is not None:
                    row = await conn.fetchrow(
                        "SELECT turn_id, started_at_ms FROM journal_turns "
                        "WHERE session_key = $1 AND status = $2 "
                        "AND user_text = $3 AND started_at_ms >= $4 "
                        "AND (user_id = $5 OR user_id IS NULL) "
                        "ORDER BY started_at_ms DESC LIMIT 1",
                        session_key,
                        TURN_IN_PROGRESS,
                        user_text,
                        cutoff,
                        user_id,
                    )
                else:
                    row = await conn.fetchrow(
                        "SELECT turn_id, started_at_ms FROM journal_turns "
                        "WHERE session_key = $1 AND status = $2 "
                        "AND user_text = $3 AND started_at_ms >= $4 "
                        "ORDER BY started_at_ms DESC LIMIT 1",
                        session_key,
                        TURN_IN_PROGRESS,
                        user_text,
                        cutoff,
                    )
        except Exception as exc:
            logger.warning("agent.journal.find_resumable_failed", error=str(exc))
            return None
        if row is None:
            return None
        turn_id = int(row["turn_id"])
        started_at_ms = int(row["started_at_ms"])
        messages = await self.load_messages(turn_id)
        return ResumeData(
            turn_id=turn_id,
            started_at_ms=started_at_ms,
            messages=messages,
        )

    async def load_messages(self, turn_id: int) -> list[dict[str, Any]]:
        """Load every message stored under ``turn_id`` in seq order."""
        try:
            async with self._p.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT seq, role, content, tool_call_id, tool_calls_json "
                    "FROM journal_turn_messages WHERE turn_id = $1 "
                    "ORDER BY seq ASC",
                    int(turn_id),
                )
        except Exception as exc:
            logger.warning("agent.journal.load_messages_failed", error=str(exc))
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            role = row["role"]
            content = row["content"]
            # SQLite stores content as NOT NULL; we keep that contract
            # for callers by normalising NULL → "" on the way out.
            if content is None:
                content = ""
            msg: dict[str, Any] = {"role": role, "content": content}
            if row["tool_call_id"] is not None:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_calls_json"] is not None:
                with contextlib.suppress(json.JSONDecodeError):
                    msg["tool_calls"] = json.loads(row["tool_calls_json"])
            out.append(msg)
        return out

    # ------------------------------------------------------------------
    # T4.4 — Error breadcrumbs
    # ------------------------------------------------------------------

    async def recent_errored_turns(
        self, session_key: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Return the most recent errored turns for ``session_key``."""
        try:
            async with self._p.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT turn_id, started_at_ms, ended_at_ms, "
                    "user_text, error FROM journal_turns "
                    "WHERE session_key = $1 AND status = $2 "
                    "ORDER BY started_at_ms DESC LIMIT $3",
                    session_key,
                    TURN_ERRORED,
                    max(1, int(limit)),
                )
        except Exception as exc:
            logger.warning(
                "agent.journal.recent_errored_failed", error=str(exc)
            )
            return []
        return [
            {
                "turn_id": int(r["turn_id"]),
                "started_at_ms": int(r["started_at_ms"]),
                "ended_at_ms": (
                    int(r["ended_at_ms"]) if r["ended_at_ms"] is not None else None
                ),
                "user_text": r["user_text"],
                "error": r["error"],
            }
            for r in rows
        ]

    async def mark_stale_in_progress_as_errored(self) -> int:
        """Sweep stale in-progress turns past :data:`RESUME_MAX_AGE_MS`.

        Called once at gateway boot so a previously-crashed process
        does not leave the table littered with phantom in-progress
        rows. Returns the number of rows flipped to ``errored``.
        """
        now = _now_ms()
        cutoff = now - RESUME_MAX_AGE_MS
        try:
            async with self._p.acquire() as conn:
                result = await conn.execute(
                    "UPDATE journal_turns SET status = $1, "
                    "ended_at_ms = $2, "
                    "error = COALESCE(error, $3) "
                    "WHERE status = $4 AND started_at_ms < $5",
                    TURN_ERRORED,
                    now,
                    "abandoned: gateway restart left turn in_progress",
                    TURN_IN_PROGRESS,
                    cutoff,
                )
        except Exception as exc:
            logger.warning("agent.journal.sweep_failed", error=str(exc))
            return 0
        # asyncpg returns the raw command tag, e.g. "UPDATE 3". Parse
        # the trailing integer; default to 0 if the tag is unexpected.
        n = 0
        if isinstance(result, str):
            parts = result.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].isdigit():
                n = int(parts[1])
        if n:
            logger.info("agent.journal.swept_stale", count=n)
        return n


__all__ = ["PostgresJournalBackend"]
