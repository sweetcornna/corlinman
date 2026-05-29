"""Tests for ``AgentJournal.list_session_summaries`` +
``delete_session`` — the two methods that back the
``/admin/sessions`` admin surface now that the chat servicer journals
into ``agent_journal.sqlite`` instead of the legacy ``sessions.sqlite``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from corlinman_server.agent_journal import AgentJournal, SessionSummary
from corlinman_server.agent_journal_backend import SESSION_SUMMARY_PREVIEW_LEN


@pytest.fixture
async def journal(tmp_path: Path) -> AgentJournal:
    j = await AgentJournal.open(tmp_path / "j.sqlite")
    yield j
    await j.close()


# ---------------------------------------------------------------------------
# list_session_summaries
# ---------------------------------------------------------------------------


async def test_list_session_summaries_groups_turns(
    journal: AgentJournal,
) -> None:
    """Three turns across two session_keys collapse to two summary rows
    with the expected per-session aggregates and ordering."""
    # Session A: two turns (text + one message each).
    t_a1 = await journal.begin_turn("sess-A", "first A")
    await journal.append_message(t_a1, "user", "first A")
    await asyncio.sleep(0.01)
    t_a2 = await journal.begin_turn("sess-A", "second A")
    await journal.append_message(t_a2, "user", "second A")
    await journal.append_message(t_a2, "assistant", "hi")

    await asyncio.sleep(0.01)
    # Session B: one turn, no messages.
    await journal.begin_turn("sess-B", "only B")

    summaries = await journal.list_session_summaries()

    assert len(summaries) == 2
    # ORDER BY MAX(started_at_ms) DESC → B (started last) is first.
    by_key = {s.session_key: s for s in summaries}
    assert summaries[0].session_key == "sess-B"

    assert by_key["sess-A"].turn_count == 2
    assert by_key["sess-A"].message_count == 3
    assert by_key["sess-A"].first_seen_at_ms <= by_key["sess-A"].last_seen_at_ms

    assert by_key["sess-B"].turn_count == 1
    assert by_key["sess-B"].message_count == 0


async def test_list_session_summaries_carries_last_user_text(
    journal: AgentJournal,
) -> None:
    """The summary row carries the most-recent turn's user_text, capped
    at 80 chars."""
    long_text = "hello world " * 20  # > 80 chars
    await journal.begin_turn("sess-preview", "first short")
    await asyncio.sleep(0.01)
    await journal.begin_turn("sess-preview", long_text)

    summaries = await journal.list_session_summaries()
    assert len(summaries) == 1
    s = summaries[0]
    assert s.session_key == "sess-preview"
    assert s.last_user_text is not None
    # Capped at SESSION_SUMMARY_PREVIEW_LEN (80).
    assert len(s.last_user_text) == SESSION_SUMMARY_PREVIEW_LEN
    assert s.last_user_text == long_text[:SESSION_SUMMARY_PREVIEW_LEN]


async def test_list_session_summaries_carries_last_status(
    journal: AgentJournal,
) -> None:
    """``last_status`` reflects the most-recent turn's status."""
    t_done = await journal.begin_turn("sess-done", "first")
    await journal.complete_turn(t_done)
    await asyncio.sleep(0.01)
    t_open = await journal.begin_turn("sess-done", "second")

    summaries = await journal.list_session_summaries()
    assert len(summaries) == 1
    assert summaries[0].last_status == "in_progress"


async def test_list_session_summaries_limit_clamps_rows(
    journal: AgentJournal,
) -> None:
    """``limit`` caps the number of rows returned (ordered by recency)."""
    for i in range(5):
        await journal.begin_turn(f"sess-{i}", f"text-{i}")
        await asyncio.sleep(0.005)

    summaries = await journal.list_session_summaries(limit=2)
    assert len(summaries) == 2
    # Most-recently-started first.
    assert summaries[0].session_key == "sess-4"
    assert summaries[1].session_key == "sess-3"


async def test_list_session_summaries_empty_when_no_turns(
    journal: AgentJournal,
) -> None:
    summaries = await journal.list_session_summaries()
    assert summaries == []


async def test_list_session_summaries_returns_typed_dataclass(
    journal: AgentJournal,
) -> None:
    """Sanity: the rows are :class:`SessionSummary` frozen dataclasses
    (the wire model relies on that immutability)."""
    await journal.begin_turn("sess-type", "hi")
    summaries = await journal.list_session_summaries()
    assert len(summaries) == 1
    assert isinstance(summaries[0], SessionSummary)
    with pytest.raises((AttributeError, Exception)):  # frozen=True
        summaries[0].session_key = "mutated"  # type: ignore[misc]


async def test_list_session_summaries_same_ms_preview_not_mixed(
    journal: AgentJournal,
) -> None:
    """R4-D6: two turns in ONE session with IDENTICAL ``started_at_ms``
    must not produce a preview row that mixes columns from different
    turns.

    ``begin_turn`` stores ``started_at_ms = ts`` unchanged, so two turns
    created in the same millisecond share ``started_at_ms`` while still
    getting distinct, strictly-increasing ``turn_id`` values (the PK
    collision retry bumps only ``turn_id``). The two independent
    correlated subqueries that build ``last_user_text`` and
    ``last_status`` only ``ORDER BY started_at_ms DESC`` — with no
    tie-breaker SQLite may resolve each subquery to a *different* one of
    the tied turns, yielding a (user_text, status) pair that belongs to
    neither real turn.

    Insert the two turns directly so the shared ``started_at_ms`` is
    deterministic (``begin_turn`` reads the wall clock fresh each call).
    The newest turn — the one with the larger ``turn_id`` — must win
    BOTH columns, so the observed pair must equal exactly one of the two
    real turns' pairs, never a cross-product mix.
    """
    conn = journal.backend._c  # type: ignore[attr-defined]
    same_ts = 5_000
    # Older turn: smaller turn_id, completed.
    await conn.execute(
        "INSERT INTO turns (turn_id, session_key, status, started_at_ms, "
        "user_text) VALUES (?, ?, ?, ?, ?)",
        (1_000, "sess-tie", "completed", same_ts, "OLDER user text"),
    )
    # Newer turn: larger turn_id, in_progress — the deterministic winner.
    await conn.execute(
        "INSERT INTO turns (turn_id, session_key, status, started_at_ms, "
        "user_text) VALUES (?, ?, ?, ?, ?)",
        (1_001, "sess-tie", "in_progress", same_ts, "NEWER user text"),
    )
    await conn.commit()

    older_pair = ("OLDER user text", "completed")
    newer_pair = ("NEWER user text", "in_progress")

    summaries = await journal.list_session_summaries()
    assert len(summaries) == 1
    s = summaries[0]
    observed = (s.last_user_text, s.last_status)

    # The pair must come from ONE turn, never a column-mix of the two.
    assert observed in (older_pair, newer_pair), (
        f"preview mixed columns from two turns: {observed!r} is neither "
        f"{older_pair!r} nor {newer_pair!r}"
    )
    # And with a turn_id tie-breaker the newest turn wins deterministically.
    assert observed == newer_pair


# ---------------------------------------------------------------------------
# delete_session
# ---------------------------------------------------------------------------


async def test_delete_session_removes_turns_and_messages(
    journal: AgentJournal,
) -> None:
    """Deleting ``sess-X`` purges its turns + cascading messages and
    leaves rows for OTHER sessions intact."""
    # Target session.
    t1 = await journal.begin_turn("sess-doomed", "doomed 1")
    await journal.append_message(t1, "user", "doomed 1")
    await journal.append_message(t1, "assistant", "doomed-reply")
    t2 = await journal.begin_turn("sess-doomed", "doomed 2")
    await journal.append_message(t2, "user", "doomed 2")

    # Bystander session that must survive.
    t_survivor = await journal.begin_turn("sess-survivor", "alive")
    await journal.append_message(t_survivor, "user", "alive")

    deleted = await journal.delete_session("sess-doomed")
    assert deleted == 2

    summaries = await journal.list_session_summaries()
    assert len(summaries) == 1
    assert summaries[0].session_key == "sess-survivor"
    assert summaries[0].message_count == 1

    # The cascading delete also wiped the doomed turn_messages — load
    # against the (now-defunct) turn id returns an empty list, never
    # the stale rows.
    msgs = await journal._load_messages(t1)
    assert msgs == []


async def test_delete_session_returns_zero_when_unknown(
    journal: AgentJournal,
) -> None:
    """Deleting a session_key with no journal rows returns 0 (the route
    layer maps that to 404)."""
    deleted = await journal.delete_session("never-existed")
    assert deleted == 0


async def test_delete_session_empty_key_returns_zero(
    journal: AgentJournal,
) -> None:
    """Defensive: an empty session_key cannot match a real row."""
    await journal.begin_turn("sess-real", "hi")
    deleted = await journal.delete_session("")
    assert deleted == 0
    # And the real row is still there.
    summaries = await journal.list_session_summaries()
    assert len(summaries) == 1
