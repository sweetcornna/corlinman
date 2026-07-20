"""Tests for the pluggable journal backend layer.

These tests cover the storage-abstraction split introduced so a future
deployment can swap the per-turn journal from a single-process SQLite
file to a shared Postgres / Redis store for multi-gateway HA.

The original ``test_agent_journal.py`` still exercises the SQLite
behavior end-to-end through the ``AgentJournal`` facade; the cases
below pin the *boundary* (Protocol conformance + env-driven selection).
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from corlinman_server.agent_journal import AgentJournal
from corlinman_server.agent_journal_backend import (
    ENV_BACKEND,
    ENV_POSTGRES_DSN,
    ENV_REDIS_URL,
    TURN_COMPLETED,
    JournalBackend,
    PostgresJournalBackend,
    RedisJournalBackend,
    SqliteJournalBackend,
    open_backend_from_env,
)

# The set of public async methods a backend MUST implement. Keep this
# list in lockstep with the JournalBackend Protocol — if a method is
# added there, add it here and the SQLite impl below will be checked.
_BACKEND_METHODS = (
    "close",
    "begin_turn",
    "complete_turn",
    "error_turn",
    "append_message",
    "append_messages",
    "find_resumable_turn",
    "recent_errored_turns",
    "mark_stale_in_progress_as_errored",
    "list_resumable_in_progress",
    "load_messages",
    "list_session_summaries",
    "delete_session",
)


# ---------------------------------------------------------------------------
# Protocol conformance — SQLite backend implements every method, with the
# correct async-ness, and is recognised by ``isinstance`` against the
# runtime-checkable Protocol.
# ---------------------------------------------------------------------------


async def test_sqlite_backend_implements_every_protocol_method(
    tmp_path: Path,
) -> None:
    backend = await SqliteJournalBackend.open(tmp_path / "j.sqlite")
    try:
        for name in _BACKEND_METHODS:
            method = getattr(backend, name, None)
            assert method is not None, f"SqliteJournalBackend missing {name!r}"
            assert callable(method), f"{name!r} is not callable"
            assert inspect.iscoroutinefunction(method), (
                f"{name!r} must be ``async def`` to satisfy JournalBackend"
            )
    finally:
        await backend.close()


async def test_sqlite_backend_satisfies_runtime_protocol(tmp_path: Path) -> None:
    """``JournalBackend`` is ``@runtime_checkable`` so ``isinstance``
    works as a smoke-test that the structural contract holds."""
    backend = await SqliteJournalBackend.open(tmp_path / "j.sqlite")
    try:
        assert isinstance(backend, JournalBackend)
    finally:
        await backend.close()


async def test_facade_round_trip_through_backend(tmp_path: Path) -> None:
    """Sanity check: ``AgentJournal`` (the facade) delegates correctly
    so a turn begun, appended-to, and completed via the public API ends
    up persisted in the backend."""
    j = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        tid = await j.begin_turn("sess", "round-trip")
        await j.append_message(tid, "user", "round-trip")
        await j.complete_turn(tid)
        # find_resumable should return None — completed turns aren't
        # resumable. This proves the write reached the backend.
        assert await j.find_resumable_turn("sess", "round-trip") is None
        assert isinstance(j.backend, SqliteJournalBackend)
    finally:
        await j.close()


# ---------------------------------------------------------------------------
# Env-driven selection — default is SQLite; postgres/redis stubs raise.
# ---------------------------------------------------------------------------


async def test_open_from_env_defaults_to_sqlite(tmp_path: Path) -> None:
    """Unset env → SQLite backend at the supplied path. This is the
    backward-compat guarantee for existing single-process deployments."""
    j = await AgentJournal.open_from_env(tmp_path / "j.sqlite", env={})
    try:
        assert isinstance(j.backend, SqliteJournalBackend)
        assert (tmp_path / "j.sqlite").exists()
    finally:
        await j.close()


async def test_open_from_env_explicit_sqlite(tmp_path: Path) -> None:
    """Explicit ``CORLINMAN_JOURNAL_BACKEND=sqlite`` also picks SQLite."""
    env = {ENV_BACKEND: "sqlite"}
    j = await AgentJournal.open_from_env(tmp_path / "j.sqlite", env=env)
    try:
        assert isinstance(j.backend, SqliteJournalBackend)
    finally:
        await j.close()


async def test_open_from_env_is_case_insensitive(tmp_path: Path) -> None:
    """Ops set env vars in mixed case all the time; tolerate it."""
    env = {ENV_BACKEND: "SQLite"}
    j = await AgentJournal.open_from_env(tmp_path / "j.sqlite", env=env)
    try:
        assert isinstance(j.backend, SqliteJournalBackend)
    finally:
        await j.close()


async def test_open_from_env_postgres_dispatches_to_postgres_backend(
    tmp_path: Path,
) -> None:
    """The selector must reach the real Postgres backend's ``open`` —
    not fall back to SQLite, not raise NotImplementedError, not raise
    a config error. We use an unreachable DSN so we can assert the
    dispatch landed in asyncpg/Postgres-backend territory without
    actually needing a live Postgres in the dev environment.

    The exact exception type depends on whether asyncpg is installed
    (ImportError-wrapped RuntimeError) or installed but unreachable
    (OSError / asyncpg.PostgresError). Either way, NotImplementedError
    is the one outcome we want to be sure is GONE.
    """
    env = {
        ENV_BACKEND: "postgres",
        # 127.0.0.1:1 — guaranteed-unreachable port; resolves instantly
        # without DNS, so the test never waits on a slow lookup.
        ENV_POSTGRES_DSN: "postgresql://nobody:nopass@127.0.0.1:1/journal",
    }
    with pytest.raises(BaseException) as excinfo:
        await AgentJournal.open_from_env(tmp_path / "j.sqlite", env=env)
    assert not isinstance(excinfo.value, NotImplementedError), (
        "Postgres backend is now implemented — the dispatcher must not "
        f"raise NotImplementedError, got {excinfo.value!r}"
    )


async def test_open_from_env_redis_raises_not_implemented(
    tmp_path: Path,
) -> None:
    env = {
        ENV_BACKEND: "redis",
        ENV_REDIS_URL: "redis://localhost:6379/0",
    }
    with pytest.raises(NotImplementedError, match="redis"):
        await AgentJournal.open_from_env(tmp_path / "j.sqlite", env=env)


async def test_open_from_env_postgres_without_dsn_is_a_config_error(
    tmp_path: Path,
) -> None:
    """A misconfigured deployment (backend selected but no DSN) must
    fail loudly at startup — not silently fall back to SQLite."""
    env = {ENV_BACKEND: "postgres"}
    with pytest.raises(RuntimeError, match=ENV_POSTGRES_DSN):
        await AgentJournal.open_from_env(tmp_path / "j.sqlite", env=env)


async def test_open_from_env_redis_without_url_is_a_config_error(
    tmp_path: Path,
) -> None:
    env = {ENV_BACKEND: "redis"}
    with pytest.raises(RuntimeError, match=ENV_REDIS_URL):
        await AgentJournal.open_from_env(tmp_path / "j.sqlite", env=env)


async def test_open_from_env_rejects_unknown_backend(tmp_path: Path) -> None:
    env = {ENV_BACKEND: "cassandra"}
    with pytest.raises(RuntimeError, match="cassandra"):
        await AgentJournal.open_from_env(tmp_path / "j.sqlite", env=env)


# ---------------------------------------------------------------------------
# Direct stub probe — Redis is still a stub (out of scope for now).
# Postgres has shipped, so its ``.open()`` is exercised in
# ``test_agent_journal_postgres.py`` against a real DB (or skipped when
# no Postgres is available).
# ---------------------------------------------------------------------------


async def test_postgres_backend_class_is_importable() -> None:
    """``PostgresJournalBackend`` is now real — importing the attribute
    via the back-compat re-export must succeed and yield a class with
    an async ``open`` classmethod, not the old stub."""
    cls = PostgresJournalBackend  # exercise the module ``__getattr__``.
    assert isinstance(cls, type), f"expected a class, got {cls!r}"
    assert inspect.iscoroutinefunction(cls.open), (
        "PostgresJournalBackend.open must be ``async def`` to satisfy "
        "the JournalBackend Protocol"
    )


async def test_redis_stub_open_raises() -> None:
    with pytest.raises(NotImplementedError):
        await RedisJournalBackend.open("redis://ignored")


# ---------------------------------------------------------------------------
# Selector returns are typed as JournalBackend — verify the runtime type.
# ---------------------------------------------------------------------------


async def test_open_backend_from_env_returns_journal_backend(
    tmp_path: Path,
) -> None:
    backend = await open_backend_from_env(tmp_path / "j.sqlite", env={})
    try:
        assert isinstance(backend, JournalBackend)
        assert isinstance(backend, SqliteJournalBackend)
    finally:
        await backend.close()


# ---------------------------------------------------------------------------
# B3 — cross-session transaction safety on the SHARED connection.
#
# ``SqliteJournalBackend`` keeps ONE ``aiosqlite.Connection`` for every
# session, but mixes transaction models on it: ``append_messages`` wraps
# its inserts in an explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` envelope,
# while ``complete_turn`` / ``error_turn`` / ``append_event`` do a bare
# autocommit ``execute()`` + ``commit()``. ``commit()`` is connection-
# *global*: if session A is mid-``BEGIN IMMEDIATE`` (awaiting between its
# INSERTs) and session B fires a bare ``commit()`` on the same connection,
# B's commit flushes A's partial rows and ends A's transaction — so a
# subsequent failure in A's batch can no longer roll the batch back.
# ``append_messages`` documents all-or-nothing atomicity; that guarantee
# is broken under concurrent multi-session load.
# ---------------------------------------------------------------------------


async def test_append_messages_atomic_under_concurrent_commit(
    tmp_path: Path,
) -> None:
    """A's batch must stay all-or-nothing even when session B commits on
    the same shared connection mid-batch.

    Reproduction of the interleaving the per-session servicer lock does
    NOT prevent (it only serialises the SAME session):

    * A (``append_messages`` on turn ``tid_a``) executes ``BEGIN
      IMMEDIATE`` + its first INSERT, then yields control.
    * B (``complete_turn`` on its own turn ``tid_b``) runs a bare
      ``commit()`` while A is parked.
    * A resumes and its second INSERT fails (we inject an error to stand
      in for any mid-batch failure).

    Correct behaviour: A's whole batch rolls back → ZERO of A's rows
    survive. Buggy behaviour: B's connection-global commit already
    durably flushed A's first row, so it survives the rollback → A's
    documented atomicity is broken.
    """
    backend = await SqliteJournalBackend.open(tmp_path / "j.sqlite")
    try:
        tid_a = await backend.begin_turn("sess-A", "task-A")
        tid_b = await backend.begin_turn("sess-B", "task-B")
        assert tid_a is not None and tid_b is not None

        conn = backend._c  # type: ignore[attr-defined]
        original_execute = conn.execute
        insert_count = {"n": 0}

        async def hooked_execute(sql: str, *args: Any, **kwargs: Any) -> Any:
            # Only A inserts into turn_messages in this test.
            if "INSERT INTO turn_messages" in sql:
                insert_count["n"] += 1
                if insert_count["n"] == 1:
                    # First INSERT lands inside A's BEGIN IMMEDIATE. Yield
                    # the event loop so B gets a chance to run its bare
                    # commit() while A's transaction is (logically) open.
                    # Under the fix, A holds the write lock here so B is
                    # parked on the lock and can't commit until A finishes;
                    # under the bug, B's commit interleaves and flushes
                    # this partial row.
                    result = await original_execute(sql, *args, **kwargs)
                    await asyncio.sleep(0)
                    return result
                # Second INSERT — inject a failure to stand in for any
                # mid-batch error. A correct envelope rolls the whole
                # batch back; the bug has already committed row 1 via B.
                raise aiosqlite.OperationalError("injected mid-batch failure")
            return await original_execute(sql, *args, **kwargs)

        conn.execute = hooked_execute  # type: ignore[method-assign]

        async def session_a() -> None:
            await backend.append_messages(
                tid_a,
                [
                    {"role": "user", "content": "A-msg-1"},
                    {"role": "assistant", "content": "A-msg-2"},
                ],
            )

        async def session_b() -> None:
            # Complete B's turn concurrently — a bare execute() + commit()
            # on the shared connection. Yield first so A reaches its open
            # transaction before B issues the commit.
            await asyncio.sleep(0)
            await backend.complete_turn(tid_b)

        try:
            await asyncio.gather(session_a(), session_b())
        finally:
            conn.execute = original_execute  # type: ignore[method-assign]

        # A's batch failed mid-flight. With correct all-or-nothing
        # atomicity NEITHER of A's rows should survive. The bug lets B's
        # connection-global commit persist A's first row before the
        # rollback fires.
        a_msgs = await backend.load_messages(tid_a)
        assert a_msgs == [], (
            "append_messages atomicity violated: A's partial batch leaked "
            f"past a mid-batch failure because session B's commit flushed "
            f"it on the shared connection — got {a_msgs!r}"
        )
        # B's own write must still have landed — the lock serialises, it
        # doesn't drop work.
        b_status = await backend.list_session_turns("sess-B")
        assert b_status and b_status[0]["status"] == TURN_COMPLETED, (
            f"session B's complete_turn was lost — got {b_status!r}"
        )
    finally:
        await backend.close()


# ---------------------------------------------------------------------------
# Same-ms tie-break — ``get_session_turn_ids`` must order deterministically.
# ---------------------------------------------------------------------------


async def test_get_session_turn_ids_breaks_same_ms_ties_by_turn_id(
    tmp_path: Path,
) -> None:
    """Two turns seeded within one wall-clock ms tie on ``started_at_ms``
    (``begin_turn``'s integrity-collision retry bumps ``turn_id`` +1 but
    keeps the timestamp) — without the ``turn_id DESC`` secondary sort
    the listing came back in scrambled natural-row order, which flaked
    ``fork_session``'s faithful-copy comparison under full-suite timing."""
    j = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        first = await j.begin_turn("sess", "first")
        second = await j.begin_turn("sess", "second")
        assert first is not None and second is not None
        await j.complete_turn(first)
        await j.complete_turn(second)
        # Force the same-ms tie regardless of how fast the seeds ran.
        backend = j.backend
        assert isinstance(backend, SqliteJournalBackend)
        await backend._c.execute(  # noqa: SLF001 — deliberate white-box seed
            "UPDATE turns SET started_at_ms = ? WHERE turn_id IN (?, ?)",
            (1_000_000, first, second),
        )
        await backend._c.commit()

        ids = await j.get_session_turn_ids("sess", limit=10)
        assert ids == sorted([first, second], reverse=True), (
            f"tie on started_at_ms must fall back to turn_id DESC — got {ids!r}"
        )
    finally:
        await j.close()
