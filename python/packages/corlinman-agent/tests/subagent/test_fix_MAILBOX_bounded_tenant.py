"""Repro + acceptance for the MAILBOX audit item.

Audit (2026-05-31, LANE-SM): subagent mailbox queues are unbounded and
process-global (no tenant scoping).  A flooding sender can grow a queue
without bound, and same-name ``agent_id``s collide across tenants.

These tests assert the fix:
* mailbox queues are bounded (excess handled per documented policy);
* a tenant prefix isolates same-name agent_ids across tenants when
  provided, falling back to a shared default when not.
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_agent.subagent.mailbox import (
    AGENT_MAILBOXES,
    DEFAULT_MAILBOX_MAXSIZE,
    clear_mailbox,
    get_or_create_mailbox,
    recv_from_mailbox,
    send_to_agent,
)


@pytest.mark.asyncio
async def test_queue_is_bounded_under_flood() -> None:
    """Flooding a single mailbox must not grow the queue without bound."""
    aid = "fix-mailbox-flood-target"
    clear_mailbox(aid)

    # Send far more than the cap; today this would grow unboundedly.
    flood = DEFAULT_MAILBOX_MAXSIZE + 50
    for i in range(flood):
        await send_to_agent(
            from_agent_id="flooder", to_agent_id=aid, message=str(i)
        )

    queue = get_or_create_mailbox(aid)
    assert queue.qsize() <= DEFAULT_MAILBOX_MAXSIZE, (
        f"queue grew to {queue.qsize()}, expected <= {DEFAULT_MAILBOX_MAXSIZE}"
    )
    clear_mailbox(aid)


@pytest.mark.asyncio
async def test_send_never_blocks_when_full() -> None:
    """A full mailbox must not block the sender (drop policy, not backpressure)."""
    aid = "fix-mailbox-noblock"
    clear_mailbox(aid)
    for i in range(DEFAULT_MAILBOX_MAXSIZE + 10):
        # Each send must complete near-instantly; if it blocked we'd hang.
        await asyncio.wait_for(
            send_to_agent(from_agent_id="f", to_agent_id=aid, message=str(i)),
            timeout=1.0,
        )
    clear_mailbox(aid)


@pytest.mark.asyncio
async def test_tenant_prefix_isolates_same_agent_id() -> None:
    """Same agent_id under different tenants must not share a queue."""
    aid = "shared-name"
    clear_mailbox(aid, tenant_id="tenant-a")
    clear_mailbox(aid, tenant_id="tenant-b")

    await send_to_agent(
        from_agent_id="src",
        to_agent_id=aid,
        message="for-a",
        tenant_id="tenant-a",
    )
    await send_to_agent(
        from_agent_id="src",
        to_agent_id=aid,
        message="for-b",
        tenant_id="tenant-b",
    )

    # Tenant A must only see its own message.
    msg_a = await recv_from_mailbox(aid, tenant_id="tenant-a")
    assert msg_a is not None
    assert msg_a["message"] == "for-a"
    # And A's queue is now drained — it never saw B's message.
    assert await recv_from_mailbox(aid, tenant_id="tenant-a", timeout_secs=0) is None

    msg_b = await recv_from_mailbox(aid, tenant_id="tenant-b")
    assert msg_b is not None
    assert msg_b["message"] == "for-b"

    clear_mailbox(aid, tenant_id="tenant-a")
    clear_mailbox(aid, tenant_id="tenant-b")


@pytest.mark.asyncio
async def test_default_tenant_fallback_shares_queue() -> None:
    """No tenant given on both sides => shared default namespace (back-compat)."""
    aid = "fix-mailbox-default-fallback"
    clear_mailbox(aid)
    await send_to_agent(from_agent_id="src", to_agent_id=aid, message="hi")
    msg = await recv_from_mailbox(aid)
    assert msg is not None
    assert msg["message"] == "hi"
    clear_mailbox(aid)


def test_tenant_keys_do_not_leak_as_bare() -> None:
    """A tenant-scoped mailbox must not be reachable under the bare agent_id."""
    aid = "fix-mailbox-keycheck"
    clear_mailbox(aid)
    clear_mailbox(aid, tenant_id="t1")
    get_or_create_mailbox(aid, tenant_id="t1")
    # The bare agent_id must NOT appear — only the namespaced key exists.
    assert aid not in AGENT_MAILBOXES
    clear_mailbox(aid, tenant_id="t1")


def test_default_tenant_key_is_bare_agent_id() -> None:
    """Back-compat: the default namespace keys on the bare agent_id."""
    aid = "fix-mailbox-defaultkey"
    clear_mailbox(aid)
    get_or_create_mailbox(aid)
    assert aid in AGENT_MAILBOXES
    clear_mailbox(aid)
