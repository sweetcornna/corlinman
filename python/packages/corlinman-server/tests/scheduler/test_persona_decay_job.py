"""R2 persona-liveness — scheduler ``persona.decay`` builtin contract tests.

Two surfaces:

* Registry — importing the builtins package registers ``persona.decay``
  so the scheduler-runtime ``run_tool`` dispatch resolves it by name.
* Action — running the builtin against a real ``agent_state.sqlite``
  sweeps decay across every row: a ``"tired"`` row with high fatigue and
  a stale ``updated_at_ms`` trends back toward ``"neutral"`` / lower
  fatigue after the sweep (the whole point — without the scheduled sweep
  mood/fatigue stay frozen on every install).

Plus the typed-envelope degradation branches the scheduler tick loop
relies on (``data_dir_unavailable`` — never raise out of a builtin).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from corlinman_persona.state import PersonaState
from corlinman_persona.store import PersonaStore
from corlinman_server.scheduler.builtins import (
    BUILTIN_ACTIONS,
    PERSONA_DECAY_BUILTIN_NAME,
    BuiltinContext,
    _persona_decay_action,
    run_builtin,
)

# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------


def test_persona_decay_is_registered_by_name() -> None:
    """Importing the builtins module registers ``persona.decay`` so the
    scheduler-runtime hook can resolve it without re-importing every
    callsite."""
    assert PERSONA_DECAY_BUILTIN_NAME == "persona.decay"
    assert PERSONA_DECAY_BUILTIN_NAME in BUILTIN_ACTIONS
    assert BUILTIN_ACTIONS[PERSONA_DECAY_BUILTIN_NAME] is _persona_decay_action


# ---------------------------------------------------------------------------
# Degradation branches — builtins must NEVER raise.
# ---------------------------------------------------------------------------


async def test_data_dir_unavailable_returns_typed_envelope() -> None:
    """``app_state`` with no ``data_dir`` slot → typed envelope, no raise."""
    context = BuiltinContext(app_state=SimpleNamespace())
    out = await _persona_decay_action(context)
    assert out == {"ok": False, "reason": "data_dir_unavailable"}


async def test_none_app_state_returns_data_dir_unavailable() -> None:
    """Degraded boot before the state bundle attaches — same short-circuit."""
    context = BuiltinContext(app_state=None)
    out = await _persona_decay_action(context)
    assert out == {"ok": False, "reason": "data_dir_unavailable"}


# ---------------------------------------------------------------------------
# Happy path — the sweep actually applies decay.
# ---------------------------------------------------------------------------


async def test_sweep_drifts_tired_high_fatigue_toward_baseline(tmp_path) -> None:
    """A ``"tired"`` row with high fatigue and a stale timestamp trends
    toward ``"neutral"`` / lower fatigue after the builtin sweep.

    Seeds the row two full days in the past so the per-row elapsed-hours
    math (now - updated_at) drives enough fatigue recovery to cross the
    ``tired_to_neutral_below`` threshold and flip the mood label back.
    """
    state_db = tmp_path / "agent_state.sqlite"
    two_days_ago_ms = int(time.time() * 1000) - 48 * 3_600_000

    async with PersonaStore(state_db) as store:
        await store.upsert(
            PersonaState(
                agent_id="grantley",
                mood="tired",
                fatigue=0.9,
                recent_topics=["a", "b"],
                updated_at_ms=two_days_ago_ms,
            )
        )

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await _persona_decay_action(context)

    assert out["ok"] is True
    assert out["rows_scanned"] == 1
    assert out["rows_changed"] == 1

    async with PersonaStore(state_db) as store:
        after = await store.get("grantley")
    assert after is not None
    # Fatigue recovered toward baseline ...
    assert after.fatigue < 0.9
    # ... far enough that the "tired" label flipped back to "neutral".
    assert after.mood == "neutral"


async def test_run_builtin_dispatches_persona_decay(tmp_path) -> None:
    """End-to-end via the public ``run_builtin`` entry point — the
    registry indirection is transparent and the sweep still runs."""
    state_db = tmp_path / "agent_state.sqlite"
    one_hour_ago_ms = int(time.time() * 1000) - 3_600_000

    async with PersonaStore(state_db) as store:
        await store.upsert(
            PersonaState(
                agent_id="grantley",
                mood="neutral",
                fatigue=0.5,
                updated_at_ms=one_hour_ago_ms,
            )
        )

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await run_builtin(PERSONA_DECAY_BUILTIN_NAME, context)

    assert out["ok"] is True
    assert out["rows_scanned"] == 1

    async with PersonaStore(state_db) as store:
        after = await store.get("grantley")
    assert after is not None
    # One hour at the 0.1/hr recovery rate → 0.5 - 0.1 = 0.4.
    assert after.fatigue < 0.5


# ---------------------------------------------------------------------------
# Topic-aging clock — hourly sweeps must still age recent_topics off the
# per-day rule (Codex finding #1: restamping updated_at_ms every sweep
# pinned floor(hours/24) at 0, so topics never aged out).
# ---------------------------------------------------------------------------


async def _sweep_at(monkeypatch, tmp_path, now_ms: int) -> dict:
    """Run the builtin once with the module clock pinned to ``now_ms``."""
    monkeypatch.setattr(
        "corlinman_server.scheduler.builtins.persona_decay.time.time",
        lambda: now_ms / 1000.0,
    )
    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    return await _persona_decay_action(context)


async def test_hourly_sweeps_age_topics_only_after_24h_cumulative(
    monkeypatch, tmp_path
) -> None:
    """Drive 25 simulated *hourly* sweeps advancing a pinned ``now``.

    The fatigue clock restamps ``updated_at_ms`` each sweep, but the
    separate topic anchor accumulates: topics must NOT drop before 24h of
    cumulative elapsed time and MUST drop at/after the 24h boundary.
    """
    state_db = tmp_path / "agent_state.sqlite"
    t0_ms = 1_000_000_000_000  # fixed epoch so the math is deterministic
    hour_ms = 3_600_000

    async with PersonaStore(state_db) as store:
        await store.upsert(
            PersonaState(
                agent_id="grantley",
                mood="neutral",
                fatigue=0.0,
                recent_topics=["a", "b", "c"],
                updated_at_ms=t0_ms,
            )
        )

    # Sweeps at +1h .. +23h: topics stay (cumulative < 24h).
    for hour in range(1, 24):
        out = await _sweep_at(monkeypatch, tmp_path, t0_ms + hour * hour_ms)
        assert out["ok"] is True
        async with PersonaStore(state_db) as store:
            after = await store.get("grantley")
        assert after is not None
        assert after.recent_topics == ["a", "b", "c"], f"dropped early at +{hour}h"

    # Sweep at +24h: exactly one day cumulative → drop one oldest topic.
    out = await _sweep_at(monkeypatch, tmp_path, t0_ms + 24 * hour_ms)
    assert out["ok"] is True
    async with PersonaStore(state_db) as store:
        after = await store.get("grantley")
    assert after is not None
    assert after.recent_topics == ["b", "c"]

    # The anchor advanced by exactly one whole day (no sub-day remainder
    # was created since the 24h boundary fell on a sweep).
    assert after.state_json["_topic_decay_anchor_ms"] == t0_ms + 24 * hour_ms


async def test_topic_anchor_preserves_sub_day_remainder(
    monkeypatch, tmp_path
) -> None:
    """A sweep that jumps past a day boundary by a fractional amount must
    advance the anchor by whole days only, keeping the remainder so the
    next day still ages on schedule rather than resetting the clock."""
    state_db = tmp_path / "agent_state.sqlite"
    t0_ms = 1_000_000_000_000
    hour_ms = 3_600_000

    async with PersonaStore(state_db) as store:
        await store.upsert(
            PersonaState(
                agent_id="grantley",
                mood="neutral",
                fatigue=0.0,
                recent_topics=["a", "b", "c"],
                updated_at_ms=t0_ms,
            )
        )

    # First sweep lands at +30h: floor(30/24) = 1 day dropped, 6h remainder.
    out = await _sweep_at(monkeypatch, tmp_path, t0_ms + 30 * hour_ms)
    assert out["ok"] is True
    async with PersonaStore(state_db) as store:
        after = await store.get("grantley")
    assert after is not None
    assert after.recent_topics == ["b", "c"]
    # Anchor advanced by exactly 24h, NOT 30h — the 6h remainder is kept.
    assert after.state_json["_topic_decay_anchor_ms"] == t0_ms + 24 * hour_ms

    # Next sweep at +48h: 24h since the anchor → exactly one more day.
    out = await _sweep_at(monkeypatch, tmp_path, t0_ms + 48 * hour_ms)
    assert out["ok"] is True
    async with PersonaStore(state_db) as store:
        after = await store.get("grantley")
    assert after is not None
    assert after.recent_topics == ["c"]
    assert after.state_json["_topic_decay_anchor_ms"] == t0_ms + 48 * hour_ms


async def test_single_big_sweep_drops_topics_like_legacy(
    monkeypatch, tmp_path
) -> None:
    """A single sweep two full days after the row's timestamp still ages
    two topics — the anchor falls back to ``updated_at_ms`` for rows that
    predate the anchor key."""
    state_db = tmp_path / "agent_state.sqlite"
    t0_ms = 1_000_000_000_000
    hour_ms = 3_600_000

    async with PersonaStore(state_db) as store:
        await store.upsert(
            PersonaState(
                agent_id="grantley",
                mood="neutral",
                fatigue=0.0,
                recent_topics=["a", "b", "c", "d"],
                updated_at_ms=t0_ms,
            )
        )

    out = await _sweep_at(monkeypatch, tmp_path, t0_ms + 48 * hour_ms)
    assert out["ok"] is True
    async with PersonaStore(state_db) as store:
        after = await store.get("grantley")
    assert after is not None
    # floor(48/24) = 2 dropped.
    assert after.recent_topics == ["c", "d"]
