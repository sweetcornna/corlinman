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

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from corlinman_server.gateway.routes_admin_b.infra._evolution_lib import (
    ApproveBody,
    BudgetKindRow,
    BudgetSnapshot,
    BudgetTotal,
    DecisionResponse,
    DenyBody,
    EvolutionSettings,
    HistoryEntryOut,
    ProposalOut,
    PutSettingsResponse,
    RollbackBody,
    _applier_unavailable,
    _apply_failed,
    _apply_settings,
    _assert_meta_approver,
    _auto_rollback_config,
    _clamp_limit,
    _config_path_unset,
    _decidable,
    _evolution_disabled,
    _history_missing,
    _invalid_state_transition,
    _invalid_status,
    _not_found,
    _now_ms,
    _project_proposal,
    _publish_settings,
    _read_settings,
    _resolve_connection,
    _rollback_failed,
    _settings_write_failed,
    _storage_error,
    _toml_dumps_settings,
    _unsupported_kind,
    _unsupported_revert_kind,
)
from corlinman_server.gateway.routes_admin_b.state import (
    config_snapshot,
    get_admin_state,
    require_admin,
)

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
