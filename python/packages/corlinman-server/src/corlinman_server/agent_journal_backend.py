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

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Sequence
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

    Session-meta fields (in-app chat MVP):

    * ``title`` — operator-supplied display label (``None`` until set
      via ``PATCH /admin/sessions/{key}``).
    * ``pinned`` — sticky ordering for the sidebar; pinned sessions
      sort above unpinned regardless of ``last_seen_at_ms``. Defaults
      to ``False`` for every legacy session that pre-dates the
      ``session_meta`` table.
    * ``archived`` — operator hint to hide the session from the
      default listing. Defaults to ``False``. The list endpoint still
      returns archived sessions today (the UI decides whether to
      filter them); a future ``?archived=...`` query param can opt
      out without a wire change.
    """

    session_key: str
    first_seen_at_ms: int
    last_seen_at_ms: int
    turn_count: int
    message_count: int
    last_user_text: str | None
    last_status: str | None
    title: str | None = None
    pinned: bool = False
    archived: bool = False


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
        pending_question_json: str | None = None,
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

        ``pending_question_json`` (ask_user) optionally stores the JSON
        payload of the ``ask_user`` tool call that ended the turn — the
        question text plus any canned answer options. Purely
        informational at this layer (no read path inside the chat
        handler yet); the admin UI is the consumer. ``None`` is the
        normal case.

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

    async def append_messages(
        self,
        turn_id: int,
        messages: list[dict[str, Any]],
    ) -> None:
        """Append multiple messages to ``turn_id`` in a single transaction.

        Each dict must carry ``role`` and ``content``; ``tool_call_id``
        and ``tool_calls`` are optional. Backends wrap the whole insert
        sequence in one transaction (one BEGIN / N inserts / one COMMIT
        on SQLite; one pooled acquire + one ``conn.transaction()`` on
        Postgres). Empty ``messages`` is a no-op.

        Perf: collapses the (assistant tool_call, tool result) pair the
        chat handler writes after every builtin dispatch into a single
        commit, cutting ~10ms / commit overhead for 3-tool rounds.
        Backward compat: ``append_message`` is preserved — this method
        is additive.
        """
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

    async def session_exists(self, session_key: str) -> bool:
        """Return ``True`` when at least one ``turns`` row exists for
        ``session_key``. Used by ``PATCH /admin/sessions/{key}`` to
        decide between 404 (no such session) and an empty-meta upsert.
        """
        ...

    async def update_session_meta(
        self,
        session_key: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> SessionSummary | None:
        """Upsert the operator-supplied metadata for ``session_key``.

        Each field is independently optional — ``None`` means "leave
        the existing value alone" (the SQL is a partial UPDATE rather
        than a full row replace). Backends MUST refuse to create a
        meta row for a session_key that has no journaled turns and
        return ``None`` so the route surfaces a 404.

        Returns the freshly-refreshed :class:`SessionSummary` (same
        shape :meth:`list_session_summaries` emits) so the route can
        echo the post-update state back without a second round-trip.
        """
        ...

    # ------------------------------------------------------------------
    # W1.2 — turn events timeline (admin observability).
    #
    # Backends that don't yet implement these may raise
    # ``NotImplementedError``; the facade catches and degrades gracefully
    # (the UI just shows an empty timeline for that turn).
    # ------------------------------------------------------------------

    async def append_event(self, envelope: Any) -> None:
        """Persist one :class:`EventEnvelope` to the turn timeline."""
        ...

    async def append_events_batch(self, envelopes: Sequence[Any]) -> None:
        """Persist many :class:`EventEnvelope` records in one transaction."""
        ...

    async def load_events(self, turn_id: str | int) -> list[dict[str, Any]]:
        """Return every event for ``turn_id`` in ``sequence ASC`` order."""
        ...

    def iter_events(
        self, turn_id: str | int, start_sequence: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream events with ``sequence > start_sequence`` (SSE catch-up)."""
        ...

    async def get_session_turn_ids(
        self, session_key: str, limit: int = 50
    ) -> list[int]:
        """Most-recent turn ids for ``session_key`` (admin SSE bootstrap)."""
        ...

    async def list_session_turns(
        self,
        session_key: str,
        *,
        limit: int = 50,
        before_turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Per-turn metadata for the past-turns navigator (W1.2 UI)."""
        ...

    async def update_turn_cost(
        self,
        turn_id: int,
        *,
        estimated_cost_usd: float | None,
        cost_status: str | None,
    ) -> None:
        """Late-binding update for the W1.2 cost columns."""
        ...


# ---------------------------------------------------------------------------
# SQLite backend — the default, drop-in replacement for the original impl.
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    turn_id              INTEGER PRIMARY KEY,
    session_key          TEXT    NOT NULL,
    status               TEXT    NOT NULL
                                CHECK (status IN ('in_progress', 'completed', 'errored')),
    started_at_ms        INTEGER NOT NULL,
    ended_at_ms          INTEGER,
    user_text            TEXT,
    user_id              TEXT,
    channel              TEXT    NOT NULL DEFAULT '',
    pending_question_json TEXT,
    error                TEXT
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

# Pre-ask_user deployments have no ``pending_question_json`` column.
# Nullable (defaults to NULL) so legacy rows round-trip cleanly. The
# column is purely informational — the chat handler does not read it
# back today; future surfaces (admin UI "session is waiting for an
# answer" badge) consume it.
_PENDING_QUESTION_MIGRATION = (
    "ALTER TABLE turns ADD COLUMN pending_question_json TEXT"
)


# ---------------------------------------------------------------------------
# W1.2 — turn_events timeline + per-turn aggregate columns.
#
# Lives alongside ``turns`` / ``turn_messages``; the gateway persists every
# ``EventEnvelope`` it emits so the admin UI replay endpoint can stream past
# turns with the same fidelity as live observers.
#
# Schema source-of-truth: ``journal_migrations/004_turn_events.sql``. We keep
# the DDL inline so the backend stays self-bootstrapping on a fresh DB
# (matches the pattern for ``turns`` / ``turn_messages``).
# ---------------------------------------------------------------------------

_TURN_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_events (
    turn_id      TEXT    NOT NULL,
    sequence     INTEGER NOT NULL,
    event_type   TEXT    NOT NULL,
    payload_json TEXT    NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    PRIMARY KEY (turn_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_turn_events_turn
    ON turn_events(turn_id);

CREATE INDEX IF NOT EXISTS idx_turn_events_timestamp
    ON turn_events(timestamp_ms);
"""

# In-app chat MVP — operator-supplied per-session metadata. Kept in a
# sibling table (not on ``turns``) so updating ``title`` / ``pinned`` /
# ``archived`` doesn't fan out across N turn rows and so legacy reads
# that ignore the columns continue to work unchanged. One row per
# session_key; LEFT JOIN'd from ``list_session_summaries`` with
# COALESCE defaults so sessions without a meta row still render.
_SESSION_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_meta (
    session_key TEXT PRIMARY KEY,
    title       TEXT,
    pinned      INTEGER NOT NULL DEFAULT 0
                        CHECK (pinned IN (0, 1)),
    archived    INTEGER NOT NULL DEFAULT 0
                        CHECK (archived IN (0, 1)),
    updated_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_meta_pinned
    ON session_meta(pinned DESC);
"""

# Per-turn aggregate columns the UI surfaces (elapsed, cost, tool count,
# reasoning token count). All nullable / defaulted so adding them to an
# existing journal can't break a pre-migration reader.
_TURN_EVENT_AGGREGATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("elapsed_ms", "ALTER TABLE turns ADD COLUMN elapsed_ms INTEGER"),
    (
        "estimated_cost_usd",
        "ALTER TABLE turns ADD COLUMN estimated_cost_usd REAL",
    ),
    ("cost_status", "ALTER TABLE turns ADD COLUMN cost_status TEXT"),
    (
        "tool_call_count",
        "ALTER TABLE turns ADD COLUMN tool_call_count INTEGER DEFAULT 0",
    ),
    (
        "reasoning_token_count",
        "ALTER TABLE turns ADD COLUMN reasoning_token_count INTEGER DEFAULT 0",
    ),
)


def _envelope_to_row(envelope: Any) -> tuple[str, int, str, str, int]:
    """Project an :class:`EventEnvelope` (or dict) into ``turn_events`` shape.

    Be defensive: W1.1's ``EventEnvelope`` dataclass is the canonical input,
    but callers may also pass a plain dict (the agent layer is still
    evolving, and the SSE replay endpoint sometimes round-trips events
    through JSON). The mapping is:

    - ``turn_id`` → ``str(envelope.turn_id)`` (stored as TEXT so int turn_ids
      and string sub-agent ids share one column)
    - ``sequence`` → ``int(envelope.sequence)``
    - ``event_type`` → ``str(envelope.event_type)`` — the discriminator tag
      (e.g. ``"TextDelta"``, ``"ToolStateRunning"``); falls back to the
      class name of ``envelope.event`` when ``event_type`` is absent
    - ``payload_json`` → ``json.dumps(envelope.payload)``, with ``event``
      and ``event.__dict__`` as fallbacks; coerced to ``"{}"`` on
      serialisation failure so the INSERT still lands
    - ``timestamp_ms`` → ``int(envelope.timestamp_ms)``; defaults to wall
      clock when missing

    Raises ``ValueError`` if ``turn_id`` or ``sequence`` cannot be derived
    — those are the storage primary key and a silent zero would corrupt
    the table.
    """
    # turn_id (PK part 1) — required.
    if isinstance(envelope, dict):
        turn_id_raw = envelope.get("turn_id")
    else:
        turn_id_raw = getattr(envelope, "turn_id", None)
    if turn_id_raw is None:
        raise ValueError("envelope missing required field: turn_id")
    turn_id = str(turn_id_raw)

    # sequence (PK part 2) — required.
    if isinstance(envelope, dict):
        seq_raw = envelope.get("sequence")
    else:
        seq_raw = getattr(envelope, "sequence", None)
    if seq_raw is None:
        raise ValueError("envelope missing required field: sequence")
    sequence = int(seq_raw)

    # event_type — fall back to class name of the wrapped event so we
    # never persist an empty discriminator.
    if isinstance(envelope, dict):
        event_type = envelope.get("event_type")
        event_obj: Any = envelope.get("event")
    else:
        event_type = getattr(envelope, "event_type", None)
        event_obj = getattr(envelope, "event", None)
    if not event_type and event_obj is not None:
        event_type = type(event_obj).__name__
    event_type = str(event_type or "Unknown")

    # payload_json — prefer ``envelope.payload``; else fall back to the
    # wrapped event (dataclass-friendly via ``__dict__``); else ``{}``.
    if isinstance(envelope, dict):
        payload = envelope.get("payload")
        if payload is None:
            payload = envelope.get("event")
    else:
        payload = getattr(envelope, "payload", None)
        if payload is None:
            payload = getattr(envelope, "event", None)
    payload_text: str
    if payload is None:
        payload_text = "{}"
    elif isinstance(payload, str):
        # Already-serialised payload — accept verbatim.
        payload_text = payload
    else:
        try:
            payload_text = json.dumps(payload, default=_payload_json_default)
        except (TypeError, ValueError):
            payload_text = "{}"

    # timestamp_ms — wall clock fallback so a malformed envelope still
    # round-trips with a sensible value.
    if isinstance(envelope, dict):
        ts_raw = envelope.get("timestamp_ms")
    else:
        ts_raw = getattr(envelope, "timestamp_ms", None)
    timestamp_ms = int(ts_raw) if ts_raw is not None else int(time.time() * 1000)

    return turn_id, sequence, event_type, payload_text, timestamp_ms


def _payload_json_default(obj: Any) -> Any:
    """Best-effort ``default=`` for :func:`json.dumps`.

    Handles dataclasses + objects exposing ``__dict__``; falls back to
    ``repr`` so a single unserialisable field can't bury the whole event.
    """
    if hasattr(obj, "__dataclass_fields__"):
        # Treat dataclass instances as their field dict — keeps the same
        # shape as ``dataclasses.asdict`` for flat dataclasses without
        # paying its recursion cost.
        return {k: getattr(obj, k) for k in obj.__dataclass_fields__}
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return repr(obj)


class SqliteJournalBackend:
    """Single-process SQLite backend over ``aiosqlite``.

    Schema is auto-created on open. WAL mode + ``synchronous = NORMAL``
    so concurrent sessions read+write without serializing on the writer.
    """

    __slots__ = ("_path", "_conn", "_write_lock")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        # B3: every session shares ONE aiosqlite connection, but the write
        # methods mix transaction models on it — explicit
        # ``BEGIN IMMEDIATE`` / ``COMMIT`` envelopes (append_messages,
        # delete_session, append_events_batch) alongside bare autocommit
        # ``execute()`` + ``commit()`` (complete_turn, error_turn,
        # append_event, ...). Because ``commit()`` is connection-*global*,
        # a bare commit from session B mid-flight would flush — and end —
        # session A's open transaction, breaking append_messages'
        # documented all-or-nothing atomicity. The per-session servicer
        # lock only serialises the SAME session, so this lock serialises
        # ALL backend writes regardless of session: no two coroutines can
        # interleave a transaction on the shared connection. Reads are
        # left lock-free (they don't commit). Tradeoff: writes no longer
        # overlap, but a chat-turn journal's write volume is low and
        # correctness wins.
        self._write_lock = asyncio.Lock()

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
            if "pending_question_json" not in existing:
                await conn.execute(_PENDING_QUESTION_MIGRATION)
                await conn.commit()
                logger.info(
                    "agent.journal.migrated",
                    migration="pending_question_json_column",
                )
            # W1.2 — turn_events table + per-turn aggregate columns. The
            # CREATE statements in ``_TURN_EVENTS_SCHEMA`` already carry
            # ``IF NOT EXISTS`` guards; the ALTERs need an explicit
            # ``PRAGMA table_info`` gate because SQLite < 3.35 doesn't
            # support ``ADD COLUMN IF NOT EXISTS`` and we still target it.
            await conn.executescript(_TURN_EVENTS_SCHEMA)
            await conn.commit()
            # In-app chat MVP — operator-supplied session metadata
            # (title / pinned / archived). Sibling table, additive: a
            # gateway that hasn't been redeployed will simply ignore
            # the column (its list query doesn't reference it). The
            # CREATE statements carry ``IF NOT EXISTS`` so re-running
            # on a fresh DB is a no-op.
            await conn.executescript(_SESSION_META_SCHEMA)
            await conn.commit()
            # Refresh the ``turns`` column set after running the script
            # (the script doesn't touch ``turns``, but we re-read to keep
            # the snapshot consistent with the loop below).
            for col_name, alter_sql in _TURN_EVENT_AGGREGATE_COLUMNS:
                if col_name not in existing:
                    await conn.execute(alter_sql)
                    await conn.commit()
                    logger.info(
                        "agent.journal.migrated",
                        migration=f"turns_{col_name}_column",
                    )
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
        pending_question_json: str | None = None,
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

        ask_user — ``pending_question_json`` optionally stores the raw
        args JSON of the ``ask_user`` tool call that terminated the
        turn. Purely informational at the storage layer; a future admin
        surface can read this to show "session is awaiting an answer".
        ``None`` is the normal case.
        """
        conn = self._c
        ts = int(time.time() * 1000)
        async with self._write_lock:
            for offset in range(0, 20):
                tid = ts + offset
                try:
                    await conn.execute(
                        "INSERT INTO turns (turn_id, session_key, status, "
                        "started_at_ms, user_text, user_id, channel, "
                        "pending_question_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            tid,
                            session_key or "",
                            TURN_IN_PROGRESS,
                            ts,
                            user_text,
                            user_id,
                            channel or "",
                            pending_question_json,
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
        """Stamp ``turn_id`` as completed and populate W1.2 aggregate columns.

        Beyond the legacy ``status`` / ``ended_at_ms`` write, this method
        now folds the per-turn aggregates the admin UI wants to surface:

        - ``elapsed_ms`` — derived inline from ``ended_at_ms -
          started_at_ms``; null if the row somehow lost its
          ``started_at_ms`` (should never happen, but defensive).
        - ``tool_call_count`` — count of ``turn_messages`` rows with
          ``role = 'tool'``; the inbound replay buffer always writes one
          tool row per builtin/external tool invocation.
        - ``reasoning_token_count`` — best-effort summation over the
          ``turn_events`` table for ``ReasoningDelta`` events (each
          payload carries a ``text`` field; we count whitespace-split
          tokens as a rough proxy). Falls back to 0 when no reasoning
          events were recorded — early sessions, models without thinking
          mode, or backends that haven't migrated yet.

        ``estimated_cost_usd`` + ``cost_status`` are NOT populated here:
        the gateway's ``_CostMeter`` lives in the servicer layer and the
        journal has no direct handle on it. Callers that *do* know the
        cost should use :meth:`update_turn_cost`; otherwise the columns
        stay NULL and the UI renders the standard "unknown" placeholder.
        """
        ended_at_ms = int(time.time() * 1000)
        conn = self._c
        # B3: hold the shared-connection write lock across the bare
        # ``commit()`` here so it can't flush another session's open
        # ``BEGIN IMMEDIATE`` transaction mid-flight.
        async with self._write_lock:
            try:
                # First flip the status. Single statement keeps the legacy
                # write semantics intact — a caller racing with a parallel
                # ``error_turn`` still sees one terminal status win.
                await conn.execute(
                    "UPDATE turns SET status = ?, ended_at_ms = ? "
                    "WHERE turn_id = ? AND status = ?",
                    (TURN_COMPLETED, ended_at_ms, turn_id, TURN_IN_PROGRESS),
                )
                await conn.commit()
                # Then compute + write the W1.2 aggregates. We compute them
                # post-status-flip so a concurrent reader hitting the row
                # between our two writes sees a consistent in-progress→
                # completed transition (just without the aggregates yet).
                # ``_populate_turn_aggregates`` is lock-free: it runs under
                # the lock we already hold (asyncio.Lock is not reentrant).
                await self._populate_turn_aggregates(turn_id)
            except aiosqlite.Error as exc:
                logger.warning("agent.journal.complete_failed", error=str(exc))

    async def _populate_turn_aggregates(self, turn_id: int) -> None:
        """Fill the W1.2 aggregate columns for ``turn_id``.

        Split out from :meth:`complete_turn` so it can also run on a
        late-arriving cost update without re-flipping the status. Best
        effort throughout — any read failure leaves the columns at their
        last value (or NULL for a fresh row) and logs a warning.

        B3: this method does NOT take ``_write_lock`` itself — it issues a
        bare ``commit()`` and so MUST only be called by a caller that
        already holds the lock (today: :meth:`complete_turn`).
        ``asyncio.Lock`` is not reentrant, so acquiring it here would
        deadlock under that caller.
        """
        conn = self._c
        try:
            # Fetch started_at_ms + ended_at_ms in one round trip.
            cur = await conn.execute(
                "SELECT started_at_ms, ended_at_ms FROM turns "
                "WHERE turn_id = ?",
                (turn_id,),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                return
            started_at_ms = int(row[0]) if row[0] is not None else None
            ended_at_ms = int(row[1]) if row[1] is not None else None
            elapsed_ms: int | None = None
            if started_at_ms is not None and ended_at_ms is not None:
                elapsed_ms = max(0, ended_at_ms - started_at_ms)

            # Tool call count — replay buffer always writes role='tool'
            # for tool results, which is the canonical signal here.
            cur = await conn.execute(
                "SELECT COUNT(*) FROM turn_messages "
                "WHERE turn_id = ? AND role = 'tool'",
                (turn_id,),
            )
            tc_row = await cur.fetchone()
            await cur.close()
            tool_call_count = int(tc_row[0]) if tc_row is not None else 0

            # Reasoning token count — best effort from turn_events
            # (W1.1 emits ``ReasoningDelta`` events). turn_id is stored
            # as TEXT so int turn_ids match via str() coercion.
            cur = await conn.execute(
                "SELECT payload_json FROM turn_events "
                "WHERE turn_id = ? AND event_type = 'ReasoningDelta'",
                (str(turn_id),),
            )
            evt_rows = await cur.fetchall()
            await cur.close()
            reasoning_tokens = 0
            for (payload_json,) in evt_rows:
                try:
                    payload = json.loads(payload_json) if payload_json else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                text = payload.get("text") if isinstance(payload, dict) else None
                if isinstance(text, str) and text:
                    # Whitespace token-count proxy. The provider's exact
                    # tokenizer is not available here; the UI uses this
                    # as a relative gauge, not for billing.
                    reasoning_tokens += len(text.split())

            await conn.execute(
                "UPDATE turns SET elapsed_ms = ?, "
                "tool_call_count = ?, reasoning_token_count = ? "
                "WHERE turn_id = ?",
                (elapsed_ms, tool_call_count, reasoning_tokens, turn_id),
            )
            await conn.commit()
        except aiosqlite.Error as exc:
            logger.warning(
                "agent.journal.populate_aggregates_failed", error=str(exc)
            )

    async def error_turn(self, turn_id: int, error: str) -> None:
        # B3: serialise the bare commit() against in-flight transactions.
        async with self._write_lock:
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
        # B3: hold the shared-connection write lock for the whole
        # BEGIN IMMEDIATE..COMMIT envelope so no other session's bare
        # commit() can flush our partial inserts mid-transaction.
        async with self._write_lock:
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

    async def append_messages(
        self,
        turn_id: int,
        messages: list[dict[str, Any]],
    ) -> None:
        """Append multiple messages in one ``BEGIN IMMEDIATE`` / ``COMMIT``
        envelope.

        Folds the (assistant tool_call, tool result) pair the chat
        handler writes after every builtin dispatch into a single
        commit. Single ``SELECT MAX(seq)`` is followed by N inserts
        with locally-incrementing seq — same on-disk shape as N
        sequential :meth:`append_message` calls, single commit cost.
        Empty ``messages`` is a no-op.

        Error posture mirrors :meth:`append_message`: ``aiosqlite.Error``
        triggers a guarded rollback and a warning log; the chat path
        keeps running. A serialisation failure on any single message
        (``json.dumps`` raises) skips that message and continues —
        same per-message contract as the single-shot path.
        """
        if not messages:
            return
        conn = self._c
        # Pre-serialise tool_calls so a TypeError mid-transaction can't
        # leave us with a half-applied batch.
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
        # B3: hold the shared-connection write lock for the whole
        # BEGIN IMMEDIATE..COMMIT envelope. This is the documented
        # all-or-nothing batch — without the lock another session's bare
        # commit() flushes our partial inserts (commit is connection-
        # global) and the batch is no longer atomic.
        async with self._write_lock:
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
                for role, content, tool_call_id, tool_calls_text in prepared:
                    await conn.execute(
                        "INSERT INTO turn_messages (turn_id, seq, role, "
                        "content, tool_call_id, tool_calls_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            turn_id,
                            next_seq,
                            role,
                            content,
                            tool_call_id,
                            tool_calls_text,
                        ),
                    )
                    next_seq += 1
                await conn.commit()
            except aiosqlite.Error as exc:
                logger.warning(
                    "agent.journal.append_batch_failed", error=str(exc)
                )
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
                #
                # In-app chat MVP — LEFT JOIN ``session_meta`` so the
                # operator-supplied title / pinned / archived ride along
                # in the same scan. COALESCE on pinned/archived so
                # sessions that pre-date the meta table (or simply have
                # no row yet) sort as unpinned + unarchived. ORDER BY
                # ``pinned_sort DESC, last_seen DESC`` keeps pinned
                # sessions on top regardless of recency.
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
                "        ORDER BY started_at_ms DESC, turn_id DESC LIMIT 1) "
                "                            AS last_user_text, "
                "       (SELECT status FROM turns "
                "        WHERE session_key = t.session_key "
                "        ORDER BY started_at_ms DESC, turn_id DESC LIMIT 1) "
                "                            AS last_status, "
                "       sm.title             AS meta_title, "
                "       COALESCE(sm.pinned, 0)   AS pinned_sort, "
                "       COALESCE(sm.archived, 0) AS archived_sort "
                "FROM turns t "
                "LEFT JOIN session_meta sm ON sm.session_key = t.session_key "
                "GROUP BY t.session_key "
                "ORDER BY pinned_sort DESC, last_seen DESC "
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
                    title=str(r[7]) if r[7] is not None else None,
                    pinned=bool(r[8]),
                    archived=bool(r[9]),
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
        # B3: hold the shared-connection write lock for the whole
        # BEGIN IMMEDIATE..COMMIT envelope (DELETE + ON DELETE CASCADE)
        # so no other session's bare commit() can flush it half-applied.
        async with self._write_lock:
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

    async def session_exists(self, session_key: str) -> bool:
        """Cheap existence probe — used by ``PATCH /admin/sessions/{key}``
        to short-circuit the upsert with a 404 when the key is unknown.

        We restrict the lookup to ``turns`` (not ``session_meta``) so a
        stale meta row left behind by a mis-fired PATCH cannot resurrect
        a deleted session. ``session_meta`` follows the lifecycle of the
        underlying turns; ``delete_session`` does NOT touch it (no need
        — deleting all turns makes the LEFT JOIN drop the row, and the
        PK conflict-free upsert path tolerates a stale row).
        """
        if not session_key:
            return False
        try:
            cur = await self._c.execute(
                "SELECT 1 FROM turns WHERE session_key = ? LIMIT 1",
                (session_key,),
            )
            row = await cur.fetchone()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("agent.journal.session_exists_failed", error=str(exc))
            return False
        return row is not None

    async def update_session_meta(
        self,
        session_key: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> SessionSummary | None:
        """Upsert title/pinned/archived for ``session_key`` and return the
        refreshed :class:`SessionSummary`.

        ``None`` for any field means "leave it alone" — the SQL is a
        partial UPDATE rather than a full row replace. Implemented via
        ``INSERT ... ON CONFLICT(session_key) DO UPDATE SET ...
        COALESCE(?, col)`` so first-touch INSERTs and subsequent
        partial UPDATEs share one statement.

        Returns ``None`` when ``session_exists`` is False — the caller
        maps that to a 404. The exists-probe + upsert is two round
        trips; for an admin-only surface that's fine and avoids the
        race between an upsert that succeeds and a session that was
        concurrently deleted (the second case is benign — the meta row
        is harmless if the session is gone, and ``list_session_summaries``
        drops it via the inner join on ``turns``).
        """
        if not await self.session_exists(session_key):
            return None
        # All-None body would be a no-op; the route layer enforces "at
        # least one field" with a 422, but we still tolerate the call
        # so a PATCH that flips back the only changed field can be a
        # no-op without erroring.
        title_param = title  # may be None (== leave alone) or str
        pinned_param: int | None = (
            None if pinned is None else (1 if pinned else 0)
        )
        archived_param: int | None = (
            None if archived is None else (1 if archived else 0)
        )
        now_ms = int(time.time() * 1000)
        # B3: serialise the bare commit() against in-flight transactions
        # on the shared connection. The surrounding reads
        # (``session_exists`` above, ``list_session_summaries`` below)
        # stay outside the lock — they don't commit.
        async with self._write_lock:
            try:
                await self._c.execute(
                    # First-touch INSERT: every field defaults to the
                    # supplied value (or NULL/0 when the caller didn't
                    # touch it). Subsequent UPDATE: COALESCE keeps the
                    # existing value when the caller passed ``None``.
                    "INSERT INTO session_meta "
                    "(session_key, title, pinned, archived, updated_at_ms) "
                    "VALUES (?, ?, COALESCE(?, 0), COALESCE(?, 0), ?) "
                    "ON CONFLICT(session_key) DO UPDATE SET "
                    "    title         = COALESCE(?, session_meta.title), "
                    "    pinned        = COALESCE(?, session_meta.pinned), "
                    "    archived      = COALESCE(?, session_meta.archived), "
                    "    updated_at_ms = ?",
                    (
                        session_key,
                        title_param,
                        pinned_param,
                        archived_param,
                        now_ms,
                        title_param,
                        pinned_param,
                        archived_param,
                        now_ms,
                    ),
                )
                await self._c.commit()
            except aiosqlite.Error as exc:
                logger.warning(
                    "agent.journal.update_session_meta_failed",
                    error=str(exc),
                    session_key=session_key,
                )
                return None
        # Re-aggregate so the response matches what the list endpoint
        # would emit on the next call. Fetching the whole list and
        # filtering is the simplest path that re-uses the same
        # serialisation; for typical (< 200 sessions) the cost is
        # negligible. A future optimisation could push a WHERE clause
        # into the aggregate query.
        summaries = await self.list_session_summaries(limit=10_000)
        for s in summaries:
            if s.session_key == session_key:
                return s
        return None

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
        # B3: serialise the bare commit() against in-flight transactions.
        async with self._write_lock:
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

    # ------------------------------------------------------------------
    # W1.2 — turn events timeline.
    #
    # Persists every ``EventEnvelope`` the gateway emits so the admin UI
    # replay endpoint can stream past turns at the same fidelity as live
    # observers. See ``journal_migrations/004_turn_events.sql`` for the
    # schema.
    # ------------------------------------------------------------------

    async def append_event(self, envelope: Any) -> None:
        """Persist a single :class:`EventEnvelope` (or dict equivalent).

        ``envelope`` is duck-typed via :func:`_envelope_to_row`: the W1.1
        dataclass works, and a plain dict with the same keys also works
        — useful for replay paths that round-trip events through JSON.

        ``INSERT OR IGNORE`` so an at-least-once emitter that
        accidentally double-sends the same ``(turn_id, sequence)`` lands
        once on disk. The SSE bridge relies on this to make catch-up +
        live tee idempotent.
        """
        try:
            row = _envelope_to_row(envelope)
        except ValueError as exc:
            logger.warning("agent.journal.append_event_skipped", error=str(exc))
            return
        # B3: serialise the bare commit() against in-flight transactions
        # on the shared connection.
        async with self._write_lock:
            try:
                await self._c.execute(
                    "INSERT OR IGNORE INTO turn_events "
                    "(turn_id, sequence, event_type, payload_json, "
                    "timestamp_ms) "
                    "VALUES (?, ?, ?, ?, ?)",
                    row,
                )
                await self._c.commit()
            except aiosqlite.Error as exc:
                logger.warning(
                    "agent.journal.append_event_failed", error=str(exc)
                )

    async def append_events_batch(self, envelopes: Sequence[Any]) -> None:
        """Batch-insert ``envelopes`` in a single transaction.

        A single turn can emit hundreds of ``TextDelta`` events; folding
        the per-row commit cost into one transaction shaves an order of
        magnitude off bulk-replay times (measured: ~6ms per single insert
        commit vs ~0.5ms per row inside one tx on a local SSD).

        Uses ``executemany`` so the underlying aiosqlite driver can
        re-use one prepared statement; matches the perf characteristics
        of the postgres backend's ``executemany`` path. Empty input is a
        no-op (same shape as :meth:`append_messages`).
        """
        if not envelopes:
            return
        prepared: list[tuple[str, int, str, str, int]] = []
        for env in envelopes:
            try:
                prepared.append(_envelope_to_row(env))
            except ValueError as exc:
                logger.warning(
                    "agent.journal.append_event_skipped", error=str(exc)
                )
                continue
        if not prepared:
            return
        conn = self._c
        # B3: hold the shared-connection write lock for the whole
        # BEGIN IMMEDIATE..COMMIT envelope so no other session's bare
        # commit() can flush these inserts mid-transaction.
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                await conn.executemany(
                    "INSERT OR IGNORE INTO turn_events "
                    "(turn_id, sequence, event_type, payload_json, "
                    "timestamp_ms) "
                    "VALUES (?, ?, ?, ?, ?)",
                    prepared,
                )
                await conn.commit()
            except aiosqlite.Error as exc:
                logger.warning(
                    "agent.journal.append_events_batch_failed", error=str(exc)
                )
                if conn.in_transaction:
                    try:
                        await conn.rollback()
                    except aiosqlite.Error as rb_exc:
                        logger.warning(
                            "agent.journal.append_events_rollback_failed",
                            error=str(rb_exc),
                        )

    async def load_events(self, turn_id: str | int) -> list[dict[str, Any]]:
        """Load every event for ``turn_id`` in ``sequence ASC`` order.

        ``turn_id`` is coerced to ``str`` to match the column type — the
        ``turns`` table uses integer turn_ids today but the events table
        stores them as TEXT so future sub-agent turn ids (which are
        strings) share one column. Returns ``[]`` for an unknown
        ``turn_id`` (no error — historical turns that pre-date W1.2 are
        legitimately empty).

        Each returned dict has keys ``turn_id``, ``sequence``,
        ``event_type``, ``payload`` (already-parsed JSON, dict on
        success or ``{}`` on parse failure), and ``timestamp_ms``.
        Shape is the SSE replay wire format directly.
        """
        try:
            cur = await self._c.execute(
                "SELECT turn_id, sequence, event_type, payload_json, "
                "timestamp_ms FROM turn_events "
                "WHERE turn_id = ? ORDER BY sequence ASC",
                (str(turn_id),),
            )
            rows = await cur.fetchall()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning("agent.journal.load_events_failed", error=str(exc))
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                payload = json.loads(r[3]) if r[3] else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            out.append(
                {
                    "turn_id": str(r[0]),
                    "sequence": int(r[1]),
                    "event_type": str(r[2]),
                    "payload": payload,
                    "timestamp_ms": int(r[4]),
                }
            )
        return out

    async def iter_events(
        self, turn_id: str | int, start_sequence: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream events for ``turn_id`` with ``sequence > start_sequence``.

        Powers the SSE catch-up path: when a client reconnects mid-turn
        carrying ``Last-Event-ID: <seq>`` it gets only the events it
        missed, not the whole timeline. ``start_sequence=0`` (the
        default) yields every event — equivalent to :meth:`load_events`
        but without buffering the whole list in memory.

        Yields the same dict shape as :meth:`load_events`. Silent on
        error — a partial stream is preferable to a 500 for a best-effort
        UI surface.
        """
        try:
            cur = await self._c.execute(
                "SELECT turn_id, sequence, event_type, payload_json, "
                "timestamp_ms FROM turn_events "
                "WHERE turn_id = ? AND sequence > ? "
                "ORDER BY sequence ASC",
                (str(turn_id), int(start_sequence)),
            )
        except aiosqlite.Error as exc:
            logger.warning("agent.journal.iter_events_failed", error=str(exc))
            return
        try:
            async for r in cur:
                try:
                    payload = json.loads(r[3]) if r[3] else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {}
                yield {
                    "turn_id": str(r[0]),
                    "sequence": int(r[1]),
                    "event_type": str(r[2]),
                    "payload": payload,
                    "timestamp_ms": int(r[4]),
                }
        finally:
            await cur.close()

    async def get_session_turn_ids(
        self, session_key: str, limit: int = 50
    ) -> list[int]:
        """Return the most recent turn ids for ``session_key``.

        Convenience for the SSE endpoint: when a client opens a session
        feed it needs to know which turn_ids to stream from. Ordered by
        ``started_at_ms DESC`` so the latest turns are first; the SSE
        bridge typically takes the head (live + next-to-live) and
        leaves the rest for the on-demand replay route. Returns ``[]``
        when no turns exist or the read fails (best-effort surface).
        """
        if not session_key or limit <= 0:
            return []
        try:
            cur = await self._c.execute(
                "SELECT turn_id FROM turns WHERE session_key = ? "
                "ORDER BY started_at_ms DESC LIMIT ?",
                (session_key, int(limit)),
            )
            rows = await cur.fetchall()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning(
                "agent.journal.get_session_turn_ids_failed", error=str(exc)
            )
            return []
        return [int(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # W1.2 (UI) — past-turns navigator.
    #
    # The session-detail page renders a pill-row of recent turns; the
    # underlying query joins the W1.2 aggregate columns (elapsed_ms,
    # tool_call_count, estimated_cost_usd, cost_status) against the base
    # ``turns`` row so the UI can render rich pills without a per-turn
    # round trip. Pagination is cursor-style (``before_turn_id``) so
    # infinite-scroll doesn't drift when a new turn lands between page
    # requests.
    # ------------------------------------------------------------------

    async def list_session_turns(
        self,
        session_key: str,
        *,
        limit: int = 50,
        before_turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return per-turn metadata for ``session_key`` in started_at_ms DESC.

        Each row carries: ``turn_id``, ``started_at_ms``, ``ended_at_ms``,
        ``status``, ``finish_reason`` (always None until a future
        migration surfaces it), ``elapsed_ms``, ``estimated_cost_usd``,
        ``cost_status``, ``tool_call_count``, ``reasoning_token_count``,
        ``user_text_preview`` (200-char truncation).

        ``before_turn_id`` is a cursor: when set, the query returns only
        turns whose ``started_at_ms`` is strictly less than the cursor
        turn's. Useful for infinite-scroll without offset drift if a new
        turn lands between page requests. Unknown cursors are tolerated
        — they resolve to a NULL started_at_ms which collapses the
        ``<`` comparison to false (empty page), which is the right shape
        for "cursor past the end".

        Empty session → ``[]``. Errors → ``[]`` (best-effort: a degraded
        admin surface is preferable to a 500 in the gateway).
        """
        if not session_key or limit <= 0:
            return []
        sql_parts = [
            "SELECT turn_id, started_at_ms, ended_at_ms, status, "
            "elapsed_ms, estimated_cost_usd, cost_status, "
            "tool_call_count, reasoning_token_count, user_text ",
            "FROM turns ",
            "WHERE session_key = ? ",
        ]
        params: list[Any] = [session_key]
        if before_turn_id is not None:
            # Composite cursor: ``(started_at_ms, turn_id) <
            # (cursor_started_at, cursor_turn_id)``. Same-ms turns
            # (``begin_turn`` collides on its wall-clock id and bumps
            # +1 without touching ``started_at_ms``) need the
            # secondary turn_id key for the cursor to be strict — a
            # bare ``started_at_ms <`` skips a same-ms tail.
            sql_parts.append(
                "AND ( "
                "  started_at_ms < ("
                "    SELECT started_at_ms FROM turns WHERE turn_id = ?"
                "  ) "
                "  OR ( "
                "    started_at_ms = ("
                "      SELECT started_at_ms FROM turns WHERE turn_id = ?"
                "    ) "
                "    AND turn_id < ? "
                "  ) "
                ") "
            )
            # The journal stores turn_id as INTEGER PK; the cursor
            # arrives as a string from the URL. Best-effort coerce so
            # a numeric cursor matches; non-numeric falls through to
            # the empty-page semantics described above. The same value
            # binds both subquery parameters and the secondary
            # comparison.
            try:
                cursor_id = int(before_turn_id)
            except (TypeError, ValueError):
                cursor_id = -1
            params.append(cursor_id)
            params.append(cursor_id)
            params.append(cursor_id)
        # Secondary sort on ``turn_id DESC`` is critical: ``begin_turn``
        # uses wall-clock ms for both ``turn_id`` and ``started_at_ms``,
        # but the integrity-collision retry bumps the id by +1 while
        # leaving ``started_at_ms`` unchanged — so two turns seeded
        # within the same ms tie on ``started_at_ms`` and the natural
        # row order would scramble them. The higher turn_id is the more
        # recently inserted row, so it leads in the listing.
        sql_parts.append("ORDER BY started_at_ms DESC, turn_id DESC LIMIT ?")
        params.append(int(limit))
        try:
            cur = await self._c.execute("".join(sql_parts), tuple(params))
            rows = await cur.fetchall()
            await cur.close()
        except aiosqlite.Error as exc:
            logger.warning(
                "agent.journal.list_session_turns_failed", error=str(exc)
            )
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            user_text = r[9]
            preview: str | None = None
            if isinstance(user_text, str):
                preview = (
                    user_text[:200] + "…"
                    if len(user_text) > 200
                    else user_text
                )
            out.append(
                {
                    "turn_id": str(r[0]),
                    "started_at_ms": int(r[1]) if r[1] is not None else None,
                    "ended_at_ms": int(r[2]) if r[2] is not None else None,
                    "status": str(r[3]) if r[3] is not None else None,
                    # finish_reason is not stored on ``turns`` today —
                    # it lives in the ``TurnComplete`` event payload. The
                    # listing surfaces ``None`` so the UI can render an
                    # "—" placeholder; a future migration may project it
                    # into a column to avoid the per-turn event scan.
                    "finish_reason": None,
                    "elapsed_ms": int(r[4]) if r[4] is not None else None,
                    "estimated_cost_usd": (
                        float(r[5]) if r[5] is not None else None
                    ),
                    "cost_status": str(r[6]) if r[6] is not None else None,
                    "tool_call_count": (
                        int(r[7]) if r[7] is not None else 0
                    ),
                    "reasoning_token_count": (
                        int(r[8]) if r[8] is not None else 0
                    ),
                    "user_text_preview": preview,
                }
            )
        return out

    async def update_turn_cost(
        self,
        turn_id: int,
        *,
        estimated_cost_usd: float | None,
        cost_status: str | None,
    ) -> None:
        """Late-binding update for the W1.2 cost columns.

        The journal does not own a ``_CostMeter`` (that lives in the
        servicer); this method lets the gateway flush a known cost
        estimate into the row once it has one. ``None`` keeps the
        existing value untouched — pass non-None for either field to
        write it. Idempotent and safe to call after ``complete_turn``.
        """
        if estimated_cost_usd is None and cost_status is None:
            return
        sets: list[str] = []
        params: list[Any] = []
        if estimated_cost_usd is not None:
            sets.append("estimated_cost_usd = ?")
            params.append(float(estimated_cost_usd))
        if cost_status is not None:
            sets.append("cost_status = ?")
            params.append(str(cost_status))
        params.append(turn_id)
        # B3: serialise the bare commit() against in-flight transactions.
        async with self._write_lock:
            try:
                await self._c.execute(
                    f"UPDATE turns SET {', '.join(sets)} WHERE turn_id = ?",
                    tuple(params),
                )
                await self._c.commit()
            except aiosqlite.Error as exc:
                logger.warning(
                    "agent.journal.update_turn_cost_failed", error=str(exc)
                )

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
