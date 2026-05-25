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
from collections.abc import AsyncIterator, Sequence
from typing import Any

import structlog

from corlinman_server.agent_journal_backend import (
    RESUME_MAX_AGE_MS,
    SESSION_SUMMARY_PREVIEW_LEN,
    TURN_COMPLETED,
    TURN_ERRORED,
    TURN_IN_PROGRESS,
    InProgressTurn,
    ResumeData,
    SessionSummary,
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
    turn_id               BIGSERIAL PRIMARY KEY,
    session_key           TEXT   NOT NULL,
    status                TEXT   NOT NULL,
    started_at_ms         BIGINT NOT NULL,
    ended_at_ms           BIGINT,
    user_text             TEXT,
    user_id               TEXT,
    channel               TEXT   NOT NULL DEFAULT '',
    pending_question_json TEXT,
    error                 TEXT
);

ALTER TABLE journal_turns ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE journal_turns ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT '';
ALTER TABLE journal_turns ADD COLUMN IF NOT EXISTS pending_question_json TEXT;

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
        channel: str = "",
        pending_question_json: str | None = None,
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

        Auto-resume: ``channel`` tags the originating channel surface
        so the boot-time
        :class:`~corlinman_server.auto_resume.AgentResumeService` can
        pick the right re-delivery path. ``""`` keeps every legacy
        caller working unchanged.

        ask_user: ``pending_question_json`` is the optional JSON
        payload of the ``ask_user`` tool call that terminated the
        turn. Purely informational — no read path inside the chat
        handler today.
        """
        ts = _now_ms()
        async with self._p.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO journal_turns "
                "(session_key, status, started_at_ms, user_text, user_id, "
                "channel, pending_question_json) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "ON CONFLICT DO NOTHING RETURNING turn_id",
                session_key or "",
                TURN_IN_PROGRESS,
                ts,
                user_text,
                user_id,
                channel or "",
                pending_question_json,
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

    async def append_messages(
        self,
        turn_id: int,
        messages: list[dict[str, Any]],
    ) -> None:
        """Append multiple messages to ``turn_id`` in one pooled
        transaction.

        Single ``acquire()`` + single ``conn.transaction()`` wraps the
        ``SELECT ... FOR UPDATE`` lock, the ``SELECT MAX(seq)``, and
        every ``INSERT`` — so a 2-message batch costs one pool round
        trip instead of two. Empty ``messages`` is a no-op.

        Mirrors :meth:`append_message`'s error posture: a single
        ``json.dumps`` failure on a per-message ``tool_calls`` payload
        skips that message and continues; transactional failures log
        and let the pool roll back automatically (the ``async with``
        contract).
        """
        if not messages:
            return
        # Pre-serialise so a TypeError can't leave a half-applied batch.
        prepared: list[tuple[str, str, str | None, str | None]] = []
        for msg in messages:
            role = str(msg.get("role") or "")
            content = msg.get("content") or ""
            if not isinstance(content, str):
                content = str(content)
            tool_call_id_val = msg.get("tool_call_id")
            tool_call_id = (
                str(tool_call_id_val) if tool_call_id_val is not None else None
            )
            tool_calls_val = msg.get("tool_calls")
            tool_calls_text: str | None = None
            if tool_calls_val is not None:
                try:
                    tool_calls_text = json.dumps(tool_calls_val)
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "agent.journal.append_serialize_failed",
                        error=str(exc),
                    )
                    continue
            prepared.append((role, content, tool_call_id, tool_calls_text))
        if not prepared:
            return
        try:
            async with self._p.acquire() as conn, conn.transaction():
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
                next_seq = (
                    int(next_seq_row["next_seq"]) if next_seq_row else 0
                )
                for role, content, tool_call_id, tool_calls_text in prepared:
                    await conn.execute(
                        "INSERT INTO journal_turn_messages "
                        "(turn_id, seq, role, content, tool_call_id, "
                        "tool_calls_json) VALUES "
                        "($1, $2, $3, $4, $5, $6)",
                        int(turn_id),
                        next_seq,
                        role,
                        content,
                        tool_call_id,
                        tool_calls_text,
                    )
                    next_seq += 1
        except Exception as exc:
            logger.warning("agent.journal.append_batch_failed", error=str(exc))

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

    async def list_session_summaries(
        self, *, limit: int = 200
    ) -> list[SessionSummary]:
        """Aggregate ``journal_turns`` by ``session_key`` for the
        ``/admin/sessions`` UI.

        Uses ``DISTINCT ON (session_key)`` semantics via a CTE so the
        most-recent turn's ``user_text`` + ``status`` come back in the
        same scan as the aggregate. One acquire per call, matching the
        other read methods.
        """
        if limit <= 0:
            return []
        try:
            async with self._p.acquire() as conn:
                rows = await conn.fetch(
                    # ``latest`` picks the most-recent turn per
                    # session_key once (DISTINCT ON + ORDER BY started
                    # DESC); ``agg`` collapses every row of that
                    # session_key into a single bucket. The join binds
                    # the two for the final row shape.
                    "WITH latest AS ( "
                    "    SELECT DISTINCT ON (session_key) "
                    "           session_key, user_text, status "
                    "    FROM journal_turns "
                    "    ORDER BY session_key, started_at_ms DESC "
                    "), agg AS ( "
                    "    SELECT session_key, "
                    "           MIN(started_at_ms) AS first_seen, "
                    "           MAX(started_at_ms) AS last_seen, "
                    "           COUNT(*)           AS turn_count "
                    "    FROM journal_turns "
                    "    GROUP BY session_key "
                    "), msg_counts AS ( "
                    "    SELECT t.session_key, COUNT(m.turn_id) AS message_count "
                    "    FROM journal_turns t "
                    "    LEFT JOIN journal_turn_messages m ON m.turn_id = t.turn_id "
                    "    GROUP BY t.session_key "
                    ") "
                    "SELECT a.session_key, a.first_seen, a.last_seen, "
                    "       a.turn_count, mc.message_count, "
                    "       l.user_text, l.status "
                    "FROM agg a "
                    "JOIN latest l USING (session_key) "
                    "JOIN msg_counts mc USING (session_key) "
                    "ORDER BY a.last_seen DESC "
                    "LIMIT $1",
                    int(limit),
                )
        except Exception as exc:
            logger.warning(
                "agent.journal.list_session_summaries_failed",
                error=str(exc),
            )
            return []
        out: list[SessionSummary] = []
        for r in rows:
            preview = r["user_text"]
            if preview is not None and len(preview) > SESSION_SUMMARY_PREVIEW_LEN:
                preview = preview[:SESSION_SUMMARY_PREVIEW_LEN]
            out.append(
                SessionSummary(
                    session_key=str(r["session_key"] or ""),
                    first_seen_at_ms=int(r["first_seen"]),
                    last_seen_at_ms=int(r["last_seen"]),
                    turn_count=int(r["turn_count"]),
                    message_count=int(r["message_count"] or 0),
                    last_user_text=preview,
                    last_status=(
                        str(r["status"]) if r["status"] is not None else None
                    ),
                )
            )
        return out

    async def delete_session(self, session_key: str) -> int:
        """Delete every turn (and its cascading messages) for
        ``session_key``. Returns the count of ``journal_turns`` rows
        actually deleted, computed via ``RETURNING turn_id`` since
        asyncpg's command tag is best-effort.

        ``journal_turn_messages`` rows are removed by the schema's
        ``REFERENCES journal_turns(turn_id) ON DELETE CASCADE``.
        """
        if not session_key:
            return 0
        try:
            async with self._p.acquire() as conn:
                rows = await conn.fetch(
                    "DELETE FROM journal_turns "
                    "WHERE session_key = $1 "
                    "RETURNING turn_id",
                    session_key,
                )
        except Exception as exc:
            logger.warning(
                "agent.journal.delete_session_failed", error=str(exc)
            )
            return 0
        return len(rows)

    async def mark_stale_in_progress_as_errored(
        self, older_than_seconds: int | None = None
    ) -> int:
        """Sweep stale in-progress turns past the cutoff.

        ``older_than_seconds=None`` keeps the legacy
        :data:`RESUME_MAX_AGE_MS` window; the boot-time
        :class:`~corlinman_server.auto_resume.AgentResumeService` passes
        a much larger window (e.g. 24h) to clear truly-abandoned rows
        without disturbing fresh in-flight turns the same scan plans to
        resume.

        Returns the number of rows flipped to ``errored``.
        """
        now = _now_ms()
        if older_than_seconds is None:
            cutoff = now - RESUME_MAX_AGE_MS
        else:
            cutoff = now - max(0, int(older_than_seconds)) * 1000
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

    async def list_resumable_in_progress(
        self, *, window_ms: int = RESUME_MAX_AGE_MS
    ) -> list[InProgressTurn]:
        """Return every in-progress turn started within ``window_ms``.

        Boot-time auto-resume scanner; ordered ``started_at_ms ASC`` so
        the gateway re-delivers turns in arrival order. Behaves like the
        SQLite peer.
        """
        cutoff = _now_ms() - max(0, int(window_ms))
        try:
            async with self._p.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT turn_id, session_key, user_id, user_text, "
                    "started_at_ms, channel FROM journal_turns "
                    "WHERE status = $1 AND started_at_ms >= $2 "
                    "ORDER BY started_at_ms ASC",
                    TURN_IN_PROGRESS,
                    cutoff,
                )
        except Exception as exc:
            logger.warning(
                "agent.journal.list_resumable_in_progress_failed",
                error=str(exc),
            )
            return []
        out: list[InProgressTurn] = []
        for r in rows:
            out.append(
                InProgressTurn(
                    turn_id=int(r["turn_id"]),
                    session_key=str(r["session_key"] or ""),
                    user_id=(
                        str(r["user_id"]) if r["user_id"] is not None else None
                    ),
                    user_text=str(r["user_text"] or ""),
                    started_at_ms=int(r["started_at_ms"]),
                    channel=str(r["channel"] or ""),
                )
            )
        return out


    # ------------------------------------------------------------------
    # W1.2 — turn events timeline.
    #
    # The Postgres backend does not yet persist per-turn events. The W1.2
    # admin observability surface targets the SQLite single-process
    # journal first (where event volume × write rate × WAL gives the
    # best perf profile for streaming replay). When a Postgres deployment
    # needs replay, ship a v5 migration with the matching
    # ``journal_turn_events`` table; until then these methods stub out
    # gracefully so the SSE bridge degrades to "no replay buffer" rather
    # than 500-ing on a backend it can't serve.
    # ------------------------------------------------------------------

    async def append_event(self, envelope: Any) -> None:  # pragma: no cover
        return None

    async def append_events_batch(
        self, envelopes: Sequence[Any]
    ) -> None:  # pragma: no cover
        return None

    async def load_events(
        self, turn_id: str | int
    ) -> list[dict[str, Any]]:  # pragma: no cover
        return []

    async def iter_events(  # type: ignore[misc]
        self, turn_id: str | int, start_sequence: int = 0
    ) -> AsyncIterator[dict[str, Any]]:  # pragma: no cover
        # Async-generator stub — yields nothing. The ``if False`` keeps
        # this as a generator function (so the caller can ``async for``
        # without an awaitable indirection) while emitting zero items.
        if False:
            yield {}
        return

    async def get_session_turn_ids(
        self, session_key: str, limit: int = 50
    ) -> list[int]:  # pragma: no cover
        # Could be implemented as a thin SELECT on journal_turns; left
        # unimplemented until a Postgres deployment actually wires the
        # SSE replay route (W1.3 ships SQLite-only).
        return []

    async def list_session_turns(
        self,
        session_key: str,
        *,
        limit: int = 50,
        before_turn_id: str | None = None,
    ) -> list[dict[str, Any]]:  # pragma: no cover
        # Postgres deployment doesn't yet wire the W1.2 UI surface; the
        # SQLite backend is the source of truth for the past-turns
        # navigator. Empty list keeps the admin route degrading
        # gracefully rather than 500-ing on a non-SQLite gateway.
        return []

    async def update_turn_cost(
        self,
        turn_id: int,
        *,
        estimated_cost_usd: float | None,
        cost_status: str | None,
    ) -> None:  # pragma: no cover
        return None


__all__ = ["PostgresJournalBackend"]
