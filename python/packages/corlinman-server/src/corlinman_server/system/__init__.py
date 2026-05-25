"""``corlinman_server.system`` — process-wide system metadata + update-checker.

W1.1 of ``docs/PLAN_AUTO_UPDATE.md``. Houses the GitHub-releases update
checker, its persisted-cache schema, and the runtime-config dataclass
the gateway lifecycle reads from ``[system.update_check]``.

Scheduler wiring and admin UI both consume the same :class:`UpdateChecker`
instance via :class:`~corlinman_server.gateway.routes_admin_b.state.AdminState`
(``admin_b_state.update_checker``). The checker itself is transport-only;
it does not register tools or cron jobs — that is W2.2's surface.
"""

from __future__ import annotations

from corlinman_server.system.update_checker import (
    SystemUpdateCheckConfig,
    UpdateChecker,
    UpdateStatus,
)

__all__ = [
    "SystemUpdateCheckConfig",
    "UpdateChecker",
    "UpdateStatus",
]
