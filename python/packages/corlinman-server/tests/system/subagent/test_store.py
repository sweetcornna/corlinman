"""Tests for :class:`corlinman_server.system.subagent.SubagentTaskStore`.

Mirrors the cases in ``tests/system/upgrader/test_state.py`` but for the
subagent shape: round-trip get, partial updates, in-flight list, log
rolling at 4 kB, persistence across instances, kill-flip semantics.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from corlinman_server.system.subagent.store import (
    SubagentRequest,
    SubagentTaskStore,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_req(
    request_id: str = "req-1",
    parent_session_key: str = "sess-A",
    subagent_type: str = "researcher",
    description: str | None = "test work",
) -> SubagentRequest:
    return SubagentRequest(
        request_id=request_id,
        parent_session_key=parent_session_key,
        parent_agent_id="agent-parent",
        subagent_type=subagent_type,
        goal="figure something out",
        description=description,
        requested_at=_now_ms(),
        requested_by="admin",
    )


@pytest.mark.asyncio
async def test_begin_then_get_roundtrips(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    req = _make_req()
    seeded = await store.begin(req)

    fetched = await store.get(req.request_id)
    assert fetched is not None
    assert fetched.request_id == req.request_id
    assert fetched.state == "queued"
    assert fetched.subagent_type == "researcher"
    assert fetched.description == "test work"
    assert fetched.parent_session_key == "sess-A"

    # Returned snapshot is a copy — mutating it MUST NOT bleed back.
    seeded.state = "succeeded"  # type: ignore[assignment]
    fetched_again = await store.get(req.request_id)
    assert fetched_again is not None
    assert fetched_again.state == "queued"


@pytest.mark.asyncio
async def test_update_partial_preserves_other_fields(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    req = _make_req()
    await store.begin(req)

    await store.update(req.request_id, state="running", started_at=12345)
    after = await store.get(req.request_id)
    assert after is not None
    assert after.state == "running"
    assert after.started_at == 12345
    assert after.subagent_type == "researcher"

    await store.update(
        req.request_id,
        state="succeeded",
        finished_at=67890,
        tool_calls_made=7,
        elapsed_ms=42,
        finish_reason="stop",
        summary="all done",
    )
    final = await store.get(req.request_id)
    assert final is not None
    assert final.state == "succeeded"
    assert final.finished_at == 67890
    assert final.tool_calls_made == 7
    assert final.summary == "all done"


@pytest.mark.asyncio
async def test_current_in_flight_lists_active_only(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    a = _make_req(request_id="req-A")
    b = _make_req(request_id="req-B", parent_session_key="sess-B")
    c = _make_req(request_id="req-C")
    await store.begin(a)
    await store.begin(b)
    await store.begin(c)
    await store.update("req-C", state="succeeded")

    active = await store.list_active()
    ids = {r.request_id for r in active}
    assert ids == {"req-A", "req-B"}

    # Scoping by parent_session_key
    scoped = await store.current_in_flight(parent_session_key="sess-A")
    assert {r.request_id for r in scoped} == {"req-A"}


@pytest.mark.asyncio
async def test_append_log_rolls_at_4kb(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    req = _make_req()
    await store.begin(req)

    chunk = "x" * 1024
    for _ in range(8):  # 8 kB of input
        await store.append_log(req.request_id, chunk)

    status = await store.get(req.request_id)
    assert status is not None
    assert len(status.log_tail.encode("utf-8")) <= 4 * 1024
    assert status.log_tail.endswith("x")


@pytest.mark.asyncio
async def test_persistence_round_trip_across_instances(tmp_path: Path) -> None:
    persist = tmp_path / ".subagent-state.json"
    store1 = SubagentTaskStore(persist)
    req = _make_req(request_id="req-PERSIST", subagent_type="editor")
    await store1.begin(req)
    await store1.update(
        "req-PERSIST",
        state="running",
        started_at=4242,
        child_session_key="sess-A::child::0",
    )
    await store1.append_log("req-PERSIST", "child output line\n")

    # Brand-new store reading the same file. D3 — a fresh instance has an
    # empty in-process task map, so the persisted ``running`` row is an
    # orphan (nothing is driving it). Boot reconciliation resolves it to the
    # terminal ``stalled`` state rather than leaving it ``running`` forever.
    # All the row's other data still round-trips intact.
    store2 = SubagentTaskStore(persist)
    recovered = await store2.get("req-PERSIST")
    assert recovered is not None
    assert recovered.state == "stalled"
    assert recovered.finish_reason == "stalled_on_restart"
    assert recovered.finished_at is not None
    assert recovered.subagent_type == "editor"
    assert recovered.started_at == 4242
    assert recovered.child_session_key == "sess-A::child::0"
    assert recovered.log_tail == "child output line\n"

    # ``stalled`` is terminal, so the orphan no longer consumes tenant quota.
    assert await store2.count_in_flight_for_tenant(req.tenant_id) == 0

    # ``get_request`` also round-trips
    fetched_req = await store2.get_request("req-PERSIST")
    assert fetched_req is not None
    assert fetched_req.subagent_type == "editor"


@pytest.mark.asyncio
async def test_inline_request_fields_roundtrip_across_instances(
    tmp_path: Path,
) -> None:
    persist = tmp_path / ".subagent-state.json"
    store1 = SubagentTaskStore(persist)
    req = SubagentRequest(
        request_id="req-inline",
        parent_session_key="sess-inline",
        parent_agent_id="agent-parent",
        subagent_type="inline-reviewer",
        goal="review this change",
        description="inline review",
        requested_at=_now_ms(),
        requested_by="model",
        tenant_id="tenant-a",
        inline_system_prompt="You are a temporary code reviewer.",
        inline_model="gpt-4o-mini",
    )
    await store1.begin(req)

    store2 = SubagentTaskStore(persist)
    fetched = await store2.get_request("req-inline")

    assert fetched is not None
    assert fetched.subagent_type == "inline-reviewer"
    assert fetched.inline_system_prompt == "You are a temporary code reviewer."
    assert fetched.inline_model == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_set_killed_flips_state(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    req = _make_req()
    await store.begin(req)
    await store.update(req.request_id, state="running")

    result = await store.set_killed(req.request_id, by="alice")
    assert result is not None
    assert result.state == "killed"
    assert result.finish_reason == "killed_by:alice"
    assert result.finished_at is not None

    # Second kill — already terminal, returns None.
    result2 = await store.set_killed(req.request_id, by="bob")
    assert result2 is None


@pytest.mark.asyncio
async def test_update_unknown_request_raises(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    with pytest.raises(KeyError):
        await store.update("does-not-exist", state="running")


@pytest.mark.asyncio
async def test_get_unknown_request_returns_none(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    assert await store.get("nope") is None


@pytest.mark.asyncio
async def test_append_log_unknown_request_noop(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    # Must not raise.
    await store.append_log("ghost", "data")


@pytest.mark.asyncio
async def test_summary_truncates_at_4kb(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    req = _make_req()
    await store.begin(req)

    big = "A" * (5 * 1024)
    await store.set_summary(req.request_id, big)

    status = await store.get(req.request_id)
    assert status is not None
    assert len(status.summary.encode("utf-8")) <= 4 * 1024


@pytest.mark.asyncio
async def test_terminal_rows_are_bounded(tmp_path: Path) -> None:
    """Terminal rows must not accumulate without bound.

    Spawning N short-lived subagents (begin → update to a terminal state)
    must not grow ``_statuses``/``_requests`` (and the on-disk flush) to
    O(N). A bounded recent-terminal window is retained; older terminal
    rows are evicted.
    """
    persist = tmp_path / ".subagent-state.json"
    store = SubagentTaskStore(persist)

    n = 5_000
    for i in range(n):
        rid = f"req-{i}"
        await store.begin(_make_req(request_id=rid))
        await store.update(rid, state="succeeded", finished_at=_now_ms())

    # The store must not retain every terminal row.
    assert len(store._statuses) <= SubagentTaskStore._TERMINAL_RETENTION_CAP
    assert len(store._requests) <= SubagentTaskStore._TERMINAL_RETENTION_CAP

    # On-disk flush stays bounded by the cap (not by N). With N=5000 the
    # unbounded store wrote ~3.5 MB; the bounded store stays well under
    # that, proportional to the retention cap rather than the spawn count.
    flush_bytes = persist.stat().st_size
    assert flush_bytes < 600_000, flush_bytes

    # The most-recent terminal rows are the ones retained.
    recovered = await store.get(f"req-{n - 1}")
    assert recovered is not None
    assert recovered.state == "succeeded"


@pytest.mark.asyncio
async def test_in_flight_rows_never_evicted(tmp_path: Path) -> None:
    """In-flight rows are preserved regardless of terminal churn.

    Correctness of ``count_in_flight_for_tenant`` / ``get`` must survive a
    flood of terminal rows that exceeds the retention cap.
    """
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")

    # Two long-lived in-flight rows, one per tenant.
    live_a = SubagentRequest(
        request_id="live-A",
        parent_session_key="sess-A",
        parent_agent_id="agent-parent",
        subagent_type="researcher",
        goal="long task",
        description=None,
        requested_at=_now_ms(),
        requested_by="admin",
        tenant_id="tenant-A",
    )
    live_b = SubagentRequest(
        request_id="live-B",
        parent_session_key="sess-B",
        parent_agent_id="agent-parent",
        subagent_type="researcher",
        goal="long task",
        description=None,
        requested_at=_now_ms(),
        requested_by="admin",
        tenant_id="tenant-B",
    )
    await store.begin(live_a)
    await store.begin(live_b)
    await store.update("live-A", state="running")
    await store.update("live-B", state="running")

    # Flood the store with far more terminal rows than the cap.
    for i in range(SubagentTaskStore._TERMINAL_RETENTION_CAP * 3):
        rid = f"term-{i}"
        await store.begin(_make_req(request_id=rid))
        await store.update(rid, state="succeeded", finished_at=_now_ms())

    # In-flight rows still present + counted correctly.
    assert await store.get("live-A") is not None
    assert await store.get("live-B") is not None
    assert await store.count_in_flight_for_tenant("tenant-A") == 1
    assert await store.count_in_flight_for_tenant("tenant-B") == 1

    active = await store.list_active()
    assert {r.request_id for r in active} == {"live-A", "live-B"}

    # Total retained = 2 in-flight + at most the terminal cap.
    assert (
        len(store._statuses) <= SubagentTaskStore._TERMINAL_RETENTION_CAP + 2
    )
