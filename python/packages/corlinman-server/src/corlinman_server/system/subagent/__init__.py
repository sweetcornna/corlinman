"""``corlinman_server.system.subagent`` — background subagent dispatch.

W1.3 of ``docs/PLAN_MULTI_AGENT.md``. Owns the persistent
:class:`SubagentTaskStore` and the :class:`AsyncSubagentDispatcher` that
schedules ``subagent_spawn`` calls with ``run_in_background=true`` as
asyncio tasks (so the parent's tool dispatch returns immediately).

Wiring contract
---------------

* The gateway lifecycle constructs one :class:`SubagentTaskStore`
  against ``$DATA_DIR/.subagent-state.json`` (cluster-friendly atomic
  JSON persistence; identical shape to the upgrader state).
* A single :class:`AsyncSubagentDispatcher` is published onto
  :class:`corlinman_server.gateway.routes_admin_b.state.AdminState`
  (``subagent_store`` + ``subagent_dispatcher``) so the
  ``/admin/subagents`` routes resolve it via :func:`get_admin_state`.
* The tool wrapper (:mod:`corlinman_agent.subagent.tool_wrapper`) reads
  the dispatcher off a per-call context object when ``run_in_background``
  is true; the dispatcher otherwise lies dormant for synchronous calls
  (default behaviour — W1.1's job to thread the flag through).
"""

from __future__ import annotations

from pathlib import Path

from corlinman_server.system.subagent.dispatcher import (
    AsyncSubagentDispatcher,
    DispatchOutcome,
    RunChildFactory,
    TenantQuotaExceeded,
)
from corlinman_server.system.subagent.store import (
    SubagentRequest,
    SubagentState,
    SubagentStatus,
    SubagentTaskStore,
)

__all__ = [
    "AsyncSubagentDispatcher",
    "DispatchOutcome",
    "RunChildFactory",
    "SubagentRequest",
    "SubagentState",
    "SubagentStatus",
    "SubagentTaskStore",
    "TenantQuotaExceeded",
    "default_persist_path",
]


def default_persist_path(data_dir: Path) -> Path:
    """Conventional location of the persisted subagent state JSON.

    ``$DATA_DIR/.subagent-state.json`` — sits next to
    ``.upgrade-state.json`` / ``.update_check.json`` so every
    system-level cache + audit file clusters under one ``ls`` prefix.
    """
    return data_dir / ".subagent-state.json"
