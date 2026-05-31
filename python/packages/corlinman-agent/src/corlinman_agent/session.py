"""Session state — short-lived per-request context shared by the reasoning loop.

Responsibility: bundle ``session_key`` (from ChannelBinding), trace context,
conversation messages, pending tool calls, and cancellation token so every
helper in :mod:`corlinman_agent` takes a single ``Session`` instead of a
growing parameter list.

The cancel token is a plain :class:`asyncio.Event` (the same primitive the
reasoning loop already uses), so :func:`corlinman_agent.cancel.combine` can
fan it in with other scopes. ``trace`` / ``messages`` / ``pending_tools`` are
mutable handles owned by the caller for the lifetime of the request.

Implemented as a ``@dataclass(slots=True)`` rather than a pydantic model: the
cancel token and trace context are arbitrary runtime objects (no validation
to do) and the surrounding modules — ``reasoning_loop.ChatStart`` etc. — use
plain slotted dataclasses, so this matches the local idiom without pulling
pydantic's ``arbitrary_types_allowed`` escape hatch into a hot path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class Session:
    """Per-conversation handle bundle threaded through the reasoning loop.

    Groups the handles that previously travelled as a growing positional
    argument list:

    * ``session_key`` — stable conversation key derived from the
      ``ChannelBinding`` (channel + peer); ``""`` for anonymous/ephemeral runs.
    * ``trace`` — opaque trace/correlation context (trace id, span, structlog
      bound logger, …) attached by the caller; ``None`` when untraced.
    * ``messages`` — the live conversation history (OpenAI-shape message
      dicts) mutated in place across rounds.
    * ``pending_tools`` — tool-call descriptors observed this round and not yet
      resolved; drained as results land.
    * ``cancel`` — the cancellation token (:class:`asyncio.Event`); fire it to
      collapse every outstanding I/O for this conversation. Defaults to a fresh
      unset event so a bare ``Session(...)`` is immediately usable.

    The container is intentionally dumb: it owns no behaviour beyond
    :meth:`is_cancelled`, so callers can read/replace fields freely.
    """

    session_key: str = ""
    trace: Any | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_tools: list[dict[str, Any]] = field(default_factory=list)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    def is_cancelled(self) -> bool:
        """Return ``True`` when the cancel token has been fired."""
        return self.cancel.is_set()
