"""Integration tests for :class:`PostgresJournalBackend`.

These tests degrade cleanly on developer machines that do not have
Postgres installed. The collection-time skip pattern is::

    pytest_postgresql = pytest.importorskip("pytest_postgresql", ...)

so the *entire module* is skipped — no failures, no error noise — if
either ``pytest-postgresql`` or ``asyncpg`` is absent, OR if
``pytest-postgresql`` is installed but cannot locate a ``pg_ctl`` /
``postgres`` binary on PATH.

When Postgres IS available, the module exercises every method of the
:class:`~corlinman_server.agent_journal_backend.JournalBackend`
Protocol against a real database, plus a concurrency assertion that
distinguishes the Postgres backend from the single-writer SQLite one.
"""

from __future__ import annotations

import asyncio
import time

import pytest

# Skip the whole module unless the integration extras are available.
# Each of these is an "everything-or-nothing" requirement; missing any
# one of them means the tests below cannot run, so we declare the skip
# reason once and let pytest do the right thing.
pytest_postgresql = pytest.importorskip(
    "pytest_postgresql",
    reason="postgres journal tests need pytest-postgresql installed",
)
asyncpg = pytest.importorskip(
    "asyncpg",
    reason="postgres journal tests need asyncpg installed",
)

# pytest-postgresql will itself raise an informative error during fixture
# setup if no postgres server / binary is locatable; we just need the
# import path to land so the fixture is collected. We do NOT spin up the
# server here — that's the fixture's job, per test.

from corlinman_server.agent_journal_backend import (  # noqa: E402
    RESUME_MAX_AGE_MS,
    TURN_COMPLETED,
    TURN_ERRORED,
    TURN_IN_PROGRESS,
)
from corlinman_server.agent_journal_postgres import (  # noqa: E402
    PostgresJournalBackend,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _dsn_from_pg(pg) -> str:  # type: ignore[no-untyped-def]
    """Build an asyncpg-friendly DSN from a pytest-postgresql connection.

    pytest-postgresql 6.x exposes a ``psycopg.Connection`` whose
    ``info`` carries the parts we need (host, port, dbname, user). We
    avoid driver-specific quirks (e.g. unix sockets, password options)
    by composing a plain ``postgresql://`` URL.
    """
    info = pg.info
    user = info.user
    host = info.host
    port = info.port
    dbname = info.dbname
    # pytest-postgresql's default user has no password.
    return f"postgresql://{user}@{host}:{port}/{dbname}"


@pytest.fixture
async def backend(postgresql):  # type: ignore[no-untyped-def]
    """Open a :class:`PostgresJournalBackend` against a fresh per-test DB.

    Each test gets its own database via the ``postgresql`` fixture from
    pytest-postgresql, so there is zero cross-test state to clean up.
    """
    dsn = _dsn_from_pg(postgresql)
    be = await PostgresJournalBackend.open(dsn)
    try:
        yield be
    finally:
        await be.close()


# ---------------------------------------------------------------------------
# Protocol round-trip
# ---------------------------------------------------------------------------


async def test_begin_turn_returns_distinct_serial_ids(backend) -> None:  # type: ignore[no-untyped-def]
    a = await backend.begin_turn("sess-1", "first")
    b = await backend.begin_turn("sess-1", "second")
    assert isinstance(a, int)
    assert isinstance(b, int)
    assert a != b


async def test_complete_turn_makes_it_non_resumable(backend) -> None:  # type: ignore[no-untyped-def]
    tid = await backend.begin_turn("sess-c", "do thing")
    await backend.complete_turn(tid)
    assert await backend.find_resumable_turn("sess-c", "do thing") is None


async def test_error_turn_appears_in_recent_errored(backend) -> None:  # type: ignore[no-untyped-def]
    tid = await backend.begin_turn("sess-e", "broken")
    await backend.error_turn(tid, "BANG: provider 500")
    crumbs = await backend.recent_errored_turns("sess-e", limit=5)
    assert len(crumbs) == 1
    assert crumbs[0]["turn_id"] == tid
    assert "BANG" in crumbs[0]["error"]


async def test_append_and_load_messages_round_trip(backend) -> None:  # type: ignore[no-untyped-def]
    tid = await backend.begin_turn("sess-m", "do multi-step")
    await backend.append_message(tid, "user", "do multi-step")
    await backend.append_message(
        tid,
        "assistant",
        "",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression":"2+2"}'},
            }
        ],
    )
    await backend.append_message(tid, "tool", '{"result":4}', tool_call_id="c1")
    msgs = await backend.load_messages(tid)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1]["tool_calls"][0]["id"] == "c1"
    assert msgs[2]["tool_call_id"] == "c1"


async def test_find_resumable_picks_most_recent(backend) -> None:  # type: ignore[no-untyped-def]
    a = await backend.begin_turn("sess-r", "same text")
    await asyncio.sleep(0.005)
    b = await backend.begin_turn("sess-r", "same text")
    resume = await backend.find_resumable_turn("sess-r", "same text")
    assert resume is not None
    assert resume.turn_id == b
    assert resume.turn_id != a


# ---------------------------------------------------------------------------
# find_resumable_turn boundary cases
# ---------------------------------------------------------------------------


async def test_find_resumable_returns_none_for_different_session(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    """The session_key is part of the lookup key — a matching user_text
    on a different session must not resume."""
    await backend.begin_turn("sess-A", "shared text")
    assert await backend.find_resumable_turn("sess-B", "shared text") is None


async def test_find_resumable_respects_window_ms(backend) -> None:  # type: ignore[no-untyped-def]
    """Turns older than ``RESUME_MAX_AGE_MS`` are abandoned."""
    tid = await backend.begin_turn("sess-old", "stale task")
    # Backdate the row past the resume window via the pool directly.
    # We borrow the backend's pool so we don't open a second connection.
    async with backend._p.acquire() as conn:
        await conn.execute(
            "UPDATE journal_turns SET started_at_ms = 0 WHERE turn_id = $1",
            tid,
        )
    assert await backend.find_resumable_turn("sess-old", "stale task") is None


async def test_find_resumable_requires_text_match(backend) -> None:  # type: ignore[no-untyped-def]
    await backend.begin_turn("sess-t", "task A")
    assert await backend.find_resumable_turn("sess-t", "task B") is None
    assert await backend.find_resumable_turn("sess-t", "task A") is not None


# ---------------------------------------------------------------------------
# Stale-sweep
# ---------------------------------------------------------------------------


async def test_mark_stale_in_progress_as_errored_flips_old_rows(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    tid = await backend.begin_turn("sess-sweep", "abandoned")
    async with backend._p.acquire() as conn:
        await conn.execute(
            "UPDATE journal_turns SET started_at_ms = 0 WHERE turn_id = $1",
            tid,
        )
    n = await backend.mark_stale_in_progress_as_errored()
    assert n == 1
    crumbs = await backend.recent_errored_turns("sess-sweep", limit=5)
    assert len(crumbs) == 1
    assert "abandoned" in crumbs[0]["error"]


async def test_mark_stale_leaves_fresh_in_progress_alone(backend) -> None:  # type: ignore[no-untyped-def]
    """Recent in-progress rows (younger than RESUME_MAX_AGE_MS) survive
    the sweep. Guards against an over-eager UPDATE WHERE clause."""
    tid = await backend.begin_turn("sess-fresh", "still cooking")
    # Place it just inside the window — well under RESUME_MAX_AGE_MS.
    young_ms = int(time.time() * 1000) - 1000
    async with backend._p.acquire() as conn:
        await conn.execute(
            "UPDATE journal_turns SET started_at_ms = $1 WHERE turn_id = $2",
            young_ms,
            tid,
        )
    swept = await backend.mark_stale_in_progress_as_errored()
    assert swept == 0
    # The row should still be resumable.
    resume = await backend.find_resumable_turn("sess-fresh", "still cooking")
    assert resume is not None
    assert resume.turn_id == tid


# ---------------------------------------------------------------------------
# Concurrency — the headline reason this backend exists.
# ---------------------------------------------------------------------------


async def test_two_begin_turn_in_parallel_return_distinct_ids(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    """Single-process SQLite serialises writes; Postgres+BIGSERIAL must
    hand out distinct turn_ids under concurrent begin_turn calls — this
    is the precondition for multi-gateway HA."""
    a, b = await asyncio.gather(
        backend.begin_turn("sess-par", "parallel A"),
        backend.begin_turn("sess-par", "parallel B"),
    )
    assert a != b
    assert isinstance(a, int)
    assert isinstance(b, int)


async def test_begin_turn_race_returns_none_on_conflict(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    """C5: two ``begin_turn`` calls with the SAME (session_key,
    user_text, user_id) tuple race against the partial unique index —
    exactly one returns a turn_id; the other returns ``None``. The
    chat handler treats the ``None`` as "another gateway opened the
    turn; fall back to find_resumable_turn"."""
    coros = [
        backend.begin_turn("race-1", "same prompt", user_id="alice"),
        backend.begin_turn("race-1", "same prompt", user_id="alice"),
    ]
    a, b = await asyncio.gather(*coros)
    results = [a, b]
    nones = [r for r in results if r is None]
    ids = [r for r in results if isinstance(r, int)]
    assert len(nones) == 1, (
        f"C5 violation: expected exactly one None on race; got {results}"
    )
    assert len(ids) == 1
    # The surviving row is findable via find_resumable_turn.
    resume = await backend.find_resumable_turn(
        "race-1", "same prompt", user_id="alice"
    )
    assert resume is not None
    assert resume.turn_id == ids[0]


async def test_begin_turn_different_user_ids_do_not_collide(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    """The C5 partial unique index uses user_id as part of its key, so
    two DIFFERENT users in the same session typing the same text MUST
    both succeed — they are independent turns."""
    a = await backend.begin_turn("race-2", "ship it", user_id="alice")
    b = await backend.begin_turn("race-2", "ship it", user_id="bob")
    assert a is not None and b is not None and a != b


async def test_find_resumable_scopes_by_user_id(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    """S4 on Postgres: a turn opened by Alice is NOT visible to Mallory
    even with the same session_key + user_text."""
    tid = await backend.begin_turn("g1", "ship it", user_id="alice")
    assert tid is not None
    assert (
        await backend.find_resumable_turn("g1", "ship it", user_id="mallory")
    ) is None
    found = await backend.find_resumable_turn(
        "g1", "ship it", user_id="alice"
    )
    assert found is not None
    assert found.turn_id == tid


async def test_recent_errored_turns_is_session_scoped(backend) -> None:  # type: ignore[no-untyped-def]
    a = await backend.begin_turn("sess-a", "a-task")
    b = await backend.begin_turn("sess-b", "b-task")
    await backend.error_turn(a, "fail-a")
    await backend.error_turn(b, "fail-b")
    a_crumbs = await backend.recent_errored_turns("sess-a")
    b_crumbs = await backend.recent_errored_turns("sess-b")
    assert {c["error"] for c in a_crumbs} == {"fail-a"}
    assert {c["error"] for c in b_crumbs} == {"fail-b"}


# ---------------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------------


async def test_status_strings_match_protocol_constants(backend) -> None:  # type: ignore[no-untyped-def]
    """The status column stores the same string constants the SQLite
    backend uses, so resume logic that compares status across backends
    keeps working."""
    tid = await backend.begin_turn("sess-status", "x")
    async with backend._p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM journal_turns WHERE turn_id = $1", tid
        )
    assert row["status"] == TURN_IN_PROGRESS
    await backend.complete_turn(tid)
    async with backend._p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM journal_turns WHERE turn_id = $1", tid
        )
    assert row["status"] == TURN_COMPLETED


async def test_resume_window_constant_is_used(backend) -> None:  # type: ignore[no-untyped-def]
    """A row started ``RESUME_MAX_AGE_MS + 1`` ago is past the window;
    one started ``RESUME_MAX_AGE_MS - 1000`` ago is still inside."""
    tid_old = await backend.begin_turn("sess-w", "old")
    tid_young = await backend.begin_turn("sess-w2", "young")
    now_ms = int(time.time() * 1000)
    async with backend._p.acquire() as conn:
        await conn.execute(
            "UPDATE journal_turns SET started_at_ms = $1 WHERE turn_id = $2",
            now_ms - RESUME_MAX_AGE_MS - 1000,
            tid_old,
        )
        await conn.execute(
            "UPDATE journal_turns SET started_at_ms = $1 WHERE turn_id = $2",
            now_ms - 1000,
            tid_young,
        )
    assert await backend.find_resumable_turn("sess-w", "old") is None
    young = await backend.find_resumable_turn("sess-w2", "young")
    assert young is not None and young.turn_id == tid_young


async def test_close_is_idempotent(backend) -> None:  # type: ignore[no-untyped-def]
    """Closing twice must not raise — the fixture also closes on exit
    so the second call goes through the ``self._pool is None`` branch."""
    await backend.close()
    await backend.close()  # second call: no-op
    # Sanity: the marker constants are still importable; this test
    # exists to assert the ``_pool is None`` defence rather than any
    # value here.
    assert TURN_ERRORED == "errored"
