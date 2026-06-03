"""Lifecycle curator — deterministic skill state transitions.

Port of hermes-agent's pure-logic curator (``agent/curator.py:256-296``).
This module is intentionally LLM-free: the background-review fork that
needs an LLM lives in :mod:`background_review`. The two compose at the
gateway entry point — observer detects idle, this module runs the
deterministic pass, then optionally the background_review fork runs an
LLM-driven consolidation. Both report back via :class:`EvolutionSignal`.

Rules ported from hermes:

* ``state == "active"`` + idle > ``stale_after_days``  → ``"stale"``
* ``state == "stale"``  + idle > ``archive_after_days`` → ``"archived"``
* ``state == "stale"``  + any use (``last_used_at > last_review_at``)
                                                       → ``"active"``
* ``pinned is True`` → skip (operator-pinned, never touch)
* ``origin != "agent-created"`` → skip (curator only manages skills it
  created — see hermes ``tools/skill_usage.py:154-200`` provenance
  filter)

The pure logic core (:func:`apply_lifecycle_transitions`) takes an
explicit ``now`` so time-travel is trivial in tests. The async outer
loop (:func:`maybe_run_curator`) gates on the per-profile
``CuratorState.last_review_at`` + ``interval_hours`` so an idle-trigger
fires at most once per configured window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from corlinman_evolution_store import (
    EVENT_CURATOR_RUN_COMPLETED,
    EVENT_CURATOR_RUN_FAILED,
    EVENT_IDLE_REFLECTION,
    EVENT_SKILL_UNUSED,
    CuratorStateRepo,
    EvolutionSignal,
    SignalSeverity,
    SignalsRepo,
)
from corlinman_skills_registry import SkillRegistry

# The deterministic decision core lives in the sibling ``_curator_logic`` so
# this module can stay focused on the async idle-trigger orchestration. The
# names are re-imported (not re-defined) here so the public surface +
# ``__all__`` + every external importer (incl. tests that monkeypatch
# ``curator.apply_lifecycle_transitions`` — resolved in this module's
# namespace by ``maybe_run_curator`` below) keep working unchanged.
from corlinman_server.gateway.evolution._curator_logic import (
    CuratorReport,
    CuratorTransition,
    apply_lifecycle_transitions,
)

__all__ = [
    "CuratorReport",
    "CuratorTransition",
    "apply_lifecycle_transitions",
    "maybe_run_curator",
]


log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Idle trigger — `maybe_run_curator`
# ---------------------------------------------------------------------------


def _now_ms(when: datetime) -> int:
    """Unix milliseconds for the signal ``observed_at`` field.

    Tests pass an explicit ``now`` to keep timestamps deterministic;
    we never call :func:`datetime.now` inside the pure path.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return int(when.timestamp() * 1000)


async def _emit_signal(
    signals: SignalsRepo,
    *,
    event_kind: str,
    severity: SignalSeverity,
    target: str | None,
    payload: dict,
    observed_at: int,
    tenant_id: str,
) -> None:
    """Best-effort signal insert. We never let a signal write failure
    prevent the curator from making forward progress on the SKILL.md
    side — the same philosophy the observer applies (see
    ``observer.py:212-217`` ``write_failed`` log)."""
    try:
        await signals.insert(
            EvolutionSignal(
                event_kind=event_kind,
                severity=severity,
                payload_json=payload,
                target=target,
                observed_at=observed_at,
                tenant_id=tenant_id,
            )
        )
    except Exception as err:  # noqa: BLE001 — log + drop
        log.warning(
            "curator.signal_write_failed",
            event_kind=event_kind,
            err=str(err),
        )


async def maybe_run_curator(
    *,
    profile_slug: str,
    registry: SkillRegistry,
    curator_repo: CuratorStateRepo,
    signals_repo: SignalsRepo,
    now: datetime | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> CuratorReport | None:
    """Check the per-profile interval; if elapsed (or ``force=True``),
    run :func:`apply_lifecycle_transitions` and emit signals.

    Returns ``None`` when the curator decided not to run (paused, or
    inside the interval window). Returns a :class:`CuratorReport`
    otherwise — even on dry-runs, so the caller can render a preview.

    Mirrors hermes ``maybe_run_curator`` / ``should_run_now``
    (``agent/curator.py:198-248``) but folds the "seed first-run state"
    behaviour from there into a single decision: when
    ``last_review_at`` is ``None`` we still run, on the theory that the
    operator just installed the curator and *wants* an immediate first
    pass. The hermes "defer first run by one interval" trick relied on
    ``hermes update`` ticking the loop every minute; corlinman's
    scheduler fires this exact entry point on its own cadence, so we
    don't need that defer.

    Side effects (when we decide to run):

    * Emit :data:`EVENT_IDLE_REFLECTION` before starting (so the admin
      UI sees the trigger even if the transition pass crashes).
    * Run :func:`apply_lifecycle_transitions`; on exception emit
      :data:`EVENT_CURATOR_RUN_FAILED` with ``{"error": str(err)}`` in
      the payload, then re-raise.
    * For each transition emit one :data:`EVENT_SKILL_UNUSED` with
      ``payload={"from": ..., "to": ..., "reason": ...,
      "days_idle": ...}``.
    * Emit :data:`EVENT_CURATOR_RUN_COMPLETED` with the summary line.
    * When ``dry_run=False``, persist via
      :meth:`CuratorStateRepo.mark_run` so the next interval window
      starts from this run.
    """
    when = now if now is not None else datetime.now(UTC)
    state = await curator_repo.get(profile_slug)

    # Paused → do nothing, not even a signal. Matches hermes
    # ``is_paused()`` short-circuit in ``should_run_now`` (curator.py:222).
    if state.paused:
        log.debug("curator.paused", profile_slug=profile_slug)
        return None

    # Interval gate. ``None`` last_review_at means "never run before" —
    # treat as eligible. Otherwise require the configured window has
    # elapsed unless the caller forced us in.
    if not force and state.last_review_at is not None:
        elapsed = when - state.last_review_at
        if elapsed < timedelta(hours=state.interval_hours):
            log.debug(
                "curator.too_soon",
                profile_slug=profile_slug,
                elapsed_hours=elapsed.total_seconds() / 3600.0,
                interval_hours=state.interval_hours,
            )
            return None

    observed_at = _now_ms(when)
    tenant_id = state.tenant_id

    # Emit the trigger signal first — even if the pass crashes mid-run
    # the admin UI can correlate the trigger with the failure event.
    await _emit_signal(
        signals_repo,
        event_kind=EVENT_IDLE_REFLECTION,
        severity=SignalSeverity.INFO,
        target=profile_slug,
        payload={
            "profile_slug": profile_slug,
            "force": force,
            "dry_run": dry_run,
        },
        observed_at=observed_at,
        tenant_id=tenant_id,
    )

    started_at = when
    try:
        transitions = apply_lifecycle_transitions(
            registry,
            state,
            now=when,
            dry_run=dry_run,
        )
    except Exception as err:  # noqa: BLE001 — re-raised below
        await _emit_signal(
            signals_repo,
            event_kind=EVENT_CURATOR_RUN_FAILED,
            severity=SignalSeverity.ERROR,
            target=profile_slug,
            payload={
                "profile_slug": profile_slug,
                "error": str(err),
                "dry_run": dry_run,
            },
            observed_at=_now_ms(when),
            tenant_id=tenant_id,
        )
        raise

    finished_at = when  # pure logic is sync — start ≈ finish at the
    # signal grain. The per-skill writebacks are tiny; if we ever need
    # truer durations we can sample ``time.monotonic()`` around the
    # call, but ``CuratorReport.duration_ms`` would still be 0 on the
    # current clock since ``now`` is fixed.

    # Count every skill we *considered* so the report's ``checked``
    # field matches the hermes ``counts["checked"]`` semantic. The
    # ``skipped`` count is everything that bailed for provenance / pin.
    checked = 0
    skipped = 0
    for skill in registry:
        checked += 1
        if skill.pinned or skill.origin != "agent-created":
            skipped += 1

    report = CuratorReport(
        profile_slug=profile_slug,
        started_at=started_at,
        finished_at=finished_at,
        transitions=transitions,
        skipped=skipped,
        checked=checked,
    )

    # Per-transition signals so the admin UI can render a "what changed"
    # list keyed by skill name without re-reading every SKILL.md.
    for transition in transitions:
        await _emit_signal(
            signals_repo,
            event_kind=EVENT_SKILL_UNUSED,
            severity=SignalSeverity.INFO,
            target=transition.skill_name,
            payload={
                "from": transition.from_state,
                "to": transition.to_state,
                "reason": transition.reason,
                "days_idle": transition.days_idle,
                "profile_slug": profile_slug,
            },
            observed_at=observed_at,
            tenant_id=tenant_id,
        )

    summary = report.summary_line()
    await _emit_signal(
        signals_repo,
        event_kind=EVENT_CURATOR_RUN_COMPLETED,
        severity=SignalSeverity.INFO,
        target=profile_slug,
        payload={
            "profile_slug": profile_slug,
            "summary": summary,
            "marked_stale": report.marked_stale,
            "archived": report.archived,
            "reactivated": report.reactivated,
            "checked": report.checked,
            "skipped": report.skipped,
            "duration_ms": report.duration_ms,
            "dry_run": dry_run,
        },
        observed_at=observed_at,
        tenant_id=tenant_id,
    )

    # Only flip the per-profile ``last_review_at`` on a real run. A
    # dry-run preview shouldn't move the interval window — the operator
    # may want to preview and then apply within the same window.
    if not dry_run:
        await curator_repo.mark_run(
            profile_slug,
            duration_ms=report.duration_ms,
            summary=summary,
            now=when,
            tenant_id=tenant_id,
        )

    return report
