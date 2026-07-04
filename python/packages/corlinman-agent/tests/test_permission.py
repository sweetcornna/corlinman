"""Tests for the T3.1 declarative permission gate."""

from __future__ import annotations

import pytest
from corlinman_agent.permission import (
    ALLOW,
    DENY,
    LOG,
    PermissionContext,
    PermissionGate,
    PermissionMode,
    PermissionRule,
    RuleMatch,
)


def test_default_gate_allows_everything() -> None:
    g = PermissionGate()
    assert g.decide("read_file") == ALLOW
    assert g.decide("run_shell") == ALLOW
    assert g.decide("nonexistent_tool") == ALLOW


def test_explicit_rule_wins_over_default() -> None:
    g = PermissionGate(
        [PermissionRule(tool="run_shell", action=DENY)],
        default_action=ALLOW,
    )
    assert g.decide("run_shell") == DENY
    assert g.decide("read_file") == ALLOW


def test_first_match_wins() -> None:
    g = PermissionGate(
        [
            PermissionRule(tool="run_shell", action=ALLOW),
            PermissionRule(tool="*", action=DENY),
        ]
    )
    assert g.decide("run_shell") == ALLOW  # specific rule first
    assert g.decide("read_file") == DENY  # wildcard catches the rest


def test_strict_mode_denies_mutating_tools_by_default() -> None:
    g = PermissionGate(strict=True)
    # Mutating tools default-denied:
    assert g.decide("write_file") == DENY
    assert g.decide("edit_file") == DENY
    assert g.decide("apply_patch") == DENY
    assert g.decide("run_shell") == DENY
    assert g.decide("revert_changes") == DENY
    # Read-only / read-side-only tools still allowed:
    assert g.decide("read_file") == ALLOW
    assert g.decide("search_files") == ALLOW
    assert g.decide("web_search") == ALLOW


def test_strict_mode_explicit_allow_overrides() -> None:
    g = PermissionGate(
        [PermissionRule(tool="run_shell", action=ALLOW)],
        strict=True,
    )
    assert g.decide("run_shell") == ALLOW  # explicit allow wins over strict
    assert g.decide("write_file") == DENY  # other mutators still denied


def test_plan_mode_denies_shell_task_kill_allows_output() -> None:
    """Codex #112: ``shell_task_kill`` mutates (terminates a live process
    group) so plan/strict mode must deny it by default; ``shell_task_output``
    is read-only and stays allowed."""
    g = PermissionGate(mode=PermissionMode.PLAN)
    assert g.decide("shell_task_kill") == DENY
    assert g.decide("shell_task_output") == ALLOW  # read-only, no blast radius


def test_shell_task_kill_inherits_run_shell_grant() -> None:
    """Codex #112 r6: shell_task_kill resolves with run_shell's verdict — an
    explicit run_shell allow rule lets the model terminate the bg tasks it
    started, even in plan/strict mode (where it'd otherwise be denied)."""
    g = PermissionGate(
        [PermissionRule(tool="run_shell", action=ALLOW)],
        mode=PermissionMode.PLAN,
    )
    # run_shell explicitly allowed → its teardown tool is allowed too.
    assert g.decide("run_shell") == ALLOW
    assert g.decide("shell_task_kill") == ALLOW
    # With NO run_shell rule, plan mode still denies the kill (default).
    g2 = PermissionGate(mode=PermissionMode.PLAN)
    assert g2.decide("shell_task_kill") == DENY
    # Strict mode: same inheritance.
    g3 = PermissionGate(
        [PermissionRule(tool="run_shell", action=ALLOW)], strict=True
    )
    assert g3.decide("shell_task_kill") == ALLOW


def test_log_decision_is_observer_only() -> None:
    g = PermissionGate([PermissionRule(tool="run_shell", action=LOG)])
    assert g.decide("run_shell") == LOG


def test_invalid_action_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        PermissionRule(tool="run_shell", action="maybe")
    with pytest.raises(ValueError):
        PermissionGate(default_action="maybe")


def test_from_env_parses_rules_and_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CORLINMAN_AGENT_PERMISSIONS",
        '[{"tool":"run_shell","action":"deny"},'
        '{"tool":"write_file","action":"allow"}]',
    )
    monkeypatch.setenv("CORLINMAN_AGENT_STRICT_MODE", "1")
    g = PermissionGate.from_env()
    assert g.strict is True
    assert g.decide("run_shell") == DENY  # explicit rule
    assert g.decide("write_file") == ALLOW  # explicit override of strict
    assert g.decide("edit_file") == DENY  # strict default for mutator
    assert g.decide("read_file") == ALLOW  # safe tool, no rule, not mutator


def test_from_env_degrades_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORLINMAN_AGENT_PERMISSIONS", "not-json{[")
    monkeypatch.delenv("CORLINMAN_AGENT_STRICT_MODE", raising=False)
    g = PermissionGate.from_env()
    assert g.rules == ()
    assert g.decide("anything") == ALLOW


def test_from_env_ignores_invalid_rule_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CORLINMAN_AGENT_PERMISSIONS",
        '[{"tool":"valid","action":"deny"},'
        '{"tool":"","action":"deny"},'
        '{"action":"deny"},'
        '{"tool":"x","action":"weird"},'
        '"not-a-dict"]',
    )
    g = PermissionGate.from_env()
    # Only the valid entry survives.
    assert len(g.rules) == 1
    assert g.rules[0].tool == "valid"


# ---------------------------------------------------------------------------
# Context-aware matching (decide_with_context)
# ---------------------------------------------------------------------------


def test_decide_with_context_legacy_rule_still_matches() -> None:
    """A rule without a ``match`` block matches every caller context."""
    g = PermissionGate([PermissionRule(tool="write_file", action=DENY)])
    # No context → deny.
    assert g.decide_with_context("write_file") == DENY
    # Rich context → still deny (rule is context-agnostic).
    assert (
        g.decide_with_context(
            "write_file",
            model="claude-sonnet-4-5",
            session_key="qq|self|123|456",
            user_id="alice",
        )
        == DENY
    )
    # And the legacy decide() shim still works.
    assert g.decide("write_file") == DENY


def test_model_glob_match() -> None:
    """``match.model = 'claude-*'`` fires only for matching model names."""
    g = PermissionGate(
        [
            PermissionRule(
                tool="run_shell",
                action=DENY,
                match=RuleMatch(model="claude-*"),
            )
        ]
    )
    assert (
        g.decide_with_context("run_shell", model="claude-sonnet-4-5") == DENY
    )
    assert (
        g.decide_with_context("run_shell", model="claude-opus-4-7") == DENY
    )
    # Non-matching model falls through to default (allow).
    assert g.decide_with_context("run_shell", model="gpt-5.5") == ALLOW
    # Missing model → never matches a non-empty pattern.
    assert g.decide_with_context("run_shell") == ALLOW


def test_session_pattern_match() -> None:
    """``match.session_pattern = 'qq|*'`` only fires for QQ-shaped keys."""
    g = PermissionGate(
        [
            PermissionRule(
                tool="run_shell",
                action=DENY,
                match=RuleMatch(session_pattern="qq|*"),
            )
        ]
    )
    assert (
        g.decide_with_context("run_shell", session_key="qq|self|1|2") == DENY
    )
    assert (
        g.decide_with_context("run_shell", session_key="telegram|self|1|2")
        == ALLOW
    )
    # Missing session_key → no match.
    assert g.decide_with_context("run_shell") == ALLOW


def test_user_pattern_match() -> None:
    """``match.user_pattern = 'admin*'`` only fires for admin-prefixed users."""
    g = PermissionGate(
        [
            PermissionRule(
                tool="run_shell",
                action=ALLOW,
                match=RuleMatch(user_pattern="admin*"),
            ),
            PermissionRule(tool="run_shell", action=DENY),
        ]
    )
    # Admin users match the first (allow) rule.
    assert g.decide_with_context("run_shell", user_id="admin") == ALLOW
    assert g.decide_with_context("run_shell", user_id="admin-2") == ALLOW
    # Non-admin users fall through to the second (deny) rule.
    assert g.decide_with_context("run_shell", user_id="alice") == DENY
    # Missing user_id → first rule can't match → fall through to deny.
    assert g.decide_with_context("run_shell") == DENY


def test_all_match_fields_must_match() -> None:
    """When ``match`` declares multiple filters, ALL must match for the
    rule to apply (logical AND, not OR)."""
    g = PermissionGate(
        [
            PermissionRule(
                tool="run_shell",
                action=DENY,
                match=RuleMatch(model="claude-*", session_pattern="qq|*"),
            )
        ]
    )
    # Both conditions satisfied → deny.
    assert (
        g.decide_with_context(
            "run_shell",
            model="claude-sonnet-4-5",
            session_key="qq|self|1|2",
        )
        == DENY
    )
    # Model matches but session doesn't → no fire, fall through to allow.
    assert (
        g.decide_with_context(
            "run_shell",
            model="claude-sonnet-4-5",
            session_key="telegram|self|1|2",
        )
        == ALLOW
    )
    # Session matches but model doesn't → no fire either.
    assert (
        g.decide_with_context(
            "run_shell",
            model="gpt-5.5",
            session_key="qq|self|1|2",
        )
        == ALLOW
    )


def test_first_match_wins_order_preserved() -> None:
    """Two rules with conflicting actions — first one declared wins."""
    # Order A: allow then deny.
    g_allow_first = PermissionGate(
        [
            PermissionRule(
                tool="run_shell",
                action=ALLOW,
                match=RuleMatch(model="claude-*"),
            ),
            PermissionRule(tool="run_shell", action=DENY),
        ]
    )
    assert (
        g_allow_first.decide_with_context(
            "run_shell", model="claude-sonnet-4-5"
        )
        == ALLOW
    )

    # Order B: deny then allow — first rule (wildcard tool match w/o
    # ``match`` block) fires before the more-specific allow.
    g_deny_first = PermissionGate(
        [
            PermissionRule(tool="run_shell", action=DENY),
            PermissionRule(
                tool="run_shell",
                action=ALLOW,
                match=RuleMatch(model="claude-*"),
            ),
        ]
    )
    assert (
        g_deny_first.decide_with_context(
            "run_shell", model="claude-sonnet-4-5"
        )
        == DENY
    )


def test_audit_log_entry_captures_resolved_decision() -> None:
    """``audit_log_entry`` returns the context + which rule fired."""
    g = PermissionGate(
        [
            PermissionRule(
                tool="run_shell",
                action=DENY,
                match=RuleMatch(model="claude-*", user_pattern="guest*"),
            )
        ]
    )
    ctx = PermissionContext(
        model="claude-sonnet-4-5",
        session_key="qq|self|1|2",
        user_id="guest-7",
    )
    decision, rule_idx = g.resolve("run_shell", ctx)
    entry = g.audit_log_entry("run_shell", ctx, decision, rule_index=rule_idx)
    assert entry["decision"] == DENY
    assert entry["tool"] == "run_shell"
    assert entry["model"] == "claude-sonnet-4-5"
    assert entry["session_key"] == "qq|self|1|2"
    assert entry["user_id"] == "guest-7"
    assert entry["rule_index"] == 0
    assert entry["strict"] is False

    # Default-action path → no rule fired → rule_index is None.
    fallback_ctx = PermissionContext(model="gpt-5.5", user_id="alice")
    decision2, idx2 = g.resolve("run_shell", fallback_ctx)
    entry2 = g.audit_log_entry(
        "run_shell", fallback_ctx, decision2, rule_index=idx2
    )
    assert entry2["decision"] == ALLOW
    assert entry2["rule_index"] is None


def test_from_env_parses_match_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env loader recognises the optional ``match`` block."""
    monkeypatch.setenv(
        "CORLINMAN_AGENT_PERMISSIONS",
        '[{"tool":"run_shell","action":"deny",'
        '"match":{"model":"claude-*","user_pattern":"guest*"}}]',
    )
    monkeypatch.delenv("CORLINMAN_AGENT_STRICT_MODE", raising=False)
    g = PermissionGate.from_env()
    assert len(g.rules) == 1
    rule = g.rules[0]
    assert rule.match is not None
    assert rule.match.model == "claude-*"
    assert rule.match.user_pattern == "guest*"
    assert rule.match.session_pattern is None

    # Behaviour: matches guest+claude, allows otherwise.
    assert (
        g.decide_with_context(
            "run_shell", model="claude-sonnet-4-5", user_id="guest-7"
        )
        == DENY
    )
    assert (
        g.decide_with_context(
            "run_shell", model="claude-sonnet-4-5", user_id="alice"
        )
        == ALLOW
    )


def test_from_env_ignores_malformed_match_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-dict or empty ``match`` block degrades to no-filter (legacy)."""
    monkeypatch.setenv(
        "CORLINMAN_AGENT_PERMISSIONS",
        '[{"tool":"run_shell","action":"deny","match":"not-a-dict"},'
        '{"tool":"write_file","action":"deny","match":{}}]',
    )
    g = PermissionGate.from_env()
    assert len(g.rules) == 2
    assert all(r.match is None for r in g.rules)
