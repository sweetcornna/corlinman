"""Tests for the T4.1 / T4.4 agent turn journal."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from corlinman_server.agent_journal import (
    AgentJournal,
    ResumeData,
    TURN_COMPLETED,
    TURN_ERRORED,
    TURN_IN_PROGRESS,
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
