"""Scheduler builtin registry — name → async callable.

Pure plumbing: :class:`BuiltinContext` (the typed parameter every
builtin takes), the mutable :data:`BUILTIN_ACTIONS` dict, and the two
free helpers (:func:`register_builtin` / :func:`run_builtin`) sibling
modules call. The builtin bodies themselves live one file over (see
:mod:`corlinman_server.scheduler.builtins`'s package docstring for the
layout rationale).

Why this module is so small: keeping the registry decoupled from any
particular builtin's body means a builtin can ``register_builtin(...)``
at import time without dragging the rest of the registry through a
circular import. The package ``__init__`` imports every body so any
``import corlinman_server.scheduler.builtins`` populates the dict in
full — but a sibling builtin can also do
``from corlinman_server.scheduler.builtins.registry import
register_builtin`` without touching the other entries.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_logger = logging.getLogger("corlinman_server.scheduler.builtins")


__all__ = [
    "BUILTIN_ACTIONS",
    "BuiltinAction",
    "BuiltinContext",
    "register_builtin",
    "resolve_data_dir",
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
# the lifecycle code reads it back. Initialised empty + populated by
# each sibling builtin module at import time so the registry mirrors
# the "register at module load" pattern used elsewhere in the codebase
# (corlinman_hooks' Python port of the Rust ``inventory!`` registry).
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


def resolve_data_dir(context: BuiltinContext) -> Path | None:
    """Find the gateway's writable data dir off the firing context.

    Shared by the maintenance builtins — this exact three-probe walk was
    copy-pasted across six builtin modules before landing here; new
    builtins must use this instead of a seventh copy.
    """
    for owner in (context.app_state, context.admin_state):
        raw = getattr(owner, "data_dir", None)
        if raw:
            try:
                return Path(str(raw))
            except (TypeError, ValueError):  # pragma: no cover — defensive
                continue
    return None
