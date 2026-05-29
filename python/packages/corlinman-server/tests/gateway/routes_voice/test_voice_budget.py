"""Tests for the voice money / quota layer: ``routes_voice/cost.py`` +
``routes_voice/budget.py``.

This is a live-WS admission gate and a billing path, so the assertions
pin the *exact* decisions and the *exact* delta-checkpoint arithmetic
rather than smoke-testing happy paths:

* :func:`evaluate_budget` — the session-start admission gate. Denies
  when the daily budget is zero (``budget_is_zero``) or the day is
  exhausted (``day_budget_exhausted``); allows otherwise with the
  correct remaining-seconds payload.
* :meth:`SessionMeter.poll` — the mid-session ~1 Hz ticker: one-shot
  ``budget_warn`` at cap-60 s, hard kill at ``max_session_seconds``,
  and day-budget mid-session termination (with the max-session hard
  kill taking priority over the day-budget kill).
* :meth:`BudgetEnforcer.tick` / :meth:`finalize` /
  ``_checkpoint_delta`` — the delta-only billing arithmetic: spend
  accrues exactly once per second across ticks, never double-bills, and
  ``finalize`` flushes the trailing delta.
"""

from __future__ import annotations

from corlinman_server.gateway.routes_voice.budget import (
    BudgetEnforcer,
    BudgetTickAction,
)
from corlinman_server.gateway.routes_voice.cost import (
    CLOSE_CODE_BUDGET,
    CLOSE_CODE_MAX_SESSION,
    BudgetDecision,
    BudgetDenyReason,
    DaySpend,
    InMemoryVoiceSpend,
    MeterTick,
    SessionMeter,
    TerminateReason,
    VoiceConfig,
    evaluate_budget,
)

# A wall-clock anchor for the monotonic-seconds maths. The meter only
# ever subtracts ``started_at`` from ``now``, so any constant works; we
# pick a non-zero value so an accidental "started_at defaults to 0" bug
# would surface as a huge elapsed.
_T0 = 10_000.0

_DAY_EPOCH = 20_237  # arbitrary days-since-epoch bucket key
_NEXT_MIDNIGHT = 1_748_563_200  # arbitrary reset_at sentinel


# ---------------------------------------------------------------------------
# evaluate_budget — the session-start admission gate
# ---------------------------------------------------------------------------


def test_evaluate_budget_denies_when_budget_is_zero() -> None:
    """A tenant with a zero daily minutes budget is refused at start —
    even with zero seconds used — with ``budget_is_zero`` and the
    next-midnight reset stamp."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=0)
    today = DaySpend.fresh(_DAY_EPOCH)

    decision = evaluate_budget(cfg, today, _NEXT_MIDNIGHT)

    assert decision.allowed is False
    assert decision.reason == BudgetDenyReason.budget_is_zero()
    assert decision.reason.kind == BudgetDenyReason.BUDGET_IS_ZERO
    assert decision.reset_at == _NEXT_MIDNIGHT
    assert decision.seconds_remaining == 0


def test_evaluate_budget_denies_when_day_exhausted_at_exact_cap() -> None:
    """Used seconds *equal* to the cap is exhausted (``>=``, not ``>``):
    the boundary belongs to the deny side."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=5)  # cap = 300 s
    today = DaySpend(day_epoch=_DAY_EPOCH, seconds_used=300, sessions_count=2)

    decision = evaluate_budget(cfg, today, _NEXT_MIDNIGHT)

    assert decision.allowed is False
    assert decision.reason == BudgetDenyReason.day_budget_exhausted(
        used_seconds=300, cap_seconds=300
    )
    assert decision.reason.kind == BudgetDenyReason.DAY_BUDGET_EXHAUSTED
    assert decision.reason.used_seconds == 300
    assert decision.reason.cap_seconds == 300
    assert decision.reset_at == _NEXT_MIDNIGHT


def test_evaluate_budget_denies_when_day_over_cap() -> None:
    """Used seconds beyond the cap is likewise exhausted, and the
    overage is reported verbatim in the deny payload."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=5)  # cap = 300 s
    today = DaySpend(day_epoch=_DAY_EPOCH, seconds_used=512, sessions_count=9)

    decision = evaluate_budget(cfg, today, _NEXT_MIDNIGHT)

    assert decision.allowed is False
    assert decision.reason == BudgetDenyReason.day_budget_exhausted(
        used_seconds=512, cap_seconds=300
    )


def test_evaluate_budget_allows_with_remaining_seconds() -> None:
    """Under-budget session is admitted and the remaining-seconds
    headroom is ``cap - used`` exactly."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=10)  # cap = 600 s
    today = DaySpend(day_epoch=_DAY_EPOCH, seconds_used=140, sessions_count=1)

    decision = evaluate_budget(cfg, today, _NEXT_MIDNIGHT)

    assert decision.allowed is True
    assert decision.reason is None
    assert decision.seconds_remaining == 600 - 140


def test_evaluate_budget_allows_fresh_day_full_headroom() -> None:
    """A first session of the day gets the whole cap as headroom."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=3)  # cap = 180 s
    today = DaySpend.fresh(_DAY_EPOCH)

    decision = evaluate_budget(cfg, today, _NEXT_MIDNIGHT)

    assert decision == BudgetDecision.allow(180)
    assert decision.allowed is True
    assert decision.seconds_remaining == 180


def test_evaluate_budget_zero_check_precedes_exhaustion() -> None:
    """When the cap is zero, the gate reports ``budget_is_zero`` rather
    than ``day_budget_exhausted`` even though 0 used >= 0 cap would also
    be "exhausted" — the zero branch is checked first."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=0)
    today = DaySpend(day_epoch=_DAY_EPOCH, seconds_used=0, sessions_count=0)

    decision = evaluate_budget(cfg, today, _NEXT_MIDNIGHT)

    assert decision.reason.kind == BudgetDenyReason.BUDGET_IS_ZERO


# ---------------------------------------------------------------------------
# SessionMeter — mid-session ticker
# ---------------------------------------------------------------------------


def test_session_meter_warns_once_at_cap_minus_60s() -> None:
    """With a 2-minute (120 s) cap and a fresh start, the warn fires at
    elapsed == cap-60 == 60 s, reports the remaining whole minutes, and
    fires exactly once (subsequent polls before the cap go back to
    ``ok``)."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=2)  # cap = 120 s
    meter = SessionMeter.start(cfg, start_seconds_used=0, started_at=_T0)

    # Sanity: warn anchor is cap-60, computed at construction.
    assert meter.warn_at_elapsed == 60

    # Before the anchor: ok, warn not yet fired.
    assert meter.poll(_T0 + 59) == MeterTick.ok()
    assert meter.warn_fired is False

    # At the anchor: a single budget_warn with 60 s -> ceil = 1 minute.
    warn = meter.poll(_T0 + 60)
    assert warn == MeterTick.budget_warn(minutes_remaining=1)
    assert meter.warn_fired is True

    # One-shot: a later poll (still under the cap) reverts to ok.
    assert meter.poll(_T0 + 75) == MeterTick.ok()


def test_session_meter_warn_minutes_remaining_rounds_up() -> None:
    """With more headroom than a single minute, ``minutes_remaining`` is
    a ``ceil`` of remaining-seconds / 60 (partial minute rounds up)."""
    # cap = 300 s; start used = 150 -> day_remaining = 150 -> warn anchor
    # at 150-60 = 90 s elapsed. At that point day_used = 150+90 = 240,
    # remaining = 60 -> 1 minute. Use a larger cap to exercise rounding.
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=5)  # cap = 300 s
    meter = SessionMeter.start(cfg, start_seconds_used=0, started_at=_T0)

    # warn anchor = 300 - 60 = 240.
    assert meter.warn_at_elapsed == 240
    warn = meter.poll(_T0 + 240)
    # remaining = 300 - 240 = 60 -> ceil(60/60) = 1.
    assert warn == MeterTick.budget_warn(minutes_remaining=1)


def test_session_meter_hard_kill_at_max_session_seconds() -> None:
    """The per-session length cap terminates with the max-session close
    code at ``elapsed >= max_session_seconds`` — independent of the day
    budget."""
    cfg = VoiceConfig(
        budget_minutes_per_tenant_per_day=600,  # huge: day budget irrelevant
        max_session_seconds=30,
    )
    meter = SessionMeter.start(cfg, start_seconds_used=0, started_at=_T0)

    assert meter.poll(_T0 + 29) == MeterTick.ok()

    kill = meter.poll(_T0 + 30)
    assert kill == MeterTick.terminate(
        reason=TerminateReason.MAX_SESSION_SECONDS,
        close_code=CLOSE_CODE_MAX_SESSION,
    )
    assert kill.kind == MeterTick.TERMINATE
    assert kill.close_code == 4001


def test_session_meter_day_budget_mid_session_termination() -> None:
    """Mid-session, once start-used + elapsed reaches the day cap the
    meter terminates with the budget close code."""
    # cap = 120 s, but 90 s already used today -> only 30 s of headroom.
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=2)  # cap = 120 s
    meter = SessionMeter.start(cfg, start_seconds_used=90, started_at=_T0)

    # 29 s in: day_used = 90 + 29 = 119 < 120 -> ok.
    assert meter.poll(_T0 + 29) == MeterTick.ok()

    # 30 s in: day_used = 90 + 30 = 120 >= 120 -> terminate (budget).
    kill = meter.poll(_T0 + 30)
    assert kill == MeterTick.terminate(
        reason=TerminateReason.DAY_BUDGET_EXHAUSTED,
        close_code=CLOSE_CODE_BUDGET,
    )
    assert kill.close_code == 4002


def test_session_meter_max_session_takes_priority_over_day_budget() -> None:
    """When both caps trip on the same poll, the hard session-length
    kill wins (it is checked first)."""
    # max_session = 30 s and day budget also exhausted at 30 s.
    cfg = VoiceConfig(
        budget_minutes_per_tenant_per_day=2,  # cap = 120 s
        max_session_seconds=30,
    )
    # start_used = 90 -> day cap also trips at elapsed 30.
    meter = SessionMeter.start(cfg, start_seconds_used=90, started_at=_T0)

    kill = meter.poll(_T0 + 30)
    assert kill.reason == TerminateReason.MAX_SESSION_SECONDS
    assert kill.close_code == CLOSE_CODE_MAX_SESSION


def test_session_meter_no_warn_anchor_when_starting_near_cap() -> None:
    """If the remaining day budget at start is <= 60 s there is no room
    for a 60-s-ahead warning, so no warn anchor is set — the meter goes
    straight from ok to terminate."""
    # cap = 120 s; start used = 90 -> day_remaining = 30 (< 60) -> no warn.
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=2)
    meter = SessionMeter.start(cfg, start_seconds_used=90, started_at=_T0)

    assert meter.warn_at_elapsed is None
    # Never emits a warn on the way to termination.
    assert meter.poll(_T0 + 10).kind == MeterTick.OK
    assert meter.poll(_T0 + 29).kind == MeterTick.OK
    assert meter.poll(_T0 + 30).kind == MeterTick.TERMINATE


def test_session_meter_elapsed_clamps_to_zero() -> None:
    """A ``now`` before ``started_at`` clamps elapsed to zero rather
    than going negative (mirrors Rust ``saturating_duration_since``)."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=5)
    meter = SessionMeter.start(cfg, start_seconds_used=0, started_at=_T0)

    assert meter.elapsed_secs(_T0 - 100) == 0
    assert meter.poll(_T0 - 100) == MeterTick.ok()


# ---------------------------------------------------------------------------
# BudgetEnforcer — delta-checkpoint billing arithmetic
# ---------------------------------------------------------------------------


def test_enforcer_checkpoints_only_the_delta_across_ticks() -> None:
    """Each tick bills only the *new* seconds since the previous
    checkpoint; spend accrues to exactly the elapsed total, never
    double-counting already-billed seconds."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=600)  # generous
    spend = InMemoryVoiceSpend()
    enforcer = BudgetEnforcer.start(
        cfg, spend, tenant="acme", day_epoch=_DAY_EPOCH, started_at=_T0
    )

    # First tick at +5 s: bills the full 5-second delta.
    action = enforcer.tick(_T0 + 5)
    assert action == BudgetTickAction.continue_()
    assert spend.snapshot("acme", _DAY_EPOCH).seconds_used == 5
    assert enforcer.last_checkpointed() == 5

    # Second tick at +12 s: bills only the 7-second delta (12 - 5).
    enforcer.tick(_T0 + 12)
    assert spend.snapshot("acme", _DAY_EPOCH).seconds_used == 12
    assert enforcer.last_checkpointed() == 12

    # Third tick at +12 s (no wall-clock progress): zero delta, no write.
    enforcer.tick(_T0 + 12)
    assert spend.snapshot("acme", _DAY_EPOCH).seconds_used == 12
    assert enforcer.last_checkpointed() == 12


def test_enforcer_does_not_rebill_on_backwards_clock() -> None:
    """A tick whose ``now`` regresses bills nothing (delta <= 0): the
    meter must never decrement the billed total."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=600)
    spend = InMemoryVoiceSpend()
    enforcer = BudgetEnforcer.start(
        cfg, spend, tenant="acme", day_epoch=_DAY_EPOCH, started_at=_T0
    )

    enforcer.tick(_T0 + 10)
    assert spend.snapshot("acme", _DAY_EPOCH).seconds_used == 10

    # Clock regresses: elapsed clamps to 5 but delta = 5 - 10 = -5 <= 0.
    enforcer.tick(_T0 + 5)
    assert spend.snapshot("acme", _DAY_EPOCH).seconds_used == 10
    assert enforcer.last_checkpointed() == 10


def test_enforcer_accrues_onto_preexisting_day_spend() -> None:
    """The enforcer reads the day's existing spend at start (for the
    meter cap maths) but only ever *adds* this session's elapsed delta
    to the store — it never re-writes the pre-existing total."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=600)
    spend = InMemoryVoiceSpend()
    # Another session already billed 200 s today.
    spend.add_seconds("acme", _DAY_EPOCH, 200)

    enforcer = BudgetEnforcer.start(
        cfg, spend, tenant="acme", day_epoch=_DAY_EPOCH, started_at=_T0
    )
    enforcer.tick(_T0 + 8)

    # Pre-existing 200 + this session's 8 = 208.
    assert spend.snapshot("acme", _DAY_EPOCH).seconds_used == 208


def test_enforcer_finalize_flushes_trailing_delta_and_returns_total() -> None:
    """``finalize`` flushes the un-checkpointed tail and returns the
    total seconds attributed to the session."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=600)
    spend = InMemoryVoiceSpend()
    enforcer = BudgetEnforcer.start(
        cfg, spend, tenant="acme", day_epoch=_DAY_EPOCH, started_at=_T0
    )

    # Tick once mid-session, then close out 4 s later.
    enforcer.tick(_T0 + 30)
    assert spend.snapshot("acme", _DAY_EPOCH).seconds_used == 30

    total = enforcer.finalize(_T0 + 34)
    assert total == 34
    # The trailing 4-second delta got flushed.
    assert spend.snapshot("acme", _DAY_EPOCH).seconds_used == 34
    assert enforcer.last_checkpointed() == 34


def test_enforcer_finalize_is_idempotent_when_no_new_time() -> None:
    """Calling ``finalize`` at the same instant as the last checkpoint
    writes no extra seconds but still reports the correct total."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=600)
    spend = InMemoryVoiceSpend()
    enforcer = BudgetEnforcer.start(
        cfg, spend, tenant="acme", day_epoch=_DAY_EPOCH, started_at=_T0
    )

    enforcer.tick(_T0 + 15)
    total = enforcer.finalize(_T0 + 15)

    assert total == 15
    assert spend.snapshot("acme", _DAY_EPOCH).seconds_used == 15


def test_enforcer_tick_emits_warning_action() -> None:
    """When the underlying meter warns, the enforcer maps it to an
    ``emit_warning`` action carrying the remaining minutes — and the
    checkpoint still bills the elapsed delta on that tick."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=2)  # cap = 120 s
    spend = InMemoryVoiceSpend()
    enforcer = BudgetEnforcer.start(
        cfg, spend, tenant="acme", day_epoch=_DAY_EPOCH, started_at=_T0
    )

    # warn anchor at cap-60 = 60 s.
    action = enforcer.tick(_T0 + 60)
    assert action == BudgetTickAction.emit_warning(minutes_remaining=1)
    assert action.kind == BudgetTickAction.EMIT_WARNING
    # The 60-second delta is still billed on the warn tick.
    assert spend.snapshot("acme", _DAY_EPOCH).seconds_used == 60


def test_enforcer_tick_emits_terminate_action_at_day_budget() -> None:
    """When the meter terminates on the day budget, the enforcer maps it
    to a ``terminate`` action carrying the reason + close code, and the
    final elapsed delta is checkpointed before termination."""
    cfg = VoiceConfig(budget_minutes_per_tenant_per_day=1)  # cap = 60 s
    spend = InMemoryVoiceSpend()
    enforcer = BudgetEnforcer.start(
        cfg, spend, tenant="acme", day_epoch=_DAY_EPOCH, started_at=_T0
    )

    action = enforcer.tick(_T0 + 60)
    assert action == BudgetTickAction.terminate(
        reason=TerminateReason.DAY_BUDGET_EXHAUSTED,
        close_code=CLOSE_CODE_BUDGET,
    )
    assert action.kind == BudgetTickAction.TERMINATE
    assert action.close_code == 4002
    # Elapsed seconds are still billed even on the terminating tick.
    assert spend.snapshot("acme", _DAY_EPOCH).seconds_used == 60


def test_enforcer_tick_emits_terminate_action_at_max_session() -> None:
    """A max-session hard kill maps to a ``terminate`` action with the
    max-session close code."""
    cfg = VoiceConfig(
        budget_minutes_per_tenant_per_day=600,
        max_session_seconds=45,
    )
    spend = InMemoryVoiceSpend()
    enforcer = BudgetEnforcer.start(
        cfg, spend, tenant="acme", day_epoch=_DAY_EPOCH, started_at=_T0
    )

    action = enforcer.tick(_T0 + 45)
    assert action == BudgetTickAction.terminate(
        reason=TerminateReason.MAX_SESSION_SECONDS,
        close_code=CLOSE_CODE_MAX_SESSION,
    )
    assert action.close_code == 4001
