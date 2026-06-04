"""Tests for :mod:`corlinman_persona.decay`.

Decay is a pure function so all tests run synchronously without sqlite.
"""

from __future__ import annotations

import pytest
from corlinman_persona.decay import DecayConfig, apply_decay
from corlinman_persona.state import PersonaState


def _state(**overrides: object) -> PersonaState:
    """Build a baseline state, allow per-test overrides."""
    base: dict[str, object] = {
        "agent_id": "mentor",
        "mood": "neutral",
        "fatigue": 0.0,
        "recent_topics": [],
        "updated_at_ms": 0,
        "state_json": {},
    }
    base.update(overrides)
    return PersonaState(**base)  # type: ignore[arg-type]


def test_zero_hours_is_no_op() -> None:
    """Hours <= 0 must return the input unchanged (and not invent state)."""
    s = _state(fatigue=0.5, recent_topics=["a", "b"])
    out = apply_decay(s, 0.0, DecayConfig())
    assert out == s


def test_negative_hours_is_no_op() -> None:
    """Clock skew should never *advance* decay; it returns the input."""
    s = _state(fatigue=0.7)
    out = apply_decay(s, -3.0, DecayConfig())
    assert out == s


def test_fatigue_recovers_linearly() -> None:
    s = _state(fatigue=1.0)
    out = apply_decay(s, 5.0, DecayConfig(fatigue_recovery_per_hour=0.1))
    # 1.0 - 5 * 0.1 = 0.5
    assert out.fatigue == pytest.approx(0.5)


def test_fatigue_floors_at_zero() -> None:
    """Recovery beyond the floor must clamp, not produce negative fatigue."""
    s = _state(fatigue=0.2)
    out = apply_decay(s, 100.0, DecayConfig(fatigue_recovery_per_hour=0.1))
    assert out.fatigue == pytest.approx(0.0)


def test_tired_flips_to_neutral_when_fatigue_drops() -> None:
    """``mood == "tired"`` + fatigue under the threshold → ``"neutral"``."""
    s = _state(mood="tired", fatigue=0.4)
    out = apply_decay(
        s,
        2.0,
        DecayConfig(fatigue_recovery_per_hour=0.1, tired_to_neutral_below=0.3),
    )
    # 0.4 - 0.2 = 0.2 → below 0.3 → flip
    assert out.fatigue == pytest.approx(0.2)
    assert out.mood == "neutral"


def test_tired_stays_tired_when_fatigue_still_high() -> None:
    s = _state(mood="tired", fatigue=0.9)
    out = apply_decay(
        s,
        1.0,
        DecayConfig(fatigue_recovery_per_hour=0.1, tired_to_neutral_below=0.3),
    )
    # Still well above 0.3.
    assert out.mood == "tired"


def test_non_tired_mood_never_changes() -> None:
    """Decay only auto-flips ``"tired"`` — other labels are untouched."""
    s = _state(mood="curious", fatigue=0.0)
    out = apply_decay(s, 24.0, DecayConfig())
    assert out.mood == "curious"


def test_recent_topics_age_one_per_day() -> None:
    s = _state(recent_topics=["a", "b", "c", "d"])
    out = apply_decay(s, 24.0, DecayConfig(recent_topics_decay_per_day=1))
    # One day → drop one oldest entry.
    assert out.recent_topics == ["b", "c", "d"]


def test_recent_topics_drop_multiple_days() -> None:
    s = _state(recent_topics=["a", "b", "c", "d", "e"])
    out = apply_decay(s, 72.5, DecayConfig(recent_topics_decay_per_day=1))
    # floor(72.5 / 24) = 3 → drop 3 oldest.
    assert out.recent_topics == ["d", "e"]


def test_recent_topics_partial_day_keeps_list() -> None:
    """Less than 24h elapsed → no topic drops."""
    s = _state(recent_topics=["a", "b"])
    out = apply_decay(s, 23.9, DecayConfig(recent_topics_decay_per_day=1))
    assert out.recent_topics == ["a", "b"]


def test_recent_topics_floor_at_empty() -> None:
    """Aging past the list length must produce an empty list, not crash."""
    s = _state(recent_topics=["a", "b"])
    out = apply_decay(s, 30 * 24.0, DecayConfig(recent_topics_decay_per_day=1))
    assert out.recent_topics == []


def test_recent_topics_decay_per_day_above_one() -> None:
    """A higher daily drop rate must compound with the day count."""
    s = _state(recent_topics=["a", "b", "c", "d", "e", "f"])
    out = apply_decay(s, 48.0, DecayConfig(recent_topics_decay_per_day=2))
    # 2 days * 2 drops/day = 4 drops.
    assert out.recent_topics == ["e", "f"]


def test_apply_decay_does_not_mutate_input() -> None:
    """The function is pure — caller's object must not be touched."""
    s = _state(fatigue=0.8, recent_topics=["a", "b"])
    apply_decay(s, 5.0, DecayConfig())
    assert s.fatigue == pytest.approx(0.8)
    assert s.recent_topics == ["a", "b"]


def test_state_json_is_passthrough() -> None:
    """Decay must not touch ``state_json``."""
    s = _state(state_json={"trust": 0.7})
    out = apply_decay(s, 24.0, DecayConfig())
    assert out.state_json == {"trust": 0.7}


# ---------------------------------------------------------------------------
# topic_hours_elapsed — decoupled topic-aging clock (Codex finding #1).
# ---------------------------------------------------------------------------


def test_topic_hours_none_matches_single_clock() -> None:
    """Default ``None`` must behave exactly as the legacy single clock:
    topic aging falls back to ``hours_elapsed``."""
    s = _state(recent_topics=["a", "b", "c", "d"])
    out_default = apply_decay(s, 24.0, DecayConfig(recent_topics_decay_per_day=1))
    out_explicit_none = apply_decay(
        s, 24.0, DecayConfig(recent_topics_decay_per_day=1), topic_hours_elapsed=None
    )
    assert out_default.recent_topics == ["b", "c", "d"]
    assert out_explicit_none.recent_topics == ["b", "c", "d"]
    assert out_default == out_explicit_none


def test_topic_clock_drops_on_its_own_clock_while_fatigue_follows_hours() -> None:
    """With a separate topic clock, fatigue tracks ``hours_elapsed`` while
    the topic drop tracks ``topic_hours_elapsed``.

    Here only one fatigue-hour elapsed (so almost no recovery) but the
    topic clock has accumulated two days — topics must still age off.
    """
    s = _state(fatigue=1.0, recent_topics=["a", "b", "c", "d"])
    out = apply_decay(
        s,
        1.0,
        DecayConfig(fatigue_recovery_per_hour=0.1, recent_topics_decay_per_day=1),
        topic_hours_elapsed=48.0,
    )
    # Fatigue followed the 1h fatigue clock: 1.0 - 1 * 0.1 = 0.9.
    assert out.fatigue == pytest.approx(0.9)
    # Topics followed the 48h topic clock: floor(48/24) = 2 dropped.
    assert out.recent_topics == ["c", "d"]


def test_topic_clock_under_a_day_keeps_topics_even_when_fatigue_recovers() -> None:
    """A sub-24h topic clock drops nothing, regardless of the fatigue
    clock — the bug was hourly sweeps pinning topic drops at 0 forever,
    but here we assert the inverse: many fatigue hours, < 1 topic day."""
    s = _state(fatigue=1.0, recent_topics=["a", "b"])
    out = apply_decay(
        s,
        10.0,
        DecayConfig(fatigue_recovery_per_hour=0.1, recent_topics_decay_per_day=1),
        topic_hours_elapsed=23.9,
    )
    # 10h of fatigue recovery: 1.0 - 10 * 0.1 = 0.0.
    assert out.fatigue == pytest.approx(0.0)
    # Topic clock under a day → no drop.
    assert out.recent_topics == ["a", "b"]


def test_topic_clock_zero_or_negative_drops_nothing() -> None:
    """A non-positive topic clock must never advance topic aging even
    though the positive fatigue clock keeps the function past its guard."""
    s = _state(fatigue=0.5, recent_topics=["a", "b", "c"])
    out = apply_decay(
        s,
        5.0,
        DecayConfig(fatigue_recovery_per_hour=0.1, recent_topics_decay_per_day=1),
        topic_hours_elapsed=0.0,
    )
    assert out.recent_topics == ["a", "b", "c"]
