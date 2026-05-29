"""Darwin curator — walk a profile's ``skills/`` directory, score each
SKILL.md, and emit ``skill.quality.issue`` signals for skills that fall
below the rubric threshold.

This is the **signal-producing** half of the darwin × hermes loop. The
**signal-consuming** half (clustering → proposal generation) lives in
:mod:`corlinman_evolution_engine.darwin`'s :class:`DarwinHandler`.

Design notes:

* The curator is intentionally a free-standing async function rather
  than a class. The CLI's ``darwin-curate`` subcommand calls it; the
  gateway lifecycle can also call it inline before kicking the engine.
* Emission strategy: one signal per dimension issue + one per runtime
  red light. All signals for a single skill share
  ``target=<skill_name>`` so the engine's default clustering
  (``min_cluster_size=3``) doubles as a quality floor — skills with
  fewer than 3 distinct issues never surface to the operator queue.
  This matches darwin's "宁少勿多" credo.
* The curator is **single-tenant aware**: every signal it writes
  carries the ``tenant_id`` the caller passes, falling back to
  ``"default"`` for single-tenant deployments.
* Idempotency on the curator side is intentional NOT enforced — re-
  running the curator twice in one day will write duplicate signals.
  Engine-side dedup (``existing_targets``) catches the second-pass
  proposal anyway, so duplicate signals only cost a few DB rows.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from corlinman_evolution_engine.darwin import (
    DEFAULT_SKILL_BLACKLIST,
    EVENT_SKILL_QUALITY_ISSUE,
    QUALITY_THRESHOLD,
    DarwinScorer,
    RubricReport,
    issue_signals_for_report,
)

if TYPE_CHECKING:
    from corlinman_evolution_store.repo import SignalsRepo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Report types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillScanResult:
    """One skill's curator outcome — used for log lines and tests."""

    skill_name: str
    source_path: str
    total_score: float
    issue_count: int
    red_light_count: int
    signals_emitted: int
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class DarwinCuratorReport:
    """Summary report returned by :func:`run_darwin_curator`.

    Mutated incrementally as the curator walks the skills dir; freeze
    a snapshot via :func:`dataclasses.replace` if you need to keep one.
    """

    skills_dir: str = ""
    tenant_id: str = ""
    started_at_ms: int = 0
    finished_at_ms: int = 0
    skills_scanned: int = 0
    skills_below_threshold: int = 0
    signals_emitted: int = 0
    skipped_blacklist: int = 0
    skipped_unreadable: int = 0
    results: list[SkillScanResult] = field(default_factory=list)

    @property
    def elapsed_ms(self) -> int:
        return max(0, self.finished_at_ms - self.started_at_ms)


# ---------------------------------------------------------------------------
# Curator entry point
# ---------------------------------------------------------------------------


def _iter_skill_files(skills_dir: Path) -> list[tuple[str, Path]]:
    """Enumerate ``(skill_name, SKILL.md path)`` tuples under ``skills_dir``.

    Handles both layouts the corlinman skill registry supports:

    * Flat: ``skills_dir/<name>.md`` → ``(name, <path>)``
    * Nested: ``skills_dir/<name>/SKILL.md`` → ``(name, <path>)``

    Skips hidden files / dirs (anything starting with ``.``) and the
    ``__pycache__`` directory. Symlinks are followed but recorded as
    their real path — the scorer reads them either way.
    """
    entries: list[tuple[str, Path]] = []
    if not skills_dir.is_dir():
        return entries
    for child in sorted(skills_dir.iterdir()):
        if child.name.startswith("."):
            continue
        if child.name == "__pycache__":
            continue
        if child.is_file() and child.suffix == ".md":
            entries.append((child.stem, child))
            continue
        if child.is_dir():
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                entries.append((child.name, skill_md))
    return entries


async def run_darwin_curator(
    *,
    skills_dir: Path,
    signals_repo: SignalsRepo,
    tenant_id: str = "default",
    threshold: float = QUALITY_THRESHOLD,
    blacklist: frozenset[str] = DEFAULT_SKILL_BLACKLIST,
    now_ms: int | None = None,
) -> DarwinCuratorReport:
    """Score every SKILL.md in ``skills_dir`` and emit signals for the
    low-quality ones.

    :param skills_dir: Profile's ``skills/`` directory (e.g.
        ``<data_dir>/profiles/default/skills/``).
    :param signals_repo: Open :class:`SignalsRepo` against
        ``evolution.sqlite``. The curator does NOT manage the
        connection lifecycle — caller opens / closes.
    :param tenant_id: Tenant slug that ends up on every emitted signal.
        Single-tenant deployments use ``"default"``.
    :param threshold: Skills with total score < ``threshold`` produce
        signals. Defaults to :data:`QUALITY_THRESHOLD` (60 / 100).
    :param blacklist: Skill names the curator must not scan. Defaults
        to :data:`DEFAULT_SKILL_BLACKLIST` (``{"configure-persona"}``).
    :param now_ms: Override the ``observed_at`` timestamp written on
        every signal. Defaults to wall-clock now. Tests use this to
        pin timestamps.

    :returns: :class:`DarwinCuratorReport` summarising the run.
    """
    started = now_ms if now_ms is not None else int(time.time() * 1000)
    report = DarwinCuratorReport(
        skills_dir=str(skills_dir),
        tenant_id=tenant_id,
        started_at_ms=started,
    )

    entries = _iter_skill_files(skills_dir)
    logger.info(
        "darwin_curator: scanning %d skill files under %s (threshold=%.1f)",
        len(entries),
        skills_dir,
        threshold,
    )

    for skill_name, skill_md in entries:
        if skill_name in blacklist:
            report.skipped_blacklist += 1
            report.results.append(
                SkillScanResult(
                    skill_name=skill_name,
                    source_path=str(skill_md),
                    total_score=0.0,
                    issue_count=0,
                    red_light_count=0,
                    signals_emitted=0,
                    skipped=True,
                    skip_reason="blacklist",
                )
            )
            continue
        try:
            rubric = DarwinScorer.score_file(skill_md, skill_name=skill_name)
        except OSError as err:
            logger.warning(
                "darwin_curator: skill_unreadable name=%s path=%s err=%s",
                skill_name,
                skill_md,
                err,
            )
            report.skipped_unreadable += 1
            report.results.append(
                SkillScanResult(
                    skill_name=skill_name,
                    source_path=str(skill_md),
                    total_score=0.0,
                    issue_count=0,
                    red_light_count=0,
                    signals_emitted=0,
                    skipped=True,
                    skip_reason=f"unreadable: {err}",
                )
            )
            continue

        report.skills_scanned += 1
        issue_count = sum(len(d.issues) for d in rubric.dimensions)
        red_light_count = len(rubric.red_lights)
        emitted = 0
        if rubric.total < threshold:
            report.skills_below_threshold += 1
            emitted = await _emit_signals_for_skill(
                signals_repo=signals_repo,
                rubric=rubric,
                tenant_id=tenant_id,
                observed_at=started,
            )
            report.signals_emitted += emitted
            logger.info(
                "darwin_curator: signaled name=%s score=%.1f issues=%d "
                "red_lights=%d signals=%d",
                skill_name,
                rubric.total,
                issue_count,
                red_light_count,
                emitted,
            )
        else:
            logger.debug(
                "darwin_curator: pass name=%s score=%.1f (above threshold)",
                skill_name,
                rubric.total,
            )

        report.results.append(
            SkillScanResult(
                skill_name=skill_name,
                source_path=str(skill_md),
                total_score=rubric.total,
                issue_count=issue_count,
                red_light_count=red_light_count,
                signals_emitted=emitted,
            )
        )

    report.finished_at_ms = int(time.time() * 1000) if now_ms is None else started
    logger.info(
        "darwin_curator: complete scanned=%d below_threshold=%d "
        "signals=%d blacklisted=%d unreadable=%d elapsed_ms=%d",
        report.skills_scanned,
        report.skills_below_threshold,
        report.signals_emitted,
        report.skipped_blacklist,
        report.skipped_unreadable,
        report.elapsed_ms,
    )
    return report


async def _emit_signals_for_skill(
    *,
    signals_repo: SignalsRepo,
    rubric: RubricReport,
    tenant_id: str,
    observed_at: int,
) -> int:
    """Insert one signal per ``issue_signals_for_report`` payload.

    Best-effort: write failures are logged but never raise — a
    transient SQLite glitch shouldn't crash the daily curator before
    it walks the rest of the skills. Mirrors the existing
    ``gateway/evolution/curator.py:_emit_signal`` philosophy.
    """
    # Lazy import to keep ``corlinman-evolution-engine`` free of a hard
    # dependency on ``corlinman-evolution-store`` at module import time
    # (tests that mock the repo never load these types).
    from corlinman_evolution_store.types import (  # noqa: PLC0415
        EvolutionSignal,
        SignalSeverity,
    )

    payloads = issue_signals_for_report(rubric)
    emitted = 0
    for payload in payloads:
        severity = (
            SignalSeverity.ERROR if "red_light" in payload else SignalSeverity.WARN
        )
        try:
            await signals_repo.insert(
                EvolutionSignal(
                    event_kind=EVENT_SKILL_QUALITY_ISSUE,
                    target=rubric.skill_name,
                    severity=severity,
                    payload_json=payload,
                    observed_at=observed_at,
                    tenant_id=tenant_id,
                )
            )
            emitted += 1
        except Exception as err:  # noqa: BLE001 — log + drop, keep walking
            logger.warning(
                "darwin_curator: signal_write_failed name=%s err=%s",
                rubric.skill_name,
                err,
            )
    return emitted


__all__ = [
    "DarwinCuratorReport",
    "SkillScanResult",
    "run_darwin_curator",
]
