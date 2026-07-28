"""Persistent QQ group-message history for the monitor/digest feature.

Records every inbound group message from *monitored* groups under
``<data_dir>/qq_group_history.sqlite`` so the digest loop can summarise
"everything person X (or everyone) said in the last N hours" — a window
the in-memory proactive buffer (30 rows, 200-char cap, sender id folded
into the display name) cannot serve.

Deliberately mirrors :mod:`corlinman_server.inbox`: aiosqlite + WAL,
best-effort writes that log-and-return instead of raising — the channel
dispatch loop must never die because a history INSERT failed.

The ``monitor_state`` table keeps each monitor rule's last-fire
timestamp so a gateway restart neither double-sends a digest nor loses
the schedule position.

Privacy note: rows are other people's chat messages. Callers must not
log message text; this module only ever logs counts and error strings.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

#: Per-message text cap. Long forwarded walls of text get truncated —
#: the digest prompt caps line length anyway, and unbounded rows would
#: let one paste balloon the store.
TEXT_CAP = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id     TEXT NOT NULL,
    group_id        TEXT NOT NULL,
    sender_user_id  TEXT NOT NULL,
    sender_name     TEXT NOT NULL DEFAULT '',
    message_id      TEXT,
    event_time_ms   INTEGER NOT NULL,
    received_at_ms  INTEGER NOT NULL,
    text            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_group_messages_window
    ON group_messages(instance_id, group_id, received_at_ms);

CREATE TABLE IF NOT EXISTS monitor_state (
    key           TEXT PRIMARY KEY,
    last_fire_ms  INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class GroupMessage:
    """One recorded group message (read view)."""

    id: int
    instance_id: str
    group_id: str
    sender_user_id: str
    sender_name: str
    message_id: str | None
    event_time_ms: int
    received_at_ms: int
    text: str


class QqGroupHistory:
    """Async SQLite-backed history of monitored QQ group messages."""

    __slots__ = ("_conn", "_path")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open(cls, path: Path) -> QqGroupHistory:
        store = cls(path)
        await store._open()
        return store

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
            raise RuntimeError("QqGroupHistory not opened — call open() first")
        return self._conn

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def record(
        self,
        *,
        instance_id: str,
        group_id: str,
        sender_user_id: str,
        sender_name: str = "",
        message_id: str | None = None,
        event_time_ms: int | None = None,
        text: str,
    ) -> int:
        """Insert one message row; returns the row id, or -1 when skipped
        (blank text) / on write failure (logged, never raised)."""
        body = (text or "").strip()
        if not body:
            return -1
        now_ms = int(time.time() * 1000)
        try:
            cur = await self._c.execute(
                "INSERT INTO group_messages (instance_id, group_id, "
                "sender_user_id, sender_name, message_id, event_time_ms, "
                "received_at_ms, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    instance_id,
                    group_id,
                    sender_user_id,
                    sender_name or "",
                    message_id,
                    event_time_ms if event_time_ms is not None else now_ms,
                    now_ms,
                    body[:TEXT_CAP],
                ),
            )
            await self._c.commit()
            row_id = cur.lastrowid
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("qq_group_history.record_failed", error=str(exc))
            return -1
        return int(row_id) if row_id is not None else -1

    async def prune(self, *, older_than_ms: int) -> int:
        """Delete rows received before the cutoff; returns rows removed."""
        try:
            cur = await self._c.execute(
                "DELETE FROM group_messages WHERE received_at_ms < ?",
                (int(older_than_ms),),
            )
            await self._c.commit()
            n = cur.rowcount or 0
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("qq_group_history.prune_failed", error=str(exc))
            return 0
        return int(n)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_window(
        self,
        *,
        instance_id: str,
        group_id: str,
        since_ms: int,
        until_ms: int | None = None,
        sender_ids: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> list[GroupMessage]:
        """Messages in ``[since_ms, until_ms)``, oldest first.

        When more than ``limit`` rows match, the NEWEST ``limit`` rows are
        returned (a digest cares about the tail of a busy window, not the
        head). ``sender_ids`` narrows to specific QQ numbers; ``None`` or
        empty means everyone.
        """
        sql = (
            "SELECT id, instance_id, group_id, sender_user_id, sender_name, "
            "message_id, event_time_ms, received_at_ms, text "
            "FROM group_messages "
            "WHERE instance_id = ? AND group_id = ? AND received_at_ms >= ?"
        )
        params: list[Any] = [instance_id, group_id, int(since_ms)]
        if until_ms is not None:
            sql += " AND received_at_ms < ?"
            params.append(int(until_ms))
        senders = [str(s) for s in (sender_ids or []) if str(s).strip()]
        if senders:
            sql += f" AND sender_user_id IN ({','.join('?' * len(senders))})"
            params.extend(senders)
        sql += " ORDER BY received_at_ms DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            cur = await self._c.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("qq_group_history.list_window_failed", error=str(exc))
            return []
        return [_row_to_message(r) for r in reversed(list(rows))]

    async def count_window(
        self,
        *,
        instance_id: str,
        group_id: str,
        since_ms: int,
        until_ms: int | None = None,
        sender_ids: Iterable[str] | None = None,
    ) -> int:
        """Row count for the same filter shape as :meth:`list_window`."""
        sql = (
            "SELECT COUNT(*) FROM group_messages "
            "WHERE instance_id = ? AND group_id = ? AND received_at_ms >= ?"
        )
        params: list[Any] = [instance_id, group_id, int(since_ms)]
        if until_ms is not None:
            sql += " AND received_at_ms < ?"
            params.append(int(until_ms))
        senders = [str(s) for s in (sender_ids or []) if str(s).strip()]
        if senders:
            sql += f" AND sender_user_id IN ({','.join('?' * len(senders))})"
            params.extend(senders)
        try:
            cur = await self._c.execute(sql, params)
            row = await cur.fetchone()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("qq_group_history.count_window_failed", error=str(exc))
            return 0
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Monitor schedule state
    # ------------------------------------------------------------------

    async def get_last_fire(self, key: str) -> int | None:
        """Last-fire timestamp (ms) for one monitor key, or None."""
        try:
            cur = await self._c.execute(
                "SELECT last_fire_ms FROM monitor_state WHERE key = ?", (key,)
            )
            row = await cur.fetchone()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("qq_group_history.get_last_fire_failed", error=str(exc))
            return None
        return int(row[0]) if row else None

    async def set_last_fire(self, key: str, ts_ms: int) -> None:
        try:
            await self._c.execute(
                "INSERT INTO monitor_state (key, last_fire_ms) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET last_fire_ms = excluded.last_fire_ms",
                (key, int(ts_ms)),
            )
            await self._c.commit()
        except aiosqlite.Error as exc:
            logger.warning("qq_group_history.set_last_fire_failed", error=str(exc))


def _row_to_message(row: Sequence[Any]) -> GroupMessage:
    return GroupMessage(
        id=int(row[0]),
        instance_id=str(row[1]),
        group_id=str(row[2]),
        sender_user_id=str(row[3]),
        sender_name=str(row[4] or ""),
        message_id=row[5],
        event_time_ms=int(row[6]),
        received_at_ms=int(row[7]),
        text=str(row[8]),
    )


__all__ = [
    "TEXT_CAP",
    "GroupMessage",
    "QqGroupHistory",
]
