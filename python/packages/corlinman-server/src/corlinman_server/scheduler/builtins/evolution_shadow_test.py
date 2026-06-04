"""``evolution.shadow_test`` builtin — R8 PASSIVE slice (L3).

Wraps :meth:`corlinman_shadow_tester.runner.ShadowRunner.run_once` in the
scheduler-builtin contract so the gateway's cron loop can fire the shadow
sandbox pass on its own cadence — *in-process*, not by shelling out. The
engine (L2) mints PENDING medium/high-risk proposals; this builtin runs
each one through a per-case tempdir copy of ``kb.sqlite`` and transitions
it ``pending → shadow_running → shadow_done`` so the operator sees a
shadow report on the queue.

This is the PASSIVE half of the loop: the runner never touches a live
target. It only replays each pending proposal against a throwaway sandbox
and records the baseline / shadow metrics. No apply, no rollback. The whole
job is wired behind ``[evolution.shadow] enabled`` which defaults OFF.

Behaviour matrix (mirrors
:mod:`corlinman_server.scheduler.builtins.evolution_darwin_curate` and
:mod:`corlinman_server.scheduler.builtins.persona_decay`):

* No ``data_dir`` reachable from ``app_state`` → builtin returns
  ``{"ok": False, "reason": "data_dir_unavailable"}``. Same envelope
  shape so the scheduler history surfaces *why* the pass skipped rather
  than logging a stack trace.
* ``corlinman_shadow_tester`` / ``corlinman_evolution_store`` not
  importable (stubbed test fixture, partial install) →
  ``{"ok": False, "reason": "deps_unavailable: ..."}``.
* SQLite locked / permission denied / runner raised → caught, returned as
  ``{"ok": False, "reason": "shadow_failed: ..."}``.

"No eval set" for a kind is *not* a failure: the runner records a
``no-eval-set`` marker on the proposal and it stays gated (never
auto-approved). That is acceptable safe behaviour — see the default
``eval_set_dir`` note below.

The actual claim → sandbox → mark-done pipeline lives in
:class:`~corlinman_shadow_tester.runner.ShadowRunner`; this wrapper only
resolves the ``evolution.sqlite`` / ``kb.sqlite`` paths off ``app_state``,
opens the evolution store, registers the three stock simulators, and
forwards a small report.
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
    "corlinman_server.scheduler.builtins.evolution_shadow_test"
)


#: Builtin name used in ``JobAction.run_tool(plugin="evolution", tool="shadow_test")``.
#: The dot-joined form matches the scheduler's ``<plugin>.<tool>`` convention.
EVOLUTION_SHADOW_TEST_BUILTIN_NAME: str = "evolution.shadow_test"


__all__ = [
    "EVOLUTION_SHADOW_TEST_BUILTIN_NAME",
    "_evolution_shadow_test_action",
]


def _resolve_data_dir(context: BuiltinContext) -> Path | None:
    """Find the gateway's writable data dir on ``app_state``.

    Same three-probe pattern :func:`_evolution_darwin_curate_action`'s
    resolver uses — falls through ``app_state`` → ``admin_state`` so a
    degraded boot that only landed one of them still discovers the path.
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


def _resolve_config(context: BuiltinContext) -> Any:
    """Return the gateway config (a plain dict) off ``app_state``.

    The live config snapshot rides on ``AppState.config``; a degraded
    boot before the watcher attached one leaves it ``None``, which the
    eval-set-dir reader below tolerates by falling back to the default
    ``<data_dir>/evolution/eval_sets`` layout.
    """
    for owner in (context.app_state, context.admin_state):
        if owner is None:
            continue
        cfg = getattr(owner, "config", None)
        if cfg is not None:
            return cfg
    return None


def _resolve_eval_set_dir(config: Any, data_dir: Path) -> Path:
    """Default ``<data_dir>/evolution/eval_sets`` unless the operator
    overrode it via ``[evolution.shadow] eval_set_dir``.

    The runner appends ``/<kind>/`` per kind and loads ``*.yaml`` from
    there. A missing per-kind subdir is handled by the runner as a
    ``no-eval-set`` marker (safe, gated) rather than an error.
    """
    if isinstance(config, dict):
        evolution = config.get("evolution")
        if isinstance(evolution, dict):
            shadow = evolution.get("shadow")
            if isinstance(shadow, dict):
                raw = shadow.get("eval_set_dir")
                if isinstance(raw, str) and raw.strip():
                    return Path(raw)
    return data_dir / "evolution" / "eval_sets"


async def _evolution_shadow_test_action(
    context: BuiltinContext,
) -> dict[str, Any]:
    """Run one ``ShadowRunner.run_once`` pass against the gateway's
    pending medium/high-risk proposals and return a small report the
    scheduler history persists verbatim.

    PASSIVE only: every claimed proposal is replayed through a tempdir
    sandbox copy of ``kb.sqlite`` and transitioned to ``shadow_done``.
    Nothing is applied. This wrapper opens the evolution store, registers
    the three stock simulators (memory_op / tag_rebalance / skill_update),
    and forwards the run counts.
    """
    data_dir = _resolve_data_dir(context)
    if data_dir is None:
        return {"ok": False, "reason": "data_dir_unavailable"}

    # Lazy imports — these packages aren't on every test fixture's
    # PYTHONPATH (the gateway-side tests stub the scheduler) and we'd
    # rather degrade than crash on a deep import chain.
    try:
        from corlinman_evolution_store import ProposalsRepo  # noqa: PLC0415
        from corlinman_evolution_store.store import (  # noqa: PLC0415
            EvolutionStore,
        )
        from corlinman_shadow_tester.runner import ShadowRunner  # noqa: PLC0415
        from corlinman_shadow_tester.simulator import (  # noqa: PLC0415
            MemoryOpSimulator,
            SkillUpdateSimulator,
            TagRebalanceSimulator,
        )
    except ImportError as exc:
        return {"ok": False, "reason": f"deps_unavailable: {exc}"}

    evolution_db = data_dir / "evolution.sqlite"
    kb_db = data_dir / "kb.sqlite"
    eval_set_dir = _resolve_eval_set_dir(_resolve_config(context), data_dir)

    try:
        async with EvolutionStore(evolution_db) as store:
            runner = ShadowRunner(
                proposals=ProposalsRepo(store.conn),
                kb_path=kb_db,
                eval_set_dir=eval_set_dir,
            )
            # The three stock simulators ported from the Rust crate. A
            # kind with no registered simulator is silently skipped, so
            # registering them all keeps the runner gating every kind the
            # engine can mint.
            runner.register_simulator(MemoryOpSimulator())
            runner.register_simulator(TagRebalanceSimulator())
            runner.register_simulator(SkillUpdateSimulator())
            summary = await runner.run_once()
    except Exception as exc:  # noqa: BLE001 - never raise out of builtin
        _logger.warning(
            "scheduler.builtin.evolution_shadow_test.failed",
            extra={"error": repr(exc)},
        )
        return {"ok": False, "reason": f"shadow_failed: {exc!r}"}

    return {
        "ok": True,
        "evolution_db": str(evolution_db),
        "kb_db": str(kb_db),
        "eval_set_dir": str(eval_set_dir),
        "proposals_claimed": summary.proposals_claimed,
        "proposals_completed": summary.proposals_completed,
        "proposals_failed": summary.proposals_failed,
        "cases_run": summary.cases_run,
        "errors": summary.errors,
    }


# Module-load-time registration so the package ``__init__`` import is
# all that's required to wire the builtin. Tests that monkeypatch the
# registry can simply replace
# ``BUILTIN_ACTIONS[EVOLUTION_SHADOW_TEST_BUILTIN_NAME]`` without redoing
# this dance.
register_builtin(
    EVOLUTION_SHADOW_TEST_BUILTIN_NAME,
    _evolution_shadow_test_action,
)
