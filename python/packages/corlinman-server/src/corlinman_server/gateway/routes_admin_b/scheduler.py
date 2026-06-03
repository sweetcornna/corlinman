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
from datetime import UTC, datetime
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

#: Sentinel slot recording that the runtime-job overlay has already been
#: rehydrated from the on-disk sidecar this process. Without it the lazy
#: rehydrate would re-read the sidecar (and clobber in-flight edits) on
#: every ``_runtime_jobs`` access.
_RUNTIME_JOBS_LOADED_KEY: str = "scheduler_runtime_jobs_loaded"

#: Filename of the runtime-job persistence sidecar under ``data_dir``.
#: A plain JSON file (not the config TOML) so admin-created jobs survive
#: a restart without us having to rewrite — and risk clobbering — the
#: operator's hand-authored ``corlinman.toml``. Loaded back into the
#: overlay + re-registered on the live :class:`SchedulerHandle` at boot.
_RUNTIME_JOBS_FILE: str = "scheduler_runtime_jobs.json"

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


class EditJobBody(BaseModel):
    """Body shape for ``PATCH /admin/scheduler/jobs/{name}``.

    Every field is optional — only the ones present are applied. ``name``
    is taken from the path (a runtime job's name is its identity, so it
    is not editable here). ``action_type`` may be changed but is
    re-validated the same way the create route validates it.
    """

    cron: str | None = None
    action_type: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    persona_id: str | None = None
    prompt_template: str | None = None
    qq_account: str | None = None
    metadata: dict[str, Any] | None = None


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
    never have to remember to seed it. On the *first* access of a
    process the on-disk sidecar (:data:`_RUNTIME_JOBS_FILE`) is
    rehydrated into it so admin-created jobs survive a restart. The
    rehydrate runs exactly once (guarded by
    :data:`_RUNTIME_JOBS_LOADED_KEY`) so an in-flight edit is never
    clobbered by a re-read.
    """
    table = state.extras.get(_RUNTIME_JOBS_KEY)
    if not isinstance(table, dict):
        table = {}
        state.extras[_RUNTIME_JOBS_KEY] = table
    if not state.extras.get(_RUNTIME_JOBS_LOADED_KEY):
        state.extras[_RUNTIME_JOBS_LOADED_KEY] = True
        _rehydrate_runtime_jobs(state, table)
    return table


def _runtime_jobs_path(state: AdminState) -> Path | None:
    """Resolve ``<data_dir>/scheduler_runtime_jobs.json`` (or ``None``
    when no data dir is wired — the overlay then lives in memory only)."""
    if state.data_dir is None:
        return None
    return state.data_dir / _RUNTIME_JOBS_FILE


def _rehydrate_runtime_jobs(
    state: AdminState, table: dict[str, _RuntimeJob]
) -> None:
    """Load persisted runtime jobs from the sidecar into ``table``.

    Best-effort + fully defensive: a missing / unreadable / malformed
    file leaves the overlay empty so a corrupt sidecar never blocks the
    admin surface. Each loaded row also re-syncs its metadata so the
    qzone builtin's per-job metadata map is repopulated on boot, and
    (when a live scheduler handle is attached) re-registers an *enabled*
    job's tick loop so it actually fires after the restart.
    """
    path = _runtime_jobs_path(state)
    if path is None:
        return
    try:
        if not path.is_file():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    rows = raw.get("jobs") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not _JOB_NAME_RE.match(name):
            continue
        rj = _RuntimeJob(
            name=name,
            cron=str(entry.get("cron", "")),
            action_type=str(entry.get("action_type", "")),
            timezone=entry.get("timezone"),
            enabled=bool(entry.get("enabled", True)),
            persona_id=entry.get("persona_id"),
            prompt_template=entry.get("prompt_template"),
            qq_account=entry.get("qq_account"),
            metadata=dict(entry.get("metadata") or {}),
            last_run_at_ms=entry.get("last_run_at_ms"),
            last_run_ok=entry.get("last_run_ok"),
            last_qzone_url=entry.get("last_qzone_url"),
            last_error=entry.get("last_error"),
            created_at_ms=int(entry.get("created_at_ms") or 0),
            updated_at_ms=int(entry.get("updated_at_ms") or 0),
        )
        table[name] = rj
        _sync_metadata(state, rj)
        if rj.enabled:
            _register_runtime_loop(state, rj)


def _persist_runtime_jobs(state: AdminState) -> None:
    """Write the runtime-job overlay to the on-disk sidecar.

    Best-effort: a write failure logs nothing and never propagates —
    persistence is durability insurance, not load-bearing for the
    in-process overlay which already reflects the mutation. Uses the
    same atomic ``write tmp + replace`` dance the config writer uses so
    a crash mid-write can't truncate the sidecar.
    """
    path = _runtime_jobs_path(state)
    if path is None:
        return
    rows = [_runtime_job_to_dict(rj) for rj in _runtime_jobs(state).values()]
    payload = json.dumps({"version": 1, "jobs": rows}, ensure_ascii=False, indent=2)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".new")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return


def _runtime_job_to_dict(rj: _RuntimeJob) -> dict[str, Any]:
    """Serialise a runtime job to the sidecar's JSON row shape."""
    return {
        "name": rj.name,
        "cron": rj.cron,
        "action_type": rj.action_type,
        "timezone": rj.timezone,
        "enabled": rj.enabled,
        "persona_id": rj.persona_id,
        "prompt_template": rj.prompt_template,
        "qq_account": rj.qq_account,
        "metadata": dict(rj.metadata),
        "last_run_at_ms": rj.last_run_at_ms,
        "last_run_ok": rj.last_run_ok,
        "last_qzone_url": rj.last_qzone_url,
        "last_error": rj.last_error,
        "created_at_ms": rj.created_at_ms,
        "updated_at_ms": rj.updated_at_ms,
    }


def _scheduler_handle(state: AdminState) -> Any | None:
    """Resolve the live :class:`SchedulerHandle`, if one is attached.

    Prefer the explicit ``state.scheduler`` slot the lifespan wires; fall
    back to the ``app_state.corlinman_scheduler_handle`` the boot path
    publishes. ``None`` keeps every registration call a safe no-op (the
    common dev / test case with no live scheduler runtime)."""
    handle = state.scheduler
    if handle is not None:
        return handle
    app_state = state.extras.get("app_state")
    if app_state is not None:
        return getattr(app_state, "corlinman_scheduler_handle", None)
    return None


def _register_runtime_loop(state: AdminState, rj: _RuntimeJob) -> None:
    """Register (or re-register) a runtime job's live tick loop.

    No-op when no scheduler handle is attached, when the handle lacks the
    ``register`` method (older handle shape), or when the job's cron /
    action_type can't be mapped to a runnable spec. Re-syncs metadata
    first so the qzone builtin sees the current persona/prompt at the
    next firing. Fully best-effort — a registration failure never blocks
    the admin mutation that triggered it.
    """
    handle = _scheduler_handle(state)
    if handle is None or not hasattr(handle, "register"):
        return
    _sync_metadata(state, rj)
    try:
        from corlinman_server.scheduler import runtime_job_spec
    except Exception:  # pragma: no cover — defensive
        return
    spec = runtime_job_spec(rj.name, rj.cron, rj.action_type)
    if spec is None:
        return
    try:
        handle.register(spec)
    except Exception:  # noqa: BLE001 — best-effort; mutation already applied
        return


def _unregister_runtime_loop(state: AdminState, name: str) -> None:
    """Cancel a runtime job's live tick loop (pause / disable / delete).

    No-op when no handle is attached or it lacks ``unregister``."""
    handle = _scheduler_handle(state)
    if handle is None or not hasattr(handle, "unregister"):
        return
    try:
        handle.unregister(name)
    except Exception:  # noqa: BLE001 — best-effort
        return


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
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
        _apply_enabled_state(state, existing)
        _persist_runtime_jobs(state)
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
    _apply_enabled_state(state, job)
    _persist_runtime_jobs(state)
    return job


def _apply_enabled_state(state: AdminState, rj: _RuntimeJob) -> None:
    """Reconcile the live tick loop with the job's ``enabled`` flag.

    ``enabled`` jobs (re)register a tick loop — re-registering also picks
    up an edited cron without a duplicate firing because
    :meth:`SchedulerHandle.register` tears the old loop down first.
    ``enabled=false`` jobs unregister their loop so a paused job stops
    firing. This is the gate that makes ``enabled`` actually mean
    something rather than being a cosmetic flag.
    """
    if rj.enabled:
        _register_runtime_loop(state, rj)
    else:
        _unregister_runtime_loop(state, rj.name)


def _set_enabled_route(name: str, *, enabled: bool) -> Any:
    """Shared pause/resume body. Flips a runtime job's ``enabled`` flag,
    reconciles its live tick loop, and re-persists the sidecar.

    Returns the refreshed :class:`JobOut` (200) or a typed error
    envelope. A resume re-validates the cron + qzone args so a job that
    was paused while broken can't resume into a loop that never fires."""
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
    if enabled:
        ok, err = _validate_cron(rj.cron or "")
        if not ok:
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_cron", "message": err or ""},
            )
        if rj.action_type == QZONE_DAILY_BUILTIN_NAME:
            ok, err = _validate_qzone_daily(
                NewJobBody(
                    name=rj.name,
                    cron=rj.cron,
                    action_type=rj.action_type,
                    persona_id=rj.persona_id,
                    prompt_template=rj.prompt_template,
                )
            )
            if not ok:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": "invalid_qzone_daily_args",
                        "message": err or "",
                    },
                )
    rj.enabled = enabled
    rj.updated_at_ms = _now_ms()
    _apply_enabled_state(state, rj)
    _persist_runtime_jobs(state)
    return _runtime_job_to_out(rj)


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


def rehydrate_runtime_jobs_on_boot(state: AdminState) -> int:
    """Boot hook — load the persisted runtime-job overlay from the sidecar
    and register every enabled job's tick loop onto the live handle.

    The gateway lifespan calls this *after* it has published the live
    :class:`SchedulerHandle` onto ``state.scheduler`` + wired
    ``state.extras['app_state']``, so :func:`_register_runtime_loop` can
    resolve the handle and :func:`_sync_metadata` can mirror onto the
    AppState. Returns the count of jobs loaded (handy for boot logging /
    tests). Idempotent — a second call is a no-op once the overlay is
    marked loaded.

    The heavy lifting is :func:`_runtime_jobs`'s lazy rehydrate; this is
    the explicit, named entrypoint so the lifespan doesn't reach into a
    private helper.
    """
    table = _runtime_jobs(state)
    return len(table)


def make_history_entry(job: str, status: str, source: str = "manual", message: str = "") -> HistoryEntry:
    return HistoryEntry(
        job=job,
        at=datetime.fromtimestamp(time.time(), tz=UTC).isoformat().replace("+00:00", "Z"),
        source=source,
        status=status,
        message=message,
    )
