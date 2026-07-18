"""``/admin/scheduler*`` — cron job listing + manual trigger + history.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/scheduler.rs``,
extended in W6 of ``docs/PLAN_PERSONA_STUDIO.md`` with:

* ``POST /admin/scheduler/jobs`` — operator-created jobs persisted in
  the runtime overlay on :attr:`AdminState.extras` (the config-defined
  ``[[scheduler.jobs]]`` rows stay the source of truth for jobs the
  operator pinned to disk; this route adds a sibling pool of
  runtime-created jobs the UI can hot-edit).
* ``POST /admin/scheduler/qzone/templates/{template_id}/enable`` —
  reads the seeded ``<DATA_DIR>/bundled_personas/<template_id>/
  daily_job.json`` and creates / updates a runtime job for it. The
  same JSON keys + idempotency rules as the generic POST above. A
  second call updates the existing job rather than creating a
  duplicate so the operator can flip ``cron`` / ``prompt_template``
  without churning the registry.

Three original routes:

* ``GET  /admin/scheduler/jobs`` — definitions from
  ``[[scheduler.jobs]]`` plus the runtime overlay above. ``next_fire_at``
  / ``last_status`` are null until the cron runtime publishes runtime
  data.
* ``POST /admin/scheduler/jobs/{name}/trigger`` — best-effort manual
  fire. Falls back to recording a ``status=not_wired`` history entry
  and returning 501 when no scheduler runtime is attached.
* ``GET  /admin/scheduler/history`` — newest-first ring-buffer history.

Reuses ``corlinman_server.scheduler.SchedulerHandle`` when available
(:attr:`AdminState.scheduler`). History is kept in a tiny in-process
ring buffer parked on :attr:`AdminState.extras["scheduler_history"]`.
Runtime jobs are parked on
:attr:`AdminState.extras["scheduler_runtime_jobs"]`.

The module-level helpers / wire-models / constants this router leans on
live in the sibling :mod:`._scheduler_lib` (extracted to keep this file a
thin route surface). They are re-imported below so ``router()`` and its
handlers stay unchanged, and so back-compat importers
(``from ...infra.scheduler import rehydrate_runtime_jobs_on_boot``) keep
resolving via the re-export.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from corlinman_server.gateway.routes_admin_b.state import (
    config_snapshot,
    get_admin_state,
    require_admin,
)
from corlinman_server.scheduler.builtins.qzone_daily import (
    QZONE_DAILY_BUILTIN_NAME,
)

from ._scheduler_lib import (
    _JOB_NAME_RE,
    _TEMPLATE_ID_RE,
    EditJobBody,
    HistoryEntry,
    JobOut,
    NewJobBody,
    _bundled_template_path,
    _history,
    _job_metadata,
    _list_jobs_from_config,
    _list_runtime_jobs,
    _load_template_body,
    _now_iso,
    _persist_runtime_jobs,
    _runtime_job_to_out,
    _runtime_jobs,
    _set_enabled_route,
    _store_job,
    _trigger_runtime_qzone_daily,
    _unregister_runtime_loop,
    _validate_cron,
    _validate_qzone_daily,
    make_history_entry,
    rehydrate_runtime_jobs_on_boot,
)

__all__ = [
    "make_history_entry",
    "rehydrate_runtime_jobs_on_boot",
    "router",
]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "scheduler"])

    @r.get("/admin/scheduler/jobs", response_model=list[JobOut])
    async def list_jobs() -> list[JobOut]:
        state = get_admin_state()
        cfg = dict(config_snapshot())
        config_jobs = _list_jobs_from_config(cfg)
        runtime_jobs = _list_runtime_jobs(state)
        # Runtime rows override config rows of the same name so an
        # operator can shadow a stale config entry while the next
        # reload picks up the runtime version.
        by_name: dict[str, JobOut] = {j.name: j for j in config_jobs}
        for j in runtime_jobs:
            by_name[j.name] = j
        return list(by_name.values())

    @r.post("/admin/scheduler/jobs", response_model=JobOut)
    async def create_job(body: NewJobBody):
        state = get_admin_state()
        # Name + cron validation up front so the operator sees a clean
        # 422 instead of a downstream cron-tick warning.
        if not _JOB_NAME_RE.match(body.name or ""):
            return JSONResponse(
                status_code=422,
                content={
                    "error": "invalid_job_name",
                    "message": "name must match [a-z0-9_.\\-]{1,128}",
                },
            )
        ok, err = _validate_cron(body.cron or "")
        if not ok:
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_cron", "message": err or ""},
            )
        if body.action_type == QZONE_DAILY_BUILTIN_NAME:
            ok, err = _validate_qzone_daily(body)
            if not ok:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": "invalid_qzone_daily_args",
                        "message": err or "",
                    },
                )
        rj = _store_job(state, body)
        return _runtime_job_to_out(rj)

    @r.patch("/admin/scheduler/jobs/{name}", response_model=JobOut)
    async def edit_job(name: str, body: EditJobBody):
        """Partial-update a runtime job. Only the fields present in the
        body are applied; the rest carry over. Config-derived jobs are
        not editable here (they 404 — edit those in the TOML)."""
        state = get_admin_state()
        table = _runtime_jobs(state)
        rj = table.get(name)
        if rj is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "not_found",
                    "resource": "runtime_scheduler_job",
                    "id": name,
                },
            )
        # Compose the post-edit shape so we can re-validate it as a whole
        # (a cron edit must still parse; a qzone job must keep its args).
        new_cron = body.cron if body.cron is not None else rj.cron
        new_action_type = (
            body.action_type if body.action_type is not None else rj.action_type
        )
        merged = NewJobBody(
            name=name,
            cron=new_cron,
            action_type=new_action_type,
            timezone=body.timezone if body.timezone is not None else rj.timezone,
            enabled=body.enabled if body.enabled is not None else rj.enabled,
            persona_id=(
                body.persona_id if body.persona_id is not None else rj.persona_id
            ),
            prompt_template=(
                body.prompt_template
                if body.prompt_template is not None
                else rj.prompt_template
            ),
            qq_account=(
                body.qq_account if body.qq_account is not None else rj.qq_account
            ),
            # B5 — pass the promoted fields through only when the PATCH sent
            # them (top-level is authoritative). When omitted (None) the
            # merged ``metadata`` carry-over below preserves the existing
            # value, and ``_compose_metadata`` leaves it untouched. A changed
            # ``jitter_minutes`` flows into metadata → ``_store_job`` →
            # ``_apply_enabled_state`` re-registers the tick loop, which reads
            # the new ``jitter_secs`` via ``_jitter_secs_from_metadata``.
            image_ref_labels=body.image_ref_labels,
            jitter_minutes=body.jitter_minutes,
            metadata=(
                body.metadata if body.metadata is not None else dict(rj.metadata)
            ),
        )
        ok, err = _validate_cron(merged.cron or "")
        if not ok:
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_cron", "message": err or ""},
            )
        if merged.action_type == QZONE_DAILY_BUILTIN_NAME:
            ok, err = _validate_qzone_daily(merged)
            if not ok:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": "invalid_qzone_daily_args",
                        "message": err or "",
                    },
                )
        updated = _store_job(state, merged)
        return _runtime_job_to_out(updated)

    @r.post("/admin/scheduler/jobs/{name}/pause", response_model=JobOut)
    async def pause_job(name: str):
        """Flip a runtime job to ``enabled=false`` and stop its tick loop."""
        return _set_enabled_route(name, enabled=False)

    @r.post("/admin/scheduler/jobs/{name}/resume", response_model=JobOut)
    async def resume_job(name: str):
        """Flip a runtime job to ``enabled=true`` and (re)start its tick
        loop. Re-validates the cron / qzone args first so a job that was
        paused while invalid can't silently resume into a never-firing
        loop."""
        return _set_enabled_route(name, enabled=True)

    @r.delete("/admin/scheduler/jobs/{name}")
    async def delete_job(name: str):
        """Remove a runtime job: cancel its tick loop, drop it from the
        overlay + metadata table, and re-persist the sidecar. Config jobs
        404 (delete those in the TOML)."""
        state = get_admin_state()
        table = _runtime_jobs(state)
        if name not in table:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "not_found",
                    "resource": "runtime_scheduler_job",
                    "id": name,
                },
            )
        _unregister_runtime_loop(state, name)
        table.pop(name, None)
        _job_metadata(state).pop(name, None)
        _persist_runtime_jobs(state)
        return {"ok": True, "deleted": name}

    @r.post(
        "/admin/scheduler/qzone/templates/{template_id}/enable",
        response_model=JobOut,
    )
    async def enable_qzone_template(template_id: str):
        state = get_admin_state()
        if not _TEMPLATE_ID_RE.match(template_id):
            return JSONResponse(
                status_code=422,
                content={
                    "error": "invalid_template_id",
                    "message": "template_id must match [a-z0-9_-]{1,64}",
                },
            )
        path = _bundled_template_path(state, template_id)
        if path is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "template_not_found",
                    "template_id": template_id,
                },
            )
        try:
            body = _load_template_body(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "template_parse_failed",
                    "message": str(exc),
                    "path": str(path),
                },
            )
        # Enable the template on activation — the template ships with
        # ``enabled=false`` for safety; flipping it to true is what
        # this route exists to do.
        body.enabled = True
        if body.action_type != QZONE_DAILY_BUILTIN_NAME:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "unsupported_template_action_type",
                    "message": (
                        f"only {QZONE_DAILY_BUILTIN_NAME} templates "
                        "may be enabled via this route"
                    ),
                    "got": body.action_type,
                },
            )
        ok, err = _validate_qzone_daily(body)
        if not ok:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "invalid_qzone_daily_args",
                    "message": err or "",
                },
            )
        ok, err = _validate_cron(body.cron)
        if not ok:
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_cron", "message": err or ""},
            )
        rj = _store_job(state, body)
        return _runtime_job_to_out(rj)

    @r.post("/admin/scheduler/jobs/{name}/trigger")
    async def trigger_job(name: str):
        state = get_admin_state()
        cfg = dict(config_snapshot())
        config_jobs = _list_jobs_from_config(cfg)
        runtime_jobs = _runtime_jobs(state)
        if not any(j.name == name for j in config_jobs) and name not in runtime_jobs:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "resource": "scheduler_job", "id": name},
            )

        history = _history(state)
        now_iso = _now_iso()

        rj = runtime_jobs.get(name)

        # If a SchedulerHandle is attached and exposes a manual-trigger
        # method, prefer it. Then check the runtime-job pool — a
        # ``qzone.daily_publish`` runtime job can be fired directly
        # against its registered builtin without a scheduler handle.
        sched = state.scheduler
        if sched is not None and hasattr(sched, "trigger"):
            try:
                await sched.trigger(name)
            except Exception as exc:  # noqa: BLE001
                entry = HistoryEntry(
                    job=name,
                    at=now_iso,
                    source="manual",
                    status="error",
                    message=str(exc),
                )
                history.push(entry)
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": "trigger_failed",
                        "message": str(exc),
                        "recorded": entry.model_dump(),
                    },
                )
            entry = HistoryEntry(
                job=name,
                at=now_iso,
                source="manual",
                status="ok",
                message="manual trigger dispatched to scheduler runtime",
            )
            history.push(entry)
            return {"ok": True, "recorded": entry.model_dump()}

        # Runtime fallback — drive the registered builtin in-process.
        # This is the path the W6 admin UI relies on when there's no
        # live scheduler handle (the common dev / test case).
        if rj is not None and rj.action_type == QZONE_DAILY_BUILTIN_NAME:
            return await _trigger_runtime_qzone_daily(state, rj, history)

        entry = HistoryEntry(
            job=name,
            at=now_iso,
            source="manual",
            status="not_wired",
            message=(
                "scheduler runtime is not yet wired; trigger attempt "
                "recorded in history"
            ),
        )
        history.push(entry)
        return JSONResponse(
            status_code=501,
            content={
                "error": "scheduler_not_wired",
                "message": entry.message,
                "recorded": entry.model_dump(),
            },
        )

    @r.get("/admin/scheduler/history", response_model=list[HistoryEntry])
    async def list_history() -> list[HistoryEntry]:
        state = get_admin_state()
        snap = _history(state).snapshot()
        snap.reverse()
        return snap

    return r
