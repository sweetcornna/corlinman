"""Tests for corlinman_agent_brain.risk_classifier module.

Covers:
- Sensitive content pattern detection
- Risk classification logic (LOW / MEDIUM / HIGH / BLOCKED)
- Batch classification
- Write policy decisions (DRAFT_FIRST / AUTO / SEMI_AUTO)
- Edge cases (empty evidence, boundary confidence values)
"""

from __future__ import annotations

import pytest
from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.models import (
    MemoryCandidate,
    MemoryKind,
    RiskLevel,
    WritePolicy,
)
from corlinman_agent_brain.risk_classifier import (
    WriteDecision,
    _contains_hard_secret,
    _contains_sensitive_content,
    classify_risk,
    classify_risk_batch,
    decide_write_action,
    redact_sensitive,
)


@pytest.fixture
def config() -> CuratorConfig:
    return CuratorConfig()


@pytest.fixture
def low_risk_candidate() -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id="cand-low",
        topic="Use pytest for testing",
        kind=MemoryKind.DECISION,
        summary="Team decided to use pytest as the test framework.",
        evidence=["session turn 3: user said use pytest"],
        confidence=0.9,
        risk=RiskLevel.LOW,
        source_session_id="sess-001",
        agent_id="agent-x",
        tenant_id="tenant-a",
        tags=["testing", "python"],
    )


@pytest.fixture
def sensitive_candidate() -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id="cand-sensitive",
        topic="API integration setup",
        kind=MemoryKind.PROJECT_CONTEXT,
        summary="Set up API with key sk-live-abc123def456ghi789jkl012mno345pqr678",
        evidence=["turn 5: user shared API key"],
        confidence=0.85,
        source_session_id="sess-002",
        agent_id="agent-x",
        tenant_id="tenant-a",
    )


@pytest.fixture
def low_confidence_candidate() -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id="cand-lowconf",
        topic="Maybe use Redis",
        kind=MemoryKind.CONCEPT,
        summary="User might want to use Redis for caching.",
        confidence=0.2,
        source_session_id="sess-003",
        agent_id="agent-x",
        tenant_id="tenant-a",
    )


class TestSensitiveContentDetection:
    def test_detects_sk_live_key(self) -> None:
        assert _contains_sensitive_content(
            "key is sk-live-abc123def456ghi789jkl012")

    def test_detects_github_pat(self) -> None:
        assert _contains_sensitive_content(
            "token: github_pat_abcdefghijklmnopqrstuv12")

    def test_detects_github_personal_token(self) -> None:
        assert _contains_sensitive_content(
            "ghp_abcdefghijklmnopqrstuvwxyz1234567890")

    def test_detects_aws_key(self) -> None:
        assert _contains_sensitive_content("AWS key: AKIAIOSFODNN7EXAMPLE")

    def test_detects_slack_token(self) -> None:
        assert _contains_sensitive_content("xoxb-123456789-abcdefgh")

    def test_detects_email(self) -> None:
        assert _contains_sensitive_content(
            "contact user@example.com for details")

    def test_detects_private_ip(self) -> None:
        assert _contains_sensitive_content("server at 192.168.1.100")

    def test_detects_url_with_token(self) -> None:
        assert _contains_sensitive_content(
            "https://api.example.com/v1?token=secret123")

    def test_detects_password_assignment(self) -> None:
        assert _contains_sensitive_content("password = mysecretpass123")

    def test_clean_text_passes(self) -> None:
        assert not _contains_sensitive_content(
            "Use Rust for the MemoryHost implementation")

    def test_empty_string_passes(self) -> None:
        assert not _contains_sensitive_content("")

    def test_code_snippet_without_secrets(self) -> None:
        assert not _contains_sensitive_content(
            "fn main() { println!(hello); }")


class TestClassifyRisk:
    def test_low_risk_normal_candidate(
        self, low_risk_candidate: MemoryCandidate, config: CuratorConfig
    ) -> None:
        risk = classify_risk(low_risk_candidate, config)
        assert risk == RiskLevel.LOW

    def test_blocked_risk_secret_summary(
        self, sensitive_candidate: MemoryCandidate, config: CuratorConfig
    ) -> None:
        # A hard secret (API key) in the summary must escalate to BLOCKED so the
        # candidate is never written to the vault under any policy.
        risk = classify_risk(sensitive_candidate, config)
        assert risk == RiskLevel.BLOCKED

    def test_blocked_risk_secret_evidence(self, config: CuratorConfig) -> None:
        cand = MemoryCandidate(
            candidate_id="c1",
            topic="Setup",
            kind=MemoryKind.PROJECT_CONTEXT,
            summary="Normal summary.",
            evidence=["user said: password=hunter2secret"],
            confidence=0.9,
        )
        risk = classify_risk(cand, config)
        assert risk == RiskLevel.BLOCKED

    def test_high_risk_pii_summary(self, config: CuratorConfig) -> None:
        # Soft-sensitive PII (email / phone) is HIGH (drafted + redacted), not
        # BLOCKED.
        cand = MemoryCandidate(
            candidate_id="c-pii",
            topic="Contact",
            kind=MemoryKind.PROJECT_CONTEXT,
            summary="Reach the owner at jane.doe@example.com for access.",
            confidence=0.9,
        )
        risk = classify_risk(cand, config)
        assert risk == RiskLevel.HIGH

    def test_high_risk_conflict_kind(self, config: CuratorConfig) -> None:
        cand = MemoryCandidate(
            candidate_id="c2",
            topic="Conflicting info",
            kind=MemoryKind.CONFLICT,
            summary="Two contradictory statements.",
            confidence=0.8,
        )
        risk = classify_risk(cand, config)
        assert risk == RiskLevel.HIGH

    def test_medium_risk_low_confidence(
        self, low_confidence_candidate: MemoryCandidate, config: CuratorConfig
    ) -> None:
        risk = classify_risk(low_confidence_candidate, config)
        assert risk == RiskLevel.MEDIUM

    def test_medium_risk_persona_low_confidence(self, config: CuratorConfig) -> None:
        cand = MemoryCandidate(
            candidate_id="c3",
            topic="User personality",
            kind=MemoryKind.AGENT_PERSONA,
            summary="User seems to prefer verbose output.",
            confidence=0.5,
        )
        risk = classify_risk(cand, config)
        assert risk == RiskLevel.MEDIUM

    def test_persona_high_confidence_is_low_risk(self, config: CuratorConfig) -> None:
        cand = MemoryCandidate(
            candidate_id="c4",
            topic="User personality",
            kind=MemoryKind.AGENT_PERSONA,
            summary="User explicitly stated preference for concise output.",
            confidence=0.85,
        )
        risk = classify_risk(cand, config)
        assert risk == RiskLevel.LOW

    def test_boundary_confidence_at_draft_threshold(self, config: CuratorConfig) -> None:
        # Exactly at draft_min_confidence (0.3) should be LOW
        cand = MemoryCandidate(
            candidate_id="c5",
            topic="Boundary test",
            kind=MemoryKind.CONCEPT,
            summary="Testing boundary.",
            confidence=0.3,
        )
        risk = classify_risk(cand, config)
        assert risk == RiskLevel.LOW

    def test_just_below_draft_threshold(self, config: CuratorConfig) -> None:
        cand = MemoryCandidate(
            candidate_id="c6",
            topic="Boundary test",
            kind=MemoryKind.CONCEPT,
            summary="Testing boundary.",
            confidence=0.29,
        )
        risk = classify_risk(cand, config)
        assert risk == RiskLevel.MEDIUM


class TestClassifyRiskBatch:
    def test_batch_updates_all(self, config: CuratorConfig) -> None:
        candidates = [
            MemoryCandidate(
                candidate_id="b1", topic="Normal",
                kind=MemoryKind.CONCEPT, summary="Safe.", confidence=0.9,
            ),
            MemoryCandidate(
                candidate_id="b2", topic="Risky",
                kind=MemoryKind.CONFLICT, summary="Conflict.", confidence=0.8,
            ),
            MemoryCandidate(
                candidate_id="b3", topic="Low conf",
                kind=MemoryKind.CONCEPT, summary="Unsure.", confidence=0.1,
            ),
        ]
        result = classify_risk_batch(candidates, config)
        assert result is candidates
        assert candidates[0].risk == RiskLevel.LOW
        assert candidates[1].risk == RiskLevel.HIGH
        assert candidates[2].risk == RiskLevel.MEDIUM

    def test_empty_batch(self, config: CuratorConfig) -> None:
        result = classify_risk_batch([], config)
        assert result == []


class TestDecideWriteAction:
    def test_blocked_always_blocks(self, config: CuratorConfig) -> None:
        cand = MemoryCandidate(
            candidate_id="w1", topic="Blocked", kind=MemoryKind.CONCEPT,
            summary="Blocked.", confidence=0.9, risk=RiskLevel.BLOCKED,
        )
        decision = decide_write_action(cand, WritePolicy.AUTO, config)
        assert decision.action == "block"
        assert decision.risk == RiskLevel.BLOCKED

    def test_draft_first_always_drafts(
        self, low_risk_candidate: MemoryCandidate, config: CuratorConfig
    ) -> None:
        decision = decide_write_action(
            low_risk_candidate, WritePolicy.DRAFT_FIRST, config)
        assert decision.action == "draft"

    def test_auto_always_writes(
        self, low_risk_candidate: MemoryCandidate, config: CuratorConfig
    ) -> None:
        decision = decide_write_action(
            low_risk_candidate, WritePolicy.AUTO, config)
        assert decision.action == "auto_write"

    def test_semi_auto_low_risk_high_confidence(
        self, low_risk_candidate: MemoryCandidate, config: CuratorConfig
    ) -> None:
        decision = decide_write_action(
            low_risk_candidate, WritePolicy.SEMI_AUTO, config)
        assert decision.action == "auto_write"

    def test_semi_auto_low_risk_low_confidence(self, config: CuratorConfig) -> None:
        cand = MemoryCandidate(
            candidate_id="w2", topic="Unsure", kind=MemoryKind.CONCEPT,
            summary="Maybe.", confidence=0.4, risk=RiskLevel.LOW,
        )
        decision = decide_write_action(cand, WritePolicy.SEMI_AUTO, config)
        assert decision.action == "draft"

    def test_semi_auto_medium_risk_drafts(self, config: CuratorConfig) -> None:
        cand = MemoryCandidate(
            candidate_id="w3", topic="Medium", kind=MemoryKind.CONCEPT,
            summary="Moderate.", confidence=0.9, risk=RiskLevel.MEDIUM,
        )
        decision = decide_write_action(cand, WritePolicy.SEMI_AUTO, config)
        assert decision.action == "draft"

    def test_semi_auto_high_risk_drafts(self, config: CuratorConfig) -> None:
        cand = MemoryCandidate(
            candidate_id="w4", topic="High", kind=MemoryKind.CONCEPT,
            summary="Risky.", confidence=0.9, risk=RiskLevel.HIGH,
        )
        decision = decide_write_action(cand, WritePolicy.SEMI_AUTO, config)
        assert decision.action == "draft"

    def test_decision_includes_reason(
        self, low_risk_candidate: MemoryCandidate, config: CuratorConfig
    ) -> None:
        decision = decide_write_action(
            low_risk_candidate, WritePolicy.SEMI_AUTO, config)
        assert len(decision.reason) > 0
        assert isinstance(decision, WriteDecision)


class TestSecretWriteIsBlocked:
    """Regression guard: a secret-bearing candidate must never be auto-written.

    Reproduces the original defect where classify_risk returned HIGH for hard
    secrets and decide_write_action(AUTO) returned 'auto_write', causing the
    secret to be persisted in cleartext to the on-disk vault.
    """

    def test_sk_live_summary_blocked_under_auto(
        self, sensitive_candidate: MemoryCandidate, config: CuratorConfig
    ) -> None:
        sensitive_candidate.risk = classify_risk(sensitive_candidate, config)
        assert sensitive_candidate.risk == RiskLevel.BLOCKED
        decision = decide_write_action(
            sensitive_candidate, WritePolicy.AUTO, config)
        assert decision.action == "block"
        assert decision.risk == RiskLevel.BLOCKED

    def test_github_pat_summary_blocked_under_auto(self, config: CuratorConfig) -> None:
        cand = MemoryCandidate(
            candidate_id="cand-ghp",
            topic="CI token setup",
            kind=MemoryKind.PROJECT_CONTEXT,
            summary="Use token ghp_abcdefghijklmnopqrstuvwxyz1234567890 for CI.",
            confidence=0.95,
        )
        cand.risk = classify_risk(cand, config)
        assert cand.risk == RiskLevel.BLOCKED
        decision = decide_write_action(cand, WritePolicy.AUTO, config)
        assert decision.action == "block"

    def test_secret_blocked_under_every_policy(
        self, sensitive_candidate: MemoryCandidate, config: CuratorConfig
    ) -> None:
        sensitive_candidate.risk = classify_risk(sensitive_candidate, config)
        for policy in (WritePolicy.AUTO, WritePolicy.SEMI_AUTO, WritePolicy.DRAFT_FIRST):
            decision = decide_write_action(sensitive_candidate, policy, config)
            assert decision.action == "block", f"policy {policy} did not block"


class TestAutoPolicyEnforcesRiskCeiling:
    """AUTO policy must honor auto_write_max_risk (default 'low')."""

    def test_high_risk_pii_drafts_under_auto(self, config: CuratorConfig) -> None:
        cand = MemoryCandidate(
            candidate_id="cand-pii",
            topic="Contact",
            kind=MemoryKind.PROJECT_CONTEXT,
            summary="Owner email is jane.doe@example.com.",
            confidence=0.95,
            risk=RiskLevel.HIGH,
        )
        decision = decide_write_action(cand, WritePolicy.AUTO, config)
        assert decision.action == "draft"
        assert decision.risk == RiskLevel.HIGH

    def test_medium_risk_drafts_under_auto(self, config: CuratorConfig) -> None:
        cand = MemoryCandidate(
            candidate_id="cand-med",
            topic="Maybe",
            kind=MemoryKind.CONCEPT,
            summary="Uncertain note.",
            confidence=0.2,
            risk=RiskLevel.MEDIUM,
        )
        decision = decide_write_action(cand, WritePolicy.AUTO, config)
        assert decision.action == "draft"

    def test_low_risk_auto_writes_under_auto(
        self, low_risk_candidate: MemoryCandidate, config: CuratorConfig
    ) -> None:
        decision = decide_write_action(
            low_risk_candidate, WritePolicy.AUTO, config)
        assert decision.action == "auto_write"


class TestRedactSensitive:
    def test_redacts_email_when_flag_set(self, config: CuratorConfig) -> None:
        out = redact_sensitive("ping jane.doe@example.com please", config)
        assert "jane.doe@example.com" not in out
        assert "[REDACTED]" in out

    def test_redacts_api_key_when_flag_set(self, config: CuratorConfig) -> None:
        out = redact_sensitive(
            "key sk-live-abc123def456ghi789jkl012mno345", config)
        assert "sk-live-abc123def456ghi789jkl012mno345" not in out
        assert "[REDACTED]" in out

    def test_redacts_phone_when_flag_set(self, config: CuratorConfig) -> None:
        out = redact_sensitive("call +1 415-555-0199 now", config)
        assert "415-555-0199" not in out
        assert "[REDACTED]" in out

    def test_no_redaction_when_flags_disabled(self) -> None:
        from dataclasses import replace

        cfg = replace(
            CuratorConfig(),
            redact_emails=False,
            redact_phone_numbers=False,
            redact_api_keys=False,
        )
        text = "ping jane.doe@example.com"
        assert redact_sensitive(text, cfg) == text

    def test_clean_text_unchanged(self, config: CuratorConfig) -> None:
        text = "Use Rust for the MemoryHost implementation"
        assert redact_sensitive(text, config) == text

    def test_hard_secret_helper(self) -> None:
        assert _contains_hard_secret("ghp_abcdefghijklmnopqrstuvwxyz1234567890")
        assert not _contains_hard_secret("jane.doe@example.com")
