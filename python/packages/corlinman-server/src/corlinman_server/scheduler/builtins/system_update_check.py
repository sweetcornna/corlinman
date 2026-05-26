"""``system.update_check`` builtin — W2.2 of ``docs/PLAN_AUTO_UPDATE.md``.

Wraps :meth:`corlinman_server.system.UpdateChecker.poll` (with
``force=False`` so it honours the checker's own TTL). The admin UI's
manual "Check now" path bypasses this registry entirely and calls
``poll(force=True)`` through ``POST /admin/system/check-updates`` —
both paths share the :class:`UpdateChecker` instance owned by
``AppState`` / ``AdminState.update_checker``.
"""

from __future__ import annotations

import logging
from typing import Any

from corlinman_server.scheduler.builtins.registry import (
    BuiltinContext,
    register_builtin,
)

_logger = logging.getLogger("corlinman_server.scheduler.builtins.system_update_check")


__all__ = [
    "_resolve_update_checker",
    "_system_update_check_action",
]


async def _system_update_check_action(context: BuiltinContext) -> dict[str, Any]:
    """Scheduler builtin that triggers a non-forced ``UpdateChecker.poll()``.

    Reads the live :class:`~corlinman_server.system.UpdateChecker` from
    the FastAPI ``app.state`` via ``context.app_state``. When the
    checker is absent (``[system.update_check] enabled = false`` or the
    data dir wasn't writable at boot) the action becomes a no-op with
    ``ok=False, reason="checker_unavailable"`` so the scheduler history
    surfaces *why* the periodic poll didn't fire rather than logging a
    stack trace.

    Returns the small report shape the admin scheduler history reads:

    * ``ok`` — bool, true iff the poll completed without raising
    * ``current`` — installed semver from :class:`UpdateStatus.current`
    * ``latest`` — newest tag GitHub reports (``None`` on degraded fetch)
    * ``available`` — flag the UI bubble keys off
    * ``last_checked_at`` — unix-ms of the last successful refresh

    Calling ``poll(force=False)`` honours the checker's own TTL — if
    the last refresh was under :attr:`SystemUpdateCheckConfig.interval_hours`
    ago we short-circuit before hitting GitHub, which keeps the
    builtin cheap even when the cron fires more often than the
    configured interval (e.g. when an operator pins the cron at "every
    minute" for testing).
    """
    checker = _resolve_update_checker(context)
    if checker is None:
        return {"ok": False, "reason": "checker_unavailable"}
    try:
        status = await checker.poll(force=False)
    except Exception as exc:  # noqa: BLE001 - never raise out of a builtin
        _logger.warning(
            "scheduler.builtin.system_update_check.poll_failed",
            extra={"error": repr(exc)},
        )
        return {"ok": False, "reason": f"poll_failed: {exc!r}"}
    return {
        "ok": True,
        "current": getattr(status, "current", None),
        "latest": getattr(status, "latest", None),
        "available": bool(getattr(status, "available", False)),
        "last_checked_at": getattr(status, "last_checked_at", None),
    }


def _resolve_update_checker(context: BuiltinContext) -> Any | None:
    """Walk the context surfaces to find the live :class:`UpdateChecker`.

    Three reach-in points, in priority order:

    1. ``app_state.corlinman_update_checker`` — the canonical attachment
       set by the gateway lifespan (see ``entrypoint.py`` W1.1 block).
    2. ``admin_state.update_checker`` — the admin_b mirror, kept in
       sync with the canonical handle but useful for tests that only
       stub the admin surface.
    3. ``app_state.update_checker`` — fallback for the degraded boot
       where W1.1's wiring lands the handle straight on the AppState
       bundle instead of FastAPI's ``app.state``.

    Returns ``None`` when every probe misses so the builtin can emit
    its typed ``checker_unavailable`` envelope rather than 500ing.
    """
    if context.app_state is not None:
        checker = getattr(context.app_state, "corlinman_update_checker", None)
        if checker is not None:
            return checker
        checker = getattr(context.app_state, "update_checker", None)
        if checker is not None:
            return checker
    if context.admin_state is not None:
        return getattr(context.admin_state, "update_checker", None)
    return None


# Register the W2.2 builtin at import time. Sibling modules call
# :func:`register_builtin` to add more without re-exporting the dict.
register_builtin("system.update_check", _system_update_check_action)
