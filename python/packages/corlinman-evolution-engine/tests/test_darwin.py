"""Tests for the ``darwin`` evolution handler — scorer, handler, curator.

The scorer is the load-bearing piece; if its dimension regexes drift
silently, the curator stops emitting useful signals and operators get
a flat dashboard with no idea anything's wrong. These tests pin the
contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corlinman_evolution_engine.darwin import (
    DEFAULT_SKILL_BLACKLIST,
    EVENT_SKILL_QUALITY_ISSUE,
    KIND_DARWIN,
    QUALITY_THRESHOLD,
    DarwinHandler,
    DarwinScorer,
    RubricReport,
    issue_signals_for_report,
)
from corlinman_evolution_engine.darwin_curator import (
    DarwinCuratorReport,
    SkillScanResult,
    run_darwin_curator,
)


# ---------------------------------------------------------------------------
# Sample SKILL.md content used across tests
# ---------------------------------------------------------------------------


_HEALTHY_SKILL = """\
---
name: sample-healthy
description: A staged multi-step wizard for testing the darwin scorer. Walks a user through three explicit phases, prompts for confirmation at each step, and surfaces failure modes via ⚠️ markers. Includes worked examples and exact parameter formats.
allowed-tools:
  - ask_user
  - web_fetch
---
# Sample Healthy Skill

## Step 1 — Identity

Call `ask_user` with the following question and 2 options.

### Step 1a

Format: `<slug>` (lowercase, [a-z0-9_-], 1-64 chars).
Example: `grantley`, `cyber_oracle`.

Edge case: if user provides invalid input → re-ask via ask_user; on
timeout fallback to defaults; on repeated failure roll back.

## Step 2 — Confirm

Use `ask_user` to confirm. Options: ["确认", "补充", "修改", "重做"].

Edge case: empty input → ⚠️ warning + retry.

## Step 3 — Persist

Atomic file write with rollback. Example:
```python
write_to_disk(path, content, atomic=True)
```

Edge case: disk full → fallback to /tmp + retry.
"""

_POOR_SKILL = """\
---
name: sample-poor
description: stub
---
# Stub

todo: write this.
"""

_RUNTIME_BOUND_SKILL = """\
---
name: sample-runtime-bound
description: A simple workflow for testing the darwin runtime red-light scan. Walks through 3 steps with examples and edge cases. Includes Step 1, Step 2, Step 3 with ask_user calls for confirmation. Lists failure modes including timeout fallback and retry edge cases.
---
# Bound to Claude Code

在 Claude Code 里使用本 skill 时，把它装进 `~/.claude/skills/`。
Cursor 用户也可以用，但需要装一个 plugin: `/plugin install foo`.

## Step 1
ask_user the user. Example format: `<value>`.
Edge case: timeout fallback retry.

## Step 2
ask_user 确认. Format: `key=value`.
Edge case: error recovery fallback.

## Step 3
ask_user finalize. Example: `done`.
Edge case: failure rollback.
"""


# ---------------------------------------------------------------------------
# DarwinScorer
# ---------------------------------------------------------------------------


class TestDarwinScorer:
    def test_healthy_skill_scores_high(self) -> None:
        # A well-structured SKILL.md should clear the threshold by a
        # wide margin so a stricter future threshold doesn't accidentally
        # flag it. Anchors the regex-driven scorer to a known-good
        # markdown shape.
        r = DarwinScorer.score_text(
            _HEALTHY_SKILL,
            source_path="sample-healthy/SKILL.md",
            skill_name="sample-healthy",
        )
        assert r.total >= 80.0, f"healthy skill scored {r.total}"
        # All six dimensions appear, in stable order.
        assert len(r.dimensions) == 6
        assert [d.name for d in r.dimensions] == [
            "Frontmatter quality",
            "Workflow clarity",
            "Edge-case coverage",
            "Checkpoint design",
            "Instruction specificity",
            "Resource integration",
        ]

    def test_stub_skill_is_floored_to_30(self) -> None:
        # Skills with bodies under ``MIN_BODY_CHARS`` are capped at 30
        # regardless of how their regex matches happen to fall.
        # Locking the floor stops a "perfect 100 for a one-line stub"
        # surprise.
        r = DarwinScorer.score_text(
            _POOR_SKILL,
            source_path="sample-poor/SKILL.md",
            skill_name="sample-poor",
        )
        assert r.total <= 30.0
        assert r.needs_review

    def test_runtime_red_lights_penalise_score(self) -> None:
        # Runtime-bound phrasing ("在 Claude Code 里", "Cursor 用户",
        # "~/.claude/skills/", "/plugin install") each cost 5 points up
        # to a 20-point cap. The skill body is otherwise reasonable, so
        # the penalty is visible — without it, total would clear ~70;
        # with all 4 hits the cap drags it to total - 20.
        r = DarwinScorer.score_text(
            _RUNTIME_BOUND_SKILL,
            source_path="sample-runtime-bound/SKILL.md",
            skill_name="sample-runtime-bound",
        )
        assert len(r.red_lights) >= 3, r.red_lights
        # The runtime penalty pulls a moderate skill clearly below 70.
        assert r.total < 70.0

    def test_missing_frontmatter_dim1_zero(self) -> None:
        # No frontmatter block → Frontmatter Quality scores 0; the skill
        # is in practice unloadable so the curator must flag it.
        r = DarwinScorer.score_text(
            "# body only\n\nno frontmatter at all here.",
            source_path="orphan.md",
            skill_name="orphan",
        )
        dim1 = next(d for d in r.dimensions if d.name == "Frontmatter quality")
        assert dim1.raw == 0
        assert any("frontmatter" in i.lower() for i in dim1.issues)

    def test_score_file_reads_disk(self, tmp_path: Path) -> None:
        # The path-based API is what the curator uses; verify it round-
        # trips through the file system.
        p = tmp_path / "sample" / "SKILL.md"
        p.parent.mkdir()
        p.write_text(_HEALTHY_SKILL, encoding="utf-8")
        r = DarwinScorer.score_file(p)
        assert r.skill_name == "sample"  # inferred from parent dir
        assert r.total >= 80.0


# ---------------------------------------------------------------------------
# issue_signals_for_report
# ---------------------------------------------------------------------------


class TestIssueSignalsForReport:
    def test_one_signal_per_issue_plus_red_lights(self) -> None:
        # The curator clusters by ``target=<skill_name>``; emitting one
        # signal per issue + one per red light lets the engine's
        # ``min_cluster_size=3`` gate double as a quality floor (a skill
        # with <3 problems silently drops). Verify the payload count
        # matches the report shape.
        r = DarwinScorer.score_text(
            _RUNTIME_BOUND_SKILL,
            source_path="x.md",
            skill_name="sample-runtime-bound",
        )
        payloads = issue_signals_for_report(r)
        expected = sum(len(d.issues) for d in r.dimensions) + len(r.red_lights)
        assert len(payloads) == expected
        # Every payload carries the skill identity + total score so the
        # handler can rebuild the report.
        for p in payloads:
            assert p["skill_name"] == "sample-runtime-bound"
            assert "total_score" in p


# ---------------------------------------------------------------------------
# DarwinHandler
# ---------------------------------------------------------------------------


def _signal_row(
    *,
    id: int,
    event_kind: str,
    target: str,
    payload: dict,
    tenant_id: str = "default",
):
    """Build a minimal SignalRow-shaped namespace for handler unit tests.

    Done as a SimpleNamespace rather than the real dataclass so we don't
    require the store package to import-mock its way in.
    """
    from types import SimpleNamespace
    import json as _json

    return SimpleNamespace(
        id=id,
        event_kind=event_kind,
        target=target,
        severity="warn",
        payload_json=_json.dumps(payload),
        trace_id=None,
        session_id=None,
        observed_at=0,
        tenant_id=tenant_id,
    )


def _make_cluster(target: str, signals: list) -> object:
    from corlinman_evolution_engine.clustering import SignalCluster

    return SignalCluster(
        event_kind=EVENT_SKILL_QUALITY_ISSUE,
        target=target,
        signals=signals,
    )


class TestDarwinHandler:
    @pytest.mark.asyncio
    async def test_proposes_one_per_cluster(self) -> None:
        from corlinman_evolution_engine.proposals import ProposalContext

        signals = [
            _signal_row(
                id=i,
                event_kind=EVENT_SKILL_QUALITY_ISSUE,
                target="poor-skill",
                payload={
                    "skill_name": "poor-skill",
                    "source_path": "poor-skill/SKILL.md",
                    "total_score": 45.0,
                    "dimension": "Workflow clarity",
                    "dimension_weight": 15,
                    "dimension_raw": 3,
                    "issue": f"issue {i}",
                },
            )
            for i in range(3)
        ]
        cluster = _make_cluster("poor-skill", signals)
        ctx = ProposalContext(
            clusters=[cluster],
            kb_path=Path("/dev/null"),
            similarity_threshold=0.95,
            max_chunks_scanned=0,
            now_ms=0,
        )
        proposals = await DarwinHandler().propose(ctx)
        assert len(proposals) == 1
        p = proposals[0]
        assert p.kind == KIND_DARWIN
        assert p.target == "skills/poor-skill.md"
        # The placeholder diff contains the DARWIN_REPORT sentinel so
        # the operator UI shows "no inline change" — actionable content
        # is in ``reasoning``.
        assert "__DARWIN_REPORT__" in p.diff
        assert "Darwin Rubric Report" in p.reasoning
        assert "poor-skill" in p.reasoning

    @pytest.mark.asyncio
    async def test_ignores_unrelated_event_kinds(self) -> None:
        from corlinman_evolution_engine.clustering import SignalCluster
        from corlinman_evolution_engine.proposals import ProposalContext

        # A cluster from another handler (e.g. skill_update) must not
        # produce a darwin proposal even though it targets a SKILL.md.
        other_signal = _signal_row(
            id=1,
            event_kind="skill.invocation.failed",
            target="web_search",
            payload={},
        )
        cluster = SignalCluster(
            event_kind="skill.invocation.failed",
            target="web_search",
            signals=[other_signal] * 3,
        )
        ctx = ProposalContext(
            clusters=[cluster],
            kb_path=Path("/dev/null"),
            similarity_threshold=0.95,
            max_chunks_scanned=0,
            now_ms=0,
        )
        assert await DarwinHandler().propose(ctx) == []

    @pytest.mark.asyncio
    async def test_blacklists_configure_persona(self) -> None:
        # The /persona wizard's own driver is exempt from darwin so
        # the evolution loop can't propose self-rewrites to the wizard
        # script that drives it.
        from corlinman_evolution_engine.proposals import ProposalContext

        signals = [
            _signal_row(
                id=i,
                event_kind=EVENT_SKILL_QUALITY_ISSUE,
                target="configure-persona",
                payload={
                    "skill_name": "configure-persona",
                    "source_path": "configure-persona/SKILL.md",
                    "total_score": 10.0,
                    "dimension": "Workflow clarity",
                    "dimension_weight": 15,
                    "dimension_raw": 1,
                    "issue": "fake issue",
                },
            )
            for i in range(5)
        ]
        cluster = _make_cluster("configure-persona", signals)
        ctx = ProposalContext(
            clusters=[cluster],
            kb_path=Path("/dev/null"),
            similarity_threshold=0.95,
            max_chunks_scanned=0,
            now_ms=0,
        )
        assert await DarwinHandler().propose(ctx) == []
        assert "configure-persona" in DEFAULT_SKILL_BLACKLIST


# ---------------------------------------------------------------------------
# Curator
# ---------------------------------------------------------------------------


class _FakeSignalsRepo:
    """Captures signal inserts for assertions; mimics the
    :class:`SignalsRepo.insert` contract well enough for tests without
    pulling in aiosqlite."""

    def __init__(self) -> None:
        self.inserted: list[object] = []

    async def insert(self, signal) -> int:  # type: ignore[no-untyped-def]
        self.inserted.append(signal)
        return len(self.inserted)


class TestDarwinCurator:
    @pytest.mark.asyncio
    async def test_emits_signals_for_low_score_skill_only(
        self, tmp_path: Path
    ) -> None:
        # One healthy skill + one poor skill in a profile dir. Curator
        # should only signal the poor one; the healthy one should pass
        # silently. The cluster-gate logic lives in the engine, not the
        # curator, but this test pins the per-skill emission decision.
        skills = tmp_path / "skills"
        (skills / "healthy").mkdir(parents=True)
        (skills / "healthy" / "SKILL.md").write_text(_HEALTHY_SKILL, encoding="utf-8")
        (skills / "poor.md").write_text(_POOR_SKILL, encoding="utf-8")

        repo = _FakeSignalsRepo()
        report = await run_darwin_curator(
            skills_dir=skills,
            signals_repo=repo,  # type: ignore[arg-type]
            tenant_id="t1",
        )
        assert report.skills_scanned == 2
        assert report.skills_below_threshold == 1
        assert report.signals_emitted >= 1
        # All signals must carry the "poor" target since healthy passed.
        poor_signals = [s for s in repo.inserted if s.target == "poor"]
        assert poor_signals
        assert all(
            s.event_kind == EVENT_SKILL_QUALITY_ISSUE for s in repo.inserted
        )
        assert all(s.tenant_id == "t1" for s in repo.inserted)

    @pytest.mark.asyncio
    async def test_skips_blacklisted_skill(self, tmp_path: Path) -> None:
        # ``configure-persona`` is blacklisted by default. Even if it
        # scores low, no signal must land.
        skills = tmp_path / "skills"
        (skills / "configure-persona").mkdir(parents=True)
        (skills / "configure-persona" / "SKILL.md").write_text(
            _POOR_SKILL, encoding="utf-8"
        )
        repo = _FakeSignalsRepo()
        report = await run_darwin_curator(
            skills_dir=skills,
            signals_repo=repo,  # type: ignore[arg-type]
        )
        assert report.skipped_blacklist == 1
        assert report.signals_emitted == 0
        assert not repo.inserted

    @pytest.mark.asyncio
    async def test_handles_missing_skills_dir(self, tmp_path: Path) -> None:
        # Curator must degrade gracefully if the profile dir doesn't
        # exist yet (fresh boot, no skills seeded). No signals, no
        # crash.
        nowhere = tmp_path / "no-such-dir"
        repo = _FakeSignalsRepo()
        report = await run_darwin_curator(
            skills_dir=nowhere,
            signals_repo=repo,  # type: ignore[arg-type]
        )
        assert report.skills_scanned == 0
        assert report.signals_emitted == 0
