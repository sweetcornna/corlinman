"""Risk Classifier and Write Policy for the Memory Curator system.

Pure-function module that classifies memory candidates by risk level
and decides write actions based on the active write policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.models import MemoryCandidate, MemoryKind, RiskLevel, WritePolicy

# ---------------------------------------------------------------------------
# Sensitive content detection patterns (compiled at module level)
# ---------------------------------------------------------------------------

# Hard secrets: API keys, tokens, credentials, and card numbers. Their mere
# presence makes a candidate unsafe to persist in cleartext under ANY policy,
# so classify_risk escalates these to RiskLevel.BLOCKED (never written).
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # API keys / tokens
    re.compile(r"(?:sk|pk)[-_](?:live|test|prod)?[-_]?[A-Za-z0-9]{20,}", re.IGNORECASE),
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),
    re.compile(r"gho_[A-Za-z0-9]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"xox[bpas]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"glpat-[A-Za-z0-9\-_]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}"),
    # Credit card numbers (basic 13-19 digit patterns with optional separators)
    re.compile(r"\b(?:\d[ \-]?){12,18}\d\b"),
    # URLs with tokens/passwords in query params
    re.compile(
        r"https?://[^\s]*[?&](?:token|password|secret|api_key|apikey|access_token|auth)=[^\s&]+",
        re.IGNORECASE,
    ),
    # Common secret variable names with values
    re.compile(
        r"(?:password|passwd|secret|api_key|apikey|api_secret|access_token|auth_token|private_key)"
        r"\s*[=:]\s*[\"']?[^\s\"',;]{4,}",
        re.IGNORECASE,
    ),
]

# Soft-sensitive: PII (emails, phones, private IPs). These are not hard secrets,
# so they escalate risk to HIGH (drafted / reviewed, and scrubbed by the
# redact_* sanitization pass) rather than being blocked outright.
_PII_PATTERNS: list[re.Pattern[str]] = [
    # Email addresses
    re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
    # Phone numbers (international formats)
    re.compile(r"(?:\+\d{1,3}[\s\-]?)?\(?\d{2,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}"),
    # IP addresses (private ranges)
    re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3})\b"
    ),
]

# Combined view used by the legacy ``_contains_sensitive_content`` helper.
_SENSITIVE_PATTERNS: list[re.Pattern[str]] = [*_SECRET_PATTERNS, *_PII_PATTERNS]

# ---------------------------------------------------------------------------
# Redaction patterns (wired to the redact_* CuratorConfig flags)
# ---------------------------------------------------------------------------

#: Replacement token written in place of redacted sensitive substrings.
_REDACTION_PLACEHOLDER = "[REDACTED]"

# API-key / token / credential patterns scrubbed when ``redact_api_keys`` is set.
_API_KEY_REDACT_PATTERNS: list[re.Pattern[str]] = list(_SECRET_PATTERNS)

# Email patterns scrubbed when ``redact_emails`` is set.
_EMAIL_REDACT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
]

# Phone-number patterns scrubbed when ``redact_phone_numbers`` is set.
_PHONE_REDACT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:\+\d{1,3}[\s\-]?)?\(?\d{2,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}"),
]


# ---------------------------------------------------------------------------
# WriteDecision dataclass
# ---------------------------------------------------------------------------


@dataclass
class WriteDecision:
    """Result of a write-policy evaluation for a memory candidate."""

    action: str  # "auto_write" | "draft" | "block"
    reason: str
    risk: RiskLevel


# Ordering used to compare a candidate's risk against ``auto_write_max_risk``.
_RISK_RANK: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.BLOCKED: 3,
}


def _risk_exceeds_auto_max(risk: RiskLevel, config: CuratorConfig) -> bool:
    """Return True if ``risk`` is above the configured auto-write ceiling."""
    try:
        max_rank = _RISK_RANK[RiskLevel(config.auto_write_max_risk)]
    except (KeyError, ValueError):
        # Unknown threshold -> be conservative and treat anything above LOW as too risky.
        max_rank = _RISK_RANK[RiskLevel.LOW]
    return _RISK_RANK[risk] > max_rank


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------


def _contains_sensitive_content(text: str) -> bool:
    """Check if text matches any sensitive content pattern (secret or PII)."""
    return any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS)


def _contains_hard_secret(text: str) -> bool:
    """Check if text contains a hard secret (API key / credential / card number)."""
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def redact_sensitive(text: str, config: CuratorConfig) -> str:
    """Scrub sensitive substrings from ``text`` per the redact_* config flags.

    Honors ``redact_api_keys`` (API keys / tokens / credentials),
    ``redact_emails`` (email addresses), and ``redact_phone_numbers`` (phone
    numbers). Each matched substring is replaced with ``[REDACTED]`` so secrets
    and PII never reach the on-disk vault in cleartext. Returns the original
    text unchanged when all relevant flags are disabled or nothing matches.
    """
    if not text:
        return text

    patterns: list[re.Pattern[str]] = []
    # API keys first so a credential embedded in a longer match is caught before
    # the coarser PII patterns run.
    if config.redact_api_keys:
        patterns.extend(_API_KEY_REDACT_PATTERNS)
    if config.redact_emails:
        patterns.extend(_EMAIL_REDACT_PATTERNS)
    if config.redact_phone_numbers:
        patterns.extend(_PHONE_REDACT_PATTERNS)

    redacted = text
    for pattern in patterns:
        redacted = pattern.sub(_REDACTION_PLACEHOLDER, redacted)
    return redacted


def classify_risk(candidate: MemoryCandidate, config: CuratorConfig) -> RiskLevel:
    """Classify the risk level of a memory candidate.

    Checks evidence and summary against sensitive patterns, candidate kind,
    and confidence thresholds to determine risk.

    Args:
        candidate: The memory candidate to classify.
        config: Curator configuration with threshold values.

    Returns:
        The determined RiskLevel.
    """
    texts_to_check = [candidate.summary, *candidate.evidence]

    # Hard secrets (API keys / credentials / card numbers) -> BLOCKED.
    # decide_write_action refuses to write BLOCKED candidates under any policy,
    # so a secret-bearing candidate can never reach the on-disk vault.
    for text in texts_to_check:
        if _contains_hard_secret(text):
            return RiskLevel.BLOCKED

    # Soft-sensitive PII (emails / phones / private IPs) -> HIGH. These are
    # drafted for review and scrubbed by the redact_* sanitization pass rather
    # than blocked outright.
    for text in texts_to_check:
        if _contains_sensitive_content(text):
            return RiskLevel.HIGH

    # Conflict kind -> HIGH
    if candidate.kind == MemoryKind.CONFLICT:
        return RiskLevel.HIGH

    # Low confidence below draft threshold -> MEDIUM
    if candidate.confidence < config.draft_min_confidence:
        return RiskLevel.MEDIUM

    # Unconfirmed persona inference -> MEDIUM
    if candidate.kind == MemoryKind.AGENT_PERSONA and candidate.confidence < 0.7:
        return RiskLevel.MEDIUM

    return RiskLevel.LOW


# ---------------------------------------------------------------------------
# Batch classification
# ---------------------------------------------------------------------------


def classify_risk_batch(
    candidates: list[MemoryCandidate], config: CuratorConfig
) -> list[MemoryCandidate]:
    """Classify risk for a batch of candidates, mutating in-place.

    Args:
        candidates: List of memory candidates to classify.
        config: Curator configuration with threshold values.

    Returns:
        The same list with each candidate's risk field updated.
    """
    for candidate in candidates:
        candidate.risk = classify_risk(candidate, config)
    return candidates


# ---------------------------------------------------------------------------
# Write policy decision
# ---------------------------------------------------------------------------


def decide_write_action(
    candidate: MemoryCandidate,
    policy: WritePolicy,
    config: CuratorConfig,
) -> WriteDecision:
    """Decide the write action for a candidate based on policy and risk.

    Args:
        candidate: The memory candidate (should already have risk classified).
        policy: The active write policy.
        config: Curator configuration.

    Returns:
        A WriteDecision with action, reason, and risk.
    """
    risk = candidate.risk

    # BLOCKED risk always blocks regardless of policy
    if risk == RiskLevel.BLOCKED:
        return WriteDecision(
            action="block",
            reason="Candidate risk is BLOCKED; cannot be written.",
            risk=risk,
        )

    # DRAFT_FIRST: always draft
    if policy == WritePolicy.DRAFT_FIRST:
        return WriteDecision(
            action="draft",
            reason="Policy is DRAFT_FIRST; all candidates go to draft.",
            risk=risk,
        )

    # AUTO: auto_write only up to the configured auto_write_max_risk ceiling
    # (blocked already handled above). Anything riskier than the ceiling is
    # drafted for manual review instead of being written automatically.
    if policy == WritePolicy.AUTO:
        if _risk_exceeds_auto_max(risk, config):
            return WriteDecision(
                action="draft",
                reason=(
                    f"Policy is AUTO but risk ({risk.value}) exceeds "
                    f"auto_write_max_risk ({config.auto_write_max_risk}); drafting."
                ),
                risk=risk,
            )
        return WriteDecision(
            action="auto_write",
            reason="Policy is AUTO; writing automatically.",
            risk=risk,
        )

    # SEMI_AUTO: decision based on risk and confidence
    if policy == WritePolicy.SEMI_AUTO:
        if risk == RiskLevel.LOW:
            if candidate.confidence >= 0.6:
                return WriteDecision(
                    action="auto_write",
                    reason=f"Low risk with sufficient confidence ({candidate.confidence:.2f} >= 0.60).",
                    risk=risk,
                )
            else:
                return WriteDecision(
                    action="draft",
                    reason=f"Low risk but confidence too low ({candidate.confidence:.2f} < 0.60); drafting.",
                    risk=risk,
                )
        elif risk == RiskLevel.MEDIUM:
            return WriteDecision(
                action="draft",
                reason="Medium risk; requires review before writing.",
                risk=risk,
            )
        elif risk == RiskLevel.HIGH:
            return WriteDecision(
                action="draft",
                reason="High risk detected; drafting with warning for manual review.",
                risk=risk,
            )

    # Fallback: conservative default
    return WriteDecision(
        action="draft",
        reason="Fallback: unknown policy or state; defaulting to draft.",
        risk=risk,
    )


__all__ = [
    "WriteDecision",
    "classify_risk",
    "classify_risk_batch",
    "decide_write_action",
    "redact_sensitive",
]
