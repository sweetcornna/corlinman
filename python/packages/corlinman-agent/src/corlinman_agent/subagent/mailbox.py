"""In-process agent mailbox — send/receive between running agents.

A single-process dict of per-agent :class:`asyncio.Queue` instances.
Each agent identifies itself by ``agent_id`` (the mangled
``ParentContext.parent_agent_id`` string that is already unique within
a tenant for the lifetime of an agent session).

Lifecycle
---------
* Mailboxes are created lazily on first send or first recv.
* Queues are **bounded** at :data:`DEFAULT_MAILBOX_MAXSIZE` slots
  (override via the ``CORLINMAN_MAILBOX_MAXSIZE`` env var).  The
  coordinator pattern is expected to be low-volume (a few inter-agent
  messages per task), but a misbehaving / flooding sender must not be
  able to grow a queue without bound.
* **Overflow policy = drop-oldest.**  When a mailbox is full, the
  oldest queued message is evicted to make room for the new one, and a
  warning is logged.  Drop-oldest (rather than reject-newest) keeps
  :func:`send_to_agent` non-blocking and ensures the receiver always
  sees the most recent coordination signals.  It also means a flooder
  can drop a victim's older messages, but the queue can never exceed
  the cap, so the blast radius is bounded memory + lost-message
  warnings rather than an OOM.
* There is intentionally no cleanup hook.  Stale queues are small
  (empty queue ≈ 56 bytes on CPython) and the dict is module-level, so
  they live for the process lifetime.  A future operator API that wants
  to flush a mailbox can call :func:`clear_mailbox` directly.

Tenant scoping
--------------
Mailboxes are keyed internally by a namespaced *string* so that two
tenants reusing the same ``agent_id`` never share a queue.  For the
default namespace (no ``tenant_id`` supplied) the key is the bare
``agent_id`` — preserving the historical single-namespace behaviour and
the old registry-key shape — while a supplied ``tenant_id`` produces a
``"<tenant_id>\x00<agent_id>"`` key.  All public functions accept an
optional ``tenant_id`` keyword.

Thread safety
-------------
Reads/writes to ``AGENT_MAILBOXES`` happen only on the asyncio event
loop (all callers are ``async``), so no extra lock is required.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TypedDict

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_MAILBOXES",
    "DEFAULT_MAILBOX_MAXSIZE",
    "AgentMessage",
    "clear_mailbox",
    "get_or_create_mailbox",
    "recv_from_mailbox",
    "send_to_agent",
]


def _resolve_maxsize() -> int:
    """Read the mailbox cap from the env, clamped to a sane positive int.

    ``CORLINMAN_MAILBOX_MAXSIZE`` overrides the built-in default of
    1024.  A value <= 0 or unparseable falls back to the default (an
    unbounded queue is never allowed here — that is the bug we are
    fixing).
    """
    raw = os.environ.get("CORLINMAN_MAILBOX_MAXSIZE")
    if raw is None:
        return 1024
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1024
    return value if value > 0 else 1024


#: Bounded capacity of each mailbox queue.  Resolved once at import time
#: from ``CORLINMAN_MAILBOX_MAXSIZE`` (default 1024).
DEFAULT_MAILBOX_MAXSIZE: int = _resolve_maxsize()

#: Namespace label used in logs / overflow warnings when a caller does
#: not supply a ``tenant_id``.
_DEFAULT_TENANT = "__default__"

#: Separator between tenant and agent in a namespaced registry key.  NUL
#: cannot appear in a tenant_id or agent_id, so the key is unambiguous.
_KEY_SEP = "\x00"


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


#: Module-level registry.  Maps a namespaced *string* key →
#: ``asyncio.Queue``.  For the default namespace the key is the bare
#: ``agent_id``; a supplied ``tenant_id`` prefixes it.  Same-name
#: agent_ids therefore stay isolated across tenants.  Never shrinks
#: automatically — see module docstring.
AGENT_MAILBOXES: dict[str, asyncio.Queue[AgentMessage]] = {}


def _key(agent_id: str, tenant_id: str | None) -> str:
    """Build the namespaced registry key for *agent_id* / *tenant_id*.

    The default namespace (no ``tenant_id``) maps to the bare
    ``agent_id`` so the registry-key shape stays backward compatible.
    """
    if not tenant_id:
        return agent_id
    return f"{tenant_id}{_KEY_SEP}{agent_id}"


def get_or_create_mailbox(
    agent_id: str, *, tenant_id: str | None = None
) -> asyncio.Queue[AgentMessage]:
    """Return the existing queue for *agent_id* or create a bounded one.

    Queues are created with ``maxsize=DEFAULT_MAILBOX_MAXSIZE`` so they
    can never grow without bound.  Mailboxes are isolated per
    ``tenant_id``; omitting it uses the shared default namespace.
    """
    key = _key(agent_id, tenant_id)
    queue = AGENT_MAILBOXES.get(key)
    if queue is None:
        queue = asyncio.Queue(maxsize=DEFAULT_MAILBOX_MAXSIZE)
        AGENT_MAILBOXES[key] = queue
    return queue


def clear_mailbox(agent_id: str, *, tenant_id: str | None = None) -> int:
    """Drain and remove the mailbox for *agent_id*.

    Returns the number of messages that were discarded.  Safe to call
    even if the mailbox never existed.
    """
    queue = AGENT_MAILBOXES.pop(_key(agent_id, tenant_id), None)
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
    tenant_id: str | None = None,
) -> float:
    """Enqueue *message* to *to_agent_id*'s mailbox.

    Returns the monotonic timestamp (``time.monotonic()``) at which the
    message was enqueued.  The caller surfaces this as ``queued_at`` in
    the tool result so the LLM has a relative ordering handle.

    Mailboxes are bounded at :data:`DEFAULT_MAILBOX_MAXSIZE`.  If the
    target mailbox is full, the **oldest** queued message is dropped to
    make room (and a warning logged); the put itself therefore never
    blocks.  Pass ``tenant_id`` to isolate the target across tenants;
    omitting it uses the shared default namespace.
    """
    queue = get_or_create_mailbox(to_agent_id, tenant_id=tenant_id)
    queued_at = time.monotonic()
    msg: AgentMessage = {
        "from_agent_id": from_agent_id,
        "message": message,
        "reply_to_turn": reply_to_turn,
        "queued_at": queued_at,
    }
    try:
        queue.put_nowait(msg)
    except asyncio.QueueFull:
        # Drop-oldest: evict one message, then retry.  The eviction +
        # retry happen on the event loop with no awaits in between, so
        # no other coroutine can refill the slot we just freed.
        dropped: AgentMessage | None = None
        try:
            dropped = queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - race-free here
            dropped = None
        logger.warning(
            "subagent.mailbox.overflow_drop_oldest "
            "to_agent_id=%s tenant_id=%s maxsize=%d dropped_from=%s",
            to_agent_id,
            tenant_id or _DEFAULT_TENANT,
            DEFAULT_MAILBOX_MAXSIZE,
            dropped["from_agent_id"] if dropped else "<none>",
        )
        queue.put_nowait(msg)
    return queued_at


async def recv_from_mailbox(
    agent_id: str,
    *,
    timeout_secs: float | None = None,
    tenant_id: str | None = None,
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
    tenant_id
        Optional tenant namespace; omitting it uses the shared default
        namespace.  Must match the ``tenant_id`` used by the sender.

    Returns
    -------
    AgentMessage | None
        The next message, or ``None`` if the queue was empty / timed
        out.
    """
    queue = get_or_create_mailbox(agent_id, tenant_id=tenant_id)

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
