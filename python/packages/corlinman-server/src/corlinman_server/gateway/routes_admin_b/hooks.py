"""``/admin/hooks`` — discoverable shell-command hooks inspection.

Routes:

* ``GET /admin/hooks``  — list all registered hooks and the canonical
  supported event names.

This route is intentionally read-only: hooks are configured via the agent
config file (the ``[hooks]`` section) or by supplying a
:class:`~corlinman_hooks.runner.HookRunner` instance to the servicer at
boot time. The admin route surface exposes what is currently registered so
operators can verify their hook configuration without tailing logs.

State requirements:

* ``state.extras["hook_runner"]`` (optional) — a
  :class:`~corlinman_hooks.runner.HookRunner` instance. When absent the
  route returns an empty registry with ``503 hook_runner_unavailable``
  in the ``status`` field (not a 503 HTTP status — the page stays
  renderable).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.state import (
    get_admin_state,
    require_admin,
)

# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class HooksResponse(BaseModel):
    """Wire shape for ``GET /admin/hooks``."""

    status: str
    """``"ok"`` when a runner is configured, ``"hook_runner_unavailable"``
    otherwise."""

    supported_events: list[str]
    """Canonical event names the hook protocol understands.
    Always populated regardless of ``status``."""

    registered: dict[str, str]
    """Mapping of ``event_key → shell_command``.
    Empty when no runner is configured or no hooks are registered."""


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    rt = APIRouter()

    @rt.get(
        "/admin/hooks",
        response_model=HooksResponse,
        summary="List registered shell-command hooks",
    )
    async def get_hooks(
        _: None = Depends(require_admin),
    ) -> Any:
        """Return the registered hook commands and supported event names.

        The response is always ``200 OK``; ``status`` distinguishes
        whether a ``HookRunner`` is wired up or not.
        """
        state = get_admin_state()
        hook_runner = state.extras.get("hook_runner") if state.extras else None

        _SUPPORTED = ["pre_tool", "post_tool", "notification"]

        if hook_runner is None:
            return HooksResponse(
                status="hook_runner_unavailable",
                supported_events=_SUPPORTED,
                registered={},
            )

        # ``HookRunner.registered`` and ``supported_events()`` are both
        # pure / no-I/O so no await is needed.
        try:
            registered = hook_runner.registered
        except Exception:  # noqa: BLE001 — degrade cleanly
            registered = {}
        try:
            supported = list(hook_runner.supported_events())
        except Exception:  # noqa: BLE001
            supported = _SUPPORTED

        return HooksResponse(
            status="ok",
            supported_events=supported,
            registered=registered,
        )

    return rt
