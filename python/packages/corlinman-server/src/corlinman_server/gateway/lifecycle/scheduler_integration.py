"""Scheduler-integration helpers for the gateway lifecycle.

Extracted from :mod:`corlinman_server.gateway.lifecycle.entrypoint`
(Modularization Phase 8, slice 2) — the cohesive set of helpers that
bridge the loaded gateway config / auto-registered defaults into the
runtime :class:`corlinman_server.scheduler.SchedulerConfig` the lifespan
spawns:

* :func:`_register_default_update_check_job` — stash the default
  ``system.update_check`` job on ``app.state`` (W2.2).
* :func:`_register_default_darwin_curate_job` — stash the default
  ``evolution.darwin_curate`` job on ``app.state`` (W3 v2.1).
* :func:`_scheduler_job_from_config_entry` — convert one
  ``[[scheduler.jobs]]`` config entry into a runtime
  :class:`~corlinman_server.scheduler.SchedulerJob`.
* :func:`_effective_scheduler_config` — merge operator + default jobs.
* :func:`_config_has_scheduler_job` — explicit-config detection.

These helpers depend on two broadly-shared entrypoint symbols that
intentionally stay in ``entrypoint`` — the pure ``_extract_section``
tolerant config reader (used by ~15 staying entrypoint helpers) and the
public ``list_default_scheduler_jobs`` reader (exported from
``entrypoint.__all__`` and imported by tests from there). To keep this
module free of a module-level import cycle back into ``entrypoint``
(Modularization Rule 5), those two are pulled in via deferred,
function-local imports at call time — the same idiom these functions
already use for the lazy ``corlinman_server.scheduler`` imports.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Canonical name of the default ``system.update_check`` scheduler job.
#: Kept module-local so the helpers below have no module-level import
#: back into :mod:`entrypoint` (which re-exports this constant).
DEFAULT_UPDATE_CHECK_JOB_NAME: str = "system.update_check"

#: Canonical name of the W3 v2 darwin daily rubric scan. Same naming
#: convention as ``system.update_check`` — ``<plugin>.<tool>`` — so the
#: scheduler's :class:`JobAction.run_tool` dispatch picks up the
#: ``EVOLUTION_DARWIN_CURATE_BUILTIN_NAME`` builtin by string match.
DEFAULT_EVOLUTION_DARWIN_CURATE_JOB_NAME: str = "evolution.darwin_curate"

#: Canonical name of the R2 hourly persona mood/fatigue decay sweep.
#: Same ``<plugin>.<tool>`` convention so the scheduler resolves the
#: ``PERSONA_DECAY_BUILTIN_NAME`` builtin by string match.
DEFAULT_PERSONA_DECAY_JOB_NAME: str = "persona.decay"


def _config_has_scheduler_job(cfg: Any | None, name: str) -> bool:
    """``True`` when the loaded config already carries a job by ``name``.

    The gateway config loader hands back dict-shaped data (see
    ``gateway.core.config`` docstring), so we read
    ``cfg["scheduler"]["jobs"]`` and look for the first entry whose
    ``name`` matches. Tolerates a missing scheduler section / non-list
    ``jobs`` value / missing ``name`` keys without raising — the
    explicit-config detection only needs to ``True`` on a clean match.

    Plain dataclass-shaped configs (``cfg.scheduler.jobs``) also work;
    we duck-type on attribute then fall back to mapping access so a
    Wave-1 ``SimpleNamespace``-shaped test config goes through the
    same branch the production loader does.
    """
    from corlinman_server.gateway.lifecycle.entrypoint import _extract_section

    scheduler = _extract_section(cfg, "scheduler")
    if scheduler is None:
        return False
    jobs = _extract_section(scheduler, "jobs")
    if not isinstance(jobs, (list, tuple)):
        return False
    for entry in jobs:
        entry_name = _extract_section(entry, "name")
        if isinstance(entry_name, str) and entry_name == name:
            return True
    return False


def _register_default_update_check_job(
    app: Any, cfg: Any | None, interval_hours: int
) -> None:
    """Stash a default ``system.update_check`` :class:`SchedulerJob` on ``app.state``.

    Behaviour matrix (matches the spec in W2.2 of
    ``docs/PLAN_AUTO_UPDATE.md``):

    * ``[system.update_check] enabled = false`` — *not* called (the
      caller guards on ``update_cfg.enabled`` first).
    * Explicit ``[[scheduler.jobs]] name = "system.update_check"``
      already in config — silent no-op so the operator's explicit
      cron / timezone / action wins.
    * Otherwise — appends a :class:`SchedulerJob` with cron
      ``"0 0 */{interval_hours} * * * *"`` and a ``run_tool``-shaped
      action pointing at the builtin name. The job lives on
      ``app.state.corlinman_default_scheduler_jobs`` (a list) so the
      scheduler runtime (once :func:`spawn` is wired into the lifespan)
      can pick it up alongside the config jobs, and tests can assert
      its presence without exercising the runtime.

    All log lines use the ``gateway.system.update_check_job.*`` prefix
    so a single grep surfaces the W2.2 wiring across boot logs.
    """
    if _config_has_scheduler_job(cfg, DEFAULT_UPDATE_CHECK_JOB_NAME):
        logger.info(
            "gateway.system.update_check_job.skipped_explicit_config",
            name=DEFAULT_UPDATE_CHECK_JOB_NAME,
        )
        return

    # Build the cron string in the project's 7-field grammar
    # (sec min hour dom mon dow year). ``0 0 */N * * * *`` fires at
    # the top of every Nth hour — matches every existing scheduler
    # job's choice in ``docs/config.example.toml``. Clamp the interval
    # so a misconfigured ``interval_hours = 0`` falls back to 1 (the
    # config dataclass already clamps to ``>=1`` but a degraded boot
    # may have skipped that path).
    interval = max(1, int(interval_hours))
    cron_expr = f"0 0 */{interval} * * * *"

    try:
        from corlinman_server.scheduler import JobAction, SchedulerJob

        job = SchedulerJob(
            name=DEFAULT_UPDATE_CHECK_JOB_NAME,
            cron=cron_expr,
            action=JobAction.run_tool(
                plugin="system",
                tool="update_check",
            ),
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "gateway.system.update_check_job.build_failed",
            error=str(exc),
        )
        return

    existing = getattr(app.state, "corlinman_default_scheduler_jobs", None)
    if not isinstance(existing, list):
        existing = []
    # De-dupe against the in-memory list too so a hot-reload that
    # re-runs this branch doesn't grow the list unbounded.
    if any(
        getattr(j, "name", None) == DEFAULT_UPDATE_CHECK_JOB_NAME
        for j in existing
    ):
        return
    existing.append(job)
    app.state.corlinman_default_scheduler_jobs = existing

    logger.info(
        "gateway.system.update_check_job.registered",
        name=DEFAULT_UPDATE_CHECK_JOB_NAME,
        cron=cron_expr,
    )


def _register_default_darwin_curate_job(app: Any, cfg: Any | None) -> None:
    """W3 v2.1 — stash a default ``evolution.darwin_curate`` scheduler
    job on ``app.state`` alongside the W2.2 update-check job.

    Same operator-override / de-dupe discipline as
    :func:`_register_default_update_check_job`:

    * Explicit ``[[scheduler.jobs]] name = "evolution.darwin_curate"``
      already in config → silent no-op so the operator's cron wins.
    * Otherwise → append a :class:`SchedulerJob` firing daily at
      ``"0 30 3 * * * *"`` (03:30 UTC, after the update-check window).
      Action is ``JobAction.run_tool(plugin="evolution",
      tool="darwin_curate")`` which the scheduler dispatches to the
      :data:`EVOLUTION_DARWIN_CURATE_BUILTIN_NAME` builtin.

    Log lines use the ``gateway.evolution.darwin_curate_job.*`` prefix
    so the wiring is greppable across boot logs.
    """
    name = DEFAULT_EVOLUTION_DARWIN_CURATE_JOB_NAME
    if _config_has_scheduler_job(cfg, name):
        logger.info(
            "gateway.evolution.darwin_curate_job.skipped_explicit_config",
            name=name,
        )
        return

    # Daily at 03:30 UTC. update_check fires every N hours; darwin
    # only needs once per day because skill content changes slowly.
    cron_expr = "0 30 3 * * * *"

    try:
        from corlinman_server.scheduler import JobAction, SchedulerJob

        job = SchedulerJob(
            name=name,
            cron=cron_expr,
            action=JobAction.run_tool(
                plugin="evolution",
                tool="darwin_curate",
            ),
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "gateway.evolution.darwin_curate_job.build_failed",
            error=str(exc),
        )
        return

    existing = getattr(app.state, "corlinman_default_scheduler_jobs", None)
    if not isinstance(existing, list):
        existing = []
    if any(getattr(j, "name", None) == name for j in existing):
        return
    existing.append(job)
    app.state.corlinman_default_scheduler_jobs = existing

    logger.info(
        "gateway.evolution.darwin_curate_job.registered",
        name=name,
        cron=cron_expr,
    )


def _register_default_persona_decay_job(app: Any, cfg: Any | None) -> None:
    """R2 persona-liveness — stash a default ``persona.decay`` scheduler
    job on ``app.state`` alongside the W2.2 update-check + W3 darwin jobs.

    Without this nothing ever sweeps mood/fatigue decay, so a fresh
    install's mood stays ``"neutral"`` / fatigue stays ``0.0`` forever.

    Same operator-override / de-dupe discipline as
    :func:`_register_default_darwin_curate_job`:

    * Explicit ``[[scheduler.jobs]] name = "persona.decay"`` already in
      config → silent no-op so the operator's cron wins.
    * Otherwise → append a :class:`SchedulerJob` firing hourly at
      ``"0 0 */1 * * * *"`` (top of every hour). Action is
      ``JobAction.run_tool(plugin="persona", tool="decay")`` which the
      scheduler dispatches to the :data:`PERSONA_DECAY_BUILTIN_NAME`
      builtin.

    Log lines use the ``gateway.persona.decay_job.*`` prefix so the
    wiring is greppable across boot logs.
    """
    name = DEFAULT_PERSONA_DECAY_JOB_NAME
    if _config_has_scheduler_job(cfg, name):
        logger.info(
            "gateway.persona.decay_job.skipped_explicit_config",
            name=name,
        )
        return

    # Hourly — mood/fatigue drift is gradual but the per-row elapsed-hours
    # math means a tick that fires under a busy window still applies the
    # right amount of decay (the builtin reads each row's updated_at).
    cron_expr = "0 0 */1 * * * *"

    try:
        from corlinman_server.scheduler import JobAction, SchedulerJob

        job = SchedulerJob(
            name=name,
            cron=cron_expr,
            action=JobAction.run_tool(
                plugin="persona",
                tool="decay",
            ),
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "gateway.persona.decay_job.build_failed",
            error=str(exc),
        )
        return

    existing = getattr(app.state, "corlinman_default_scheduler_jobs", None)
    if not isinstance(existing, list):
        existing = []
    if any(getattr(j, "name", None) == name for j in existing):
        return
    existing.append(job)
    app.state.corlinman_default_scheduler_jobs = existing

    logger.info(
        "gateway.persona.decay_job.registered",
        name=name,
        cron=cron_expr,
    )


def _scheduler_job_from_config_entry(entry: Any) -> Any | None:
    """Convert one ``[[scheduler.jobs]]`` config entry into a runtime
    :class:`SchedulerJob`, or ``None`` for a misshapen entry.

    R4-F1: operator-defined scheduler jobs were *display-only* in the
    Python port — the admin route rendered them but nothing converted
    them into runtime specs. This bridges the loaded-config shape (dict
    *or* dataclass, read via :func:`_extract_section`) into the
    :class:`SchedulerJob` the runtime spawns. Best-effort: one bad entry
    returns ``None`` and is skipped rather than aborting boot.
    """
    from corlinman_server.gateway.lifecycle.entrypoint import _extract_section

    name = _extract_section(entry, "name")
    cron = _extract_section(entry, "cron")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(cron, str) or not cron:
        return None
    try:
        from corlinman_server.scheduler import JobAction, SchedulerJob
    except Exception:  # pragma: no cover — defensive
        return None

    action_obj = _extract_section(entry, "action")
    kind = _extract_section(action_obj, "type") or _extract_section(action_obj, "kind")
    if kind is None and isinstance(action_obj, dict):
        # Nested-key discriminant form: ``action = { subprocess = {...} }``.
        for key in ("subprocess", "run_tool", "run_agent"):
            if key in action_obj:
                kind = key
                break

    try:
        if kind == "subprocess":
            command = _extract_section(action_obj, "command")
            if not isinstance(command, str) or not command:
                return None
            raw_args = _extract_section(action_obj, "args")
            args = (
                tuple(str(a) for a in raw_args)
                if isinstance(raw_args, (list, tuple))
                else ()
            )
            raw_timeout = _extract_section(action_obj, "timeout_secs")
            timeout = int(raw_timeout) if isinstance(raw_timeout, int) else 600
            action = JobAction.subprocess(
                command=command, args=args, timeout_secs=timeout
            )
        elif kind == "run_tool":
            plugin = _extract_section(action_obj, "plugin")
            tool = _extract_section(action_obj, "tool")
            if not isinstance(plugin, str) or not isinstance(tool, str):
                return None
            action = JobAction.run_tool(
                plugin=plugin, tool=tool, args=_extract_section(action_obj, "args")
            )
        elif kind == "run_agent":
            prompt = _extract_section(action_obj, "prompt")
            if not isinstance(prompt, str) or not prompt:
                return None
            action = JobAction.run_agent(prompt=prompt)
        else:
            return None
    except Exception:  # pragma: no cover — defensive
        return None

    tz = _extract_section(entry, "timezone")
    return SchedulerJob(
        name=name, cron=cron, action=action, timezone=tz if isinstance(tz, str) else None
    )


def _effective_scheduler_config(app: Any, cfg: Any | None) -> Any:
    """Assemble the :class:`SchedulerConfig` the lifespan spawns.

    Merges, de-duped by name (first writer wins):

    1. operator ``[[scheduler.jobs]]`` from the loaded config; then
    2. the auto-registered defaults on
       ``app.state.corlinman_default_scheduler_jobs`` (the registration
       helpers only add a default when the config does *not* already
       declare it, so there is no overlap to resolve).
    """
    from corlinman_server.gateway.lifecycle.entrypoint import (
        _extract_section,
        list_default_scheduler_jobs,
    )
    from corlinman_server.scheduler import SchedulerConfig

    jobs: list[Any] = []
    seen: set[str] = set()

    section = _extract_section(cfg, "scheduler")
    raw_jobs = _extract_section(section, "jobs")
    if isinstance(raw_jobs, (list, tuple)):
        for entry in raw_jobs:
            job = _scheduler_job_from_config_entry(entry)
            if job is not None and job.name not in seen:
                jobs.append(job)
                seen.add(job.name)

    for job in list_default_scheduler_jobs(app):
        jname = getattr(job, "name", None)
        if isinstance(jname, str) and jname and jname not in seen:
            jobs.append(job)
            seen.add(jname)

    return SchedulerConfig(jobs=tuple(jobs))
