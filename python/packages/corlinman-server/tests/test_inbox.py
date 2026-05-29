"""Tests for the T4.3 durable inbound-message queue."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiosqlite
import pytest

from corlinman_server.inbox import (
    INBOX_DEAD,
    INBOX_DISPATCHED,
    INBOX_DONE,
    INBOX_PENDING,
    Inbox,
)


@pytest.fixture
async def inbox(tmp_path: Path) -> Inbox:
    ib = await Inbox.open(tmp_path / "inbox.sqlite")
    yield ib
    await ib.close()


async def test_enqueue_returns_id_and_creates_pending_row(inbox: Inbox) -> None:
    iid = await inbox.enqueue(
        channel="qq",
        session_key="qq|self|1|2",
        message_id="msg-100",
        user_text="hello",
    )
    assert iid > 0
    pending = await inbox.list_pending()
    assert len(pending) == 1
    entry = pending[0]
    assert entry.id == iid
    assert entry.channel == "qq"
    assert entry.session_key == "qq|self|1|2"
    assert entry.message_id == "msg-100"
    assert entry.user_text == "hello"
    assert entry.status == INBOX_PENDING


async def test_mark_dispatched_then_done_lifecycle(inbox: Inbox) -> None:
    iid = await inbox.enqueue(channel="qq", session_key="s1", user_text="hi")
    await inbox.mark_dispatched(iid)
    recent = await inbox.list_recent(limit=1)
    assert recent[0].status == INBOX_DISPATCHED
    await inbox.mark_done(iid)
    recent = await inbox.list_recent(limit=1)
    assert recent[0].status == INBOX_DONE
    # done rows don't show up in pending.
    pending = await inbox.list_pending()
    assert pending == []


async def test_mark_dead_with_error(inbox: Inbox) -> None:
    iid = await inbox.enqueue(channel="qq", session_key="s1", user_text="hi")
    await inbox.mark_dead(iid, error="poison message")
    recent = await inbox.list_recent(limit=1)
    assert recent[0].status == INBOX_DEAD
    assert recent[0].error == "poison message"


async def test_list_pending_filters_by_channel(inbox: Inbox) -> None:
    await inbox.enqueue(channel="qq", session_key="s1", user_text="qq-msg")
    await inbox.enqueue(channel="telegram", session_key="s2", user_text="tg-msg")
    qq_pending = await inbox.list_pending(channel="qq")
    tg_pending = await inbox.list_pending(channel="telegram")
    assert {e.user_text for e in qq_pending} == {"qq-msg"}
    assert {e.user_text for e in tg_pending} == {"tg-msg"}


async def test_list_pending_orders_oldest_first(inbox: Inbox) -> None:
    a = await inbox.enqueue(channel="qq", session_key="s1", user_text="A")
    await asyncio.sleep(0.01)
    b = await inbox.enqueue(channel="qq", session_key="s1", user_text="B")
    pending = await inbox.list_pending()
    assert [e.id for e in pending] == [a, b]


async def test_reset_stale_dispatched_flips_old_rows(inbox: Inbox) -> None:
    iid = await inbox.enqueue(channel="qq", session_key="s1", user_text="x")
    await inbox.mark_dispatched(iid)
    # Backdate the row by overriding updated_at_ms so the reset finds it.
    async with aiosqlite.connect(inbox._path) as conn:
        await conn.execute(
            "UPDATE inbox SET updated_at_ms = 0 WHERE id = ?", (iid,)
        )
        await conn.commit()
    n = await inbox.reset_stale_dispatched(older_than_seconds=10)
    assert n == 1
    pending = await inbox.list_pending()
    assert len(pending) == 1
    assert pending[0].status == INBOX_PENDING
    assert "stale" in (pending[0].error or "")


async def test_increment_retry_flips_to_dead_after_max(inbox: Inbox) -> None:
    iid = await inbox.enqueue(channel="qq", session_key="s1", user_text="x")
    r1 = await inbox.increment_retry(iid, error="bump 1")
    r2 = await inbox.increment_retry(iid, error="bump 2")
    r3 = await inbox.increment_retry(iid, error="bump 3")
    assert r1 == 1
    assert r2 == 2
    assert r3 == 3  # at the cap
    recent = await inbox.list_recent(limit=1)
    assert recent[0].status == INBOX_DEAD
    assert recent[0].retries == 3


async def test_increment_retry_is_atomic_under_concurrency(inbox: Inbox) -> None:
    """Two concurrent ``increment_retry`` calls on the same row must
    both land — the original SELECT-then-UPDATE pattern lost one of the
    two increments to a read-modify-write race (#R2-002), leaving the
    counter stuck below ``_MAX_RETRIES`` so the row never reached
    ``dead`` and the message kept retrying forever.
    """
    start_ms = int(time.time() * 1000)
    iid = await inbox.enqueue(channel="qq", session_key="s1", user_text="x")
    await asyncio.gather(
        inbox.increment_retry(iid, error="race-a"),
        inbox.increment_retry(iid, error="race-b"),
    )
    recent = await inbox.list_recent(limit=1)
    assert recent[0].retries == 2
    # The atomic UPDATE must also preserve the error and timestamp writes
    # — a future refactor that drops COALESCE(?, error) or updated_at_ms
    # would silently corrupt audit data without these asserts (#R3-001).
    assert recent[0].error in {"race-a", "race-b"}
    assert recent[0].updated_at_ms >= start_ms


async def test_increment_retry_does_not_resurrect_done(inbox: Inbox) -> None:
    """A stray ``increment_retry`` on an already-``done`` row must be a
    no-op — otherwise the atomic UPDATE flips the row back to ``pending``
    with ``retries=1`` and the boot drainer redelivers an already-
    processed message (#R3-001).
    """
    iid = await inbox.enqueue(channel="qq", session_key="s1", user_text="x")
    await inbox.mark_done(iid)
    r = await inbox.increment_retry(iid, error="stray")
    assert r == -1  # no-op signal for callers
    recent = await inbox.list_recent(limit=1)
    assert recent[0].status == INBOX_DONE
    assert recent[0].retries == 0
    assert recent[0].error is None


async def test_increment_retry_does_not_resurrect_dead(inbox: Inbox) -> None:
    """Same guard for the terminal ``dead`` status — once given up on,
    a retry attempt must not reset retries and flip the row back to
    ``pending`` (#R3-001).
    """
    iid = await inbox.enqueue(channel="qq", session_key="s1", user_text="x")
    await inbox.mark_dead(iid, error="poison")
    r = await inbox.increment_retry(iid, error="stray")
    assert r == -1
    recent = await inbox.list_recent(limit=1)
    assert recent[0].status == INBOX_DEAD
    assert recent[0].retries == 0
    assert recent[0].error == "poison"


async def test_increment_retry_flips_to_dead_atomically(inbox: Inbox) -> None:
    """The status flip to ``dead`` must happen in the same statement as
    the retries bump, so a concurrent reader never observes
    ``retries >= _MAX_RETRIES`` with status still ``pending``.
    """
    iid = await inbox.enqueue(channel="qq", session_key="s1", user_text="x")
    await inbox.increment_retry(iid, error="1")
    await inbox.increment_retry(iid, error="2")
    # crossing the cap on this call must flip status to dead in one go.
    r = await inbox.increment_retry(iid, error="3")
    assert r == 3
    recent = await inbox.list_recent(limit=1)
    assert recent[0].retries == 3
    assert recent[0].status == INBOX_DEAD


async def test_stuck_dispatched_count(inbox: Inbox) -> None:
    iid = await inbox.enqueue(channel="qq", session_key="s1", user_text="x")
    await inbox.mark_dispatched(iid)
    async with aiosqlite.connect(inbox._path) as conn:
        await conn.execute(
            "UPDATE inbox SET updated_at_ms = 0 WHERE id = ?", (iid,)
        )
        await conn.commit()
    n = await inbox.stuck_dispatched_count(older_than_seconds=10)
    assert n == 1


async def test_concurrent_enqueues_get_distinct_ids(inbox: Inbox) -> None:
    """Two near-simultaneous enqueues must produce distinct rows."""
    ids = await asyncio.gather(
        inbox.enqueue(channel="qq", session_key="s1", user_text="A"),
        inbox.enqueue(channel="qq", session_key="s1", user_text="B"),
        inbox.enqueue(channel="qq", session_key="s1", user_text="C"),
    )
    assert len(set(ids)) == 3
    pending = await inbox.list_pending()
    assert len(pending) == 3


# ---------------------------------------------------------------------------
# Auto-resume — cross-channel inbox usage
# ---------------------------------------------------------------------------


async def test_inbox_accepts_telegram_channel_rows(inbox: Inbox) -> None:
    """The boot-replay dispatcher writes Telegram (and Discord / Slack /
    Feishu) rows through the same inbox the QQ dispatcher already uses.
    The CHECK constraint sits on ``status``, not ``channel`` — any
    channel id round-trips verbatim.
    """
    tg_id = await inbox.enqueue(
        channel="telegram",
        session_key="tg|chat:42",
        message_id="resume:1700000000",
        user_text="please continue",
    )
    disc_id = await inbox.enqueue(
        channel="discord",
        session_key="disc|g:1|c:2",
        message_id="resume:1700000001",
        user_text="finish the task",
    )
    assert tg_id > 0 and disc_id > 0

    tg_rows = await inbox.list_pending(channel="telegram")
    assert len(tg_rows) == 1
    assert tg_rows[0].user_text == "please continue"
    assert tg_rows[0].status == INBOX_PENDING

    disc_rows = await inbox.list_pending(channel="discord")
    assert len(disc_rows) == 1
    assert disc_rows[0].user_text == "finish the task"


async def test_inbox_message_id_carries_resume_marker(inbox: Inbox) -> None:
    """Synthesized boot-replay rows use ``message_id="resume:<turn_id>"``
    so a channel handler can detect a resume-injected row (and e.g.
    suppress the "received your message" ack)."""
    turn_id = 1700123456789
    await inbox.enqueue(
        channel="telegram",
        session_key="tg|sess",
        message_id=f"resume:{turn_id}",
        user_text="continue",
    )
    rows = await inbox.list_pending(channel="telegram")
    assert len(rows) == 1
    assert rows[0].message_id == f"resume:{turn_id}"
    assert rows[0].message_id.startswith("resume:")
