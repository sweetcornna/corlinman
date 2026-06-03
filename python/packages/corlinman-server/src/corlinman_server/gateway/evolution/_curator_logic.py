"""Pure deterministic state-transition logic for the lifecycle curator.

Extracted verbatim from :mod:`.curator` so that module can stay focused on
the async idle-trigger orchestration (:func:`.curator.maybe_run_curator`).
This module owns the LLM-free, side-effect-light decision core: the result
dataclasses, the day-delta math, the per-skill transition classifier, the
SKILL.md body re-reader, and the :func:`apply_lifecycle_transitions` writer.
``curator.py`` re-imports these names. This module MUST NOT import
``curator`` (no cycle).

Rules ported from hermes ``agent/curator.py:256-296`` (see the ``curator``
module docstring for the full cascade).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from corlinman_evolution_store import CuratorState
from corlinman_skills_registry import Skill, SkillRegistry, write_skill_md
from corlinman_skills_registry.usage import SkillUsage

__all__ = [
    "CuratorReport",
    "CuratorTransition",
    "apply_lifecycle_transitions",
]


log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CuratorTransition:
    """One state change applied (or proposed, in dry-run) by
    :func:`apply_lifecycle_transitions`.

    The ``reason`` field is one of ``"stale_threshold"`` /
    ``"archive_threshold"`` / ``"reactivated"`` — surfaced verbatim in
    the ``EVENT_SKILL_UNUSED`` signal payload so the admin UI can render
    *why* a transition happened without re-deriving thresholds.
    """

    skill_name: str
    from_state: str
    to_state: str
    reason: str
    days_idle: float


@dataclass(frozen=True)
class CuratorReport:
    """Result of a single :func:`maybe_run_curator` invocation.

    ``checked`` counts every skill the curator considered (whether or
    not it transitioned); ``skipped`` counts skills filtered out for
    provenance / pinning reasons. ``checked - skipped`` ≈ the eligible
    pool, but the literal subtraction isn't exact because skills with
    no state change still count toward ``checked``.
    """

    profile_slug: str
    started_at: datetime
    finished_at: datetime
    transitions: list[CuratorTransition]
    skipped: int
    checked: int

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def marked_stale(self) -> int:
        return sum(1 for t in self.transitions if t.to_state == "stale")

    @property
    def archived(self) -> int:
        return sum(1 for t in self.transitions if t.to_state == "archived")

    @property
    def reactivated(self) -> int:
        return sum(1 for t in self.transitions if t.to_state == "active")

    def summary_line(self) -> str:
        """One-line human summary stored in
        :attr:`CuratorState.last_review_summary`."""
        return (
            f"stale={self.marked_stale} archived={self.archived} "
            f"reactivated={self.reactivated} checked={self.checked} "
            f"skipped={self.skipped} duration={self.duration_ms}ms"
        )


# ---------------------------------------------------------------------------
# Pure logic — `apply_lifecycle_transitions`
# ---------------------------------------------------------------------------


def _days_between(later: datetime, earlier: datetime | None) -> float:
    """Inclusive-friendly day delta. ``None`` earlier → ``inf`` so
    a never-used skill is treated as maximally idle (the caller still
    falls back to ``created_at`` before reaching this branch)."""
    if earlier is None:
        return float("inf")
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=UTC)
    if later.tzinfo is None:
        later = later.replace(tzinfo=UTC)
    return (later - earlier).total_seconds() / 86400.0


def _classify_transition(
    skill: Skill,
    usage: SkillUsage,
    state_row: CuratorState,
    now: datetime,
) -> CuratorTransition | None:
    """Pure: decide whether ``skill`` needs a state change. ``None`` ==
    no-op.

    Mirrors the hermes ``agent/curator.py:256-296`` cascade, with the
    provenance filter from ``tools/skill_usage.py:154-200`` hoisted to
    the top so non-eligible skills bail before any time math.
    """
    # Provenance / pin guards — must come first so we don't even count
    # the days for skills we'd never touch anyway.
    if skill.pinned:
        return None
    if skill.origin != "agent-created":
        return None

    # Anchor: prefer recorded ``last_used_at``, else fall back to
    # ``created_at`` so a brand-new skill that hasn't been used yet
    # doesn't immediately archive itself.
    last_active = usage.last_used_at if usage.last_used_at is not None else skill.created_at
    days_idle = _days_between(now, last_active)

    # Reactivation: a stale skill got used after the last curator review.
    # We can only assert "after" when we have both timestamps; if
    # ``last_review_at`` is None we'd need a different signal (so we
    # leave the active→stale → stale→archived ladder to fire instead).
    if (
        skill.state == "stale"
        and usage.last_used_at is not None
        and state_row.last_review_at is not None
        and usage.last_used_at > state_row.last_review_at
    ):
        return CuratorTransition(
            skill_name=skill.name,
            from_state="stale",
            to_state="active",
            reason="reactivated",
            days_idle=days_idle,
        )

    # stale → archived (check before active → stale so the longer
    # threshold wins on a skill that crossed both at once).
    if skill.state == "stale" and days_idle > state_row.archive_after_days:
        return CuratorTransition(
            skill_name=skill.name,
            from_state="stale",
            to_state="archived",
            reason="archive_threshold",
            days_idle=days_idle,
        )

    # active → stale
    if skill.state == "active" and days_idle > state_row.stale_after_days:
        return CuratorTransition(
            skill_name=skill.name,
            from_state="active",
            to_state="stale",
            reason="stale_threshold",
            days_idle=days_idle,
        )

    return None


def _split_body(source_path: Path) -> str:
    """Re-read the SKILL.md body so the round-trip write doesn't lose
    handcrafted Markdown when the curator only meant to flip a state
    flag.

    We can't trust :attr:`Skill.body_markdown` once the agent has been
    in memory for a while — a sibling skill writer (W4.4 background
    review) may have rewritten the body on disk without re-syncing the
    in-memory copy. Always pull the body off disk just before write.
    """
    from corlinman_skills_registry.parse import split_frontmatter

    text = source_path.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    if split is None:
        # File doesn't have frontmatter (shouldn't happen for a
        # registry-loaded skill) — fall back to the whole text as body.
        return text
    _yaml_str, body = split
    return body


def apply_lifecycle_transitions(
    registry: SkillRegistry,
    state_row: CuratorState,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[CuratorTransition]:
    """Run the deterministic pass over every skill in ``registry``.

    With ``dry_run=True`` returns the proposed transitions without
    mutating any SKILL.md on disk (the in-memory :class:`Skill` objects
    are left untouched too — callers can re-classify after writes
    elsewhere). With ``dry_run=False`` (the default) writes back via
    :func:`write_skill_md`, preserving the body verbatim by reading it
    off disk just before the write.

    The pure-logic decision sits in :func:`_classify_transition` —
    this wrapper is the side-effect surface.
    """
    when = now if now is not None else datetime.now(UTC)
    transitions: list[CuratorTransition] = []

    for skill in registry:
        usage = registry.usage_for(skill.name)
        transition = _classify_transition(skill, usage, state_row, when)
        if transition is None:
            continue
        transitions.append(transition)
        if dry_run:
            continue
        # Mutate the in-memory model in place then round-trip the file.
        # ``Skill`` is a pydantic v2 BaseModel with frozen=False so
        # direct attribute assignment is supported.
        skill.state = transition.to_state  # type: ignore[assignment]
        path = registry.path_for(skill.name)
        if path is None:
            # Registry returned a skill whose path can't be resolved —
            # nothing on disk to write back. Keep the in-memory flip
            # so subsequent passes see the new state.
            log.warning(
                "curator.path_missing",
                skill=skill.name,
                to_state=transition.to_state,
            )
            continue
        # ``path_for`` returns the directory; the SKILL.md itself is on
        # ``skill.source_path``.
        body = _split_body(skill.source_path)
        write_skill_md(skill.source_path, skill, body)

    return transitions
