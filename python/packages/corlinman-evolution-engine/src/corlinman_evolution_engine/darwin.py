"""Darwin handler — SKILL.md quality scoring + hermes-loop integration.

Darwin (达尔文.skill) is a SKILL.md self-optimizer borrowed from
``alchaincyf/darwin-skill``. This module ports the **structural half**
of its 8-dimension rubric into a corlinman evolution handler so the
hermes signal / proposal / approval pipeline can carry darwin's
findings to the operator queue.

W3 v1 scope (this file):

* :class:`DarwinScorer` — read a SKILL.md, score 6 structural
  dimensions (frontmatter / workflow / edge-cases / checkpoints /
  specificity / resource integration) + a runtime-adaptability
  red-light scan, return a :class:`RubricReport` (deterministic,
  no LLM).
* :class:`DarwinHandler` — subscribes to ``skill.quality.issue``
  signal clusters emitted by ``darwin_curator``, mints one
  proposal per affected SKILL.md with the rubric report attached
  in ``reasoning``. The ``diff`` field carries a placeholder marker
  (``# DARWIN-REPORT``) because v1 doesn't auto-edit the file —
  operators read the report and decide.

Out of scope (deferred to W3b):

* Effectiveness dimensions (the 40 / 100 split that requires running
  test prompts against the skill via a sub-agent — needs the runner
  infra).
* Hill-climbing multi-round optimization loop.
* LLM-driven unified-diff generation at apply time.

Reuse pattern:

The handler follows :class:`SkillUpdateHandler` exactly — same
``existing_targets`` semantics (dedup by ``(target, tenant_id)``),
same ``risk="medium"`` + ``budget_cost=2`` shape, same
``skills/<name>.md`` target convention. The hermes pipeline picks the
proposals up unchanged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from corlinman_evolution_engine.clustering import SignalCluster
from corlinman_evolution_engine.proposals import EvolutionProposal, ProposalContext
from corlinman_evolution_engine.store import fetch_existing_targets

if TYPE_CHECKING:
    import aiosqlite

# ---------------------------------------------------------------------------
# Wire constants
# ---------------------------------------------------------------------------

#: ``EvolutionKind`` wire string. Aligned with the ``alchaincyf/darwin-skill``
#: name so operators reading the proposal queue can recognise the source.
KIND_DARWIN = "darwin"

#: Signal event_kind the curator emits per issue. A skill with N issues
#: generates N signals all sharing ``target=<skill_name>``; the cluster
#: gate (``min_cluster_size``) doubles as a quality threshold — skills
#: with too few issues to cluster never get flagged. Aligns with darwin's
#: design: "宁少勿多" — only worst skills surface to the operator.
EVENT_SKILL_QUALITY_ISSUE = "skill.quality.issue"

#: Skills the curator must NOT scan. ``configure-persona`` drives the
#: ``/persona`` wizard itself; letting darwin propose edits to its own
#: orchestration script would be self-rewriting and dangerous in v1.
DEFAULT_SKILL_BLACKLIST: frozenset[str] = frozenset(
    {
        "configure-persona",
    }
)


# ---------------------------------------------------------------------------
# Rubric scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionScore:
    """One dimension's contribution + raw findings.

    ``raw`` is 1-10 (the darwin convention); ``weighted`` is
    ``raw * weight`` divided into the 0-1 fraction the dimension
    contributes to ``RubricReport.total``. ``issues`` are short strings
    surfaced verbatim in the rubric report — the operator reads these
    to understand what's wrong without re-running the scorer.
    """

    name: str
    weight: int
    raw: int
    issues: tuple[str, ...] = ()

    @property
    def weighted(self) -> float:
        """Score * weight, scaled to its share of the 100-point total."""
        return float(self.raw * self.weight) / 10.0


@dataclass(frozen=True)
class RubricReport:
    """End-to-end scoring outcome for one SKILL.md.

    ``total`` is on a 0-100 scale (sum of weighted dim scores minus
    runtime red-light penalties, floored at 0). ``red_lights`` captures
    Claude-Code / Cursor / single-runtime binding phrases that would
    otherwise drag the skill's portability — each red light hits
    ``RUNTIME_RED_LIGHT_PENALTY`` points up to ``RUNTIME_RED_LIGHT_MAX_PENALTY``.
    """

    skill_name: str
    source_path: str
    total: float
    dimensions: tuple[DimensionScore, ...]
    red_lights: tuple[str, ...] = ()

    @property
    def needs_review(self) -> bool:
        """``True`` when the score is under the curator's emit threshold."""
        return self.total < QUALITY_THRESHOLD

    def to_markdown(self) -> str:
        """Render the report as the markdown the operator sees in
        ``EvolutionProposal.reasoning``.
        """
        lines: list[str] = [
            f"# Darwin Rubric Report — `{self.skill_name}`",
            "",
            f"**Total**: {self.total:.1f} / 100  ",
            f"**Path**: `{self.source_path}`",
            "",
            "## Dimension Scores",
            "",
            "| # | Dimension | Weight | Raw | Weighted |",
            "|---|-----------|--------|-----|----------|",
        ]
        for i, d in enumerate(self.dimensions, 1):
            lines.append(
                f"| {i} | {d.name} | {d.weight} | {d.raw}/10 | "
                f"{d.weighted:.1f} |"
            )
        lines.append("")
        any_issue = False
        for d in self.dimensions:
            if not d.issues:
                continue
            if not any_issue:
                lines.append("## Issues Found")
                lines.append("")
                any_issue = True
            lines.append(f"### {d.name}")
            for issue in d.issues:
                lines.append(f"- {issue}")
            lines.append("")
        if self.red_lights:
            lines.append("## Runtime Red Lights")
            lines.append("")
            lines.append(
                "Skill bound to a single runtime — should be portable across "
                "skills-compatible agents."
            )
            lines.append("")
            for rl in self.red_lights:
                lines.append(f"- `{rl}`")
            lines.append("")
        return "\n".join(lines)


# Score threshold below which the curator emits signals. 60 = a passing
# grade on a 100-point scale; everything under that is "needs review".
QUALITY_THRESHOLD: float = 60.0

# Runtime red-light penalties — per-occurrence cost and per-skill cap.
RUNTIME_RED_LIGHT_PENALTY: float = 5.0
RUNTIME_RED_LIGHT_MAX_PENALTY: float = 20.0

# Skills with very short bodies get scored down on the workflow /
# specificity dimensions; threshold below which we don't even bother
# (probably a stub / placeholder).
MIN_BODY_CHARS: int = 200


# Regex pool. Compiled at module load so per-skill scoring is cheap.
_FRONTMATTER_BLOCK = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)
_ORDERED_STEP = re.compile(r"(?m)^\s*\d+\.\s+\S")
_HEADING_STEP = re.compile(r"(?im)^#+\s*(step|阶段|stage|phase)\b")
_EDGE_KEYWORDS = re.compile(
    r"(异常|错误|失败|fallback|fall\s?-?back|边界|edge\s?case|"
    r"recovery|降级|超时|timeout|retry|重试|回滚|rollback|⚠️|⚠)",
    re.IGNORECASE,
)
_CHECKPOINT_KEYWORDS = re.compile(
    r"(ask_user|审阅|确认|user\s?confirm|checkpoint|gate|approval)",
    re.IGNORECASE,
)
_SPECIFIC_KEYWORDS = re.compile(
    r"(例:|例如|示例|example|format:|格式:|---\s*\n|```|^\s*-\s|^\s*\*\s|"
    r"\d+\s*(字符|chars|bytes|KB|MiB))",
    re.IGNORECASE | re.MULTILINE,
)
_RESOURCE_REF = re.compile(
    r"(references/|scripts/|templates/|assets/|examples/)([\w\-./]+)"
)

# Runtime red-light triggers. Mirrors darwin's "Red lights" scan but
# tuned to the corlinman context (we don't penalize references to
# corlinman itself — that's where the skill is running).
_RUNTIME_RED_LIGHT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"在\s*Claude\s*Code\s*里"),
    re.compile(r"Claude\s*Code\s*skill\b", re.IGNORECASE),
    re.compile(r"Cursor\s*用户"),
    re.compile(r"Cursor\s*Only", re.IGNORECASE),
    re.compile(r"\[!\[Claude\s*Code\s*Skill\]"),
    re.compile(r"~/\.claude/skills/"),
    re.compile(r"/plugin\s+install\b", re.IGNORECASE),
    re.compile(r"在\s*Codex\s*中使用"),
    re.compile(r"only\s+for\s+(claude|cursor|codex)", re.IGNORECASE),
)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter_block, body)``; empty frontmatter when none."""
    m = _FRONTMATTER_BLOCK.match(text)
    if not m:
        return "", text
    fm = m.group(1)
    body = text[m.end() :]
    return fm, body


def _score_frontmatter(fm: str) -> DimensionScore:
    """Dim 1 — Frontmatter quality (weight 8). Mirrors darwin's
    standards: needs ``name`` field, ``description`` ≤ 1024 chars and
    non-empty.
    """
    issues: list[str] = []
    raw = 10
    if not fm.strip():
        issues.append("frontmatter block missing (skill won't register)")
        return DimensionScore(name="Frontmatter quality", weight=8, raw=0,
                              issues=tuple(issues))
    name_match = re.search(r"(?m)^name\s*:\s*(\S+)", fm)
    if not name_match:
        issues.append("`name:` field missing")
        raw -= 4
    desc_match = re.search(r"(?ms)^description\s*:\s*(.+?)(?=^\w+\s*:|\Z)", fm)
    if not desc_match:
        issues.append("`description:` field missing")
        raw -= 4
    else:
        desc = desc_match.group(1).strip()
        if len(desc) < 40:
            issues.append(
                f"`description:` too short ({len(desc)} chars) — agents won't"
                " trigger on natural-language matches"
            )
            raw -= 3
        elif len(desc) > 1024:
            issues.append(
                f"`description:` exceeds 1024 chars ({len(desc)}) — many "
                "runtimes truncate; first sentence becomes the trigger"
            )
            raw -= 2
    raw = max(0, raw)
    return DimensionScore(name="Frontmatter quality", weight=8, raw=raw,
                          issues=tuple(issues))


def _score_workflow(body: str) -> DimensionScore:
    """Dim 2 — Workflow clarity (weight 15). Needs explicit ordered
    steps with inputs/outputs.
    """
    issues: list[str] = []
    ordered = len(_ORDERED_STEP.findall(body))
    heading_steps = len(_HEADING_STEP.findall(body))
    total_steps = ordered + heading_steps
    if total_steps == 0:
        issues.append(
            "no numbered steps or Step/Stage/Phase headings detected — agents "
            "have nothing to march through"
        )
        raw = 2
    elif total_steps < 3:
        issues.append(
            f"only {total_steps} step markers found — workflows under 3 "
            "steps usually elide important branches"
        )
        raw = 5
    elif total_steps < 6:
        raw = 8
    else:
        raw = 10
    return DimensionScore(name="Workflow clarity", weight=15, raw=raw,
                          issues=tuple(issues))


def _score_edge_cases(body: str) -> DimensionScore:
    """Dim 3 — Edge-case coverage (weight 10). Looks for explicit
    failure / fallback / recovery language.
    """
    matches = _EDGE_KEYWORDS.findall(body)
    n = len(matches)
    issues: list[str] = []
    if n == 0:
        issues.append("no failure / fallback / recovery language found")
        raw = 2
    elif n < 3:
        issues.append(
            f"only {n} edge-case markers — most operations have ≥3 failure "
            "modes worth documenting"
        )
        raw = 5
    elif n < 6:
        raw = 8
    else:
        raw = 10
    return DimensionScore(name="Edge-case coverage", weight=10, raw=raw,
                          issues=tuple(issues))


def _score_checkpoints(body: str) -> DimensionScore:
    """Dim 4 — Checkpoint design (weight 7). Counts ``ask_user`` /
    审阅 / 确认 occurrences as proxies for "agent pauses for user
    confirmation before doing something irreversible".
    """
    n = len(_CHECKPOINT_KEYWORDS.findall(body))
    issues: list[str] = []
    if n == 0:
        issues.append(
            "no user-confirmation checkpoints (ask_user / 审阅 / 确认) — "
            "agents may make irreversible changes without consent"
        )
        raw = 3
    elif n < 3:
        raw = 6
    else:
        raw = 10
    return DimensionScore(name="Checkpoint design", weight=7, raw=raw,
                          issues=tuple(issues))


def _score_specificity(body: str) -> DimensionScore:
    """Dim 5 — Instruction specificity (weight 15). Counts presence of
    examples, code fences, formatted lists, exact parameters.
    """
    matches = _SPECIFIC_KEYWORDS.findall(body)
    n = len(matches)
    issues: list[str] = []
    if n < 3:
        issues.append(
            "very few specifics (examples / code fences / parameters) — "
            "instructions stay abstract and ambiguous"
        )
        raw = 3
    elif n < 8:
        issues.append(
            f"only {n} concrete markers found — agents benefit from more "
            "examples / format specs"
        )
        raw = 6
    elif n < 20:
        raw = 8
    else:
        raw = 10
    return DimensionScore(name="Instruction specificity", weight=15, raw=raw,
                          issues=tuple(issues))


def _score_resource_integration(body: str, source_dir: Path | None) -> DimensionScore:
    """Dim 6 — Resource integration (weight 5). Checks that
    references / scripts / templates / assets paths mentioned in the
    body actually exist on disk relative to the SKILL.md.

    If ``source_dir`` is ``None`` we skip the existence check and just
    reward the presence of refs (the curator passes a real dir; some
    unit tests will pass ``None``).
    """
    refs = _RESOURCE_REF.findall(body)
    issues: list[str] = []
    if not refs:
        # Many skills are pure prose — that's fine. Default to 10.
        return DimensionScore(name="Resource integration", weight=5, raw=10,
                              issues=())
    if source_dir is None:
        return DimensionScore(name="Resource integration", weight=5, raw=8,
                              issues=())
    missing: list[str] = []
    for prefix, rest in refs:
        candidate = source_dir / prefix.rstrip("/") / rest.lstrip("/")
        if not candidate.exists():
            missing.append(f"{prefix}{rest}")
    if missing:
        issues.append(
            f"{len(missing)} referenced path(s) missing on disk: "
            + ", ".join(sorted(set(missing))[:3])
            + (" …" if len(missing) > 3 else "")
        )
        if len(missing) >= len(refs) / 2:
            raw = 3
        else:
            raw = 6
    else:
        raw = 10
    return DimensionScore(name="Resource integration", weight=5, raw=raw,
                          issues=tuple(issues))


def _detect_red_lights(text: str) -> tuple[str, ...]:
    """Scan for single-runtime binding phrases.

    Returns the matched snippets (deduplicated, capped at 5 — the
    operator only needs a few examples to see the pattern).
    """
    hits: list[str] = []
    seen: set[str] = set()
    for pat in _RUNTIME_RED_LIGHT_PATTERNS:
        for m in pat.finditer(text):
            snippet = m.group(0).strip()
            if snippet in seen:
                continue
            seen.add(snippet)
            hits.append(snippet)
            if len(hits) >= 5:
                return tuple(hits)
    return tuple(hits)


class DarwinScorer:
    """Pure-function scorer that turns a SKILL.md path into a
    :class:`RubricReport`. Stateless on purpose: every call re-reads
    the file from disk, so the curator can run it in a tight loop with
    no caching surprises.
    """

    @staticmethod
    def score_file(
        path: Path | str,
        *,
        skill_name: str | None = None,
    ) -> RubricReport:
        """Score the SKILL.md at ``path``. ``skill_name`` defaults to
        the parent directory name (for nested layouts) or the filename
        stem (for flat ``brainstorming.md`` style).
        """
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        return DarwinScorer.score_text(
            text,
            source_path=str(p),
            skill_name=skill_name or _infer_skill_name(p),
            source_dir=p.parent if p.parent.is_dir() else None,
        )

    @staticmethod
    def score_text(
        text: str,
        *,
        source_path: str,
        skill_name: str,
        source_dir: Path | None = None,
    ) -> RubricReport:
        """Score raw markdown text. Used by ``score_file`` and unit
        tests; production callers should prefer the path variant.
        """
        fm, body = _split_frontmatter(text)
        dim1 = _score_frontmatter(fm)
        dim2 = _score_workflow(body)
        dim3 = _score_edge_cases(body)
        dim4 = _score_checkpoints(body)
        dim5 = _score_specificity(body)
        dim6 = _score_resource_integration(body, source_dir)
        red_lights = _detect_red_lights(text)
        red_light_penalty = min(
            RUNTIME_RED_LIGHT_MAX_PENALTY,
            len(red_lights) * RUNTIME_RED_LIGHT_PENALTY,
        )
        # Sum of weighted dim scores: weights add to 60 (darwin reserves
        # 40 points for the effectiveness half, deferred to W3b). We
        # normalize to 0-100 so the reported total reads as a familiar
        # percentage even while the effectiveness dimensions are still
        # off. Effectiveness lands later by adding weight 40 worth of
        # dims to the same sum (which then totals 100 natively).
        gross_60 = sum(d.weighted for d in (dim1, dim2, dim3, dim4, dim5, dim6))
        gross_100 = gross_60 * (100.0 / 60.0)
        total = max(0.0, gross_100 - red_light_penalty)
        # Tiny-body floor — skills with < MIN_BODY_CHARS body are
        # capped at 30 even if the rubric somehow scores higher.
        if len(body.strip()) < MIN_BODY_CHARS:
            total = min(total, 30.0)
        return RubricReport(
            skill_name=skill_name,
            source_path=source_path,
            total=total,
            dimensions=(dim1, dim2, dim3, dim4, dim5, dim6),
            red_lights=red_lights,
        )


def _infer_skill_name(p: Path) -> str:
    """Best-effort skill name from a SKILL.md path.

    Nested layout: ``…/configure-persona/SKILL.md`` → ``configure-persona``.
    Flat layout: ``…/brainstorming.md`` → ``brainstorming``.
    """
    if p.name == "SKILL.md":
        return p.parent.name
    return p.stem


# ---------------------------------------------------------------------------
# Signal helpers (curator-side use)
# ---------------------------------------------------------------------------


def issue_signals_for_report(report: RubricReport) -> list[dict[str, object]]:
    """Build the ``payload_json`` dicts the curator emits as signals.

    One signal per dimension issue + one per red light. All share
    ``target=<skill_name>`` so they cluster naturally — a skill with
    ≥3 issues fires the engine's default ``min_cluster_size=3`` gate.
    The handler reads these payloads back to reconstruct the report
    without re-scoring.
    """
    payloads: list[dict[str, object]] = []
    for dim in report.dimensions:
        for issue in dim.issues:
            payloads.append(
                {
                    "report_version": 1,
                    "skill_name": report.skill_name,
                    "source_path": report.source_path,
                    "total_score": report.total,
                    "dimension": dim.name,
                    "dimension_weight": dim.weight,
                    "dimension_raw": dim.raw,
                    "issue": issue,
                }
            )
    for rl in report.red_lights:
        payloads.append(
            {
                "report_version": 1,
                "skill_name": report.skill_name,
                "source_path": report.source_path,
                "total_score": report.total,
                "red_light": rl,
            }
        )
    return payloads


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _build_diff_placeholder(skill_name: str) -> str:
    """Placeholder diff body. v1 doesn't auto-edit SKILL.md; the
    rubric report is the actionable content (in ``reasoning``). The
    placeholder still parses as a unified diff so the existing applier
    doesn't choke when serializing.
    """
    skill_file = f"skills/{skill_name}.md"
    return (
        f"--- a/{skill_file}\n"
        f"+++ b/{skill_file}\n"
        f"@@ __DARWIN_REPORT__,0 +__DARWIN_REPORT__,0 @@\n"
    )


def _aggregate_report_from_cluster(cluster: SignalCluster) -> str:
    """Reconstruct the rubric report markdown from a cluster's signal
    payloads.

    The curator emits one signal per issue + one per red light. Each
    signal carries the same skill-wide context (name, total score,
    source path) in its payload, so we read those off the first
    signal and aggregate the issue / red-light strings across all
    signals in the cluster.
    """
    issues_by_dim: dict[str, list[str]] = {}
    weights: dict[str, int] = {}
    raws: dict[str, int] = {}
    red_lights: list[str] = []
    skill_name = cluster.target or "<unknown>"
    source_path = ""
    total_score: float | None = None
    for sig in cluster.signals:
        # ``SignalRow.payload`` is a pre-parsed dict (see
        # corlinman_evolution_engine.store._row_to_signal). Older mock
        # signals carry the raw JSON string instead — fall through so
        # the engine's own tests can still feed scripted payloads.
        raw_payload = getattr(sig, "payload", None)
        if isinstance(raw_payload, dict):
            payload = raw_payload
        else:
            raw_json = getattr(sig, "payload_json", None) or "{}"
            try:
                payload = json.loads(raw_json)
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(payload, dict):
            continue
        if not source_path:
            source_path = str(payload.get("source_path", ""))
        if total_score is None and "total_score" in payload:
            try:
                total_score = float(payload["total_score"])
            except (TypeError, ValueError):
                total_score = None
        if "red_light" in payload:
            red_lights.append(str(payload["red_light"]))
            continue
        dim = str(payload.get("dimension", ""))
        if not dim:
            continue
        issues_by_dim.setdefault(dim, []).append(str(payload.get("issue", "")))
        if "dimension_weight" in payload:
            try:
                weights[dim] = int(payload["dimension_weight"])
            except (TypeError, ValueError):
                pass
        if "dimension_raw" in payload:
            try:
                raws[dim] = int(payload["dimension_raw"])
            except (TypeError, ValueError):
                pass
    lines: list[str] = [
        f"# Darwin Rubric Report — `{skill_name}`",
        "",
    ]
    if total_score is not None:
        lines.append(f"**Total**: {total_score:.1f} / 100  ")
    if source_path:
        lines.append(f"**Path**: `{source_path}`")
    lines.append("")
    if issues_by_dim:
        lines.append("## Issues Found")
        lines.append("")
        for dim, issues in issues_by_dim.items():
            w = weights.get(dim)
            r = raws.get(dim)
            header = f"### {dim}"
            if w is not None and r is not None:
                header += f" — weight {w}, raw {r}/10"
            lines.append(header)
            for issue in issues:
                lines.append(f"- {issue}")
            lines.append("")
    if red_lights:
        lines.append("## Runtime Red Lights")
        lines.append("")
        lines.append(
            "Skill bound to a single runtime — should be portable across "
            "skills-compatible agents."
        )
        lines.append("")
        for rl in red_lights:
            lines.append(f"- `{rl}`")
        lines.append("")
    return "\n".join(lines)


class DarwinHandler:
    """``KindHandler`` for the ``darwin`` kind.

    Picks up clusters of ``skill.quality.issue`` signals — each cluster
    corresponds to one SKILL.md with at least ``min_cluster_size``
    distinct issues (default 3). Emits one ``EvolutionProposal`` per
    cluster, with the rubric report aggregated from signal payloads in
    ``reasoning``. ``existing_targets`` dedup prevents re-proposing on
    consecutive daily runs while a previous proposal is still pending.

    ``risk="medium"`` + ``budget_cost=2`` mirror :class:`SkillUpdateHandler`
    because the impact shape is the same (a SKILL.md will be edited).
    The W3a applier writes a ``.bak`` backup before any change, so the
    rollback path is cheap.
    """

    @property
    def kind(self) -> str:
        return KIND_DARWIN

    async def existing_targets(self, conn: object) -> set[tuple[str, str]]:
        sqlite_conn: aiosqlite.Connection = conn  # type: ignore[assignment]
        return await fetch_existing_targets(sqlite_conn, self.kind)

    async def propose(self, ctx: ProposalContext) -> list[EvolutionProposal]:
        relevant = [
            c
            for c in ctx.clusters
            if c.event_kind == EVENT_SKILL_QUALITY_ISSUE and c.target
        ]
        if not relevant:
            return []
        # Strongest signal first — clusters with more issues are the
        # worst-quality skills and should hit the operator queue
        # before tail entries when ``max_proposals_per_run`` truncates.
        relevant.sort(key=lambda c: c.size, reverse=True)
        proposals: list[EvolutionProposal] = []
        for cluster in relevant:
            skill_name = cluster.target or ""
            if skill_name in DEFAULT_SKILL_BLACKLIST:
                continue
            reasoning = _aggregate_report_from_cluster(cluster)
            proposals.append(
                EvolutionProposal(
                    kind=self.kind,
                    target=f"skills/{skill_name}.md",
                    diff=_build_diff_placeholder(skill_name),
                    reasoning=reasoning,
                    risk="medium",
                    budget_cost=2,
                    signal_ids=cluster.signal_ids,
                    trace_ids=cluster.trace_ids,
                    tenant_id=cluster.tenant_id,
                )
            )
        return proposals


# Re-export the helper for the curator. Kept here so the curator can
# import a single module.
__all__ = [
    "DEFAULT_SKILL_BLACKLIST",
    "EVENT_SKILL_QUALITY_ISSUE",
    "KIND_DARWIN",
    "MIN_BODY_CHARS",
    "QUALITY_THRESHOLD",
    "RUNTIME_RED_LIGHT_MAX_PENALTY",
    "RUNTIME_RED_LIGHT_PENALTY",
    "DarwinHandler",
    "DarwinScorer",
    "DimensionScore",
    "RubricReport",
    "issue_signals_for_report",
]
