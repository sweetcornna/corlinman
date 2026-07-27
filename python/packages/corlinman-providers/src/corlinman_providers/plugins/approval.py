"""Tool approval queue backed by SQLite ``pending_approvals``.

Python port of the design TODOs captured in
``rust/crates/corlinman-plugins/src/approval.rs``. The Rust file is itself a
stub (TODO comments only); the Python port lands a working implementation
because the providers package is the one that actually executes plugin tool
calls.

Lifecycle:
  1. The runtime enqueues an ``AwaitingApproval`` record carrying
     ``call_id``, ``plugin``, ``tool``, ``args_preview``, ``session_key`` and
     ``reason``.
  2. An admin UI polls :meth:`pending` and writes a decision back via
     :meth:`decide`.
  3. The waiting coroutine (returned by :meth:`enqueue_and_wait`) resolves
     with the decision.

First-use policy (plan §7.8): the higher-level dispatcher should consult
:meth:`is_first_use` for non-bundled plugins and short-circuit to a
``prompt`` decision on the first call per ``session_key``.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import aiosqlite


class ApprovalDecision(StrEnum):
    """Operator decision attached to a pending approval.

    ``TIMEOUT`` (W3-4) is never operator-supplied: it marks a row whose
    parked stream ended (or whose agent-side wait expired) before a human
    answered, so the audit trail can tell "a human denied this" apart
    from "nobody answered in time".
    """

    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"
    TIMEOUT = "timeout"


@dataclass
class ApprovalRequest:
    """A single ``AwaitingApproval`` row."""

    call_id: str
    plugin: str
    tool: str
    args_preview: str
    session_key: str
    reason: str
    created_at: float = field(default_factory=time.time)


@dataclass
class ApprovalRecord(ApprovalRequest):
    """An approval row plus its current decision state.

    ``decision_reason`` (W3-4) is the decider's free-text rationale —
    the admin UI's deny reason, or an explanatory note on timeout rows.
    ``reason`` (inherited) stays the *request* side: why the call needed
    approval in the first place.
    """

    decision: ApprovalDecision | None = None
    decided_at: float | None = None
    decision_reason: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_approvals (
    call_id          TEXT NOT NULL,
    plugin           TEXT NOT NULL,
    tool             TEXT NOT NULL,
    args_preview     TEXT NOT NULL,
    session_key      TEXT NOT NULL,
    reason           TEXT NOT NULL,
    created_at       REAL NOT NULL,
    decision         TEXT,
    decided_at       REAL,
    decision_reason  TEXT,
    -- Composite on purpose (mirrors ApprovalBroker._key): provider
    -- tool-call ids are NOT globally unique — index-style ``call_0``
    -- collides across concurrent streams, and a bare call_id PK would
    -- let one stream's row clobber another's.
    PRIMARY KEY (session_key, call_id)
);

CREATE INDEX IF NOT EXISTS idx_pending_approvals_session
    ON pending_approvals(session_key);
"""


def default_approvals_db_path(data_dir: str | Path | None = None) -> Path:
    """The durable approvals DB location: ``<data_dir>/authz/approvals.sqlite3``.

    Mirrors the GrantStore's data-dir resolution (explicit arg, then
    ``CORLINMAN_DATA_DIR``, then ``~/.corlinman``) so both authz artefacts
    live side by side under ``<data_dir>/authz/``.
    """
    if data_dir is None:
        env = os.environ.get("CORLINMAN_DATA_DIR", "").strip()
        data_dir = Path(env) if env else Path.home() / ".corlinman"
    return Path(data_dir) / "authz" / "approvals.sqlite3"


async def _apply_migrations(conn: aiosqlite.Connection) -> None:
    """Bring a pre-W3-4 table (no ``decision_reason`` column) up to date.

    The Python plane never shipped a migration runner for this table
    (the Rust-era vector migration v3 was lost in the port), so the
    store self-migrates: additive ``ALTER TABLE`` guarded by a pragma
    check, safe to run on every connection bootstrap.
    """
    cur = await conn.execute("PRAGMA table_info(pending_approvals)")
    rows = await cur.fetchall()
    columns = {row[1] for row in rows}
    if "decision_reason" not in columns:
        await conn.execute(
            "ALTER TABLE pending_approvals ADD COLUMN decision_reason TEXT"
        )
    # W3-4 review fix: rows must key on (session_key, call_id) — rebuild a
    # legacy table whose sole PK was call_id (pk flag on exactly that row).
    pk_cols = [row[1] for row in rows if row[5]]
    if pk_cols == ["call_id"]:
        await conn.executescript(
            """
            ALTER TABLE pending_approvals RENAME TO pending_approvals_old;
            """
            + _SCHEMA
            + """
            INSERT OR IGNORE INTO pending_approvals
                (call_id, plugin, tool, args_preview, session_key, reason,
                 created_at, decision, decided_at, decision_reason)
            SELECT call_id, plugin, tool, args_preview, session_key, reason,
                   created_at, decision, decided_at, decision_reason
              FROM pending_approvals_old;
            DROP TABLE pending_approvals_old;
            """
        )


class ApprovalStore:
    """SQLite-backed persistence for the approval queue.

    Built on ``aiosqlite`` so it composes with the rest of the async plane.
    For ``:memory:`` databases we hold a single shared connection (each
    ``aiosqlite.connect`` to ``:memory:`` opens a private in-memory DB, so
    multiple connections would each see a different empty database). For
    file-backed paths we open a fresh connection per call so the store
    survives gateway restarts.

    The default (``path=None``) is a real file under ``<data_dir>/authz/``
    (W3-4) — the previous ``":memory:"`` default meant every gateway
    restart silently dropped the pending queue. Tests that want a
    throwaway store pass ``":memory:"`` explicitly.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path) if path is not None else str(default_approvals_db_path())
        self._lock = asyncio.Lock()
        self._initialised = False
        # Shared connection for in-memory databases; None for file paths.
        self._shared_conn: aiosqlite.Connection | None = None

    async def _ensure_shared(self) -> aiosqlite.Connection:
        if self._shared_conn is None:
            conn = await aiosqlite.connect(self.path)
            conn.row_factory = aiosqlite.Row
            await conn.executescript(_SCHEMA)
            await _apply_migrations(conn)
            await conn.commit()
            self._shared_conn = conn
            self._initialised = True
        return self._shared_conn

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        if self.path == ":memory:":
            async with self._lock:
                conn = await self._ensure_shared()
                yield conn
            return

        if not self._initialised:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            if not self._initialised:
                async with self._lock:
                    if not self._initialised:
                        await conn.executescript(_SCHEMA)
                        await _apply_migrations(conn)
                        await conn.commit()
                        self._initialised = True
            yield conn

    async def init(self) -> None:
        """Idempotent schema bootstrap; tests call this eagerly."""
        async with self._conn():
            pass

    async def close(self) -> None:
        """Close the shared in-memory connection, if any."""
        if self._shared_conn is not None:
            await self._shared_conn.close()
            self._shared_conn = None
            self._initialised = False

    async def insert(self, request: ApprovalRequest) -> None:
        """Insert a fresh pending row. Existing rows with the same
        ``call_id`` are replaced (defensive — the gateway should never
        reuse call_ids).
        """
        async with self._conn() as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO pending_approvals
                    (call_id, plugin, tool, args_preview, session_key,
                     reason, created_at, decision, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    request.call_id,
                    request.plugin,
                    request.tool,
                    request.args_preview,
                    request.session_key,
                    request.reason,
                    request.created_at,
                ),
            )
            await conn.commit()

    async def decide(
        self,
        call_id: str,
        decision: ApprovalDecision,
        *,
        reason: str | None = None,
        session_key: str | None = None,
    ) -> bool:
        """Record an operator decision against ``call_id``. Returns ``True``
        when a row was updated, ``False`` otherwise (unknown id or already
        decided). ``reason`` (W3-4) is the decider's rationale and lands in
        the ``decision_reason`` column for the audit trail. ``session_key``
        narrows to the composite key; ``None`` updates every pending row of
        that call_id (callers that already resolved ambiguity).
        """
        async with self._conn() as conn:
            if session_key is None:
                cur = await conn.execute(
                    """
                    UPDATE pending_approvals
                       SET decision = ?, decided_at = ?, decision_reason = ?
                     WHERE call_id = ? AND decision IS NULL
                    """,
                    (decision.value, time.time(), reason, call_id),
                )
            else:
                cur = await conn.execute(
                    """
                    UPDATE pending_approvals
                       SET decision = ?, decided_at = ?, decision_reason = ?
                     WHERE call_id = ? AND session_key = ? AND decision IS NULL
                    """,
                    (decision.value, time.time(), reason, call_id, session_key),
                )
            await conn.commit()
            return cur.rowcount > 0

    async def pending_for_call(self, call_id: str) -> list[ApprovalRecord]:
        """Every still-pending row for ``call_id`` (any session).

        The admin decide route uses this to detect ambiguity: provider
        call ids collide across concurrent streams, so a bare call_id
        may name more than one parked approval.
        """
        async with self._conn() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM pending_approvals
                 WHERE call_id = ? AND decision IS NULL
              ORDER BY created_at ASC
                """,
                (call_id,),
            )
            return [_row_to_record(row) for row in await cur.fetchall()]

    async def reconcile_orphaned(self, *, reason: str = "gateway restart") -> int:
        """Mark every still-pending row ``timeout`` (boot-time sweep).

        A durable queue re-opened after a crash/restart holds rows whose
        streams died with the old process — no decision can ever reach
        them, so leaving them pending would show phantom entries forever
        and let ``decide`` report success for calls nothing consumes
        (W3-4 review fix). Returns the number of rows swept.
        """
        async with self._conn() as conn:
            cur = await conn.execute(
                """
                UPDATE pending_approvals
                   SET decision = ?, decided_at = ?, decision_reason = ?
                 WHERE decision IS NULL
                """,
                (ApprovalDecision.TIMEOUT.value, time.time(), reason),
            )
            await conn.commit()
            return cur.rowcount

    async def get(self, call_id: str) -> ApprovalRecord | None:
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM pending_approvals WHERE call_id = ?",
                (call_id,),
            )
            row = await cur.fetchone()
            return _row_to_record(row) if row else None

    async def pending(self) -> list[ApprovalRecord]:
        async with self._conn() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM pending_approvals
                 WHERE decision IS NULL
              ORDER BY created_at ASC
                """
            )
            rows = await cur.fetchall()
            return [_row_to_record(r) for r in rows]

    async def decided(self, *, limit: int = 200) -> list[ApprovalRecord]:
        """Decided rows, most recent first (W3-4).

        The formal query surface for the admin ``include_decided`` view
        and the SSE ``decided`` frames — callers no longer reach into the
        private ``_conn`` helper to re-implement this SQL.
        """
        async with self._conn() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM pending_approvals
                 WHERE decision IS NOT NULL
              ORDER BY decided_at DESC
                 LIMIT ?
                """,
                (int(limit),),
            )
            rows = await cur.fetchall()
            return [_row_to_record(r) for r in rows]

    async def has_prior_approval_for_session(
        self, session_key: str, plugin: str
    ) -> bool:
        """Whether ``session_key`` has ever decided ``allow`` for ``plugin``.

        Used by the first-use policy: non-bundled plugins are gated behind
        a ``prompt`` decision on their first call in a session.
        """
        async with self._conn() as conn:
            cur = await conn.execute(
                """
                SELECT 1 FROM pending_approvals
                 WHERE session_key = ? AND plugin = ? AND decision = ?
                 LIMIT 1
                """,
                (session_key, plugin, ApprovalDecision.ALLOW.value),
            )
            return (await cur.fetchone()) is not None


def _row_to_record(row: aiosqlite.Row) -> ApprovalRecord:
    decision_raw = row["decision"]
    # ``decision_reason`` exists on every migrated DB, but stay tolerant of
    # a row object built from a legacy SELECT (defensive keys() probe).
    decision_reason = (
        row["decision_reason"] if "decision_reason" in row.keys() else None
    )
    return ApprovalRecord(
        call_id=row["call_id"],
        plugin=row["plugin"],
        tool=row["tool"],
        args_preview=row["args_preview"],
        session_key=row["session_key"],
        reason=row["reason"],
        created_at=float(row["created_at"]),
        decision=ApprovalDecision(decision_raw) if decision_raw else None,
        decided_at=float(row["decided_at"]) if row["decided_at"] is not None else None,
        decision_reason=decision_reason,
    )


class ApprovalQueue:
    """Coordinates :class:`ApprovalStore` with in-memory waiters.

    The store is the durable source of truth (survives gateway restarts);
    the in-process waiter map wakes the awaiting tool call as soon as
    :meth:`decide` is invoked.
    """

    def __init__(self, store: ApprovalStore | None = None) -> None:
        self.store = store or ApprovalStore()
        self._waiters: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._waiters_lock = asyncio.Lock()

    @staticmethod
    def new_call_id() -> str:
        """Generate a fresh, opaque call id."""
        return f"call_{uuid.uuid4().hex}"

    async def enqueue(self, request: ApprovalRequest) -> None:
        """Persist a request without blocking. Pair with :meth:`wait` to
        block until a decision arrives.
        """
        await self.store.insert(request)

    async def wait(self, call_id: str, *, timeout: float | None = None) -> ApprovalDecision:
        """Block until an operator decides ``call_id``. Raises
        :class:`asyncio.TimeoutError` if ``timeout`` elapses first.
        """
        # Fast-path: maybe a decision is already persisted.
        record = await self.store.get(call_id)
        if record is not None and record.decision is not None:
            return record.decision

        async with self._waiters_lock:
            fut = self._waiters.get(call_id)
            if fut is None:
                fut = asyncio.get_running_loop().create_future()
                self._waiters[call_id] = fut

        try:
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            async with self._waiters_lock:
                self._waiters.pop(call_id, None)

    async def enqueue_and_wait(
        self,
        request: ApprovalRequest,
        *,
        timeout: float | None = None,
    ) -> ApprovalDecision:
        """Convenience: persist + wait in one call."""
        await self.enqueue(request)
        return await self.wait(request.call_id, timeout=timeout)

    async def decide(
        self,
        call_id: str,
        decision: ApprovalDecision,
        *,
        reason: str | None = None,
        session_key: str | None = None,
    ) -> bool:
        """Persist the decision (+ optional rationale) then wake any
        in-process waiter. ``session_key`` narrows to the composite row
        key (provider call ids collide across streams)."""
        wrote = await self.store.decide(
            call_id, decision, reason=reason, session_key=session_key
        )
        async with self._waiters_lock:
            fut = self._waiters.get(call_id)
        if fut is not None and not fut.done():
            fut.set_result(decision)
        return wrote

    async def pending(self) -> list[ApprovalRecord]:
        return await self.store.pending()

    async def is_first_use(self, session_key: str, plugin: str) -> bool:
        """First-use policy helper: returns ``True`` when the session has
        no prior ``allow`` decision for the plugin.
        """
        return not await self.store.has_prior_approval_for_session(
            session_key, plugin
        )


__all__ = [
    "ApprovalDecision",
    "ApprovalQueue",
    "ApprovalRecord",
    "ApprovalRequest",
    "ApprovalStore",
    "default_approvals_db_path",
]
