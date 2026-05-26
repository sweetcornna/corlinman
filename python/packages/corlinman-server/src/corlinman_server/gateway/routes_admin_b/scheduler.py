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
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)
from corlinman_server.scheduler.builtins.qzone_daily import (
    QZONE_DAILY_BUILTIN_NAME,
)

#: Slot name for the runtime-job overlay on
#: :attr:`AdminState.extras`. Keep stable — other modules may probe it.
_RUNTIME_JOBS_KEY: str = "scheduler_runtime_jobs"

#: Same idea for the in-process history ring buffer.
_HISTORY_KEY: str = "scheduler_history"

#: Slot the dispatcher reads to find a job's metadata. Mirrors the
#: shape the ``qzone.daily_publish`` builtin expects (see its
#: :func:`_resolve_metadata` helper). The map is keyed by job name.
_JOB_METADATA_KEY: str = "scheduler_job_metadata"

#: Bounded template-id slug — ``[a-z0-9_-]{1,64}`` mirrors the persona
#: id rule so the route segment is safe to splice into a filesystem
#: path without traversal risk.
_TEMPLATE_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

#: Same rule for job names.
_JOB_NAME_RE = re.compile(r"^[a-z0-9_.\-]{1,128}$")


class JobOut(BaseModel):
    name: str
    cron: str
    timezone: str | None = None
    action_kind: str
    next_fire_at: str | None = None
    last_status: str | None = None
    # W6 extensions — present on runtime jobs only; config-derived rows
    # leave them at their defaults so the existing UI keeps working.
    action_type: str | None = None
    enabled: bool = True
    persona_id: str | None = None
    prompt_template: str | None = None
    qq_account: str | None = None
    last_run_at_ms: int | None = None
    last_run_ok: bool | None = None
    last_qzone_url: str | None = None
    last_error: str | None = None
    source: str = "config"  # "config" | "runtime"


class HistoryEntry(BaseModel):
    job: str
    at: str
    source: str
    status: str
    message: str


class NewJobBody(BaseModel):
    """Body shape for ``POST /admin/scheduler/jobs``.

    Forward-compatible — only ``name`` / ``cron`` / ``action_type`` are
    required; per-action_type fields ride along as optional. The route
    validates per ``action_type`` to keep operator errors close to the
    submission point.
    """

    name: str
    cron: str
    action_type: str
    timezone: str | None = None
    enabled: bool = True
    persona_id: str | None = None
    prompt_template: str | None = None
    qq_account: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass
class _RuntimeJob:
    """One row in the runtime-job overlay.

    Lives on :attr:`AdminState.extras`; the route layer serialises it
    into a :class:`JobOut` for the wire.
    """

    name: str
    cron: str
    action_type: str
    timezone: str | None = None
    enabled: bool = True
    persona_id: str | None = None
    prompt_template: str | None = None
    qq_account: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    last_run_at_ms: int | None = None
    last_run_ok: bool | None = None
    last_qzone_url: str | None = None
    last_error: str | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0


class SchedulerHistory:
    """In-process ring buffer matching the Rust ``SchedulerHistory``.

    Capped at 100 entries. Push is fire-and-forget; readers get a
    snapshot via :meth:`snapshot`.
    """

    MAX = 100

    def __init__(self) -> None:
        self._buf: list[HistoryEntry] = []

    def push(self, entry: HistoryEntry) -> None:
        self._buf.append(entry)
        if len(self._buf) > self.MAX:
            del self._buf[: len(self._buf) - self.MAX]

    def snapshot(self) -> list[HistoryEntry]:
        return list(self._buf)


def _history(state: AdminState) -> SchedulerHistory:
    h = state.extras.get(_HISTORY_KEY)
    if isinstance(h, SchedulerHistory):
        return h
    new = SchedulerHistory()
    state.extras[_HISTORY_KEY] = new
    return new


def _runtime_jobs(state: AdminState) -> dict[str, _RuntimeJob]:
    """Return the mutable runtime-job overlay map.

    Keyed by job name. The map is created on first access so callers
    never have to remember to seed it.
    """
    table = state.extras.get(_RUNTIME_JOBS_KEY)
    if isinstance(table, dict):
        return table
    new: dict[str, _RuntimeJob] = {}
    state.extras[_RUNTIME_JOBS_KEY] = new
    return new


def _job_metadata(state: AdminState) -> dict[str, dict[str, Any]]:
    """Return the mutable per-job metadata map.

    The ``qzone.daily_publish`` builtin reads it via
    ``app_state.scheduler_job_metadata`` (the lifecycle mirrors the
    AdminState extras onto AppState). Routes write through this helper
    so the two surfaces stay in sync.
    """
    table = state.extras.get(_JOB_METADATA_KEY)
    if isinstance(table, dict):
        return table
    new: dict[str, dict[str, Any]] = {}
    state.extras[_JOB_METADATA_KEY] = new
    # Best-effort mirror onto AppState so the live builtin sees the
    # same dict. The AppState attach is non-fatal — degraded boots
    # without an AppState bundle still serve the routes.
    app_state = state.extras.get("app_state")
    if app_state is not None:
        try:
            app_state.scheduler_job_metadata = new
        except Exception:  # pragma: no cover — degraded boot tolerant
            pass
    return new


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _action_kind(action: Any) -> str:
    """Best-effort mapper from the config job-action dict to the
    Rust-side string label (`run_agent` / `run_tool` / `subprocess`)."""
    if isinstance(action, dict):
        for key in ("run_agent", "run_tool", "subprocess"):
            if key in action:
                return key
        if "kind" in action and isinstance(action["kind"], str):
            return action["kind"]
    return "unknown"


def _list_jobs_from_config(cfg: dict[str, Any]) -> list[JobOut]:
    out: list[JobOut] = []
    sched = cfg.get("scheduler") if isinstance(cfg, dict) else None
    jobs = (sched or {}).get("jobs") or []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        out.append(
            JobOut(
                name=str(j.get("name", "")),
                cron=str(j.get("cron", "")),
                timezone=j.get("timezone"),
                action_kind=_action_kind(j.get("action")),
                next_fire_at=None,
                last_status=None,
                source="config",
            )
        )
    return out


def _runtime_job_to_out(rj: _RuntimeJob) -> JobOut:
    """Serialise an in-memory runtime job to the wire shape."""
    return JobOut(
        name=rj.name,
        cron=rj.cron,
        timezone=rj.timezone,
        action_kind=_action_kind_for_runtime(rj.action_type),
        next_fire_at=None,
        last_status=_runtime_last_status(rj),
        action_type=rj.action_type,
        enabled=rj.enabled,
        persona_id=rj.persona_id,
        prompt_template=rj.prompt_template,
        qq_account=rj.qq_account,
        last_run_at_ms=rj.last_run_at_ms,
        last_run_ok=rj.last_run_ok,
        last_qzone_url=rj.last_qzone_url,
        last_error=rj.last_error,
        source="runtime",
    )


def _action_kind_for_runtime(action_type: str) -> str:
    """Map an action_type slug to the legacy ``action_kind`` discriminant
    so the existing UI rows still render with a meaningful badge."""
    if action_type == QZONE_DAILY_BUILTIN_NAME:
        return "run_tool"
    return action_type or "unknown"


def _runtime_last_status(rj: _RuntimeJob) -> str | None:
    if rj.last_run_ok is None:
        return None
    return "ok" if rj.last_run_ok else "error"


def _list_runtime_jobs(state: AdminState) -> list[JobOut]:
    rows = _runtime_jobs(state)
    return [_runtime_job_to_out(rj) for rj in rows.values()]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_cron(expr: str) -> tuple[bool, str | None]:
    """Round-trip ``expr`` through ``scheduler.cron.parse``.

    Returns ``(ok, error_message)``. We swallow the parse failure into
    a string so the route layer can return a 422 with a clean body
    instead of leaking the parser's exception type.
    """
    try:
        from corlinman_server.scheduler.cron import parse as cron_parse
    except Exception as exc:  # pragma: no cover — defensive
        return False, f"cron_parser_unavailable: {exc!r}"
    try:
        cron_parse(expr)
    except Exception as exc:  # noqa: BLE001 — surface as 422
        return False, str(exc)
    return True, None


def _validate_qzone_daily(body: NewJobBody) -> tuple[bool, str | None]:
    """Type-specific gate for ``qzone.daily_publish`` jobs."""
    if not body.persona_id or not body.persona_id.strip():
        return False, "persona_id is required for qzone.daily_publish"
    if not body.prompt_template or not body.prompt_template.strip():
        return False, "prompt_template is required for qzone.daily_publish"
    return True, None


def _store_job(state: AdminState, body: NewJobBody) -> _RuntimeJob:
    """Idempotent upsert into the runtime overlay.

    Re-submitting the same ``name`` updates the existing row in place
    (preserves ``created_at_ms`` and the last-run summary fields). This
    is the contract the qzone-template enable route relies on.
    """
    table = _runtime_jobs(state)
    now = _now_ms()
    existing = table.get(body.name)
    if existing is not None:
        existing.cron = body.cron
        existing.timezone = body.timezone
        existing.action_type = body.action_type
        existing.enabled = body.enabled
        existing.persona_id = body.persona_id
        existing.prompt_template = body.prompt_template
        existing.qq_account = body.qq_account
        # Merge metadata with the per-action_type fields so the
        # dispatcher's metadata resolver sees one consolidated dict.
        existing.metadata = _compose_metadata(body)
        existing.updated_at_ms = now
        _sync_metadata(state, existing)
        return existing

    job = _RuntimeJob(
        name=body.name,
        cron=body.cron,
        action_type=body.action_type,
        timezone=body.timezone,
        enabled=body.enabled,
        persona_id=body.persona_id,
        prompt_template=body.prompt_template,
        qq_account=body.qq_account,
        metadata=_compose_metadata(body),
        created_at_ms=now,
        updated_at_ms=now,
    )
    table[body.name] = job
    _sync_metadata(state, job)
    return job


def _compose_metadata(body: NewJobBody) -> dict[str, Any]:
    """Roll the per-action_type fields into the metadata dict the
    builtin's :func:`_resolve_metadata` reads."""
    composed: dict[str, Any] = dict(body.metadata or {})
    if body.persona_id is not None:
        composed.setdefault("persona_id", body.persona_id)
    if body.prompt_template is not None:
        composed.setdefault("prompt_template", body.prompt_template)
    if body.qq_account is not None:
        composed.setdefault("qq_account", body.qq_account)
    return composed


def _sync_metadata(state: AdminState, rj: _RuntimeJob) -> None:
    """Mirror the runtime job's metadata into the per-job metadata table
    that the qzone-daily builtin reads."""
    _job_metadata(state)[rj.name] = dict(rj.metadata)


# ---------------------------------------------------------------------------
# Grantley template loader
# ---------------------------------------------------------------------------


def _bundled_template_path(state: AdminState, template_id: str) -> Path | None:
    """Resolve ``<DATA_DIR>/bundled_personas/<id>/daily_job.json``.

    The seeded copy under the data dir wins because operators may edit
    it; the in-wheel default is the fallback. Returns ``None`` when no
    candidate exists on disk.
    """
    candidates: list[Path] = []
    data_dir = state.data_dir
    if data_dir is not None:
        candidates.append(
            data_dir / "bundled_personas" / template_id / "daily_job.json"
        )
    # In-wheel fallback for tests / deployments that haven't run the
    # first-boot seeder yet.
    try:
        from importlib.resources import as_file, files

        traversable = files("corlinman_server.bundled_personas") / template_id / "daily_job.json"
        try:
            with as_file(traversable) as p:
                wheel_path = Path(p)
        except (FileNotFoundError, OSError):
            wheel_path = None
        if wheel_path is not None:
            candidates.append(wheel_path)
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        pass
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _load_template_body(path: Path) -> NewJobBody:
    """Parse a ``daily_job.json`` template into a :class:`NewJobBody`."""
    raw = path.read_text(encoding="utf-8")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("template must be a JSON object")
    return NewJobBody(
        name=str(obj.get("name") or ""),
        cron=str(obj.get("cron") or ""),
        action_type=str(obj.get("action_type") or QZONE_DAILY_BUILTIN_NAME),
        timezone=obj.get("timezone"),
        enabled=bool(obj.get("enabled", False)),
        persona_id=obj.get("persona_id"),
        prompt_template=obj.get("prompt_template"),
        qq_account=obj.get("qq_account"),
        metadata=obj.get("metadata", {}) or {},
    )


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


async def _trigger_runtime_qzone_daily(
    state: AdminState,
    rj: _RuntimeJob,
    history: SchedulerHistory,
) -> Any:
    """Fire a runtime ``qzone.daily_publish`` job in-process.

    Builds a fresh :class:`BuiltinContext` carrying ``state.extras
    ['app_state']`` (mirroring what the scheduler tick loop would pass)
    plus the job's metadata, dispatches the registered builtin, and
    folds the audit dict into the history ring buffer + the
    runtime-job's per-row summary fields.
    """
    from corlinman_server.scheduler.builtins import (
        BuiltinContext,
        run_builtin,
    )

    # Mirror metadata into the per-job table the builtin reads. Already
    # synced at create-time, but this is cheap insurance against an
    # operator who poked the dict directly.
    _job_metadata(state)[rj.name] = dict(rj.metadata)

    app_state = state.extras.get("app_state")
    # The builtin probes ``app_state.scheduler_job_metadata`` to find
    # ``metadata`` by name — wire the live table onto AppState now.
    if app_state is not None:
        try:
            app_state.scheduler_job_metadata = _job_metadata(state)
        except Exception:  # pragma: no cover — degraded boot tolerant
            pass

    ctx = BuiltinContext(app_state=app_state, admin_state=state, name=rj.name)
    result = await run_builtin(rj.action_type, ctx)

    now_iso = _now_iso()
    ok = bool(result.get("ok"))
    rj.last_run_at_ms = _now_ms()
    rj.last_run_ok = ok
    rj.last_qzone_url = result.get("qzone_url") if ok else None
    rj.last_error = None if ok else (result.get("error") or "unknown")

    status_word = "ok" if ok else "error"
    message_bits: list[str] = []
    if ok:
        if result.get("tid"):
            message_bits.append(f"tid={result['tid']}")
        if result.get("qzone_url"):
            message_bits.append(f"url={result['qzone_url']}")
    else:
        err = result.get("error") or "unknown"
        message_bits.append(f"error={err}")
        if result.get("message"):
            message_bits.append(str(result["message"]))
    history.push(
        HistoryEntry(
            job=rj.name,
            at=now_iso,
            source="manual",
            status=status_word,
            message="; ".join(message_bits) or "qzone.daily_publish ran",
        )
    )
    return {"ok": ok, "result": result, "job": _runtime_job_to_out(rj).model_dump()}


# ---------------------------------------------------------------------------
# Pure helper for tests — exposed so the test module can stamp records
# directly without depending on the dataclass internals.
# ---------------------------------------------------------------------------


def make_history_entry(job: str, status: str, source: str = "manual", message: str = "") -> HistoryEntry:
    return HistoryEntry(
        job=job,
        at=datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        source=source,
        status=status,
        message=message,
    )
