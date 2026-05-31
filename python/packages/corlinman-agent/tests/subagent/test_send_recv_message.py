"""Tests for coordinator send_message / recv_message tools.

Covers:
* :func:`dispatch_send_message` — happy path, args-invalid paths.
* :func:`dispatch_recv_message` — happy path, empty mailbox, timeout path.
* :mod:`corlinman_agent.subagent.mailbox` internals:
  ``send_to_agent``, ``recv_from_mailbox``, ``clear_mailbox``,
  ``get_or_create_mailbox``.
* Tool schema shapes (``agent_send_message_tool_schema``,
  ``agent_recv_message_tool_schema``).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from corlinman_agent.subagent.api import ParentContext
from corlinman_agent.subagent.mailbox import (
    AGENT_MAILBOXES,
    clear_mailbox,
    get_or_create_mailbox,
    recv_from_mailbox,
    send_to_agent,
)
from corlinman_agent.subagent.tool_wrapper import (
    AGENT_RECV_MESSAGE_TOOL,
    AGENT_SEND_MESSAGE_TOOL,
    ARGS_INVALID_ERROR,
    agent_recv_message_tool_schema,
    agent_send_message_tool_schema,
    dispatch_recv_message,
    dispatch_send_message,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(agent_id: str = "root::coordinator::0") -> ParentContext:
    return ParentContext(
        tenant_id="tenant-test",
        parent_agent_id=agent_id,
        parent_session_key="sess-test",
    )


# ---------------------------------------------------------------------------
# mailbox module unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_creates_mailbox() -> None:
    aid = "test-agent-send-creates"
    AGENT_MAILBOXES.pop(aid, None)  # clean slate

    await send_to_agent(
        from_agent_id="sender",
        to_agent_id=aid,
        message="hello",
    )
    assert aid in AGENT_MAILBOXES
    assert AGENT_MAILBOXES[aid].qsize() == 1
    clear_mailbox(aid)


@pytest.mark.asyncio
async def test_recv_returns_message() -> None:
    aid = "test-agent-recv-basic"
    clear_mailbox(aid)

    await send_to_agent(from_agent_id="src", to_agent_id=aid, message="ping")
    msg = await recv_from_mailbox(aid)
    assert msg is not None
    assert msg["message"] == "ping"
    assert msg["from_agent_id"] == "src"
    assert msg["reply_to_turn"] is None
    clear_mailbox(aid)


@pytest.mark.asyncio
async def test_recv_returns_none_on_empty() -> None:
    aid = "test-agent-recv-empty"
    clear_mailbox(aid)

    result = await recv_from_mailbox(aid, timeout_secs=0)
    assert result is None


@pytest.mark.asyncio
async def test_recv_with_reply_to_turn() -> None:
    aid = "test-agent-recv-turn"
    clear_mailbox(aid)

    await send_to_agent(
        from_agent_id="src",
        to_agent_id=aid,
        message="context reply",
        reply_to_turn=3,
    )
    msg = await recv_from_mailbox(aid)
    assert msg is not None
    assert msg["reply_to_turn"] == 3
    clear_mailbox(aid)


@pytest.mark.asyncio
async def test_clear_mailbox_returns_count() -> None:
    aid = "test-agent-clear"
    clear_mailbox(aid)

    for i in range(5):
        await send_to_agent(from_agent_id="src", to_agent_id=aid, message=str(i))

    count = clear_mailbox(aid)
    assert count == 5
    assert aid not in AGENT_MAILBOXES


@pytest.mark.asyncio
async def test_clear_mailbox_nonexistent_returns_zero() -> None:
    assert clear_mailbox("nonexistent-agent-xyz") == 0


@pytest.mark.asyncio
async def test_recv_blocking_timeout() -> None:
    """Blocking recv with a short timeout on an empty mailbox returns None."""
    aid = "test-agent-timeout"
    clear_mailbox(aid)

    result = await recv_from_mailbox(aid, timeout_secs=0.05)
    assert result is None


@pytest.mark.asyncio
async def test_recv_blocking_gets_message() -> None:
    """Blocking recv returns message sent concurrently."""
    aid = "test-agent-blocking"
    clear_mailbox(aid)

    async def _send_later() -> None:
        await asyncio.sleep(0.02)
        await send_to_agent(from_agent_id="other", to_agent_id=aid, message="arrived")

    task = asyncio.create_task(_send_later())
    msg = await recv_from_mailbox(aid, timeout_secs=0.5)
    await task

    assert msg is not None
    assert msg["message"] == "arrived"
    clear_mailbox(aid)


def test_get_or_create_mailbox_idempotent() -> None:
    aid = "test-idem"
    AGENT_MAILBOXES.pop(aid, None)
    q1 = get_or_create_mailbox(aid)
    q2 = get_or_create_mailbox(aid)
    assert q1 is q2
    clear_mailbox(aid)


# ---------------------------------------------------------------------------
# dispatch_send_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_send_message_happy_path() -> None:
    to_id = "target-agent-ds"
    clear_mailbox(to_id)
    ctx = _ctx("sender-agent")

    result = await dispatch_send_message(
        args_json=json.dumps({"to_agent_id": to_id, "message": "hello world"}),
        parent_ctx=ctx,
    )
    envelope = json.loads(result)
    assert envelope["sent"] is True
    assert envelope["to"] == to_id
    assert isinstance(envelope["queued_at"], float)

    # Verify it really landed in the mailbox.
    msg = await recv_from_mailbox(to_id)
    assert msg is not None
    assert msg["message"] == "hello world"
    assert msg["from_agent_id"] == "sender-agent"
    clear_mailbox(to_id)


@pytest.mark.asyncio
async def test_dispatch_send_message_with_reply_to_turn() -> None:
    to_id = "target-agent-turn"
    clear_mailbox(to_id)
    ctx = _ctx()

    result = await dispatch_send_message(
        args_json=json.dumps(
            {"to_agent_id": to_id, "message": "context", "reply_to_turn": 7}
        ),
        parent_ctx=ctx,
    )
    envelope = json.loads(result)
    assert envelope["sent"] is True

    msg = await recv_from_mailbox(to_id)
    assert msg is not None
    assert msg["reply_to_turn"] == 7
    clear_mailbox(to_id)


@pytest.mark.asyncio
async def test_dispatch_send_message_missing_to_agent_id() -> None:
    ctx = _ctx()
    result = await dispatch_send_message(
        args_json=json.dumps({"message": "oops"}),
        parent_ctx=ctx,
    )
    envelope = json.loads(result)
    assert envelope["sent"] is False
    assert ARGS_INVALID_ERROR in envelope["error"]


@pytest.mark.asyncio
async def test_dispatch_send_message_empty_to_agent_id() -> None:
    ctx = _ctx()
    result = await dispatch_send_message(
        args_json=json.dumps({"to_agent_id": "", "message": "oops"}),
        parent_ctx=ctx,
    )
    envelope = json.loads(result)
    assert envelope["sent"] is False
    assert ARGS_INVALID_ERROR in envelope["error"]


@pytest.mark.asyncio
async def test_dispatch_send_message_invalid_json() -> None:
    ctx = _ctx()
    result = await dispatch_send_message(
        args_json=b"not-json",
        parent_ctx=ctx,
    )
    envelope = json.loads(result)
    assert envelope["sent"] is False
    assert ARGS_INVALID_ERROR in envelope["error"]


@pytest.mark.asyncio
async def test_dispatch_send_message_not_object() -> None:
    ctx = _ctx()
    result = await dispatch_send_message(
        args_json=json.dumps(["not", "a", "dict"]),
        parent_ctx=ctx,
    )
    envelope = json.loads(result)
    assert envelope["sent"] is False


# ---------------------------------------------------------------------------
# dispatch_recv_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_recv_message_happy_path() -> None:
    ctx = _ctx("recv-agent")
    clear_mailbox("recv-agent")

    # Pre-populate the mailbox.
    await send_to_agent(
        from_agent_id="sender",
        to_agent_id="recv-agent",
        message="received",
        reply_to_turn=2,
    )

    result = await dispatch_recv_message(args_json=json.dumps({}), parent_ctx=ctx)
    envelope = json.loads(result)
    assert envelope["message"] == "received"
    assert envelope["from_agent_id"] == "sender"
    assert envelope["reply_to_turn"] == 2
    assert isinstance(envelope["queued_at"], float)
    clear_mailbox("recv-agent")


@pytest.mark.asyncio
async def test_dispatch_recv_message_empty() -> None:
    ctx = _ctx("empty-recv-agent")
    clear_mailbox("empty-recv-agent")

    result = await dispatch_recv_message(args_json=json.dumps({}), parent_ctx=ctx)
    envelope = json.loads(result)
    assert envelope["message"] is None


@pytest.mark.asyncio
async def test_dispatch_recv_message_with_timeout() -> None:
    ctx = _ctx("timeout-recv-agent")
    clear_mailbox("timeout-recv-agent")

    result = await dispatch_recv_message(
        args_json=json.dumps({"timeout_secs": 0.05}),
        parent_ctx=ctx,
    )
    envelope = json.loads(result)
    assert envelope["message"] is None


@pytest.mark.asyncio
async def test_dispatch_recv_message_invalid_timeout() -> None:
    ctx = _ctx()
    result = await dispatch_recv_message(
        args_json=json.dumps({"timeout_secs": -1}),
        parent_ctx=ctx,
    )
    envelope = json.loads(result)
    assert envelope["message"] is None
    assert ARGS_INVALID_ERROR in envelope.get("error", "")


@pytest.mark.asyncio
async def test_dispatch_recv_message_invalid_json() -> None:
    ctx = _ctx()
    result = await dispatch_recv_message(
        args_json=b"{{bad",
        parent_ctx=ctx,
    )
    envelope = json.loads(result)
    assert envelope["message"] is None
    assert ARGS_INVALID_ERROR in envelope.get("error", "")


# ---------------------------------------------------------------------------
# Tool schema shape tests
# ---------------------------------------------------------------------------


def test_send_schema_shape() -> None:
    schema = agent_send_message_tool_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == AGENT_SEND_MESSAGE_TOOL
    props = fn["parameters"]["properties"]
    assert "to_agent_id" in props
    assert "message" in props
    assert "reply_to_turn" in props
    assert fn["parameters"]["required"] == ["to_agent_id", "message"]
    assert fn["parameters"]["additionalProperties"] is False


def test_recv_schema_shape() -> None:
    schema = agent_recv_message_tool_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == AGENT_RECV_MESSAGE_TOOL
    props = fn["parameters"]["properties"]
    assert "timeout_secs" in props
    assert fn["parameters"]["required"] == []
    assert fn["parameters"]["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Round-trip: send → recv
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_send_recv() -> None:
    """End-to-end: one agent sends, the other receives via dispatch funcs."""
    sender_ctx = _ctx("agent-alpha")
    receiver_ctx = _ctx("agent-beta")
    clear_mailbox("agent-alpha")
    clear_mailbox("agent-beta")

    send_result = await dispatch_send_message(
        args_json=json.dumps(
            {"to_agent_id": "agent-beta", "message": "coordinate now"}
        ),
        parent_ctx=sender_ctx,
    )
    assert json.loads(send_result)["sent"] is True

    recv_result = await dispatch_recv_message(
        args_json=json.dumps({}),
        parent_ctx=receiver_ctx,
    )
    envelope = json.loads(recv_result)
    assert envelope["message"] == "coordinate now"
    assert envelope["from_agent_id"] == "agent-alpha"

    clear_mailbox("agent-alpha")
    clear_mailbox("agent-beta")
