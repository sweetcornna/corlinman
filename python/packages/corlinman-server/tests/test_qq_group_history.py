"""Tests for the QQ group-message history store (monitor digest)."""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_server.qq_group_history import TEXT_CAP, QqGroupHistory


@pytest.fixture
async def store(tmp_path: Path) -> QqGroupHistory:
    s = await QqGroupHistory.open(tmp_path / "history.sqlite")
    yield s
    await s.close()


async def _seed(store: QqGroupHistory, *, count: int, start_ms: int = 1000) -> None:
    for i in range(count):
        await store.record(
            instance_id="inst",
            group_id="123",
            sender_user_id=str(100 + i),
            sender_name=f"user{i}",
            message_id=f"m{i}",
            event_time_ms=start_ms + i,
            text=f"message {i}",
        )


async def test_record_and_list_roundtrip(store: QqGroupHistory) -> None:
    row_id = await store.record(
        instance_id="inst",
        group_id="123",
        sender_user_id="11111",
        sender_name="老明",
        message_id="42",
        event_time_ms=5_000,
        text="早饭吃了吗",
    )
    assert row_id > 0
    rows = await store.list_window(
        instance_id="inst", group_id="123", since_ms=0
    )
    assert len(rows) == 1
    msg = rows[0]
    assert msg.sender_user_id == "11111"
    assert msg.sender_name == "老明"
    assert msg.message_id == "42"
    assert msg.event_time_ms == 5_000
    assert msg.text == "早饭吃了吗"
    assert msg.received_at_ms > 0


async def test_blank_text_is_skipped_and_long_text_capped(
    store: QqGroupHistory,
) -> None:
    assert (
        await store.record(
            instance_id="inst", group_id="123", sender_user_id="1", text="   "
        )
        == -1
    )
    await store.record(
        instance_id="inst",
        group_id="123",
        sender_user_id="1",
        text="x" * (TEXT_CAP + 500),
    )
    rows = await store.list_window(instance_id="inst", group_id="123", since_ms=0)
    assert len(rows) == 1
    assert len(rows[0].text) == TEXT_CAP


async def test_window_scoping_by_instance_group_and_sender(
    store: QqGroupHistory,
) -> None:
    await _seed(store, count=3)
    await store.record(
        instance_id="other", group_id="123", sender_user_id="9", text="other inst"
    )
    await store.record(
        instance_id="inst", group_id="999", sender_user_id="9", text="other group"
    )
    rows = await store.list_window(instance_id="inst", group_id="123", since_ms=0)
    assert [r.text for r in rows] == ["message 0", "message 1", "message 2"]
    only = await store.list_window(
        instance_id="inst", group_id="123", since_ms=0, sender_ids=["101"]
    )
    assert [r.text for r in only] == ["message 1"]
    assert (
        await store.count_window(
            instance_id="inst", group_id="123", since_ms=0, sender_ids=["101", "102"]
        )
        == 2
    )


async def test_time_window_bounds(store: QqGroupHistory) -> None:
    import asyncio

    await store.record(
        instance_id="inst", group_id="123", sender_user_id="1", text="early"
    )
    await asyncio.sleep(0.005)  # distinct received_at_ms for the boundary
    await store.record(
        instance_id="inst", group_id="123", sender_user_id="1", text="late"
    )
    rows = await store.list_window(instance_id="inst", group_id="123", since_ms=0)
    assert [r.text for r in rows] == ["early", "late"]
    cut = rows[1].received_at_ms
    older = await store.list_window(
        instance_id="inst", group_id="123", since_ms=0, until_ms=cut
    )
    assert [r.text for r in older] == ["early"]
    newer = await store.list_window(
        instance_id="inst", group_id="123", since_ms=cut
    )
    assert [r.text for r in newer] == ["late"]


async def test_limit_keeps_newest_rows_oldest_first(store: QqGroupHistory) -> None:
    await _seed(store, count=5)
    rows = await store.list_window(
        instance_id="inst", group_id="123", since_ms=0, limit=2
    )
    assert [r.text for r in rows] == ["message 3", "message 4"]


async def test_prune_removes_old_rows(store: QqGroupHistory) -> None:
    await _seed(store, count=3)
    rows = await store.list_window(instance_id="inst", group_id="123", since_ms=0)
    cutoff = rows[-1].received_at_ms + 1
    removed = await store.prune(older_than_ms=cutoff)
    assert removed == 3
    assert (
        await store.count_window(instance_id="inst", group_id="123", since_ms=0) == 0
    )


async def test_last_fire_roundtrip_and_upsert(store: QqGroupHistory) -> None:
    assert await store.get_last_fire("inst:m1") is None
    await store.set_last_fire("inst:m1", 1_000)
    assert await store.get_last_fire("inst:m1") == 1_000
    await store.set_last_fire("inst:m1", 2_000)
    assert await store.get_last_fire("inst:m1") == 2_000
    assert await store.get_last_fire("inst:other") is None
