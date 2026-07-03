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

    discovered: dict[str, int] = {}
    """``event → handler count`` for file-discovered HOOK.yaml hooks."""

    declarative: list[dict[str, Any]] = []
    """Declarative matcher-group summaries (``[hooks.declarative]``):
    event / matcher / if / kinds / async per group."""

    warnings: list[str] = []
    """Parse warnings collected from the declarative config block."""

    live_events: list[str] = []
    """Events that currently have a production emit site — a declarative
    group on any other event is accepted but will not fire yet."""


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

        from corlinman_server.hooks_live import LIVE_HOOK_EVENTS

        if hook_runner is None:
            return HooksResponse(
                status="hook_runner_unavailable",
                supported_events=[],
                registered={},
                live_events=sorted(LIVE_HOOK_EVENTS),
            )

        # ``HookRunner``'s introspection surface is pure / no-I/O so no
        # await is needed. Every read degrades independently — a partial
        # answer beats a 500 on an ops page.
        def _read(attr: str, default: Any) -> Any:
            try:
                value = getattr(hook_runner, attr)
                return value() if callable(value) else value
            except Exception:  # noqa: BLE001 — degrade cleanly
                return default

        return HooksResponse(
            status="ok",
            supported_events=list(_read("supported_events", [])),
            registered=dict(_read("registered", {})),
            discovered=dict(_read("discovered_events", {})),
            declarative=list(_read("declarative_groups", [])),
            warnings=list(_read("declarative_warnings", [])),
            live_events=sorted(LIVE_HOOK_EVENTS),
        )

    return rt
