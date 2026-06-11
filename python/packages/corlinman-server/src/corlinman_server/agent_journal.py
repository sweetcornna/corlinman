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

This module is a thin **facade** over a pluggable
:class:`~corlinman_server.agent_journal_backend.JournalBackend` so a
future deployment can swap from a single-process SQLite file to a shared
Postgres / Redis store for multi-gateway HA. The default backend stays
SQLite — the previous behavior is preserved bit-for-bit.

Backend selection is controlled by env (see
:func:`~corlinman_server.agent_journal_backend.open_backend_from_env`):

- ``CORLINMAN_JOURNAL_BACKEND`` — ``sqlite`` (default) / ``postgres`` / ``redis``
- ``CORLINMAN_JOURNAL_POSTGRES_DSN`` — used when backend = postgres (stub)
- ``CORLINMAN_JOURNAL_REDIS_URL`` — used when backend = redis (stub)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import structlog

from corlinman_server.agent_journal_backend import (
    ENV_BACKEND,
    ENV_POSTGRES_DSN,
    ENV_REDIS_URL,
    RESUME_MAX_AGE_MS,
    TURN_COMPLETED,
    TURN_ERRORED,
    TURN_IN_PROGRESS,
    InProgressTurn,
    JournalBackend,
    ResumeData,
    SessionSummary,
    SqliteJournalBackend,
    open_backend_from_env,
)

logger = structlog.get_logger(__name__)


class AgentJournal:
    """Public facade for the per-turn journal.

    Delegates all storage work to a :class:`JournalBackend`. The default
    factory :meth:`open` keeps backward compat with the original
    SQLite-only constructor signature; new callers should prefer
    :meth:`open_from_env` so deployments can swap backends via env vars.
    """

    __slots__ = ("_backend",)

    def __init__(self, backend: JournalBackend) -> None:
        self._backend = backend

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    async def open(cls, path: Path) -> AgentJournal:
        """Open a SQLite-backed journal at ``path`` (legacy entry point).

        Preserved verbatim so existing call sites and tests keep working.
        New code should use :meth:`open_from_env`.
        """
        backend = await SqliteJournalBackend.open(path)
        return cls(backend)

    @classmethod
    async def open_from_env(
        cls,
        sqlite_path: Path,
        env: dict[str, str] | None = None,
    ) -> AgentJournal:
        """Open whichever backend ``CORLINMAN_JOURNAL_BACKEND`` selects.

        ``sqlite_path`` is only consulted when the env selects the
        SQLite backend (the default), so existing single-process
        deployments are unaffected.

        ``env`` is injectable for tests; production callers pass
        ``None`` and the real ``os.environ`` is read.
        """
        backend = await open_backend_from_env(sqlite_path, env=env)
        return cls(backend)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._backend.close()

    @property
    def backend(self) -> JournalBackend:
        """Underlying backend (read-only). Useful for diagnostics + tests."""
        return self._backend

    @property
    def _path(self) -> Path:
        """Backward-compat shim: the original ``AgentJournal`` exposed
        a ``_path`` attribute that a handful of tests reach into. The
        new layout hides the path on the SQLite backend, so we forward
        the read here. Returns ``None``-equivalent semantics will raise
        AttributeError for non-SQLite backends — by design, since the
        path concept only makes sense for the file backend.
        """
        backend = self._backend
        if isinstance(backend, SqliteJournalBackend):
            return backend.path
        raise AttributeError(
            "_path is only defined on the SQLite backend; current backend is "
            f"{type(backend).__name__}"
        )

    # ------------------------------------------------------------------
    # Turn lifecycle — straight delegation.
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
        """Forward to the backend, including the optional S4 user_id scope.

        Backends that race-check against a partial unique index (Postgres
        C5) may return ``None`` to signal "another gateway already opened
        a turn for the same (session_key, user_text, user_id)"; SQLite
        returns the new id unchanged because the per-session asyncio lock
        keeps concurrent writers from racing in the same process.

        ``channel`` (auto-resume) is the channel-id (``"qq"`` /
        ``"telegram"`` / ``""`` for HTTP) the row originated on, so the
        boot-time :class:`AgentResumeService` can pick the right
        re-delivery surface. Default ``""`` preserves every existing
        call site verbatim.

        ``pending_question_json`` (ask_user) optionally stores the JSON
        payload of an ``ask_user`` tool call that ended the turn — a
        question + canned answer options. Purely informational at this
        layer; the chat handler doesn't read it back yet.
        """
        return await self._backend.begin_turn(
            session_key,
            user_text,
            user_id=user_id,
            channel=channel,
            pending_question_json=pending_question_json,
        )

    async def complete_turn(self, turn_id: int) -> None:
        await self._backend.complete_turn(turn_id)

    async def error_turn(self, turn_id: int, error: str) -> None:
        await self._backend.error_turn(turn_id, error)

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
        attachments: Any | None = None,
    ) -> None:
        await self._backend.append_message(
            turn_id,
            role,
            content,
            tool_call_id=tool_call_id,
            tool_calls=tool_calls,
            attachments=attachments,
        )

    async def append_messages(
        self,
        turn_id: int,
        messages: list[dict[str, Any]],
    ) -> None:
        """Append ``messages`` (each a dict with ``role``/``content`` plus
        optional ``tool_call_id`` / ``tool_calls``) to ``turn_id`` in a
        single backend transaction.

        Additive — :meth:`append_message` keeps working unchanged. The
        chat handler uses this to fold the (assistant tool_call, tool
        result) pair from a builtin dispatch into one commit, saving
        ~5ms per pair vs. two sequential single-message appends.
        """
        await self._backend.append_messages(turn_id, messages)

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
        """Find a resumable turn, optionally scoped to ``user_id`` (S4).

        See :meth:`JournalBackend.find_resumable_turn`. The default
        ``user_id=None`` preserves the legacy user_text-only match for
        callers that don't carry a channel sender (HTTP turns).
        """
        return await self._backend.find_resumable_turn(
            session_key, user_text, user_id=user_id
        )

    async def _load_messages(self, turn_id: int) -> list[dict[str, Any]]:
        """Backward-compat alias for the original private loader.

        Existing tests reach into ``_load_messages`` directly; the new
        backend exposes ``load_messages`` (public). This shim keeps the
        old name working without leaking the protected attribute into
        the rest of the codebase.
        """
        return await self._backend.load_messages(turn_id)

    # ------------------------------------------------------------------
    # T4.4 — Error breadcrumbs
    # ------------------------------------------------------------------

    async def recent_errored_turns(
        self, session_key: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        return await self._backend.recent_errored_turns(session_key, limit)

    async def mark_stale_in_progress_as_errored(
        self, older_than_seconds: int | None = None
    ) -> int:
        """Sweep abandoned in-progress turns; flip them to ``errored``.

        ``older_than_seconds=None`` keeps the legacy
        :data:`RESUME_MAX_AGE_MS` cutoff (5 minutes); the boot-time
        :class:`~corlinman_server.auto_resume.AgentResumeService` passes
        e.g. ``older_than_seconds=24 * 3600`` to clear deeply abandoned
        rows without disturbing the fresh window the same scan plans to
        re-deliver.
        """
        return await self._backend.mark_stale_in_progress_as_errored(
            older_than_seconds
        )

    async def list_resumable_in_progress(
        self, *, window_ms: int = RESUME_MAX_AGE_MS
    ) -> list[InProgressTurn]:
        """Return every in-progress turn started within ``window_ms``.

        Powers the boot-time auto-resume scanner. See
        :class:`~corlinman_server.auto_resume.AgentResumeService` for
        the consumer.
        """
        return await self._backend.list_resumable_in_progress(
            window_ms=window_ms
        )

    # ------------------------------------------------------------------
    # /admin/sessions surface — projected straight from the journal so
    # the UI no longer reads from the dead ``sessions.sqlite`` file.
    # ------------------------------------------------------------------

    async def list_session_summaries(
        self, *, limit: int = 200
    ) -> list[SessionSummary]:
        """Return one :class:`SessionSummary` per ``session_key``,
        ordered by ``last_seen_at_ms DESC``. Powers
        ``GET /admin/sessions``.
        """
        return await self._backend.list_session_summaries(limit=limit)

    async def delete_session(self, session_key: str) -> int:
        """Wipe every turn (and its cascading messages) for
        ``session_key``. Returns the count of ``turns`` rows deleted —
        the route maps ``0`` to ``404 not_found``.
        """
        return await self._backend.delete_session(session_key)

    async def session_exists(self, session_key: str) -> bool:
        """Cheap existence probe powering the ``PATCH /admin/sessions/{key}``
        404 branch — see :meth:`JournalBackend.session_exists`."""
        return await self._backend.session_exists(session_key)

    async def update_session_meta(
        self,
        session_key: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> SessionSummary | None:
        """Upsert title/pinned/archived for ``session_key`` and return
        the refreshed :class:`SessionSummary`, or ``None`` when the
        session has no journaled turns (route → 404).

        See :meth:`JournalBackend.update_session_meta` for the
        partial-update semantics (None means "leave alone").
        """
        return await self._backend.update_session_meta(
            session_key,
            title=title,
            pinned=pinned,
            archived=archived,
        )

    # ------------------------------------------------------------------
    # W1.2 — turn events timeline (admin observability).
    #
    # Straight delegation; the backend layer owns the schema + insert/
    # query logic so a Postgres deployment can stub these out without
    # affecting the SQLite default path.
    # ------------------------------------------------------------------

    async def append_event(self, envelope: Any) -> None:
        """Persist one :class:`EventEnvelope` to the turn timeline.

        Accepts the W1.1 dataclass *or* a dict with the same keys —
        useful for SSE replay paths that round-trip through JSON. See
        :func:`corlinman_server.agent_journal_backend._envelope_to_row`
        for the exact projection.
        """
        await self._backend.append_event(envelope)

    async def append_events_batch(self, envelopes: Sequence[Any]) -> None:
        """Persist many envelopes in one transaction.

        Folds the per-row commit overhead into a single ``BEGIN`` /
        ``COMMIT`` envelope; a single turn can emit hundreds of
        ``TextDelta`` events and this method shaves an order of
        magnitude off the bulk-write cost vs sequential
        :meth:`append_event` calls.
        """
        await self._backend.append_events_batch(envelopes)

    async def load_events(self, turn_id: str | int) -> list[dict[str, Any]]:
        """Load every event for ``turn_id`` in ``sequence ASC`` order.

        Each dict carries ``turn_id``, ``sequence``, ``event_type``,
        ``payload`` (parsed JSON), ``timestamp_ms`` — the SSE replay
        wire format. Returns ``[]`` for an unknown / pre-W1.2 turn.
        """
        return await self._backend.load_events(turn_id)

    def iter_events(
        self, turn_id: str | int, start_sequence: int = 0, limit: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Async-iterate events with ``sequence > start_sequence``.

        SSE catch-up path: a reconnecting client with
        ``Last-Event-ID: <seq>`` gets only the events it missed.
        ``start_sequence=0`` (default) yields every event, equivalent to
        :meth:`load_events` but unbuffered. ``limit`` caps the rows so the
        catch-up can page in bounded chunks.
        """
        return self._backend.iter_events(turn_id, start_sequence, limit)

    async def latest_sequence(self, turn_id: str | int) -> int:
        """Highest stored ``sequence`` for ``turn_id`` (``-1`` if none).

        Snapshotted by the SSE catch-up as a fixed upper bound so paging
        terminates instead of chasing an active turn's moving tail.
        """
        return await self._backend.latest_sequence(turn_id)

    async def get_session_turn_ids(
        self, session_key: str, limit: int = 50
    ) -> list[int]:
        """Most-recent turn ids for ``session_key`` (admin SSE bootstrap).

        Ordered by ``started_at_ms DESC``; the SSE bridge picks the head
        for live replay and defers the tail to the on-demand
        per-turn endpoint.
        """
        return await self._backend.get_session_turn_ids(session_key, limit)

    async def list_session_turns(
        self,
        session_key: str,
        *,
        limit: int = 50,
        before_turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return per-turn metadata for the past-turns navigator.

        Each row carries ``turn_id``, ``started_at_ms``, ``ended_at_ms``,
        ``status``, ``finish_reason`` (None until a future migration),
        ``elapsed_ms``, ``estimated_cost_usd``, ``cost_status``,
        ``tool_call_count``, ``reasoning_token_count``, and
        ``user_text_preview`` (200-char truncation with an ellipsis).

        Ordered by ``started_at_ms DESC``. Pagination via the
        ``before_turn_id`` cursor — returns turns started strictly
        before the cursor turn's ``started_at_ms``, suitable for
        infinite-scroll without offset drift.
        """
        return await self._backend.list_session_turns(
            session_key, limit=limit, before_turn_id=before_turn_id
        )

    async def update_turn_cost(
        self,
        turn_id: int,
        *,
        estimated_cost_usd: float | None,
        cost_status: str | None,
    ) -> None:
        """Late-binding update for the W1.2 cost columns.

        The journal does not own a ``_CostMeter`` (that lives in the
        servicer); the gateway calls this once it has a confident
        estimate. Idempotent and safe to call after ``complete_turn``.
        """
        await self._backend.update_turn_cost(
            turn_id,
            estimated_cost_usd=estimated_cost_usd,
            cost_status=cost_status,
        )


__all__ = [
    "ENV_BACKEND",
    "ENV_POSTGRES_DSN",
    "ENV_REDIS_URL",
    "RESUME_MAX_AGE_MS",
    "TURN_COMPLETED",
    "TURN_ERRORED",
    "TURN_IN_PROGRESS",
    "AgentJournal",
    "InProgressTurn",
    "JournalBackend",
    "ResumeData",
    "SessionSummary",
]
