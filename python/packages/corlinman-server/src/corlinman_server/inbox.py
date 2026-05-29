"""Durable inbound-message queue for chat channels (T4.3).

Records every accepted channel event under
``<data_dir>/inbox.sqlite`` with a four-state lifecycle:

- ``pending``    — received from the channel, not yet dispatched
- ``dispatched`` — handed off to chat_service.run
- ``done``       — chat finished (Done event surfaced + reply sent)
- ``dead``       — gave up after too many retry attempts

Purpose: a gateway crash between "received from NapCat" and
"completed chat reply" no longer drops the message — the row stays
``pending`` (or ``dispatched``) in the queue and the boot-time
drainer can flag / replay it.

Scope today: QQ channel only. Other channels (Telegram, Discord, etc.)
can adopt the same pattern by accepting an optional ``inbox`` param
and writing their inbound events through.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

INBOX_PENDING = "pending"
INBOX_DISPATCHED = "dispatched"
INBOX_DONE = "done"
INBOX_DEAD = "dead"

_MAX_RETRIES = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel         TEXT NOT NULL,
    session_key     TEXT NOT NULL,
    message_id      TEXT,
    user_text       TEXT,
    payload_json    TEXT,
    status          TEXT NOT NULL
                    CHECK (status IN ('pending','dispatched','done','dead')),
    received_at_ms  INTEGER NOT NULL,
    updated_at_ms   INTEGER NOT NULL,
    retries         INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_inbox_status_received
    ON inbox(status, received_at_ms);
CREATE INDEX IF NOT EXISTS idx_inbox_channel_msg
    ON inbox(channel, message_id);
"""


@dataclass(frozen=True)
class InboxEntry:
    """One row of the inbox queue (read view)."""

    id: int
    channel: str
    session_key: str
    message_id: str | None
    user_text: str | None
    payload_json: str | None
    status: str
    received_at_ms: int
    updated_at_ms: int
    retries: int
    error: str | None


class Inbox:
    """Async SQLite-backed durable queue for inbound chat messages."""

    __slots__ = ("_conn", "_path")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open(cls, path: Path) -> Inbox:
        ib = cls(path)
        await ib._open()
        return ib

    async def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA busy_timeout = 5000")
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
            raise RuntimeError("Inbox not opened — call open() first")
        return self._conn

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        *,
        channel: str,
        session_key: str,
        message_id: str | None = None,
        user_text: str | None = None,
        payload_json: str | None = None,
    ) -> int:
        """Insert a new pending row and return its id."""
        now_ms = int(time.time() * 1000)
        try:
            cur = await self._c.execute(
                "INSERT INTO inbox (channel, session_key, message_id, "
                "user_text, payload_json, status, received_at_ms, updated_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    channel,
                    session_key or "",
                    message_id,
                    user_text,
                    payload_json,
                    INBOX_PENDING,
                    now_ms,
                    now_ms,
                ),
            )
            await self._c.commit()
            inbox_id = cur.lastrowid
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("inbox.enqueue_failed", error=str(exc))
            return -1
        return int(inbox_id) if inbox_id is not None else -1

    async def mark_dispatched(self, inbox_id: int) -> None:
        await self._set_status(inbox_id, INBOX_DISPATCHED)

    async def mark_done(self, inbox_id: int) -> None:
        await self._set_status(inbox_id, INBOX_DONE)

    async def mark_dead(self, inbox_id: int, error: str | None = None) -> None:
        await self._set_status(inbox_id, INBOX_DEAD, error=error)

    async def increment_retry(self, inbox_id: int, error: str | None = None) -> int:
        """Bump retries; flip to dead if over the cap. Returns new retries.

        Atomic single-statement UPDATE...RETURNING so two concurrent
        callers on the same row can't both read the same ``retries`` and
        clobber each other's increment (#R2-002).
        """
        now_ms = int(time.time() * 1000)
        try:
            cur = await self._c.execute(
                "UPDATE inbox SET retries = retries + 1, "
                "status = CASE WHEN retries + 1 >= ? THEN ? ELSE ? END, "
                "updated_at_ms = ?, error = COALESCE(?, error) "
                "WHERE id = ? AND status IN (?, ?) RETURNING retries",
                (
                    _MAX_RETRIES,
                    INBOX_DEAD,
                    INBOX_PENDING,
                    now_ms,
                    error,
                    inbox_id,
                    INBOX_PENDING,
                    INBOX_DISPATCHED,
                ),
            )
            row = await cur.fetchone()
            await cur.close()
            await self._c.commit()
        except aiosqlite.Error as exc:
            logger.warning("inbox.retry_update_failed", error=str(exc))
            return -1
        if row is None:
            return -1
        return int(row[0])

    async def _set_status(
        self,
        inbox_id: int,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        try:
            await self._c.execute(
                "UPDATE inbox SET status = ?, updated_at_ms = ?, "
                "error = COALESCE(?, error) WHERE id = ?",
                (status, int(time.time() * 1000), error, inbox_id),
            )
            await self._c.commit()
        except aiosqlite.Error as exc:
            logger.warning(
                "inbox.set_status_failed", status=status, error=str(exc)
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_pending(
        self, *, channel: str | None = None, limit: int = 100
    ) -> list[InboxEntry]:
        """List rows in pending or dispatched status (oldest first)."""
        sql = (
            "SELECT id, channel, session_key, message_id, user_text, "
            "payload_json, status, received_at_ms, updated_at_ms, retries, error "
            "FROM inbox WHERE status IN (?, ?)"
        )
        params: list[Any] = [INBOX_PENDING, INBOX_DISPATCHED]
        if channel is not None:
            sql += " AND channel = ?"
            params.append(channel)
        sql += " ORDER BY received_at_ms ASC LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            cur = await self._c.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("inbox.list_pending_failed", error=str(exc))
            return []
        return [_row_to_entry(r) for r in rows]

    async def list_recent(
        self, *, channel: str | None = None, limit: int = 50
    ) -> list[InboxEntry]:
        """List the most-recent rows in any status (newest first)."""
        sql = (
            "SELECT id, channel, session_key, message_id, user_text, "
            "payload_json, status, received_at_ms, updated_at_ms, retries, error "
            "FROM inbox"
        )
        params: list[Any] = []
        if channel is not None:
            sql += " WHERE channel = ?"
            params.append(channel)
        sql += " ORDER BY received_at_ms DESC LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            cur = await self._c.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("inbox.list_recent_failed", error=str(exc))
            return []
        return [_row_to_entry(r) for r in rows]

    async def stuck_dispatched_count(self, older_than_seconds: int = 600) -> int:
        """Count rows stuck in ``dispatched`` longer than ``older_than_seconds``.

        Useful for boot-time observability — a healthy gateway shouldn't
        have any of these once it has been running for a while. The
        boot drainer logs this number to surface stale work.
        """
        cutoff = int(time.time() * 1000) - older_than_seconds * 1000
        try:
            cur = await self._c.execute(
                "SELECT COUNT(*) FROM inbox WHERE status = ? AND updated_at_ms < ?",
                (INBOX_DISPATCHED, cutoff),
            )
            row = await cur.fetchone()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("inbox.stuck_count_failed", error=str(exc))
            return 0
        return int(row[0]) if row else 0

    async def reset_stale_dispatched(self, older_than_seconds: int = 600) -> int:
        """Boot-time helper: rows still ``dispatched`` after a long stall
        almost certainly belong to a crashed previous process. Flip them
        back to ``pending`` so the next live arrival can supersede them
        (T4.1's resume already gives us the in-flight half-finished
        turn). Returns the count flipped."""
        cutoff = int(time.time() * 1000) - older_than_seconds * 1000
        try:
            cur = await self._c.execute(
                "UPDATE inbox SET status = ?, updated_at_ms = ?, "
                "error = COALESCE(error, ?) "
                "WHERE status = ? AND updated_at_ms < ?",
                (
                    INBOX_PENDING,
                    int(time.time() * 1000),
                    "stale: gateway restart left row in dispatched",
                    INBOX_DISPATCHED,
                    cutoff,
                ),
            )
            await self._c.commit()
            n = cur.rowcount or 0
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("inbox.reset_stale_failed", error=str(exc))
            return 0
        if n:
            logger.info("inbox.reset_stale_dispatched", count=n)
        return int(n)


def _row_to_entry(row: Sequence[Any]) -> InboxEntry:
    return InboxEntry(
        id=int(row[0]),
        channel=str(row[1]),
        session_key=str(row[2]),
        message_id=row[3],
        user_text=row[4],
        payload_json=row[5],
        status=str(row[6]),
        received_at_ms=int(row[7]),
        updated_at_ms=int(row[8]),
        retries=int(row[9]),
        error=row[10],
    )


__all__ = [
    "INBOX_DEAD",
    "INBOX_DISPATCHED",
    "INBOX_DONE",
    "INBOX_PENDING",
    "Inbox",
    "InboxEntry",
]
