"""Persistent per-turn journal for the agent servicer.

Backs three Tier-4 capabilities:

- **T4.1** Per-turn resume — when the gateway restarts mid-turn, a fresh
  Chat RPC that re-arrives with the *same user text* (and a matching
  ``session_key``) can find the interrupted turn in ``status =
  'in_progress'`` and replay its prior tool results into the loop.
- **T4.2** is orthogonal (an async lock); the journal does not own it.
- **T4.4** Error breadcrumbs — every unhandled exception in the chat
  handler stamps the turn ``status = 'errored'`` with a truncated error
  message; ``recent_errored_turns`` exposes the last N for diagnostics.

Storage is a dedicated SQLite file
(``<data_dir>/agent_journal.sqlite``), separate from
:class:`corlinman_replay.SqliteSessionStore` (which is a clean
replay tool with its own contract).

The journal is open-on-first-use, lazy, never blocks the chat path on
the open. WAL mode + ``synchronous = NORMAL`` so concurrent sessions
read+write without serializing on the writer.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)


# Status enum for the ``turns`` table.
TURN_IN_PROGRESS = "in_progress"
TURN_COMPLETED = "completed"
TURN_ERRORED = "errored"

# A turn is "fresh" for resume purposes only if it started within the
# last 5 minutes. Older interrupted turns are abandoned — the user has
# moved on; treat the new message as a new task.
_RESUME_MAX_AGE_MS = 5 * 60 * 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    turn_id        INTEGER PRIMARY KEY,
    session_key    TEXT    NOT NULL,
    status         TEXT    NOT NULL
                          CHECK (status IN ('in_progress', 'completed', 'errored')),
    started_at_ms  INTEGER NOT NULL,
    ended_at_ms    INTEGER,
    user_text      TEXT,
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


class AgentJournal:
    """Async SQLite-backed per-turn journal."""

    __slots__ = ("_path", "_conn")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open(cls, path: Path) -> AgentJournal:
        j = cls(path)
        await j._open()
        return j

    async def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA busy_timeout = 5000")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("AgentJournal not opened — call open() first")
        return self._conn

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    async def begin_turn(
        self, session_key: str, user_text: str
    ) -> int:
        """Insert an in-progress row; return the new turn_id.

        ``turn_id`` is wall-clock ms — uniqueness across one process
        is good enough for a chat-turn store. Two opens in the same
        ms collide; we retry with ms+1 on the rare ``UNIQUE`` failure.
        """
        conn = self._c
        ts = int(time.time() * 1000)
        for offset in range(0, 20):
            tid = ts + offset
            try:
                await conn.execute(
                    "INSERT INTO turns (turn_id, session_key, status, "
                    "started_at_ms, user_text) VALUES (?, ?, ?, ?, ?)",
                    (tid, session_key or "", TURN_IN_PROGRESS, ts, user_text),
                )
                await conn.commit()
                return tid
            except aiosqlite.IntegrityError:
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
        """Append one message to the turn. ``seq`` is computed under
        ``BEGIN IMMEDIATE`` so concurrent appends to the same turn can't
        observe a stale max(seq) (the chat handler is single-task per
        session, but defending the invariant is cheap)."""
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
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM turn_messages WHERE turn_id = ?",
                (turn_id,),
            )
            row = await cur.fetchone()
            await cur.close()
            next_seq = int(row[0]) if row is not None else 0
            await conn.execute(
                "INSERT INTO turn_messages (turn_id, seq, role, content, "
                "tool_call_id, tool_calls_json) VALUES (?, ?, ?, ?, ?, ?)",
                (turn_id, next_seq, role, content, tool_call_id, tool_calls_text),
            )
            await conn.commit()
        except aiosqlite.Error as exc:
            logger.warning("agent.journal.append_failed", error=str(exc))
            try:
                await conn.execute("ROLLBACK")
            except aiosqlite.Error:
                pass

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------

    async def find_resumable_turn(
        self, session_key: str, user_text: str
    ) -> ResumeData | None:
        """Return the most-recent in-progress turn for ``session_key``
        whose ``user_text`` matches and that is younger than the resume
        window. The caller decides whether to actually resume — this
        method only finds the candidate."""
        if not session_key or not user_text:
            return None
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - _RESUME_MAX_AGE_MS
        try:
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
        messages = await self._load_messages(turn_id)
        return ResumeData(
            turn_id=turn_id,
            started_at_ms=started_at_ms,
            messages=messages,
        )

    async def _load_messages(self, turn_id: int) -> list[dict[str, Any]]:
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
        cutoff = int(time.time() * 1000) - _RESUME_MAX_AGE_MS
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


__all__ = [
    "AgentJournal",
    "ResumeData",
    "TURN_COMPLETED",
    "TURN_ERRORED",
    "TURN_IN_PROGRESS",
]
