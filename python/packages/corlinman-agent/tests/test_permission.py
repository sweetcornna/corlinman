"""Tests for the T3.1 declarative permission gate."""

from __future__ import annotations

import pytest

from corlinman_agent.permission import (
    ALLOW,
    DENY,
    LOG,
    PermissionGate,
    PermissionRule,
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
