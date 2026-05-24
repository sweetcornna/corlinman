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


@dataclass(frozen=True)
class InProgressTurn:
    """A single in-progress journal row, projected for the boot-time
    auto-resume scanner.

    Distinct from :class:`ResumeData` — this one is the "row header" the
    :class:`AgentResumeService` uses to decide *whether* a turn needs to
    be re-driven (and through which channel). The full replay buffer is
    not loaded here; the eventual chat-handler re-entry will call
    :meth:`JournalBackend.find_resumable_turn` to pick up the messages.

    ``channel`` is the channel identifier the turn was originated on
    (``"qq"`` / ``"telegram"`` / ``"discord"`` / ``""`` for HTTP /
    pre-channel-column legacy rows). The auto-resume service uses it to
    pick the right re-delivery path.
    """

    turn_id: int
    session_key: str
    user_id: str | None
    user_text: str
    started_at_ms: int
    channel: str


# Cap for the ``last_user_text`` preview so the wire row never grows
# unbounded — the admin sessions UI only renders the first line.
SESSION_SUMMARY_PREVIEW_LEN = 80


@dataclass(frozen=True)
class SessionSummary:
    """One row of the ``/admin/sessions`` listing, projected straight
    out of the journal's ``turns`` table.

    Distinct from :class:`corlinman_replay.SessionSummary` (the legacy
    ``sessions.sqlite`` shape) — this one lives on the journal side and
    is what the admin route really wants to surface now that the
    servicer writes there and not into ``sessions.sqlite``.

    ``first_seen_at_ms`` / ``last_seen_at_ms`` are wall-clock unix
    milliseconds derived from ``MIN``/``MAX(started_at_ms)`` per
    session_key — they are turn-start times, not message times, so two
    turns spaced minutes apart will show distinct ``last_seen`` values
    even when ``message_count`` doesn't change.

    ``last_user_text`` is the first 80 chars of the most-recent turn's
    ``user_text`` column (``None`` when no user_text was journaled —
    rare, but possible for malformed callers).

    ``last_status`` is the ``status`` of the most-recent turn — one of
    ``"in_progress" | "completed" | "errored"`` so the UI can render an
    appropriate badge.
    """

    session_key: str
    first_seen_at_ms: int
    last_seen_at_ms: int
    turn_count: int
    message_count: int
    last_user_text: str | None
    last_status: str | None


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
        channel: str = "",
    ) -> int | None:
        """Insert an in-progress row; return the new turn_id.

        ``user_id`` (S4) scopes the row to a specific channel sender so a
        group-chat replay attack — Mallory parroting Alice's user_text on
        the same session_key — can't pick up Alice's in-progress turn.
        ``None`` keeps the legacy "no sender" semantics for HTTP callers.

        ``channel`` (auto-resume) tags the row with the channel that
        originated it (``"qq"`` / ``"telegram"`` / ``"discord"`` / ``""``
        for HTTP). The boot-time :class:`AgentResumeService` uses the
        tag to dispatch the right re-delivery path. The default ``""``
        keeps every existing call site (and every pre-column row)
        working unchanged.

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

    async def mark_stale_in_progress_as_errored(
        self, older_than_seconds: int | None = None
    ) -> int:
        """Sweep abandoned in-progress turns past the resume window.

        ``older_than_seconds`` overrides the default
        :data:`RESUME_MAX_AGE_MS` cutoff; the
        :class:`~corlinman_server.auto_resume.AgentResumeService` uses
        this to clear *very* old rows (24h+) at gateway boot so a long
        downtime doesn't leak abandoned rows forever. ``None`` keeps the
        legacy resume-window cutoff.
        """
        ...

    async def list_resumable_in_progress(
        self, *, window_ms: int = RESUME_MAX_AGE_MS
    ) -> list[InProgressTurn]:
        """Return every in-progress turn started within ``window_ms``.

        Boot-time scanner for the gateway auto-resume service: each row
        in the return value is a candidate for re-delivery (the channel
        handler that owns the row's ``channel`` is responsible for
        deciding whether to actually re-deliver — see
        :class:`~corlinman_server.auto_resume.AgentResumeService`).

        Rows are ordered by ``started_at_ms ASC`` so re-delivery
        respects original arrival order — important when two turns on
        the same session arrived rapidly before the crash.
        """
        ...

    async def load_messages(self, turn_id: int) -> list[dict[str, Any]]:
        """Load every message under ``turn_id`` in seq order.

        Public on the backend so it can be tested in isolation; callers
        normally read ``ResumeData.messages``.
        """
        ...

    async def list_session_summaries(
        self, *, limit: int = 200
    ) -> list[SessionSummary]:
        """Aggregate ``turns`` by ``session_key`` and return one row per
        session ordered by ``MAX(started_at_ms) DESC``.

        Powers the ``/admin/sessions`` admin surface. Backends compute
        the aggregate server-side so a chat history with 100k turns
        across 50 sessions doesn't ship 100k rows over the wire.
        """
        ...

    async def delete_session(self, session_key: str) -> int:
        """Wipe every turn (and its cascading messages) for
        ``session_key``. Returns the number of turn rows deleted.

        Returns ``0`` when no turns matched — the route layer maps that
        to ``404``. ``turn_messages`` rows are removed by the schema's
        ``ON DELETE CASCADE`` (SQLite) / explicit ``REFERENCES ... ON
        DELETE CASCADE`` (Postgres) so callers don't need to issue a
        second statement.
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
    channel        TEXT    NOT NULL DEFAULT '',
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

# Pre-auto-resume deployments have a ``turns`` table without ``channel``.
# Same gated-ALTER pattern as ``user_id``. ``NOT NULL DEFAULT ''`` matches
# the inline schema so a row inserted by an OLD process and read by a NEW
# one round-trips to the canonical empty string instead of NULL.
_CHANNEL_MIGRATION = (
    "ALTER TABLE turns ADD COLUMN channel TEXT NOT NULL DEFAULT ''"
)


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
        # S4 / auto-resume: additive ``user_id`` and ``channel`` column
        # migrations on pre-existing journals. ``PRAGMA table_info`` is
        # the documented way to check column presence in SQLite —
        # adding the column only when missing keeps the open path
        # idempotent (re-running it on a fresh DB is a no-op).
        #
        # The migrations are runnable on the live VPS without manual
        # intervention — operators redeploying the gateway pick this
        # path up on the next process start, and the same code branch
        # leaves brand-new DBs (where the inline schema already added
        # the column) untouched.
        try:
            cur = await conn.execute("PRAGMA table_info(turns)")
            rows = await cur.fetchall()
            await cur.close()
            existing = {str(r[1]) for r in rows}
            if "user_id" not in existing:
                await conn.execute(_USER_ID_MIGRATION)
                await conn.commit()
                logger.info("agent.journal.migrated", migration="user_id_column")
            if "channel" not in existing:
                await conn.execute(_CHANNEL_MIGRATION)
                await conn.commit()
                logger.info("agent.journal.migrated", migration="channel_column")
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
        channel: str = "",
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

        Auto-resume — ``channel`` tags the row with the originating
        channel id (``"qq"`` / ``"telegram"`` / ``"discord"`` / ``""``
        for HTTP). Read back by the boot-time
        :class:`~corlinman_server.auto_resume.AgentResumeService` so
        re-delivery dispatches to the right surface. Default ``""``
        preserves every existing call site.
        """
        conn = self._c
        ts = int(time.time() * 1000)
        for offset in range(0, 20):
            tid = ts + offset
            try:
                await conn.execute(
                    "INSERT INTO turns (turn_id, session_key, status, "
                    "started_at_ms, user_text, user_id, channel) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        tid,
                        session_key or "",
                        TURN_IN_PROGRESS,
                        ts,
                        user_text,
                        user_id,
                        channel or "",
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

    async def list_session_summaries(
        self, *, limit: int = 200
    ) -> list[SessionSummary]:
        """Aggregate ``turns`` by ``session_key`` for the
        ``/admin/sessions`` UI.

        The aggregate is computed in a single SQL statement using a
        ``GROUP BY session_key`` with a correlated subquery for the
        most-recent turn's ``user_text`` + ``status`` so the listing
        scales to thousands of turns without round-tripping each
        session_key from Python.
        """
        if limit <= 0:
            return []
        try:
            cur = await self._c.execute(
                # The outer aggregate gives us the counts; the
                # subquery (``last_turn``) pins the *most recent* turn's
                # user_text + status for the preview column. Using a
                # correlated subquery keeps the query a single round
                # trip; an alternative window-function form is
                # equivalent on SQLite >= 3.25 but more verbose.
                "SELECT t.session_key, "
                "       MIN(t.started_at_ms) AS first_seen, "
                "       MAX(t.started_at_ms) AS last_seen, "
                "       COUNT(*)             AS turn_count, "
                "       (SELECT COUNT(*) FROM turn_messages tm "
                "        WHERE tm.turn_id IN (SELECT turn_id FROM turns "
                "                             WHERE session_key = t.session_key)) "
                "                            AS message_count, "
                "       (SELECT user_text FROM turns "
                "        WHERE session_key = t.session_key "
                "        ORDER BY started_at_ms DESC LIMIT 1) "
                "                            AS last_user_text, "
                "       (SELECT status FROM turns "
                "        WHERE session_key = t.session_key "
                "        ORDER BY started_at_ms DESC LIMIT 1) "
                "                            AS last_status "
                "FROM turns t "
                "GROUP BY t.session_key "
                "ORDER BY last_seen DESC "
                "LIMIT ?",
                (int(limit),),
            )
            rows = await cur.fetchall()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning(
                "agent.journal.list_session_summaries_failed",
                error=str(exc),
            )
            return []
        out: list[SessionSummary] = []
        for r in rows:
            preview = r[5]
            if preview is not None and len(preview) > SESSION_SUMMARY_PREVIEW_LEN:
                preview = preview[:SESSION_SUMMARY_PREVIEW_LEN]
            out.append(
                SessionSummary(
                    session_key=str(r[0] or ""),
                    first_seen_at_ms=int(r[1]),
                    last_seen_at_ms=int(r[2]),
                    turn_count=int(r[3]),
                    message_count=int(r[4]),
                    last_user_text=preview,
                    last_status=str(r[6]) if r[6] is not None else None,
                )
            )
        return out

    async def delete_session(self, session_key: str) -> int:
        """Wipe every turn (and its cascading turn_messages) for
        ``session_key``. Returns the count of ``turns`` rows deleted.

        Uses an explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` envelope (same
        shape as :meth:`append_message`) so the DELETE + ON DELETE
        CASCADE fire atomically and the rowcount we report matches what
        actually survived the commit. ``aiosqlite``'s
        ``async with conn:`` shortcut re-awaits the connection, which
        explodes once the worker thread is already started — keep the
        manual envelope.
        """
        if not session_key:
            return 0
        conn = self._c
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "DELETE FROM turns WHERE session_key = ?",
                (session_key,),
            )
            n = cur.rowcount or 0
            await cur.close()
            await conn.commit()
        except aiosqlite.Error as exc:
            logger.warning(
                "agent.journal.delete_session_failed", error=str(exc)
            )
            if conn.in_transaction:
                try:
                    await conn.rollback()
                except aiosqlite.Error as rb_exc:
                    logger.warning(
                        "agent.journal.delete_session_rollback_failed",
                        error=str(rb_exc),
                    )
            return 0
        return int(n)

    async def mark_stale_in_progress_as_errored(
        self, older_than_seconds: int | None = None
    ) -> int:
        """Sweep stale in-progress turns and stamp them errored.

        ``older_than_seconds=None`` keeps the legacy
        :data:`RESUME_MAX_AGE_MS` (5-min) cutoff — used by the per-RPC
        defensive sweep. The boot-time
        :class:`~corlinman_server.auto_resume.AgentResumeService` passes
        a much larger window (e.g. 24h) so deeply abandoned rows from
        long downtimes get cleared without disturbing fresh in-flight
        turns that the same boot pass is about to resume.
        """
        now_ms = int(time.time() * 1000)
        if older_than_seconds is None:
            cutoff = now_ms - RESUME_MAX_AGE_MS
        else:
            cutoff = now_ms - max(0, int(older_than_seconds)) * 1000
        try:
            cur = await self._c.execute(
                "UPDATE turns SET status = ?, ended_at_ms = ?, "
                "error = COALESCE(error, ?) "
                "WHERE status = ? AND started_at_ms < ?",
                (
                    TURN_ERRORED,
                    now_ms,
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

    async def list_resumable_in_progress(
        self, *, window_ms: int = RESUME_MAX_AGE_MS
    ) -> list[InProgressTurn]:
        """Return every in-progress turn started within ``window_ms``.

        Powers the boot-time auto-resume scanner. Ordered by
        ``started_at_ms ASC`` so the gateway re-delivers turns in their
        original arrival order — important when a single session
        received two rapid messages before the crash.

        Empty list on any read failure — degrading silently here is
        correct because auto-resume is best-effort; a fully missed
        re-delivery falls back to the user re-sending.
        """
        cutoff = int(time.time() * 1000) - max(0, int(window_ms))
        try:
            cur = await self._c.execute(
                "SELECT turn_id, session_key, user_id, user_text, "
                "started_at_ms, channel FROM turns "
                "WHERE status = ? AND started_at_ms >= ? "
                "ORDER BY started_at_ms ASC",
                (TURN_IN_PROGRESS, cutoff),
            )
            rows = await cur.fetchall()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning(
                "agent.journal.list_resumable_in_progress_failed",
                error=str(exc),
            )
            return []
        out: list[InProgressTurn] = []
        for r in rows:
            out.append(
                InProgressTurn(
                    turn_id=int(r[0]),
                    session_key=str(r[1] or ""),
                    user_id=str(r[2]) if r[2] is not None else None,
                    user_text=str(r[3] or ""),
                    started_at_ms=int(r[4]),
                    channel=str(r[5] or ""),
                )
            )
        return out


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
    "InProgressTurn",
    "JournalBackend",
    "PostgresJournalBackend",
    "RedisJournalBackend",
    "RESUME_MAX_AGE_MS",
    "ResumeData",
    "SESSION_SUMMARY_PREVIEW_LEN",
    "SessionSummary",
    "SqliteJournalBackend",
    "TURN_COMPLETED",
    "TURN_ERRORED",
    "TURN_IN_PROGRESS",
    "open_backend_from_env",
]
