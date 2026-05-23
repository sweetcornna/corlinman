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

from pathlib import Path
from typing import Any

import structlog

from corlinman_server.agent_journal_backend import (
    ENV_BACKEND,
    ENV_POSTGRES_DSN,
    ENV_REDIS_URL,
    JournalBackend,
    ResumeData,
    SqliteJournalBackend,
    TURN_COMPLETED,
    TURN_ERRORED,
    TURN_IN_PROGRESS,
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

    async def begin_turn(self, session_key: str, user_text: str) -> int:
        return await self._backend.begin_turn(session_key, user_text)

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
    ) -> None:
        await self._backend.append_message(
            turn_id,
            role,
            content,
            tool_call_id=tool_call_id,
            tool_calls=tool_calls,
        )

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------

    async def find_resumable_turn(
        self, session_key: str, user_text: str
    ) -> ResumeData | None:
        return await self._backend.find_resumable_turn(session_key, user_text)

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

    async def mark_stale_in_progress_as_errored(self) -> int:
        return await self._backend.mark_stale_in_progress_as_errored()


__all__ = [
    "AgentJournal",
    "ENV_BACKEND",
    "ENV_POSTGRES_DSN",
    "ENV_REDIS_URL",
    "JournalBackend",
    "ResumeData",
    "TURN_COMPLETED",
    "TURN_ERRORED",
    "TURN_IN_PROGRESS",
]
