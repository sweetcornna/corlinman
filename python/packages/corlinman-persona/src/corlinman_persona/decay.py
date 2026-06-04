"""Pure-function persona decay.

The decay job is deterministic and additive — given a ``PersonaState``
plus a wall-clock delta, it returns a new state. No I/O, no randomness.
The CLI wires this up to the actual store; tests can drive it directly
with a synthetic clock.

We don't decay the ``mood`` string itself — it's a categorical label.
Instead we let ``fatigue`` recover and use a single threshold rule to
flip a ``"tired"`` mood back to ``"neutral"`` once the agent is rested.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from corlinman_persona.state import PersonaState


@dataclass(frozen=True)
class DecayConfig:
    """Tunables for :func:`apply_decay`. All defaults match the values
    in ``docs/design/phase3-roadmap.md`` §6 ``[persona]``.
    """

    # Recovery rate for fatigue, per hour of elapsed wall time.
    fatigue_recovery_per_hour: float = 0.1
    # Threshold below which a ``"tired"`` mood auto-flips to ``"neutral"``.
    # The number is intentionally generous (0.3) so the agent recovers
    # before fatigue hits 0 — mood is a coarser signal than the float.
    tired_to_neutral_below: float = 0.3
    # Number of recent_topics dropped per full day elapsed. The roadmap
    # asks for "1 per day" — we keep it as an int knob for clarity.
    recent_topics_decay_per_day: int = 1
    # Reserved for future use (mood numerics). Wired into :class:`DecayConfig`
    # now so the cron-job TOML can carry it without a schema bump later.
    mood_decay_per_hour: float = 0.05


def apply_decay(
    state: PersonaState,
    hours_elapsed: float,
    config: DecayConfig,
    topic_hours_elapsed: float | None = None,
) -> PersonaState:
    """Return a new ``PersonaState`` with decay applied.

    Pure function — does not mutate the input. Negative or zero
    ``hours_elapsed`` is a no-op (the caller may pass timestamps that
    haven't advanced; we don't want to "advance into the past").

    Rules (mirrors roadmap §5):
      - ``fatigue``: ``max(0.0, fatigue - hours_elapsed * recovery_per_hour)``.
      - ``mood``: if it was ``"tired"`` and the new fatigue dropped
        below :attr:`DecayConfig.tired_to_neutral_below`, flip to
        ``"neutral"``. Other mood labels are left alone.
      - ``recent_topics``: drop ``floor(topic_hours / 24) *
        recent_topics_decay_per_day`` of the oldest entries, where
        ``topic_hours`` is ``topic_hours_elapsed`` when supplied else
        ``hours_elapsed``. Drop is clamped at the list length (over-aged
        states bottom out at empty).
      - ``updated_at_ms`` is left to the store layer; this function does
        not invent timestamps.

    ``topic_hours_elapsed`` decouples the topic-aging clock from the
    fatigue clock. Fatigue (and the ``"tired"`` → ``"neutral"`` flip)
    always tracks ``hours_elapsed``; the ``recent_topics`` drop tracks
    ``topic_hours_elapsed`` instead when it is provided. This lets a
    high-frequency sweep recover fatigue every tick while still aging
    topics off a slower, cumulative day clock. When ``None`` (the
    default) topic aging falls back to ``hours_elapsed`` — identical to
    the original single-clock behaviour, so existing callers, the
    ``decay-once`` CLI, and prior tests are unaffected.
    """
    topic_hours = hours_elapsed if topic_hours_elapsed is None else topic_hours_elapsed
    # No-op only when BOTH clocks are non-positive. With a decoupled topic
    # clock, fatigue time can be 0 (row just stamped) while topic time has
    # accrued past a day — we must still age topics in that case.
    if hours_elapsed <= 0 and topic_hours <= 0:
        return state

    new_fatigue = (
        max(0.0, state.fatigue - hours_elapsed * config.fatigue_recovery_per_hour)
        if hours_elapsed > 0
        else state.fatigue
    )

    new_mood = state.mood
    if state.mood == "tired" and new_fatigue < config.tired_to_neutral_below:
        new_mood = "neutral"

    days_elapsed = math.floor(topic_hours / 24.0) if topic_hours > 0 else 0
    drop_count = days_elapsed * config.recent_topics_decay_per_day
    if drop_count <= 0:
        new_topics = list(state.recent_topics)
    elif drop_count >= len(state.recent_topics):
        new_topics = []
    else:
        # Oldest entries live at the head of the list (push_recent_topic
        # appends to the tail), so slicing from ``drop_count:`` ages them
        # out from the front.
        new_topics = list(state.recent_topics[drop_count:])

    return replace(
        state,
        mood=new_mood,
        fatigue=new_fatigue,
        recent_topics=new_topics,
    )


__all__ = ["DecayConfig", "apply_decay"]
