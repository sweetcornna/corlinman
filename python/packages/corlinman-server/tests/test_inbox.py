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
