"""``evolution.darwin_curate`` builtin — W3 v2.1.

Wraps :func:`corlinman_evolution_engine.darwin_curator.run_darwin_curator`
in the scheduler-builtin contract so the gateway's cron loop can fire
darwin's structural rubric scan on its own cadence. The engine itself
already auto-discovers the resulting ``skill.quality.issue`` signals on
its next ``run-once`` and the existing ``DarwinHandler`` mints proposals
into the operator queue — no admin click needed.

Behaviour matrix:

* No ``data_dir`` reachable from ``app_state`` → builtin returns
  ``{"ok": False, "reason": "data_dir_unavailable"}``. Mirrors the
  W2.2 ``checker_unavailable`` envelope shape so the scheduler
  history surfaces *why* the curator skipped rather than logging a
  stack trace.
* Skills dir doesn't exist (fresh boot, no profile seeded yet) → the
  curator's own degradation path returns a 0-scanned report; we relay
  that as ``ok=True`` with zero counts.
* SQLite locked / permission denied → caught, returned as
  ``{"ok": False, "reason": "store_open_failed: ..."}``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from corlinman_server.scheduler.builtins.registry import (
    BuiltinContext,
    register_builtin,
)

_logger = logging.getLogger(
    "corlinman_server.scheduler.builtins.evolution_darwin_curate"
)


#: Builtin name used in ``JobAction.run_tool(plugin="evolution", tool="darwin_curate")``.
#: The dot-joined form matches the scheduler's ``<plugin>.<tool>`` convention.
EVOLUTION_DARWIN_CURATE_BUILTIN_NAME: str = "evolution.darwin_curate"


__all__ = [
    "EVOLUTION_DARWIN_CURATE_BUILTIN_NAME",
    "_evolution_darwin_curate_action",
]


def _resolve_data_dir(context: BuiltinContext) -> Path | None:
    """Find the gateway's writable data dir on ``app_state``.

    Same three-probe pattern :func:`_resolve_update_checker` uses.
    Falls back through ``app_state`` → ``admin_state`` so degraded
    boots that only land one of them still discover the path.
    """
    for owner in (context.app_state, context.admin_state):
        if owner is None:
            continue
        raw = getattr(owner, "data_dir", None)
        if raw is None:
            continue
        if isinstance(raw, Path):
            return raw
        return Path(str(raw))
    return None


def _resolve_active_profile(context: BuiltinContext) -> str:
    """Return the slug whose skills/ the curator scans.

    v1 hard-codes ``"default"`` — the only profile the seeded
    starter_skills bundle lands in today. Future multi-profile
    deployments can override via ``app_state.corlinman_active_profile``
    without touching the builtin (we read that attr first when
    present).
    """
    for owner in (context.app_state, context.admin_state):
        if owner is None:
            continue
        slug = getattr(owner, "corlinman_active_profile", None)
        if isinstance(slug, str) and slug.strip():
            return slug.strip()
    return "default"


async def _evolution_darwin_curate_action(
    context: BuiltinContext,
) -> dict[str, Any]:
    """Walk the active profile's skills/, score each SKILL.md, emit
    ``skill.quality.issue`` signals for any that fall below the
    rubric threshold. Returns a small report the scheduler history
    persists verbatim.

    The actual scoring + signal emission lives in
    :func:`corlinman_evolution_engine.darwin_curator.run_darwin_curator`;
    this wrapper only resolves paths off ``app_state``, opens the
    evolution-store connection, and forwards counts.
    """
    data_dir = _resolve_data_dir(context)
    if data_dir is None:
        return {"ok": False, "reason": "data_dir_unavailable"}

    # Lazy imports — these packages aren't on every test fixture's
    # PYTHONPATH (the gateway-side tests stub the scheduler) and we'd
    # rather degrade than crash on a deep import chain.
    try:
        from corlinman_evolution_engine.darwin_curator import (  # noqa: PLC0415
            run_darwin_curator,
        )
        from corlinman_evolution_store.repo import SignalsRepo  # noqa: PLC0415
        from corlinman_evolution_store.store import (  # noqa: PLC0415
            EvolutionStore,
        )

        from corlinman_server.profiles import (  # noqa: PLC0415
            profile_skills_dir,
        )
    except ImportError as exc:
        return {
            "ok": False,
            "reason": f"deps_unavailable: {exc}",
        }

    slug = _resolve_active_profile(context)
    skills_dir = profile_skills_dir(data_dir, slug)
    evolution_db = data_dir / "evolution.sqlite"

    try:
        async with EvolutionStore(evolution_db) as store:
            signals_repo = SignalsRepo(store.conn)
            report = await run_darwin_curator(
                skills_dir=skills_dir,
                signals_repo=signals_repo,
                tenant_id="default",
            )
    except Exception as exc:  # noqa: BLE001 - never raise out of builtin
        _logger.warning(
            "scheduler.builtin.evolution_darwin_curate.failed",
            extra={"error": repr(exc)},
        )
        return {"ok": False, "reason": f"store_open_failed: {exc!r}"}

    return {
        "ok": True,
        "profile": slug,
        "skills_dir": str(skills_dir),
        "skills_scanned": report.skills_scanned,
        "skills_below_threshold": report.skills_below_threshold,
        "signals_emitted": report.signals_emitted,
        "skipped_blacklist": report.skipped_blacklist,
        "skipped_unreadable": report.skipped_unreadable,
        "elapsed_ms": report.elapsed_ms,
    }


# Module-load-time registration so the package ``__init__`` import is
# all that's required to wire the builtin. Tests that monkeypatch the
# registry can simply replace ``BUILTIN_ACTIONS[EVOLUTION_DARWIN_CURATE_BUILTIN_NAME]``
# without redoing this dance.
register_builtin(
    EVOLUTION_DARWIN_CURATE_BUILTIN_NAME,
    _evolution_darwin_curate_action,
)
