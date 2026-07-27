"""Unified-gate approval hook for MCP ``sampling/createMessage`` (W3-2).

``[mcp.sampling].mode = "ask"`` routes each inbound sampling request
through an approval hook — but the production construction site
(``lifecycle/entrypoint.py``) never passed one, so ``ask`` mode denied
every request unconditionally. This module normalizes the hook onto the
unified authorization gate under the canonical ``sampling:<server>`` key
(plan §1.2.2), closing that gap:

* an explicit ``allow`` / ``log`` rule for ``sampling:<server>`` (or a
  matching glob like ``sampling:*``) approves the request;
* a remembered grant on an ``ask`` rule approves it (the gate folds
  grants in);
* ``bypass`` mode approves everything (operator opted out of gating);
* everything else — including "no rule matched" — DENIES. ``ask`` mode
  is an explicit opt-in to per-request policy, so the gate's permissive
  ``default_action`` must not silently auto-approve it; the historical
  fail-closed polarity (C1) is preserved.

The request's model rides along as the argument surface, so arg-scoped
rules like ``sampling:trusted-server(claude-*)`` work.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

__all__ = ["build_sampling_approval_hook"]


def build_sampling_approval_hook(
    gate: Any | None = None,
) -> Callable[[str, Any], Awaitable[bool]]:
    """Build the ``(server_name, request) -> bool`` approval hook.

    ``gate`` is injectable for tests; production uses a fresh
    :class:`~corlinman_agent.authz.AuthzGate` (call-time evaluation — the
    live ``[permissions]`` config applies without restart). Never raises:
    any internal error denies (fail-closed).
    """
    from corlinman_agent.authz import AuthzGate, PermissionMode, Subject

    authz = gate if gate is not None else AuthzGate()

    async def _hook(server_name: str, request: Any) -> bool:
        key = f"sampling:{server_name}"
        try:
            model = str(getattr(request, "model", "") or "")
            action, rule_index = authz.resolve_external(
                (key,), Subject(), {"model": model} if model else None
            )
            explicit = rule_index is not None or authz.mode is PermissionMode.BYPASS
            approved = bool(explicit and action in ("allow", "log"))
            logger.info(
                "gateway.mcp.sampling_authz",
                server=server_name,
                key=key,
                action=action,
                approved=approved,
            )
            return approved
        except Exception as exc:  # noqa: BLE001 — approval failure denies
            logger.warning(
                "gateway.mcp.sampling_authz_failed",
                server=server_name,
                error=str(exc),
            )
            return False

    return _hook
