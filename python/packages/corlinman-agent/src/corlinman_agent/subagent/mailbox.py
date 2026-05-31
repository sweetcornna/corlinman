"""In-process agent mailbox — send/receive between running agents.

A single-process dict of per-agent :class:`asyncio.Queue` instances.
Each agent identifies itself by ``agent_id`` (the mangled
``ParentContext.parent_agent_id`` string that is already unique within
a tenant for the lifetime of an agent session).

Lifecycle
---------
* Mailboxes are created lazily on first send or first recv.
* Queues are unbounded (no ``maxsize``).  The coordinator pattern is
  expected to be low-volume (a few inter-agent messages per task).  If
  high-throughput usage becomes a concern, cap the queue at that time.
* There is intentionally no cleanup hook.  Stale queues are small
  (empty queue ≈ 56 bytes on CPython) and the dict is module-level, so
  they live for the process lifetime.  A future operator API that wants
  to flush a mailbox can call :func:`clear_mailbox` directly.

Thread safety
-------------
Reads/writes to ``_MAILBOXES`` happen only on the asyncio event loop
(all callers are ``async``), so no extra lock is required.
"""

from __future__ import annotations

import asyncio
import time
from typing import TypedDict

__all__ = [
    "AGENT_MAILBOXES",
    "AgentMessage",
    "clear_mailbox",
    "get_or_create_mailbox",
    "recv_from_mailbox",
    "send_to_agent",
]


class AgentMessage(TypedDict):
    """One message in an agent's mailbox."""

    from_agent_id: str
    message: str
    #: ``reply_to_turn`` is an optional int the sender can set to
    #: link the message back to a specific parent turn number so
    #: the receiver can correlate context.
    reply_to_turn: int | None
    #: Monotonic timestamp (seconds, float) recorded at enqueue time.
    queued_at: float


#: Module-level registry.  Maps ``agent_id → asyncio.Queue[AgentMessage]``.
#: Never shrinks automatically — see module docstring.
AGENT_MAILBOXES: dict[str, asyncio.Queue[AgentMessage]] = {}


def get_or_create_mailbox(agent_id: str) -> asyncio.Queue[AgentMessage]:
    """Return the existing queue for *agent_id* or create a new one."""
    if agent_id not in AGENT_MAILBOXES:
        AGENT_MAILBOXES[agent_id] = asyncio.Queue()
    return AGENT_MAILBOXES[agent_id]


def clear_mailbox(agent_id: str) -> int:
    """Drain and remove the mailbox for *agent_id*.

    Returns the number of messages that were discarded.  Safe to call
    even if the mailbox never existed.
    """
    queue = AGENT_MAILBOXES.pop(agent_id, None)
    if queue is None:
        return 0
    count = 0
    while not queue.empty():
        try:
            queue.get_nowait()
            count += 1
        except asyncio.QueueEmpty:
            break
    return count


async def send_to_agent(
    *,
    from_agent_id: str,
    to_agent_id: str,
    message: str,
    reply_to_turn: int | None = None,
) -> float:
    """Enqueue *message* to *to_agent_id*'s mailbox.

    Returns the monotonic timestamp (``time.monotonic()``) at which the
    message was enqueued.  The caller surfaces this as ``queued_at`` in
    the tool result so the LLM has a relative ordering handle.

    This is a coroutine for API symmetry with :func:`recv_from_mailbox`;
    the put itself is non-blocking (queues are unbounded).
    """
    queue = get_or_create_mailbox(to_agent_id)
    queued_at = time.monotonic()
    msg: AgentMessage = {
        "from_agent_id": from_agent_id,
        "message": message,
        "reply_to_turn": reply_to_turn,
        "queued_at": queued_at,
    }
    await queue.put(msg)
    return queued_at


async def recv_from_mailbox(
    agent_id: str,
    *,
    timeout_secs: float | None = None,
) -> AgentMessage | None:
    """Dequeue the next message from *agent_id*'s mailbox.

    Parameters
    ----------
    agent_id
        The receiving agent's id.
    timeout_secs
        How long to wait for a message.  ``None`` (default) returns
        immediately with ``None`` if the queue is empty (non-blocking
        poll).  ``0`` also returns immediately.  Any positive float
        waits up to that many seconds.

    Returns
    -------
    AgentMessage | None
        The next message, or ``None`` if the queue was empty / timed
        out.
    """
    queue = get_or_create_mailbox(agent_id)

    # Non-blocking fast path.
    effective_timeout = timeout_secs if timeout_secs is not None else 0.0
    if effective_timeout <= 0:
        try:
            return queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # Blocking path with timeout.
    try:
        return await asyncio.wait_for(queue.get(), timeout=effective_timeout)
    except TimeoutError:
        return None
