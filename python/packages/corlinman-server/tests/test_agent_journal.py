"""Tests for the T4.1 / T4.4 agent turn journal."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.agent_journal import (
    AgentJournal,
)


@pytest.fixture
async def journal(tmp_path: Path) -> AgentJournal:
    j = await AgentJournal.open(tmp_path / "journal.sqlite")
    yield j
    await j.close()


async def test_begin_turn_creates_in_progress_row(journal: AgentJournal) -> None:
    tid = await journal.begin_turn("sess-1", "hello world")
    assert tid > 0
    # Same-text resume should find it.
    resume = await journal.find_resumable_turn("sess-1", "hello world")
    assert resume is not None
    assert resume.turn_id == tid


async def test_complete_turn_takes_it_out_of_resumable(
    journal: AgentJournal,
) -> None:
    tid = await journal.begin_turn("sess-2", "do thing")
    await journal.complete_turn(tid)
    # A completed turn is no longer resumable.
    resume = await journal.find_resumable_turn("sess-2", "do thing")
    assert resume is None


async def test_error_turn_records_breadcrumb(journal: AgentJournal) -> None:
    tid = await journal.begin_turn("sess-3", "broken thing")
    await journal.error_turn(tid, "BANG: provider 500")
    crumbs = await journal.recent_errored_turns("sess-3", limit=5)
    assert len(crumbs) == 1
    assert crumbs[0]["turn_id"] == tid
    assert crumbs[0]["user_text"] == "broken thing"
    assert "BANG" in crumbs[0]["error"]


async def test_append_and_load_messages_round_trip(
    journal: AgentJournal,
) -> None:
    tid = await journal.begin_turn("sess-4", "do multi-step")
    await journal.append_message(tid, "user", "do multi-step")
    await journal.append_message(
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
    await journal.append_message(
        tid, "tool", '{"result":4}', tool_call_id="c1"
    )
    msgs = await journal._load_messages(tid)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1]["tool_calls"][0]["id"] == "c1"
    assert msgs[2]["tool_call_id"] == "c1"


async def test_find_resumable_only_matches_within_window(
    journal: AgentJournal,
) -> None:
    """Old in-progress turns are abandoned (older than ~5 minutes)."""
    import aiosqlite

    tid = await journal.begin_turn("sess-old", "stale task")
    # Backdate it past the resume window.
    async with aiosqlite.connect(journal._path) as conn:
        await conn.execute(
            "UPDATE turns SET started_at_ms = 0 WHERE turn_id = ?", (tid,)
        )
        await conn.commit()
    resume = await journal.find_resumable_turn("sess-old", "stale task")
    assert resume is None


async def test_find_resumable_requires_text_match(
    journal: AgentJournal,
) -> None:
    """A different user text in the same session is a fresh task, not a resume."""
    await journal.begin_turn("sess-x", "task A")
    assert await journal.find_resumable_turn("sess-x", "task B") is None
    assert await journal.find_resumable_turn("sess-x", "task A") is not None


async def test_find_resumable_picks_most_recent_on_collision(
    journal: AgentJournal,
) -> None:
    """If the user resent the same text twice, the later in-progress wins."""
    tid_a = await journal.begin_turn("sess-c", "same task")
    await asyncio.sleep(0.01)
    tid_b = await journal.begin_turn("sess-c", "same task")
    resume = await journal.find_resumable_turn("sess-c", "same task")
    assert resume is not None
    assert resume.turn_id in (tid_a, tid_b)
    # Picks the more recent one (DESC order).
    assert resume.turn_id == tid_b


async def test_sweep_stale_in_progress_marks_errored(
    journal: AgentJournal,
) -> None:
    """The boot-time sweep stamps abandoned in_progress rows errored."""
    import aiosqlite

    tid = await journal.begin_turn("sess-sweep", "abandoned task")
    async with aiosqlite.connect(journal._path) as conn:
        await conn.execute(
            "UPDATE turns SET started_at_ms = 0 WHERE turn_id = ?", (tid,)
        )
        await conn.commit()

    n = await journal.mark_stale_in_progress_as_errored()
    assert n == 1
    crumbs = await journal.recent_errored_turns("sess-sweep", limit=5)
    assert len(crumbs) == 1
    assert "abandoned" in crumbs[0]["error"]


async def test_resume_returns_messages_in_seq_order(
    journal: AgentJournal,
) -> None:
    """ResumeData.messages are ordered ascending by seq."""
    tid = await journal.begin_turn("sess-order", "ordered task")
    await journal.append_message(tid, "user", "ordered task")
    await journal.append_message(tid, "assistant", "ok working")
    await journal.append_message(tid, "tool", "{}", tool_call_id="ct")
    resume = await journal.find_resumable_turn("sess-order", "ordered task")
    assert resume is not None
    assert [m["role"] for m in resume.messages] == [
        "user", "assistant", "tool",
    ]


async def test_recent_errored_turns_is_session_scoped(
    journal: AgentJournal,
) -> None:
    a = await journal.begin_turn("sess-a", "a-task")
    b = await journal.begin_turn("sess-b", "b-task")
    await journal.error_turn(a, "fail-a")
    await journal.error_turn(b, "fail-b")
    a_crumbs = await journal.recent_errored_turns("sess-a")
    b_crumbs = await journal.recent_errored_turns("sess-b")
    assert {c["error"] for c in a_crumbs} == {"fail-a"}
    assert {c["error"] for c in b_crumbs} == {"fail-b"}


# ---------------------------------------------------------------------------
# S4 — user_id scoping (group-chat replay-attack protection)
# ---------------------------------------------------------------------------


async def test_find_resumable_does_not_cross_users_within_session(
    journal: AgentJournal,
) -> None:
    """S4: two distinct ``user_id`` values on the same ``session_key`` with
    the same ``user_text`` must NOT resume each other's turn.

    The group-chat replay attack: Mallory parrots Alice's exact text;
    without ``user_id`` scoping the journal would happily hand Mallory
    Alice's in-progress turn. With S4, Mallory's lookup misses.
    """
    # Alice opens a turn in group ``g1`` with text "ship it".
    alice_tid = await journal.begin_turn(
        "g1", "ship it", user_id="alice"
    )
    assert alice_tid is not None
    # Mallory in the same group replays Alice's text — must NOT resume.
    mallory_match = await journal.find_resumable_turn(
        "g1", "ship it", user_id="mallory"
    )
    assert mallory_match is None, (
        "S4 violation: Mallory could resume Alice's turn by replaying her text"
    )
    # Alice herself can still resume — same user_id + same text + same session.
    alice_match = await journal.find_resumable_turn(
        "g1", "ship it", user_id="alice"
    )
    assert alice_match is not None
    assert alice_match.turn_id == alice_tid


async def test_find_resumable_legacy_null_user_id_is_visible_to_anyone(
    journal: AgentJournal,
) -> None:
    """Rows journaled before S4 had no ``user_id`` (NULL). The lookup
    tolerates NULL so a redeploy doesn't strand mid-flight resumes —
    but rows journaled WITH a user_id still get scoped."""
    legacy_tid = await journal.begin_turn("s-mix", "legacy task")
    assert legacy_tid is not None
    # An S4-aware lookup with any user_id can still pick up the NULL row.
    legacy_match = await journal.find_resumable_turn(
        "s-mix", "legacy task", user_id="someone"
    )
    assert legacy_match is not None
    assert legacy_match.turn_id == legacy_tid


async def test_find_resumable_no_user_id_arg_is_legacy_match(
    journal: AgentJournal,
) -> None:
    """HTTP callers without a channel sender pass ``user_id=None``
    (the default); the match is user_text-only, matching pre-S4 behaviour."""
    tid_a = await journal.begin_turn("h1", "http task", user_id="alice")
    assert tid_a is not None
    # No user_id supplied → legacy match wins regardless of who began it.
    legacy_lookup = await journal.find_resumable_turn("h1", "http task")
    assert legacy_lookup is not None
    assert legacy_lookup.turn_id == tid_a


# ---------------------------------------------------------------------------
# L5 — aiosqlite begin/rollback semantics
# ---------------------------------------------------------------------------


async def test_complete_turn_still_works_after_append_collision(
    journal: AgentJournal,
) -> None:
    """L5 regression: an ``append_message`` that hits the rollback path
    must not leave the connection in a half-aborted transaction state
    that silently no-ops the subsequent ``complete_turn`` write.
    """
    tid = await journal.begin_turn("s-L5", "L5 task")
    assert tid is not None

    # Append a normal message — proves the journal is healthy.
    await journal.append_message(tid, "user", "L5 task")

    # Force an append failure by feeding a PRIMARY KEY collision: two
    # appends with the same (turn_id, seq) clash. We synthesise the
    # collision by inserting a row directly at a guessed-next seq and
    # then asking the journal to append again — the SELECT MAX(seq)
    # will see seq=1 already taken, INSERT fails with IntegrityError.
    # We bypass into the backend via the public ``backend`` accessor.
    import aiosqlite  # local to keep the public test surface tidy
    backend = journal.backend
    # Insert a row at seq=1 directly so the next ``append_message`` →
    # MAX(seq)+1 = 1 collides on the PRIMARY KEY.
    conn = backend._c  # noqa: SLF001 — test-only deep poke
    try:
        await conn.execute(
            "INSERT INTO turn_messages (turn_id, seq, role, content) "
            "VALUES (?, ?, ?, ?)",
            (tid, 1, "assistant", "directly inserted"),
        )
        await conn.commit()
    except aiosqlite.Error:
        # If sqlite refuses the manual write, the test setup is wrong —
        # bail out cleanly so a real bug isn't masked.
        pytest.skip("could not stage a manual conflicting row")

    # The next ``append_message`` attempt should hit IntegrityError
    # inside its transaction; the L5 fix logs + rolls back cleanly.
    await journal.append_message(tid, "user", "this should fail to land")

    # Crucial assertion: ``complete_turn`` still works. Pre-L5 this
    # silently no-op'd because the connection sat in autocommit limbo
    # with the previous tx half-aborted.
    await journal.complete_turn(tid)
    # If complete_turn took effect, find_resumable_turn must miss
    # (completed turns aren't resumable).
    assert (
        await journal.find_resumable_turn("s-L5", "L5 task")
    ) is None, (
        "L5 violation: complete_turn silently no-op'd after a rolled-back "
        "append_message left the connection in a dirty tx state"
    )


# ---------------------------------------------------------------------------
# Auto-resume — channel column + list_resumable_in_progress
# ---------------------------------------------------------------------------


async def test_list_resumable_in_progress_returns_recent_in_progress_only(
    journal: AgentJournal,
) -> None:
    """The scanner returns rows that are BOTH in_progress AND fresh.

    A completed turn must not appear (it doesn't need re-delivery).
    An errored turn must not appear (the user already saw the failure
    or moved on). Only in_progress wins.
    """
    # Two in_progress rows on different sessions; one will get completed.
    keep = await journal.begin_turn(
        "sess-keep", "still cooking", channel="telegram"
    )
    completed = await journal.begin_turn("sess-done", "finished", channel="qq")
    await journal.complete_turn(completed)

    errored = await journal.begin_turn("sess-err", "broke", channel="discord")
    await journal.error_turn(errored, "fail")

    rows = await journal.list_resumable_in_progress()
    seen_turn_ids = {r.turn_id for r in rows}
    assert keep in seen_turn_ids
    assert completed not in seen_turn_ids
    assert errored not in seen_turn_ids


async def test_list_resumable_in_progress_respects_window(
    journal: AgentJournal,
) -> None:
    """Rows older than ``window_ms`` are excluded.

    Backdate the started_at_ms past the window — the scanner must not
    pick the row up.
    """
    import aiosqlite

    tid = await journal.begin_turn("sess-old", "stale", channel="telegram")
    async with aiosqlite.connect(journal._path) as conn:
        await conn.execute(
            "UPDATE turns SET started_at_ms = 0 WHERE turn_id = ?", (tid,)
        )
        await conn.commit()

    # Default window (5 min via RESUME_MAX_AGE_MS) excludes the row.
    rows = await journal.list_resumable_in_progress()
    assert tid not in {r.turn_id for r in rows}

    # An extremely large window picks it up (sanity check the param wires).
    rows_wide = await journal.list_resumable_in_progress(
        window_ms=10**13
    )
    assert tid in {r.turn_id for r in rows_wide}


async def test_begin_turn_persists_channel_field(
    journal: AgentJournal,
) -> None:
    """``channel`` round-trips through the row so the scanner can
    dispatch re-delivery to the right surface."""
    tid_tg = await journal.begin_turn(
        "sess-tg", "telegram task", channel="telegram"
    )
    tid_qq = await journal.begin_turn(
        "sess-qq", "qq task", channel="qq"
    )
    # HTTP turn — no channel.
    tid_http = await journal.begin_turn("sess-http", "http task")

    rows = await journal.list_resumable_in_progress()
    by_id = {r.turn_id: r for r in rows}
    assert by_id[tid_tg].channel == "telegram"
    assert by_id[tid_qq].channel == "qq"
    assert by_id[tid_http].channel == ""


async def test_schema_migration_adds_channel_column_to_legacy_db(
    tmp_path,
) -> None:
    """A SQLite file written by the pre-auto-resume code path must get
    the ``channel`` column added when the new code opens it — no manual
    psql / sqlite3 step required on the VPS.
    """
    import aiosqlite

    path = tmp_path / "legacy.sqlite"
    # Hand-roll the pre-auto-resume schema (no ``channel`` column).
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "CREATE TABLE turns ("
            " turn_id INTEGER PRIMARY KEY,"
            " session_key TEXT NOT NULL,"
            " status TEXT NOT NULL,"
            " started_at_ms INTEGER NOT NULL,"
            " ended_at_ms INTEGER,"
            " user_text TEXT,"
            " user_id TEXT,"
            " error TEXT)"
        )
        await conn.execute(
            "CREATE TABLE turn_messages ("
            " turn_id INTEGER NOT NULL,"
            " seq INTEGER NOT NULL,"
            " role TEXT NOT NULL,"
            " content TEXT NOT NULL,"
            " tool_call_id TEXT,"
            " tool_calls_json TEXT,"
            " PRIMARY KEY (turn_id, seq))"
        )
        # Insert a pre-existing row to make sure migration doesn't drop data.
        await conn.execute(
            "INSERT INTO turns (turn_id, session_key, status, "
            "started_at_ms, user_text) VALUES (?, ?, ?, ?, ?)",
            (42, "sess-legacy", "in_progress", 1234, "legacy task"),
        )
        await conn.commit()

    # Open via the public API — migration must fire.
    journal = await AgentJournal.open(path)
    try:
        # The legacy row survives and has the default '' channel.
        rows = await journal.list_resumable_in_progress(window_ms=10**13)
        legacy = {r.turn_id: r for r in rows}
        assert 42 in legacy
        assert legacy[42].channel == ""

        # A fresh begin_turn now accepts and persists the channel kwarg.
        new_tid = await journal.begin_turn(
            "sess-post", "post-migration", channel="telegram"
        )
        assert new_tid is not None
        rows_after = await journal.list_resumable_in_progress(window_ms=10**13)
        by_id = {r.turn_id: r for r in rows_after}
        assert by_id[new_tid].channel == "telegram"
    finally:
        await journal.close()


async def test_mark_stale_in_progress_accepts_older_than_seconds(
    journal: AgentJournal,
) -> None:
    """The boot-time auto-resume sweep passes a multi-hour cutoff; the
    journal must honour it instead of the 5-min default."""
    import aiosqlite

    # A turn started 10 minutes ago — older than the 5-min default but
    # younger than the 1-hour cutoff we'll pass.
    tid = await journal.begin_turn("sess-mid", "mid-age", channel="telegram")
    import time as _time

    backdated = int(_time.time() * 1000) - 10 * 60 * 1000
    async with aiosqlite.connect(journal._path) as conn:
        await conn.execute(
            "UPDATE turns SET started_at_ms = ? WHERE turn_id = ?",
            (backdated, tid),
        )
        await conn.commit()

    # Sweep with a 1-hour cutoff — the row should NOT flip (it's
    # younger than 1 h).
    swept_long = await journal.mark_stale_in_progress_as_errored(
        older_than_seconds=3600
    )
    assert swept_long == 0

    # Sweep with a 1-minute cutoff — now it DOES flip (it's 10 min old).
    swept_short = await journal.mark_stale_in_progress_as_errored(
        older_than_seconds=60
    )
    assert swept_short == 1


# ---------------------------------------------------------------------------
# Perf — batched ``append_messages`` collapses N writes into one transaction.
# ---------------------------------------------------------------------------


async def test_append_messages_single_transaction(
    journal: AgentJournal,
) -> None:
    """A two-message batch must execute exactly one BEGIN IMMEDIATE /
    one COMMIT and produce N rows at strictly-incrementing seq.

    Hot path: the chat handler calls this after every builtin tool
    dispatch to journal the (assistant tool_call, tool result) pair.
    Two separate ``append_message`` calls cost ~10ms in transactional
    overhead; this single-transaction shape collapses that to ~5ms.

    Implementation: sniff ``conn.execute`` on the live backend to count
    ``BEGIN IMMEDIATE`` and ``COMMIT`` invocations across the batch.
    """
    tid = await journal.begin_turn("sess-batch", "batched task")
    assert tid is not None

    # ``AgentJournal`` exposes the backend so we can monkey the
    # underlying aiosqlite connection. Reach in deliberately — this is
    # a perf assertion that pins the wire shape.
    from corlinman_server.agent_journal_backend import SqliteJournalBackend

    backend = journal.backend
    assert isinstance(backend, SqliteJournalBackend)
    conn = backend._c  # type: ignore[attr-defined]

    # Wrap execute() — record the SQL the backend issues during the
    # batch. ``commit()`` on aiosqlite is a separate coroutine, not an
    # execute("COMMIT"), so we also wrap that to count commits.
    sql_log: list[str] = []
    commit_count = {"n": 0}

    original_execute = conn.execute
    original_commit = conn.commit

    async def spy_execute(sql: str, *args: Any, **kwargs: Any) -> Any:
        sql_log.append(sql)
        return await original_execute(sql, *args, **kwargs)

    async def spy_commit() -> Any:
        commit_count["n"] += 1
        return await original_commit()

    conn.execute = spy_execute  # type: ignore[method-assign]
    conn.commit = spy_commit  # type: ignore[method-assign]
    try:
        await journal.append_messages(
            tid,
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": '{"expression":"2+2"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": '{"result":4}',
                    "tool_call_id": "c1",
                },
            ],
        )
    finally:
        conn.execute = original_execute  # type: ignore[method-assign]
        conn.commit = original_commit  # type: ignore[method-assign]

    # ── Wire-shape assertions ─────────────────────────────────────
    # Exactly ONE BEGIN IMMEDIATE for the whole batch.
    begins = [s for s in sql_log if s.strip().upper().startswith("BEGIN")]
    assert len(begins) == 1, (
        f"expected 1 BEGIN IMMEDIATE for the batch, got {len(begins)}: "
        f"{begins!r}"
    )
    # Exactly ONE commit for the whole batch.
    assert commit_count["n"] == 1, (
        f"expected 1 commit for the batch, got {commit_count['n']}"
    )
    # N INSERTs into turn_messages (one per message — 2 here).
    inserts = [
        s for s in sql_log
        if "INSERT INTO turn_messages" in s
    ]
    assert len(inserts) == 2, (
        f"expected 2 INSERTs for the 2-message batch, got {len(inserts)}: "
        f"{inserts!r}"
    )
    # Exactly ONE SELECT MAX(seq) — the per-message increment is local
    # to the loop so we don't pay a round-trip per row.
    seq_selects = [
        s for s in sql_log
        if "MAX(seq)" in s and "turn_messages" in s
    ]
    assert len(seq_selects) == 1, (
        f"expected 1 SELECT MAX(seq), got {len(seq_selects)}: "
        f"{seq_selects!r}"
    )

    # ── Data-shape assertion ──────────────────────────────────────
    # The batch persisted in strict seq order with no holes.
    msgs = await journal._load_messages(tid)
    assert [m["role"] for m in msgs] == ["assistant", "tool"]
    assert msgs[0]["tool_calls"][0]["id"] == "c1"
    assert msgs[1]["tool_call_id"] == "c1"


async def test_append_messages_empty_list_is_noop(
    journal: AgentJournal,
) -> None:
    """``append_messages(turn_id, [])`` must not touch the DB or raise.

    Defensive — callers that accumulate a batch may end up with an
    empty list (e.g. all entries failed pre-serialisation). The wire
    shape stays correct: no BEGIN, no INSERT, no COMMIT.
    """
    tid = await journal.begin_turn("sess-empty-batch", "noop")
    assert tid is not None

    # Pre-batch row count from turn_messages.
    from corlinman_server.agent_journal_backend import SqliteJournalBackend

    backend = journal.backend
    assert isinstance(backend, SqliteJournalBackend)
    conn = backend._c  # type: ignore[attr-defined]

    cur = await conn.execute(
        "SELECT COUNT(*) FROM turn_messages WHERE turn_id = ?", (tid,)
    )
    row = await cur.fetchone()
    await cur.close()
    before = int(row[0]) if row is not None else 0

    await journal.append_messages(tid, [])

    cur = await conn.execute(
        "SELECT COUNT(*) FROM turn_messages WHERE turn_id = ?", (tid,)
    )
    row = await cur.fetchone()
    await cur.close()
    after = int(row[0]) if row is not None else 0
    assert before == after, (
        "append_messages([]) must not insert any rows; "
        f"before={before} after={after}"
    )


async def test_append_messages_round_trips_in_order(
    journal: AgentJournal,
) -> None:
    """Batched messages land in the order they were supplied — seq is
    assigned strictly increasing inside the single transaction.
    """
    tid = await journal.begin_turn("sess-order-batch", "ordering")
    assert tid is not None

    await journal.append_messages(
        tid,
        [
            {"role": "user", "content": "step 1"},
            {"role": "assistant", "content": "ack"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "noop", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "{}", "tool_call_id": "c1"},
        ],
    )

    msgs = await journal._load_messages(tid)
    assert [m["role"] for m in msgs] == [
        "user", "assistant", "assistant", "tool",
    ]
    # The 3rd row carries the tool_calls payload; the 4th carries the
    # tool_call_id back-reference.
    assert msgs[2]["tool_calls"][0]["id"] == "c1"
    assert msgs[3]["tool_call_id"] == "c1"
