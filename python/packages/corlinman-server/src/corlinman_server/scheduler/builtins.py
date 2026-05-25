"""Scheduler builtin actions — process-local callables wired by name.

W2.2 of ``docs/PLAN_AUTO_UPDATE.md`` §2 Wave 2. The scheduler's
:class:`~corlinman_server.scheduler.JobAction` carrier supports a
``"run_tool"`` discriminant whose end-to-end execution path is still
"unsupported_action" in :func:`corlinman_server.scheduler.dispatch` (the
Rust crate at the same revision is in the same state). Until that path
lands, the gateway needs a way to invoke process-local maintenance jobs
(currently: the GitHub-releases update poll) without spawning a
subprocess that re-imports the whole tree.

This module exposes a tiny *builtin* registry — a name → async-callable
map — that any future runtime hook can dispatch through. Each callable
takes one :class:`BuiltinContext` (carrying ``app_state``, the FastAPI
``app`` handle, the live ``AdminState`` bundle, the job's ``run_id``)
and returns a plain JSON-serialisable dict describing the outcome. The
contract is deliberately permissive:

* Builtins **must not raise**. Any exception is caught at the boundary
  and turned into a ``{"ok": False, "reason": "..."}`` envelope so the
  scheduler tick loop never dies on a bad release fetch / sqlite write.
* Builtins should be idempotent at the *poll* level — the
  :class:`UpdateChecker` already short-circuits on its own TTL when the
  cron fires under the interval window.

The single registered action this wave is ``system.update_check`` which
wraps :meth:`UpdateChecker.poll` (with ``force=False`` so it honours the
checker's own TTL). The admin UI's manual "Check now" path bypasses
this registry entirely and calls ``poll(force=True)`` through
``POST /admin/system/check-updates`` — the two paths share the
:class:`UpdateChecker` instance owned by ``AppState`` /
``AdminState.update_checker``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger("corlinman_server.scheduler.builtins")


__all__ = [
    "BUILTIN_ACTIONS",
    "BuiltinContext",
    "register_builtin",
    "run_builtin",
]


@dataclass(frozen=True)
class BuiltinContext:
    """Per-firing context handed to a builtin callable.

    Only ``app_state`` is mandatory in spirit — every today-registered
    builtin reads handles off it (``corlinman_update_checker``, etc.).
    The other slots are present for future builtins (subagent
    supervisor, log compaction) so the signature doesn't churn when the
    next maintenance job lands. All slots are nullable so tests can
    instantiate the context with only the field they exercise.
    """

    app_state: Any | None = None
    admin_state: Any | None = None
    run_id: str | None = None
    name: str | None = None


# Builtin signature — ``BUILTIN_ACTIONS`` is the registry the scheduler
# (and tests) read by name. Each callable returns a JSON-serialisable
# dict describing the outcome; the caller wraps the dict into a hook
# event / scheduler history entry as appropriate.
BuiltinAction = Callable[[BuiltinContext], Awaitable[dict[str, Any]]]


# Mutable on-purpose: external test code patches entries in-place and
# the lifecycle code reads it back. Initialised empty + populated below
# so import order matches the rest of the codebase's "register at
# module load" pattern (mirrors how ``corlinman_hooks`` ports the Rust
# `inventory!` registry).
BUILTIN_ACTIONS: dict[str, BuiltinAction] = {}


def register_builtin(name: str, action: BuiltinAction) -> None:
    """Register ``action`` under ``name``. Idempotent on re-import.

    Re-registering an existing name silently replaces the prior entry —
    matches the "last-in-wins" semantics tests rely on when they
    monkeypatch a builtin to a stub. A real production conflict would
    surface via the scheduler's history (two builtins emitting under
    the same name) rather than here.
    """
    BUILTIN_ACTIONS[name] = action


async def run_builtin(name: str, context: BuiltinContext) -> dict[str, Any]:
    """Dispatch ``name`` against the registry. Never raises.

    Returns a JSON-serialisable dict in every branch:

    * unknown ``name`` → ``{"ok": False, "reason": "unknown_builtin: <name>"}``
    * action raised → ``{"ok": False, "reason": "builtin_raised: <repr>"}``
    * action returned a non-dict → coerced to ``{"ok": False, "reason": ...}``

    The wrapper is deliberately permissive so a misbehaving builtin
    can't taint the scheduler's long-lived tick loop.
    """
    action = BUILTIN_ACTIONS.get(name)
    if action is None:
        return {"ok": False, "reason": f"unknown_builtin: {name}"}
    try:
        result = await action(context)
    except Exception as exc:  # noqa: BLE001 - mirror dispatch's catch-all
        _logger.warning(
            "scheduler.builtin.raised",
            extra={"builtin_name": name, "error": repr(exc)},
        )
        return {"ok": False, "reason": f"builtin_raised: {exc!r}"}
    if not isinstance(result, dict):
        _logger.warning(
            "scheduler.builtin.non_dict_result",
            extra={"builtin_name": name, "type": type(result).__name__},
        )
        return {"ok": False, "reason": f"non_dict_result: {type(result).__name__}"}
    return result


# ---------------------------------------------------------------------------
# system.update_check
# ---------------------------------------------------------------------------


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


# Register the W2.2 builtin at import time. Sibling modules can call
# :func:`register_builtin` to add more without re-exporting the dict.
register_builtin("system.update_check", _system_update_check_action)
