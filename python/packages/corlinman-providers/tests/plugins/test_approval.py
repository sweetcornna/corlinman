"""Tests for ``corlinman_providers.plugins.approval``.

These cover the Python implementation only (the Rust source is a TODO stub).
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_providers.plugins.approval import (
    ApprovalDecision,
    ApprovalQueue,
    ApprovalRequest,
    ApprovalStore,
)


@pytest.mark.asyncio
async def test_store_insert_and_pending() -> None:
    store = ApprovalStore(":memory:")
    req = ApprovalRequest(
        call_id="call_a",
        plugin="bash",
        tool="run",
        args_preview="ls -la",
        session_key="sess_1",
        reason="first use",
    )
    await store.insert(req)

    pending = await store.pending()
    assert len(pending) == 1
    assert pending[0].call_id == "call_a"
    assert pending[0].decision is None


@pytest.mark.asyncio
async def test_store_decide_round_trip() -> None:
    store = ApprovalStore(":memory:")
    req = ApprovalRequest(
        call_id="call_b",
        plugin="bash",
        tool="run",
        args_preview="echo hi",
        session_key="sess_2",
        reason="manual",
    )
    await store.insert(req)
    assert await store.decide("call_b", ApprovalDecision.ALLOW) is True

    record = await store.get("call_b")
    assert record is not None
    assert record.decision == ApprovalDecision.ALLOW
    assert record.decided_at is not None

    # Already decided rows are not updated again.
    assert await store.decide("call_b", ApprovalDecision.DENY) is False
    record = await store.get("call_b")
    assert record is not None
    assert record.decision == ApprovalDecision.ALLOW


@pytest.mark.asyncio
async def test_store_decide_unknown_returns_false() -> None:
    store = ApprovalStore(":memory:")
    assert await store.decide("does-not-exist", ApprovalDecision.ALLOW) is False


@pytest.mark.asyncio
async def test_has_prior_approval_for_session() -> None:
    store = ApprovalStore(":memory:")
    assert await store.has_prior_approval_for_session("sess_x", "bash") is False

    await store.insert(
        ApprovalRequest(
            call_id="call_c",
            plugin="bash",
            tool="run",
            args_preview="...",
            session_key="sess_x",
            reason="...",
        )
    )
    assert await store.has_prior_approval_for_session("sess_x", "bash") is False
    await store.decide("call_c", ApprovalDecision.DENY)
    assert await store.has_prior_approval_for_session("sess_x", "bash") is False
    await store.insert(
        ApprovalRequest(
            call_id="call_d",
            plugin="bash",
            tool="run",
            args_preview="...",
            session_key="sess_x",
            reason="...",
        )
    )
    await store.decide("call_d", ApprovalDecision.ALLOW)
    assert await store.has_prior_approval_for_session("sess_x", "bash") is True


@pytest.mark.asyncio
async def test_queue_enqueue_and_wait_resolves_on_decide() -> None:
    queue = ApprovalQueue(store=ApprovalStore(":memory:"))
    call_id = queue.new_call_id()
    req = ApprovalRequest(
        call_id=call_id,
        plugin="bash",
        tool="run",
        args_preview="ls",
        session_key="sess_q",
        reason="reason",
    )

    waiter = asyncio.create_task(queue.enqueue_and_wait(req, timeout=2.0))
    # Wait briefly to ensure the waiter has subscribed.
    await asyncio.sleep(0.01)
    assert await queue.decide(call_id, ApprovalDecision.ALLOW) is True

    decision = await waiter
    assert decision == ApprovalDecision.ALLOW


@pytest.mark.asyncio
async def test_queue_wait_fast_path_for_already_decided() -> None:
    queue = ApprovalQueue(store=ApprovalStore(":memory:"))
    call_id = queue.new_call_id()
    req = ApprovalRequest(
        call_id=call_id,
        plugin="bash",
        tool="run",
        args_preview="ls",
        session_key="sess_fast",
        reason="reason",
    )
    await queue.enqueue(req)
    await queue.decide(call_id, ApprovalDecision.DENY)
    decision = await queue.wait(call_id, timeout=1.0)
    assert decision == ApprovalDecision.DENY


@pytest.mark.asyncio
async def test_queue_wait_timeout() -> None:
    queue = ApprovalQueue(store=ApprovalStore(":memory:"))
    call_id = queue.new_call_id()
    await queue.enqueue(
        ApprovalRequest(
            call_id=call_id,
            plugin="bash",
            tool="run",
            args_preview="ls",
            session_key="sess_to",
            reason="reason",
        )
    )
    with pytest.raises(asyncio.TimeoutError):
        await queue.wait(call_id, timeout=0.05)


@pytest.mark.asyncio
async def test_is_first_use_policy() -> None:
    queue = ApprovalQueue(store=ApprovalStore(":memory:"))
    assert await queue.is_first_use("sess_fu", "bash") is True

    call_id = queue.new_call_id()
    await queue.enqueue(
        ApprovalRequest(
            call_id=call_id,
            plugin="bash",
            tool="run",
            args_preview="ls",
            session_key="sess_fu",
            reason="reason",
        )
    )
    await queue.decide(call_id, ApprovalDecision.ALLOW)
    assert await queue.is_first_use("sess_fu", "bash") is False


# ---------------------------------------------------------------------------
# W3-4 — durable queue: reason column, timeout decisions, decided() query,
# file-backed persistence + self-migration.
# ---------------------------------------------------------------------------


def _req(
    call_id: str = "call_w34", *, session_key: str = "acme::s1"
) -> ApprovalRequest:
    return ApprovalRequest(
        call_id=call_id,
        plugin="github",
        tool="create_issue",
        args_preview='{"title": "hi"}',
        session_key=session_key,
        reason="permission rule requires approval",
    )


@pytest.mark.asyncio
async def test_decide_persists_decision_reason() -> None:
    store = ApprovalStore(":memory:")
    await store.insert(_req())
    assert await store.decide(
        "call_w34", ApprovalDecision.DENY, reason="not on my watch"
    )
    rec = await store.get("call_w34")
    assert rec is not None
    assert rec.decision is ApprovalDecision.DENY
    assert rec.decision_reason == "not on my watch"


@pytest.mark.asyncio
async def test_timeout_is_a_distinct_decision() -> None:
    """`decision` distinguishes a human deny from a timeout (W3-4 AC3)."""
    store = ApprovalStore(":memory:")
    await store.insert(_req("call_t"))
    await store.insert(_req("call_d"))
    await store.decide("call_t", ApprovalDecision.TIMEOUT, reason="nobody answered")
    await store.decide("call_d", ApprovalDecision.DENY)
    t = await store.get("call_t")
    d = await store.get("call_d")
    assert t is not None and t.decision is ApprovalDecision.TIMEOUT
    assert d is not None and d.decision is ApprovalDecision.DENY
    assert t.decision.value != d.decision.value
    # A timeout can never overwrite a real decision (decide only touches
    # rows whose decision is still NULL).
    assert not await store.decide("call_d", ApprovalDecision.TIMEOUT)


@pytest.mark.asyncio
async def test_decided_query_lists_newest_first() -> None:
    store = ApprovalStore(":memory:")
    await store.insert(_req("call_1"))
    await store.insert(_req("call_2"))
    await store.decide("call_1", ApprovalDecision.ALLOW)
    await store.decide("call_2", ApprovalDecision.DENY, reason="nope")
    decided = await store.decided()
    assert [r.call_id for r in decided] == ["call_2", "call_1"]
    assert decided[0].decision_reason == "nope"
    assert (await store.pending()) == []


@pytest.mark.asyncio
async def test_file_backed_store_survives_restart(tmp_path) -> None:
    """W3-4 AC2: pending rows survive a gateway restart (fresh store on
    the same path sees them)."""
    path = tmp_path / "authz" / "approvals.sqlite3"
    store = ApprovalStore(path)
    await store.insert(_req("call_restart"))

    reborn = ApprovalStore(path)
    pending = await reborn.pending()
    assert [r.call_id for r in pending] == ["call_restart"]


@pytest.mark.asyncio
async def test_legacy_table_is_migrated_in_place(tmp_path) -> None:
    """A DB created before the ``decision_reason`` column self-migrates."""
    import sqlite3

    path = tmp_path / "approvals.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE pending_approvals (
            call_id        TEXT PRIMARY KEY,
            plugin         TEXT NOT NULL,
            tool           TEXT NOT NULL,
            args_preview   TEXT NOT NULL,
            session_key    TEXT NOT NULL,
            reason         TEXT NOT NULL,
            created_at     REAL NOT NULL,
            decision       TEXT,
            decided_at     REAL
        );
        INSERT INTO pending_approvals VALUES
            ('call_old', 'p', 't', '{}', 's', 'r', 1.0, NULL, NULL);
        """
    )
    conn.commit()
    conn.close()

    store = ApprovalStore(path)
    pending = await store.pending()
    assert [r.call_id for r in pending] == ["call_old"]
    assert pending[0].decision_reason is None
    assert await store.decide(
        "call_old", ApprovalDecision.ALLOW, reason="migrated fine"
    )
    rec = await store.get("call_old")
    assert rec is not None and rec.decision_reason == "migrated fine"


def test_default_path_is_a_real_file_under_data_dir(tmp_path, monkeypatch) -> None:
    """The default store path is durable (no more ``:memory:`` default)."""
    from corlinman_providers.plugins.approval import default_approvals_db_path

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    assert default_approvals_db_path() == tmp_path / "authz" / "approvals.sqlite3"
    store = ApprovalStore()
    assert store.path == str(tmp_path / "authz" / "approvals.sqlite3")


@pytest.mark.asyncio
async def test_composite_key_disambiguates_call_id_collisions(tmp_path) -> None:
    """W3-4 review fix: rows key on (session_key, call_id) — index-style
    provider ids (``call_0``) parked by two concurrent streams must not
    clobber each other, and a scoped decide touches only its own row."""
    store = ApprovalStore(tmp_path / "authz" / "approvals.sqlite3")
    await store.insert(_req("call_0", session_key="t::a"))
    await store.insert(_req("call_0", session_key="t::b"))

    both = await store.pending_for_call("call_0")
    assert {r.session_key for r in both} == {"t::a", "t::b"}

    assert await store.decide(
        "call_0", ApprovalDecision.ALLOW, session_key="t::b"
    )
    remaining = await store.pending_for_call("call_0")
    assert [r.session_key for r in remaining] == ["t::a"]


@pytest.mark.asyncio
async def test_reconcile_orphaned_sweeps_only_pending(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "authz" / "approvals.sqlite3")
    await store.insert(_req("call_a", session_key="t::a"))
    await store.insert(_req("call_b", session_key="t::b"))
    await store.decide("call_b", ApprovalDecision.ALLOW, session_key="t::b")

    swept = await store.reconcile_orphaned(reason="gateway restart")
    assert swept == 1
    a = await store.get("call_a")
    b = await store.get("call_b")
    assert a is not None and a.decision is ApprovalDecision.TIMEOUT
    assert b is not None and b.decision is ApprovalDecision.ALLOW  # untouched
