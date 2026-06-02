"""``/admin/evolution*`` — EvolutionLoop proposal queue admin endpoints.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/evolution.rs``.

Seven routes — including the two ``EvolutionApplier`` paths, ``/apply``
and ``/rollback``, now wired to the real
:class:`corlinman_auto_rollback.EvolutionApplier`:

* ``GET  /admin/evolution``                — list proposals filtered by
  ``?status=pending&limit=50`` (defaults: ``pending``, 50, max 200).
* ``GET  /admin/evolution/budget``         — per-kind weekly quota snapshot
  (the engine + UI both consume the same wire shape).
* ``GET  /admin/evolution/history``        — terminal-state (applied /
  rolled_back) audit rows joined against the proposals table so the
  History tab can render baseline metrics + shadow metrics in one round
  trip.
* ``GET  /admin/evolution/{id}``           — single proposal detail.
* ``POST /admin/evolution/{id}/approve``   — body ``{"decided_by": "..."}``.
  Transitions ``pending|shadow_done → approved``.
* ``POST /admin/evolution/{id}/deny``      — body ``{"decided_by", "reason"}``.
  Transitions ``pending|shadow_done → denied``; deny reason is appended
  to ``reasoning`` with a ``[DENIED: ...]`` prefix.
* ``POST /admin/evolution/{id}/apply``     — drive
  :meth:`EvolutionApplier.apply`. Transitions an ``approved`` proposal
  to ``applied``, writes the ``evolution_history`` audit row, and opens
  / closes an ``apply_intent_log`` ticket. Typed ``ApplyError`` variants
  map onto 404 / 409 ``invalid_state_transition`` / 400
  ``unsupported_kind`` / 500 ``apply_failed`` envelopes.
* ``POST /admin/evolution/{id}/rollback``  — drive
  :meth:`EvolutionApplier.revert`. Transitions an ``applied`` proposal
  back to its captured pre-apply status and stamps the rollback audit
  fields. Maps onto 404 / 409 / 410 ``history_missing`` / 400
  ``unsupported_revert_kind`` / 500 ``rollback_failed`` envelopes.

When the evolution store is wired but the applier package cannot be
imported, both routes 503 with ``applier_unavailable`` (distinct from
the global ``evolution_disabled``) so the UI can tell the two apart.

### State machine

Illegal transitions return **409 Conflict** with
``{"error": "invalid_state_transition", "from": "...", "to": "..."}``.

```text
pending ─┐
         ├─► approved ──► applied
shadow_done ─┘   │
                 └─► denied
```

### Disabled mode

When ``AdminState.evolution_store`` is ``None`` every route 503s with
``{"error": "evolution_disabled", ...}`` — same UX as the Rust gate so
the admin UI can render a single subsystem-off banner.

### Meta-approver gate

Phase 4 W2 B1 iter 5: meta kinds (``engine_config`` / ``engine_prompt``
/ ``observer_filter`` / ``cluster_threshold``) require the ``decided_by``
identifier to appear in ``[admin].meta_approver_users``. Non-meta kinds
short-circuit. Empty allow-list (the config default) means **no one**
can approve meta — operators MUST opt in by listing the user explicitly.
Returns 403 ``meta_approver_required`` with ``{user, kind}`` otherwise.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)

# ---------------------------------------------------------------------------
# Constants (mirror the Rust DEFAULT_LIMIT / MAX_LIMIT)
# ---------------------------------------------------------------------------

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Phase 4 W2 B1: meta kinds that need a vetted operator. Mirrors
# ``EvolutionKind::is_meta`` on the typed enum — duplicated here as a
# plain set so the route can stay importable even when
# ``corlinman_evolution_store`` is not installed (defensive: lazy
# imports below preserve the same "503 disabled" UX as the Rust gate).
META_KINDS = frozenset(
    {
        "engine_config",
        "engine_prompt",
        "observer_filter",
        "cluster_threshold",
    }
)

# Statuses from which approve/deny are allowed.
_DECIDABLE_STATUSES = frozenset({"pending", "shadow_done"})


# ---------------------------------------------------------------------------
# Wire shapes (pydantic v2)
# ---------------------------------------------------------------------------


class ProposalOut(BaseModel):
    """Wire-projection of one proposal row. Mirrors the Rust
    ``ProposalOut`` struct field-for-field so existing UI clients
    don't notice the language switch."""

    id: str
    kind: str
    target: str
    diff: str
    reasoning: str
    risk: str
    budget_cost: int
    status: str
    shadow_metrics: Any | None = None
    signal_ids: list[int] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)
    created_at: int
    decided_at: int | None = None
    decided_by: str | None = None
    applied_at: int | None = None
    rollback_of: str | None = None
    eval_run_id: str | None = None
    baseline_metrics_json: Any | None = None
    auto_rollback_at: int | None = None
    auto_rollback_reason: str | None = None


class ApproveBody(BaseModel):
    decided_by: str


class DenyBody(BaseModel):
    decided_by: str
    reason: str | None = None


class RollbackBody(BaseModel):
    reason: str | None = None


class DecisionResponse(BaseModel):
    id: str
    status: str


class BudgetKindRow(BaseModel):
    kind: str
    limit: int
    used: int
    remaining: int


class BudgetTotal(BaseModel):
    limit: int
    used: int
    remaining: int


class BudgetSnapshot(BaseModel):
    enabled: bool
    window_start_ms: int
    window_end_ms: int
    weekly_total: BudgetTotal
    per_kind: list[BudgetKindRow]


class HistoryEntryOut(BaseModel):
    proposal_id: str
    kind: str
    target: str
    risk: str
    status: str
    applied_at: int
    rolled_back_at: int | None = None
    rollback_reason: str | None = None
    auto_rollback_reason: str | None = None
    metrics_baseline: Any
    shadow_metrics: Any | None = None
    baseline_metrics_json: Any | None = None
    before_sha: str
    after_sha: str
    eval_run_id: str | None = None
    reasoning: str


# ---------------------------------------------------------------------------
# Settings wire shapes (GET/PUT /admin/evolution/settings)
# ---------------------------------------------------------------------------
#
# The meta-approver allow-list (``[admin].meta_approver_users``) plus the
# budget (``[evolution.budget]``) and auto-rollback (``[evolution.auto_rollback]``)
# tunables all live in the config TOML, but there was no admin surface to
# edit them — so the empty default of ``meta_approver_users`` 403'd every
# meta approval out of the box with no way to opt anyone in. These two
# routes give the UI a focused read/write seam over just those three
# sections, persisting through the same atomic config-write path
# ``/admin/config`` uses (``admin_write_lock`` + temp-file replace +
# ``config_swap_fn`` publish + py-config re-emit).


class AutoRollbackThresholdsModel(BaseModel):
    default_err_rate_delta_pct: float = 0.0
    default_p95_latency_delta_pct: float = 0.0
    signal_window_secs: int = 0
    min_baseline_signals: int = 0


class AutoRollbackSettings(BaseModel):
    enabled: bool = False
    grace_window_hours: int = 72
    thresholds: AutoRollbackThresholdsModel = Field(
        default_factory=AutoRollbackThresholdsModel
    )


class BudgetSettings(BaseModel):
    enabled: bool = False
    weekly_total: int = 0
    per_kind: dict[str, int] = Field(default_factory=dict)


class EvolutionSettings(BaseModel):
    meta_approver_users: list[str] = Field(default_factory=list)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    auto_rollback: AutoRollbackSettings = Field(default_factory=AutoRollbackSettings)


class PutSettingsResponse(BaseModel):
    status: str  # "ok"
    settings: EvolutionSettings


# ---------------------------------------------------------------------------
# Error envelopes (mirror the Rust JSON shapes byte-for-byte)
# ---------------------------------------------------------------------------


def _evolution_disabled() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "evolution_disabled",
            "message": "evolution proposal queue is not configured on this gateway",
        },
    )


def _applier_unavailable() -> JSONResponse:
    """The :class:`EvolutionApplier` could not be constructed even though
    the evolution store is wired — the ``corlinman_auto_rollback``
    package is not importable in this environment. Distinguished from
    ``evolution_disabled`` (store missing) so the UI can tell the two
    apart; degrades gracefully instead of crashing the route."""
    return JSONResponse(
        status_code=503,
        content={
            "error": "applier_unavailable",
            "message": (
                "evolution applier could not be loaded on this gateway; "
                "the corlinman-auto-rollback package is not installed"
            ),
        },
    )


def _unsupported_kind(kind: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": "unsupported_kind",
            "kind": kind,
            "message": "no forward handler for this kind yet",
        },
    )


def _unsupported_revert_kind(kind: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": "unsupported_revert_kind",
            "kind": kind,
            "message": "no inverse handler for this kind yet",
        },
    )


def _history_missing(proposal_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=410,
        content={
            "error": "history_missing",
            "proposal_id": proposal_id,
            "message": (
                "evolution_history row missing for this proposal; "
                "cannot revert without an inverse_diff"
            ),
        },
    )


def _apply_failed(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "apply_failed", "message": message},
    )


def _rollback_failed(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "rollback_failed", "message": message},
    )


def _invalid_state_transition(from_status: str, to_status: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "invalid_state_transition",
            "from": from_status,
            "to": to_status,
        },
    )


def _not_found(id_: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": "not_found",
            "resource": "evolution_proposal",
            "id": id_,
        },
    )


def _invalid_status(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "invalid_status", "message": message},
    )


def _storage_error(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "storage_error", "message": message},
    )


def _meta_approver_required(user: str, kind: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": "meta_approver_required",
            "user": user,
            "kind": kind,
        },
    )


def _config_path_unset() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "config_path_unset",
            "message": "gateway booted without a config file path",
        },
    )


def _settings_write_failed(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "write_failed", "message": message},
    )


# ---------------------------------------------------------------------------
# Helpers — lazy import + adapter
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """Unix milliseconds. Matches the Rust ``now_ms`` helper."""
    return int(time.time() * 1000)


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def _resolve_connection(store: Any) -> Any:
    """The Python ``EvolutionStore`` exposes its underlying
    ``aiosqlite.Connection`` via the ``conn`` property; older / mock
    stores may use ``connection`` or be a raw connection themselves.
    Accept all three so the routes don't depend on the exact handle
    shape — mirrors the same try-ladder in :mod:`.memory`.
    """
    return getattr(store, "conn", None) or getattr(store, "connection", None) or store


def _auto_rollback_config(state: AdminState) -> Any:
    """Build the :class:`EvolutionAutoRollbackConfig` the operator apply
    route threads into the :class:`EvolutionApplier`.

    BUG-08-wire: without it the applier captures the apply-time metrics
    baseline over the conservative *default* ``signal_window_secs``,
    which the AutoRollback monitor (which re-samples over the
    operator-configured window) then diffs asymmetrically → false
    breaches. Sourcing the config from the live ``[evolution.auto_rollback]``
    snapshot — the same section the scheduled CLI reads via
    ``corlinman_auto_rollback.cli._load_auto_rollback_config`` — keeps
    the two windows symmetric.

    Returns ``None`` when the auto-rollback package can't be imported so
    the caller falls back to the bare constructor (still a valid, if
    default-windowed, baseline). Missing / malformed config sections
    collapse to the package defaults, mirroring the CLI loader.
    """
    try:
        from corlinman_auto_rollback import (  # noqa: PLC0415
            AutoRollbackThresholds,
            EvolutionAutoRollbackConfig,
        )
    except ImportError:
        return None

    cfg = config_snapshot(state)
    evolution = cfg.get("evolution") if isinstance(cfg, dict) else None
    ar = evolution.get("auto_rollback") if isinstance(evolution, dict) else None
    if not isinstance(ar, dict):
        return EvolutionAutoRollbackConfig()

    th = AutoRollbackThresholds()
    th_raw = ar.get("thresholds")
    if isinstance(th_raw, dict):
        try:
            th = AutoRollbackThresholds(
                default_err_rate_delta_pct=float(
                    th_raw.get(
                        "default_err_rate_delta_pct", th.default_err_rate_delta_pct
                    )
                ),
                default_p95_latency_delta_pct=float(
                    th_raw.get(
                        "default_p95_latency_delta_pct",
                        th.default_p95_latency_delta_pct,
                    )
                ),
                signal_window_secs=int(
                    th_raw.get("signal_window_secs", th.signal_window_secs)
                ),
                min_baseline_signals=int(
                    th_raw.get("min_baseline_signals", th.min_baseline_signals)
                ),
            )
        except (TypeError, ValueError):
            # A malformed knob falls back to the conservative defaults
            # rather than 500ing the apply — the baseline shape stays
            # valid either way.
            th = AutoRollbackThresholds()

    try:
        grace = int(ar.get("grace_window_hours", 72))
    except (TypeError, ValueError):
        grace = 72
    return EvolutionAutoRollbackConfig(
        enabled=bool(ar.get("enabled", False)),
        grace_window_hours=grace,
        thresholds=th,
    )


def _project_proposal(p: Any) -> ProposalOut:
    """Map a typed :class:`EvolutionProposal` (from
    :mod:`corlinman_evolution_store`) onto the wire envelope. Defensive
    against missing attributes so the projection survives schema drift
    (extra columns on the source struct are dropped silently)."""
    shadow_metrics = getattr(p, "shadow_metrics", None)
    if shadow_metrics is not None:
        # ShadowMetrics is a dataclass with a single ``data`` dict
        # attribute; emit just the dict on the wire to match the Rust
        # ``serde_json::to_value(MetricsSnapshot)`` projection.
        data = getattr(shadow_metrics, "data", None)
        shadow_metrics = data if data is not None else shadow_metrics

    kind = getattr(p, "kind", "")
    risk = getattr(p, "risk", "")
    status = getattr(p, "status", "")
    rollback_of = getattr(p, "rollback_of", None)

    return ProposalOut(
        id=str(getattr(p, "id", "")),
        kind=kind.as_str() if hasattr(kind, "as_str") else str(kind),
        target=str(getattr(p, "target", "")),
        diff=str(getattr(p, "diff", "")),
        reasoning=str(getattr(p, "reasoning", "")),
        risk=risk.as_str() if hasattr(risk, "as_str") else str(risk),
        budget_cost=int(getattr(p, "budget_cost", 0)),
        status=status.as_str() if hasattr(status, "as_str") else str(status),
        shadow_metrics=shadow_metrics,
        signal_ids=list(getattr(p, "signal_ids", []) or []),
        trace_ids=list(getattr(p, "trace_ids", []) or []),
        created_at=int(getattr(p, "created_at", 0)),
        decided_at=getattr(p, "decided_at", None),
        decided_by=getattr(p, "decided_by", None),
        applied_at=getattr(p, "applied_at", None),
        rollback_of=str(rollback_of) if rollback_of else None,
        eval_run_id=getattr(p, "eval_run_id", None),
        baseline_metrics_json=getattr(p, "baseline_metrics_json", None),
        auto_rollback_at=getattr(p, "auto_rollback_at", None),
        auto_rollback_reason=getattr(p, "auto_rollback_reason", None),
    )


def _assert_meta_approver(
    state: AdminState, kind_str: str, decided_by: str
) -> JSONResponse | None:
    """Phase 4 W2 B1 iter 5 gate. Returns ``None`` when the call is
    allowed, otherwise the 403 envelope to short-circuit with."""
    if kind_str not in META_KINDS:
        return None
    cfg = config_snapshot(state)
    admin_cfg = cfg.get("admin") if isinstance(cfg, dict) else None
    allow_list: list[str] = []
    if isinstance(admin_cfg, dict):
        raw = admin_cfg.get("meta_approver_users") or []
        if isinstance(raw, list):
            allow_list = [str(u) for u in raw]
    if decided_by in allow_list:
        return None
    return _meta_approver_required(decided_by, kind_str)


def _decidable(status_str: str) -> bool:
    return status_str in _DECIDABLE_STATUSES


def _read_settings(state: AdminState) -> EvolutionSettings:
    """Project the live config snapshot onto :class:`EvolutionSettings`.

    Defensive against missing / malformed sections so a partially
    configured (or empty) snapshot collapses to the model defaults
    rather than raising — mirrors the read-side leniency the budget
    route already relies on."""
    cfg = config_snapshot(state)
    admin_cfg = cfg.get("admin") if isinstance(cfg, dict) else None
    evo_cfg = cfg.get("evolution") if isinstance(cfg, dict) else None

    approvers: list[str] = []
    if isinstance(admin_cfg, dict):
        raw = admin_cfg.get("meta_approver_users")
        if isinstance(raw, list):
            approvers = [str(u) for u in raw]

    budget_raw = evo_cfg.get("budget") if isinstance(evo_cfg, dict) else None
    budget = BudgetSettings()
    if isinstance(budget_raw, dict):
        per_kind: dict[str, int] = {}
        raw_pk = budget_raw.get("per_kind")
        if isinstance(raw_pk, dict):
            for k, v in raw_pk.items():
                try:
                    per_kind[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        try:
            weekly_total = int(budget_raw.get("weekly_total", 0))
        except (TypeError, ValueError):
            weekly_total = 0
        budget = BudgetSettings(
            enabled=bool(budget_raw.get("enabled", False)),
            weekly_total=weekly_total,
            per_kind=per_kind,
        )

    ar_raw = evo_cfg.get("auto_rollback") if isinstance(evo_cfg, dict) else None
    auto_rollback = AutoRollbackSettings()
    if isinstance(ar_raw, dict):
        th = AutoRollbackThresholdsModel()
        th_raw = ar_raw.get("thresholds")
        if isinstance(th_raw, dict):
            try:
                th = AutoRollbackThresholdsModel(
                    default_err_rate_delta_pct=float(
                        th_raw.get("default_err_rate_delta_pct", 0.0)
                    ),
                    default_p95_latency_delta_pct=float(
                        th_raw.get("default_p95_latency_delta_pct", 0.0)
                    ),
                    signal_window_secs=int(th_raw.get("signal_window_secs", 0)),
                    min_baseline_signals=int(th_raw.get("min_baseline_signals", 0)),
                )
            except (TypeError, ValueError):
                th = AutoRollbackThresholdsModel()
        try:
            grace = int(ar_raw.get("grace_window_hours", 72))
        except (TypeError, ValueError):
            grace = 72
        auto_rollback = AutoRollbackSettings(
            enabled=bool(ar_raw.get("enabled", False)),
            grace_window_hours=grace,
            thresholds=th,
        )

    return EvolutionSettings(
        meta_approver_users=approvers,
        budget=budget,
        auto_rollback=auto_rollback,
    )


def _apply_settings(cfg: dict[str, Any], settings: EvolutionSettings) -> dict[str, Any]:
    """Return a deep-ish copy of ``cfg`` with the three managed sections
    overwritten from ``settings``. Only the keys this surface owns are
    touched — every other key in ``[admin]`` / ``[evolution]`` is
    preserved so we don't clobber unrelated config the operator set via
    the raw ``/admin/config`` editor."""
    import copy as _copy  # noqa: PLC0415

    out = _copy.deepcopy(cfg) if isinstance(cfg, dict) else {}

    admin_section = out.get("admin")
    if not isinstance(admin_section, dict):
        admin_section = {}
    admin_section["meta_approver_users"] = list(settings.meta_approver_users)
    out["admin"] = admin_section

    evo_section = out.get("evolution")
    if not isinstance(evo_section, dict):
        evo_section = {}
    evo_section["budget"] = {
        "enabled": settings.budget.enabled,
        "weekly_total": settings.budget.weekly_total,
        "per_kind": dict(settings.budget.per_kind),
    }
    evo_section["auto_rollback"] = {
        "enabled": settings.auto_rollback.enabled,
        "grace_window_hours": settings.auto_rollback.grace_window_hours,
        "thresholds": {
            "default_err_rate_delta_pct": (
                settings.auto_rollback.thresholds.default_err_rate_delta_pct
            ),
            "default_p95_latency_delta_pct": (
                settings.auto_rollback.thresholds.default_p95_latency_delta_pct
            ),
            "signal_window_secs": (
                settings.auto_rollback.thresholds.signal_window_secs
            ),
            "min_baseline_signals": (
                settings.auto_rollback.thresholds.min_baseline_signals
            ),
        },
    }
    out["evolution"] = evo_section
    return out


def _toml_dumps_settings(cfg: dict[str, Any]) -> str:
    try:
        import tomli_w  # noqa: PLC0415

        return tomli_w.dumps(cfg)
    except ImportError:  # pragma: no cover
        import toml  # type: ignore  # noqa: PLC0415

        return str(toml.dumps(cfg))


async def _publish_settings(state: AdminState, cfg: dict[str, Any]) -> None:
    """Best-effort hot-swap + py-config re-emit, matching ``/admin/config``."""
    swap_fn = state.extras.get("config_swap_fn")
    if swap_fn is not None:
        res = swap_fn(cfg)
        if hasattr(res, "__await__"):
            await res
    if state.py_config_path is not None:
        try:
            from corlinman_server.gateway.lifecycle import (  # noqa: PLC0415
                write_py_config,
            )
        except ImportError:
            return
        res2 = write_py_config(cfg, state.py_config_path)
        if hasattr(res2, "__await__"):
            await res2


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:  # noqa: C901 — single APIRouter factory, mirrors Rust pattern
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "evolution"])

    # `/admin/evolution/budget` and `/admin/evolution/history` are
    # registered before `/admin/evolution/{id}` so the literal paths
    # win the FastAPI router match (otherwise the path-param would
    # capture "budget" / "history" and try to look up a proposal of
    # that id). FastAPI uses first-registration-wins on overlapping
    # path templates, same convention as Rust's axum router.

    @r.get("/admin/evolution", response_model=list[ProposalOut])
    async def list_proposals(
        status: str = Query("pending"),
        limit: int | None = Query(None),
    ):
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                EvolutionStatus,
                ProposalsRepo,
            )
        except ImportError:
            return _evolution_disabled()

        try:
            status_enum = EvolutionStatus.from_str(status)
        except Exception as exc:  # noqa: BLE001 — typed ParseError mapped to 400
            return _invalid_status(str(exc))

        n = _clamp_limit(limit)
        repo = ProposalsRepo(_resolve_connection(store))
        try:
            rows = await repo.list_by_status(status_enum, n)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))
        return [_project_proposal(p).model_dump() for p in rows]

    @r.get("/admin/evolution/budget", response_model=BudgetSnapshot)
    async def budget():
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                EvolutionKind,
                ProposalsRepo,
                iso_week_window,
            )
        except ImportError:
            return _evolution_disabled()

        cfg = config_snapshot(state)
        evo_cfg = cfg.get("evolution") if isinstance(cfg, dict) else None
        budget_cfg = (evo_cfg or {}).get("budget") or {}
        enabled = bool(budget_cfg.get("enabled", False))
        weekly_total_limit = int(budget_cfg.get("weekly_total", 0))
        per_kind_cfg: dict[str, int] = {}
        raw_per_kind = budget_cfg.get("per_kind")
        if isinstance(raw_per_kind, dict):
            for k, v in raw_per_kind.items():
                try:
                    per_kind_cfg[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue

        now = _now_ms()
        window_start_ms, window_end_ms = iso_week_window(now)

        repo = ProposalsRepo(_resolve_connection(store))
        try:
            weekly_used = await repo.count_proposals_in_iso_week(now, None)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))

        rows: list[BudgetKindRow] = []
        for kind_str, limit in per_kind_cfg.items():
            if limit == 0:
                # Explicit zero cap means "block this kind entirely" —
                # the engine handles that without surfacing a row in the
                # snapshot. Mirrors the Rust filter.
                continue
            try:
                kind_enum = EvolutionKind.from_str(kind_str)
            except Exception:  # noqa: BLE001 — unknown kind in config: skip + carry on
                continue
            try:
                used = await repo.count_proposals_in_iso_week(now, kind_enum)
            except Exception as exc:  # noqa: BLE001
                return _storage_error(str(exc))
            rows.append(
                BudgetKindRow(
                    kind=kind_str,
                    limit=limit,
                    used=int(used),
                    remaining=max(limit - int(used), 0),
                )
            )
        rows.sort(key=lambda row: row.kind)

        snap = BudgetSnapshot(
            enabled=enabled,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            weekly_total=BudgetTotal(
                limit=weekly_total_limit,
                used=int(weekly_used),
                remaining=max(weekly_total_limit - int(weekly_used), 0),
            ),
            per_kind=rows,
        )
        return snap

    @r.get("/admin/evolution/history", response_model=list[HistoryEntryOut])
    async def history(limit: int | None = Query(None)):
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        n = _clamp_limit(limit)
        conn = _resolve_connection(store)

        sql = (
            "SELECT h.proposal_id, p.kind, p.target, p.risk, p.status, "
            "       h.applied_at, h.rolled_back_at, h.rollback_reason, "
            "       p.auto_rollback_reason, h.metrics_baseline, "
            "       p.shadow_metrics, p.baseline_metrics_json, "
            "       h.before_sha, h.after_sha, p.eval_run_id, p.reasoning "
            "  FROM evolution_history h "
            "  JOIN evolution_proposals p ON p.id = h.proposal_id "
            " ORDER BY h.applied_at DESC "
            " LIMIT ?"
        )
        try:
            cursor = await conn.execute(sql, (n,))
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))

        import json as _json  # noqa: PLC0415 — local import keeps top-level lean

        out: list[HistoryEntryOut] = []
        for row in rows:
            # Row order matches the SELECT column order above.
            try:
                metrics_baseline_str = row[9]
                metrics_baseline = (
                    _json.loads(metrics_baseline_str)
                    if isinstance(metrics_baseline_str, str)
                    else metrics_baseline_str
                )
            except Exception as exc:  # noqa: BLE001 — malformed JSON is a 500
                return _storage_error(f"metrics_baseline: {exc}")

            def _opt_json(val: Any) -> Any | None:
                if val is None:
                    return None
                if not isinstance(val, str):
                    return val
                try:
                    return _json.loads(val)
                except Exception:  # noqa: BLE001 — best-effort, return None on bad JSON
                    return None

            out.append(
                HistoryEntryOut(
                    proposal_id=str(row[0]),
                    kind=str(row[1]),
                    target=str(row[2]),
                    risk=str(row[3]),
                    status=str(row[4]),
                    applied_at=int(row[5]),
                    rolled_back_at=row[6],
                    rollback_reason=row[7],
                    auto_rollback_reason=row[8],
                    metrics_baseline=metrics_baseline,
                    shadow_metrics=_opt_json(row[10]),
                    baseline_metrics_json=_opt_json(row[11]),
                    before_sha=str(row[12]),
                    after_sha=str(row[13]),
                    eval_run_id=row[14],
                    reasoning=str(row[15] or ""),
                )
            )
        return [e.model_dump() for e in out]

    @r.get("/admin/evolution/settings", response_model=EvolutionSettings)
    async def get_settings():
        # Registered before ``/admin/evolution/{id}`` so the literal
        # ``settings`` path wins the FastAPI match (same first-registration
        # discipline the ``budget`` / ``history`` routes follow above).
        state = get_admin_state()
        return _read_settings(state).model_dump()

    @r.put("/admin/evolution/settings", response_model=PutSettingsResponse)
    async def put_settings(body: EvolutionSettings):
        state = get_admin_state()
        if state.config_path is None:
            return _config_path_unset()

        async with state.admin_write_lock:
            current = dict(config_snapshot(state))
            merged = _apply_settings(current, body)
            try:
                serialised = _toml_dumps_settings(merged)
            except Exception as exc:  # noqa: BLE001
                return _settings_write_failed(f"serialise: {exc}")
            path = state.config_path
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".new")
                tmp.write_text(serialised, encoding="utf-8")
                tmp.replace(path)
            except OSError as exc:
                return _settings_write_failed(str(exc))
            await _publish_settings(state, merged)

        # Echo the persisted projection so the UI can reconcile without a
        # follow-up GET (the merge may normalise per-kind ints etc).
        return PutSettingsResponse(status="ok", settings=body).model_dump()

    @r.get("/admin/evolution/{id}", response_model=ProposalOut)
    async def get_proposal(id: str):
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                NotFoundError,
                ProposalId,
                ProposalsRepo,
            )
        except ImportError:
            return _evolution_disabled()

        repo = ProposalsRepo(_resolve_connection(store))
        try:
            proposal = await repo.get(ProposalId(id))
        except NotFoundError:
            return _not_found(id)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))
        return _project_proposal(proposal).model_dump()

    @r.post("/admin/evolution/{id}/approve", response_model=DecisionResponse)
    async def approve_proposal(id: str, body: ApproveBody):
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                EvolutionStatus,
                NotFoundError,
                ProposalId,
                ProposalsRepo,
            )
        except ImportError:
            return _evolution_disabled()

        repo = ProposalsRepo(_resolve_connection(store))
        try:
            current = await repo.get(ProposalId(id))
        except NotFoundError:
            return _not_found(id)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))

        current_status = current.status
        current_status_str = (
            current_status.as_str()
            if hasattr(current_status, "as_str")
            else str(current_status)
        )
        if not _decidable(current_status_str):
            return _invalid_state_transition(current_status_str, "approved")

        kind = current.kind
        kind_str = kind.as_str() if hasattr(kind, "as_str") else str(kind)
        meta_resp = _assert_meta_approver(state, kind_str, body.decided_by)
        if meta_resp is not None:
            return meta_resp

        try:
            await repo.set_decision(
                ProposalId(id),
                EvolutionStatus.APPROVED,
                _now_ms(),
                body.decided_by,
            )
        except NotFoundError:
            return _not_found(id)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))
        return DecisionResponse(id=id, status="approved")

    @r.post("/admin/evolution/{id}/deny", response_model=DecisionResponse)
    async def deny_proposal(id: str, body: DenyBody):
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                EvolutionStatus,
                NotFoundError,
                ProposalId,
                ProposalsRepo,
            )
        except ImportError:
            return _evolution_disabled()

        conn = _resolve_connection(store)
        repo = ProposalsRepo(conn)
        try:
            current = await repo.get(ProposalId(id))
        except NotFoundError:
            return _not_found(id)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))

        current_status = current.status
        current_status_str = (
            current_status.as_str()
            if hasattr(current_status, "as_str")
            else str(current_status)
        )
        if not _decidable(current_status_str):
            return _invalid_state_transition(current_status_str, "denied")

        # Mirror the Rust deny path: preserve the operator-supplied
        # reason inside ``reasoning`` with a fixed ``[DENIED: ...]``
        # prefix so the History tab surfaces it without a new column.
        reason = (body.reason or "").strip()
        if reason:
            current_reasoning = getattr(current, "reasoning", "") or ""
            updated = (
                f"[DENIED: {reason}]"
                if not current_reasoning
                else f"{current_reasoning}\n[DENIED: {reason}]"
            )
            try:
                cursor = await conn.execute(
                    "UPDATE evolution_proposals SET reasoning = ? WHERE id = ?",
                    (updated, id),
                )
                affected = cursor.rowcount
                await cursor.close()
                await conn.commit()
            except Exception as exc:  # noqa: BLE001
                return _storage_error(str(exc))
            if affected == 0:
                return _not_found(id)

        try:
            await repo.set_decision(
                ProposalId(id),
                EvolutionStatus.DENIED,
                _now_ms(),
                body.decided_by,
            )
        except NotFoundError:
            return _not_found(id)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))
        return DecisionResponse(id=id, status="denied")

    @r.post("/admin/evolution/{id}/apply")
    async def apply_proposal(id: str):
        """Drive :meth:`EvolutionApplier.apply`. Transitions an
        ``approved`` proposal to ``applied``, writes the audit row, and
        opens / closes an ``apply_intent_log`` ticket.

        Maps the typed :class:`ApplyError` set onto the same 4xx / 5xx
        envelopes the Rust route emits — clients already depend on the
        ``invalid_state_transition`` shape for the not-approved case."""
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        try:
            from corlinman_auto_rollback import (  # noqa: PLC0415
                EvolutionApplier,
                NotApprovedApplyError,
                NotFoundApplyError,
                UnsupportedKindApplyError,
            )
        except ImportError:
            return _applier_unavailable()

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                EvolutionStatus,
                ProposalId,
            )
        except ImportError:
            return _evolution_disabled()

        applier = EvolutionApplier(
            _resolve_connection(store), config=_auto_rollback_config(state)
        )
        try:
            history = await applier.apply(ProposalId(id))
        except NotFoundApplyError:
            return _not_found(id)
        except NotApprovedApplyError as exc:
            # Mirror the approve / deny 409 contract — the not-approved
            # status is the "from" of an illegal apply transition.
            return _invalid_state_transition(
                exc.status, EvolutionStatus.APPLIED.as_str()
            )
        except UnsupportedKindApplyError as exc:
            return _unsupported_kind(exc.kind)
        except Exception as exc:  # InternalApplyError + stragglers
            return _apply_failed(str(exc))

        return JSONResponse(
            status_code=200,
            content={
                "id": id,
                "status": "applied",
                "history_id": history.id,
            },
        )

    @r.post("/admin/evolution/{id}/rollback")
    async def rollback_proposal(
        id: str,
        body: RollbackBody | None = None,
    ):
        """Drive :meth:`EvolutionApplier.revert`. The AutoRollback
        monitor calls the same code path programmatically on a metrics
        breach; this route is the operator's manual-action surface.

        Maps the shared :class:`RevertError` set onto 4xx / 5xx
        envelopes mirroring the Rust route."""
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        try:
            from corlinman_auto_rollback import (  # noqa: PLC0415
                EvolutionApplier,
                HistoryMissingRevertError,
                NotAppliedRevertError,
                NotFoundRevertError,
                UnsupportedKindRevertError,
            )
        except ImportError:
            return _applier_unavailable()

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                EvolutionStatus,
                ProposalId,
            )
        except ImportError:
            return _evolution_disabled()

        reason = (body.reason if body is not None else None) or "operator: unknown"

        applier = EvolutionApplier(
            _resolve_connection(store), config=_auto_rollback_config(state)
        )
        try:
            await applier.revert(ProposalId(id), reason)
        except NotFoundRevertError:
            return _not_found(id)
        except NotAppliedRevertError as exc:
            # Distinct from the apply path's 409 because the forward
            # state machine is ``applied → rolled_back``; the UI should
            # tell "never applied" from "already rolled back".
            return _invalid_state_transition(
                exc.status, EvolutionStatus.ROLLED_BACK.as_str()
            )
        except UnsupportedKindRevertError as exc:
            return _unsupported_revert_kind(exc.kind)
        except HistoryMissingRevertError:
            return _history_missing(id)
        except Exception as exc:  # InternalRevertError + stragglers
            return _rollback_failed(str(exc))

        return JSONResponse(
            status_code=200,
            content={
                "id": id,
                "status": "rolled_back",
                "reason": reason,
            },
        )

    return r
