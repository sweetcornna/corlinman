"""``gateway.observability`` — task-observability fan-out plumbing.

Exposes the gateway-side :class:`JournalBackedEmitter` that conforms to
the :class:`corlinman_agent.events.EventEmitter` protocol. Every
:class:`corlinman_agent.events.EventEnvelope` produced by the reasoning
loop / runner pool / subagent supervisor is teed through one shared
emitter into:

* the per-turn :class:`~corlinman_server.agent_journal.AgentJournal`
  (durable storage; powers SSE replay);
* in-process per-session SSE subscribers (live observers in the admin
  UI).

The W1.3 admin routes (``/admin/sessions/{key}/events/live`` +
``/admin/sessions/{key}/turns/{turn_id}/events``) consume the same
envelopes — the fan-out path lives here so the route handlers stay slim.
"""

from __future__ import annotations

from corlinman_server.gateway.observability.emitter import (
    BubbleEmitter,
    JournalBackedEmitter,
)
from corlinman_server.gateway.observability.live_subagents import (
    LiveSubagentRegistry,
    LiveSubagentRow,
    run_journal_subagent_tail,
)

__all__ = [
    "BubbleEmitter",
    "JournalBackedEmitter",
    "LiveSubagentRegistry",
    "LiveSubagentRow",
    "run_journal_subagent_tail",
]
