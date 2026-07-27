"""Unit coverage of the ``[approvals]`` → ``[permissions]`` translator
(audit W3-2, plan §2.2). Verdict-equivalence against the legacy gateway
matcher lives server-side (``test_authz_approvals_translation_property``)
because the legacy ``match_rule`` cannot be imported from here.
"""

from __future__ import annotations

import json

from corlinman_agent.authz import Subject
from corlinman_agent.authz.approvals_compat import (
    merge_approvals_into_permissions,
    translate_approvals_rules,
)
from corlinman_agent.permission import PermissionGate, parse_rule_list


def _gate(rules: list[dict]) -> PermissionGate:
    return PermissionGate(parse_rule_list(json.dumps(rules)))


def test_translation_table_rows() -> None:
    """The §2.2 table, row by row."""
    rules = translate_approvals_rules(
        {
            "rules": [
                {"plugin": "file-ops", "mode": "auto"},
                {"plugin": "file-ops", "tool": "write", "mode": "deny"},
                {"plugin": "net", "tool": "fetch", "mode": "prompt"},
            ]
        }
    )
    by_tool = {r["tool"]: r["action"] for r in rules}
    assert by_tool["plugin:file-ops/*"] == "allow"
    assert by_tool["plugin:file-ops/write"] == "deny"
    assert by_tool["plugin:net/fetch"] == "ask"


def test_general_rules_emitted_before_specific() -> None:
    """Risk R7 / §2.2: reverse-specificity output so last-match-wins
    reproduces the legacy "exact beats plugin-wide" preference."""
    rules = translate_approvals_rules(
        {
            "rules": [
                {"plugin": "p", "tool": "t", "mode": "deny"},
                {"plugin": "p", "mode": "auto"},
            ]
        }
    )
    tools = [r["tool"] for r in rules]
    assert tools.index("plugin:p/*") < tools.index("plugin:p/t")
    gate = _gate(rules)
    assert gate.resolve_external_with_args(("plugin:p/t",), Subject(), None)[0] == "deny"
    assert gate.resolve_external_with_args(("plugin:p/other",), Subject(), None)[0] == "allow"


def test_first_declaration_wins_within_tier() -> None:
    """The legacy matcher took the FIRST matching rule per tier; a later
    duplicate declaration must be dropped, not override."""
    rules = translate_approvals_rules(
        {
            "rules": [
                {"plugin": "p", "tool": "t", "mode": "auto"},
                {"plugin": "p", "tool": "t", "mode": "deny"},
            ]
        }
    )
    assert [r["action"] for r in rules] == ["allow"]


def test_session_whitelist_becomes_scoped_allow() -> None:
    rules = translate_approvals_rules(
        {
            "rules": [
                {
                    "plugin": "p",
                    "tool": "t",
                    "mode": "prompt",
                    "allow_session_keys": ["trusted-1"],
                },
            ]
        }
    )
    gate = _gate(rules)
    keys = ("plugin:p/t",)
    # Whitelisted session: allow without prompting.
    allowed = gate.resolve_external_with_args(
        keys, Subject(session_key="trusted-1"), None
    )
    assert allowed[0] == "allow"
    # Any other session still prompts.
    other = gate.resolve_external_with_args(
        keys, Subject(session_key="random"), None
    )
    assert other[0] == "ask"


def test_exact_deny_beats_plugin_wide_whitelist() -> None:
    """Legacy: the exact tier wins outright, even against a whitelisted
    session on the plugin-wide prompt rule."""
    rules = translate_approvals_rules(
        {
            "rules": [
                {
                    "plugin": "p",
                    "mode": "prompt",
                    "allow_session_keys": ["trusted-1"],
                },
                {"plugin": "p", "tool": "t", "mode": "deny"},
            ]
        }
    )
    gate = _gate(rules)
    denied = gate.resolve_external_with_args(
        ("plugin:p/t",), Subject(session_key="trusted-1"), None
    )
    assert denied[0] == "deny"
    # Plugin-wide + whitelist still allows the OTHER tools.
    allowed = gate.resolve_external_with_args(
        ("plugin:p/other",), Subject(session_key="trusted-1"), None
    )
    assert allowed[0] == "allow"


def test_unknown_mode_and_missing_plugin_skipped() -> None:
    rules = translate_approvals_rules(
        {
            "rules": [
                {"plugin": "p", "mode": "yolo"},
                {"tool": "orphan", "mode": "auto"},
                "not-a-table",
            ]
        }
    )
    assert rules == []
    assert translate_approvals_rules(None) == []
    assert translate_approvals_rules("garbage") == []


def test_merge_prepends_translated_rules() -> None:
    merged = merge_approvals_into_permissions(
        {"rules": [{"plugin": "p", "mode": "deny"}]},
        {"mode": "default", "rules": [{"tool": "plugin:p/*", "action": "allow"}]},
    )
    assert merged is not None
    assert merged["mode"] == "default"
    tools_actions = [(r["tool"], r["action"]) for r in merged["rules"]]
    # Translated first → the operator's [permissions] rule wins under
    # last-match-wins (risk R7).
    assert tools_actions[0] == ("plugin:p/*", "deny")
    assert tools_actions[-1] == ("plugin:p/*", "allow")


def test_merge_without_approvals_is_identity() -> None:
    perms = {"mode": "plan"}
    assert merge_approvals_into_permissions(None, perms) == perms
    assert merge_approvals_into_permissions(None, None) is None
