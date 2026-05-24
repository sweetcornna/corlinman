"""Tests for the pluggable journal backend layer.

These tests cover the storage-abstraction split introduced so a future
deployment can swap the per-turn journal from a single-process SQLite
file to a shared Postgres / Redis store for multi-gateway HA.

The original ``test_agent_journal.py`` still exercises the SQLite
behavior end-to-end through the ``AgentJournal`` facade; the cases
below pin the *boundary* (Protocol conformance + env-driven selection).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from corlinman_server.agent_journal import AgentJournal
from corlinman_server.agent_journal_backend import (
    ENV_BACKEND,
    ENV_POSTGRES_DSN,
    ENV_REDIS_URL,
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
    "find_resumable_turn",
    "recent_errored_turns",
    "mark_stale_in_progress_as_errored",
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
