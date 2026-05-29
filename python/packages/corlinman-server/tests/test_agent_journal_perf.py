"""Reproduction + regression tests for two defects in
``agent_journal_backend.py``:

* PERF-006 — ``list_session_summaries`` computed ``message_count`` /
  ``last_user_text`` / ``last_status`` via THREE correlated subqueries
  per grouped session row (O(sessions × turns × msgs)). After the
  rewrite the same fields are computed without a per-row correlated
  subquery, while the RESULT stays byte-for-byte identical (same
  columns, same last-turn tie-break ``started_at_ms DESC, turn_id
  DESC``, same ``message_count`` semantics).

* BUG-010 — ``begin_turn`` derived ``turn_id`` from wall-clock ms and,
  on 20 same-ms PK collisions, fell through to ``return ts`` — i.e.
  returned the colliding id from iteration 0 WITHOUT inserting a row,
  so the caller's later writes mutated a DIFFERENT existing turn. The
  returned id MUST correspond to a row this call actually inserted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corlinman_server.agent_journal_backend import (
    TURN_IN_PROGRESS,
    SqliteJournalBackend,
)


@pytest.fixture
async def backend(tmp_path: Path) -> SqliteJournalBackend:
    b = await SqliteJournalBackend.open(tmp_path / "j.sqlite")
    yield b
    await b.close()


# ---------------------------------------------------------------------------
# PERF-006 — list_session_summaries rewrite must return identical results
# over a multi-session / multi-turn fixture, and avoid per-row correlated
# subqueries.
# ---------------------------------------------------------------------------


async def _seed_multi_session(backend: SqliteJournalBackend) -> None:
    """Seed a deterministic multi-session / multi-turn fixture directly so
    started_at_ms / turn_id / status / message counts are fully controlled
    (begin_turn reads the wall clock, which is not deterministic enough to
    pin the last-turn tie-break)."""
    conn = backend._c
    # session_key, turn_id, status, started_at_ms, user_text
    turns = [
        # sess-A: 3 turns. Newest = turn_id 103 @ t=300 -> "A latest".
        ("sess-A", 101, "completed", 100, "A first"),
        ("sess-A", 102, "errored", 200, "A second"),
        ("sess-A", 103, "in_progress", 300, "A latest"),
        # sess-B: 2 turns sharing the SAME started_at_ms -> tie broken by
        # turn_id DESC, so turn_id 202 ("B newer") wins both columns.
        ("sess-B", 201, "completed", 500, "B older"),
        ("sess-B", 202, "in_progress", 500, "B newer"),
        # sess-C: single turn, no messages.
        ("sess-C", 301, "completed", 50, "C only"),
    ]
    for session_key, turn_id, status, started_at_ms, user_text in turns:
        await conn.execute(
            "INSERT INTO turns (turn_id, session_key, status, started_at_ms, "
            "user_text) VALUES (?, ?, ?, ?, ?)",
            (turn_id, session_key, status, started_at_ms, user_text),
        )
    # Messages: sess-A has 4 across its turns, sess-B has 1, sess-C has 0.
    msgs = [
        (101, 0, "user", "A first"),
        (101, 1, "assistant", "ok"),
        (102, 0, "user", "A second"),
        (103, 0, "user", "A latest"),
        (201, 0, "user", "B older"),
    ]
    for turn_id, seq, role, content in msgs:
        await conn.execute(
            "INSERT INTO turn_messages (turn_id, seq, role, content) "
            "VALUES (?, ?, ?, ?)",
            (turn_id, seq, role, content),
        )
    await conn.commit()


def _reference_summaries(backend: SqliteJournalBackend) -> None:
    """Placeholder — kept intentionally unused; the reference values are
    asserted inline below."""


async def test_list_session_summaries_correctness_multi(
    backend: SqliteJournalBackend,
) -> None:
    """The rewritten query must return the SAME aggregates / preview /
    status / ordering as the original three-correlated-subquery form over
    a multi-session, multi-turn fixture — including the
    ``started_at_ms DESC, turn_id DESC`` last-turn tie-break."""
    await _seed_multi_session(backend)

    summaries = await backend.list_session_summaries()
    by_key = {s.session_key: s for s in summaries}

    # Three sessions, ordered by MAX(started_at_ms) DESC:
    #   sess-B max=500, sess-A max=300, sess-C max=50.
    assert [s.session_key for s in summaries] == ["sess-B", "sess-A", "sess-C"]

    a = by_key["sess-A"]
    assert a.turn_count == 3
    assert a.message_count == 4
    assert a.first_seen_at_ms == 100
    assert a.last_seen_at_ms == 300
    # Newest turn (turn_id 103 @ t=300) wins the preview + status.
    assert a.last_user_text == "A latest"
    assert a.last_status == TURN_IN_PROGRESS

    b = by_key["sess-B"]
    assert b.turn_count == 2
    assert b.message_count == 1
    assert b.first_seen_at_ms == 500
    assert b.last_seen_at_ms == 500
    # Tie on started_at_ms (both 500) -> turn_id DESC: turn_id 202 wins.
    assert b.last_user_text == "B newer"
    assert b.last_status == TURN_IN_PROGRESS

    c = by_key["sess-C"]
    assert c.turn_count == 1
    assert c.message_count == 0
    assert c.first_seen_at_ms == 50
    assert c.last_seen_at_ms == 50
    assert c.last_user_text == "C only"
    assert c.last_status == "completed"


async def test_list_session_summaries_no_correlated_subquery(
    backend: SqliteJournalBackend,
) -> None:
    """PERF-006: the query plan must not run a per-grouped-row correlated
    subquery over ``turns`` / ``turn_messages``.

    We capture the SQL the method issues, then ask SQLite for its
    ``EXPLAIN QUERY PLAN``. The original implementation emitted
    ``CORRELATED SCALAR SUBQUERY`` nodes (one per summary column); the
    rewrite must not.
    """
    await _seed_multi_session(backend)

    conn = backend._c
    captured: list[str] = []
    original_execute = conn.execute

    async def hooked_execute(sql: str, *args, **kwargs):
        # The summary query is the multi-line SELECT against ``turns t``.
        if "FROM turns t" in sql and "session_key" in sql:
            captured.append(sql)
        return await original_execute(sql, *args, **kwargs)

    conn.execute = hooked_execute  # type: ignore[method-assign]
    try:
        await backend.list_session_summaries()
    finally:
        conn.execute = original_execute  # type: ignore[method-assign]

    assert captured, "did not capture the list_session_summaries query"
    summary_sql = captured[-1]
    # EXPLAIN QUERY PLAN needs a bound limit; reuse the same param shape.
    cur = await conn.execute("EXPLAIN QUERY PLAN " + summary_sql, (200,))
    plan_rows = await cur.fetchall()
    await cur.close()
    plan_text = "\n".join(str(r[-1]) for r in plan_rows)

    assert "CORRELATED" not in plan_text.upper(), (
        "list_session_summaries still uses a correlated subquery per "
        f"session row:\n{plan_text}"
    )


# ---------------------------------------------------------------------------
# BUG-010 — begin_turn must never return a colliding / uninserted turn_id.
# ---------------------------------------------------------------------------


async def test_begin_turn_collision_exhaustion_returns_inserted_id(
    backend: SqliteJournalBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force 20 same-ms collisions: pin the ms source to a constant and
    pre-insert turn_ids ts..ts+19 so every offset in range(0, 20) collides.

    Before the fix ``begin_turn`` falls through to ``return ts`` — the id
    that ALREADY collided on iteration 0 — WITHOUT inserting a row, so the
    caller's later writes corrupt the pre-existing ts row.

    After the fix the 21st ``begin_turn`` must return a UNIQUE, newly
    inserted turn_id that did NOT exist before the call.
    """
    fixed_ts = 9_000_000
    # Pin begin_turn's ms source so every retry offset lands on a taken id.
    monkeypatch.setattr(
        "corlinman_server.agent_journal_backend.time.time",
        lambda: fixed_ts / 1000.0,
    )

    conn = backend._c
    # Pre-insert ts .. ts+19 (the whole retry range) under a foreign session
    # so they are NOT this caller's rows.
    for offset in range(0, 20):
        await conn.execute(
            "INSERT INTO turns (turn_id, session_key, status, started_at_ms, "
            "user_text) VALUES (?, ?, ?, ?, ?)",
            (
                fixed_ts + offset,
                "sess-squatter",
                "completed",
                fixed_ts,
                f"squatter-{offset}",
            ),
        )
    await conn.commit()

    # Snapshot the pre-existing ids so we can prove the returned id is new.
    cur = await conn.execute("SELECT turn_id FROM turns")
    pre_ids = {int(r[0]) for r in await cur.fetchall()}
    await cur.close()

    tid = await backend.begin_turn("sess-victim", "the real turn")

    # 1) The returned id must be a real, newly-inserted id — not None, not
    #    the colliding ts.
    assert tid is not None
    assert tid not in pre_ids, (
        f"begin_turn returned an id ({tid}) that already existed before the "
        "call — it never inserted its own row"
    )
    assert tid != fixed_ts, (
        "begin_turn returned ts (the id that collided on iteration 0) "
        "without inserting a row"
    )

    # 2) A row with that id must exist, owned by THIS caller's session, in
    #    the in_progress state begin_turn writes.
    cur = await conn.execute(
        "SELECT session_key, status, user_text FROM turns WHERE turn_id = ?",
        (tid,),
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None, f"no row inserted for returned turn_id {tid}"
    assert row[0] == "sess-victim"
    assert row[1] == TURN_IN_PROGRESS
    assert row[2] == "the real turn"

    # 3) The pre-existing squatter rows are untouched (no cross-turn
    #    corruption): the ts row still belongs to sess-squatter.
    cur = await conn.execute(
        "SELECT session_key FROM turns WHERE turn_id = ?", (fixed_ts,)
    )
    sq = await cur.fetchone()
    await cur.close()
    assert sq is not None and sq[0] == "sess-squatter", (
        "begin_turn corrupted the pre-existing ts row"
    )
