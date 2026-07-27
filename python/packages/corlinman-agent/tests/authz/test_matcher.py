"""Matcher-core tests — the W3-1 additions (tenant / surface scopes,
memory / note fields, the ``scope`` spelling).

The legacy grammar (``tool(pattern)`` sugar, SEC-05 candidates, fnmatch)
keeps its coverage in ``test_permission.py`` / ``test_gf_permissions_wire``
/ ``test_fix_SEC05_per_arg_deny_bypass``; this file only pins what W3-1
added.
"""

from __future__ import annotations

import json

from corlinman_agent.authz.matcher import (
    PermissionRule,
    RuleMatch,
    parse_rule_list,
)
from corlinman_agent.authz.model import ALLOW, DENY, Subject

# ---------------------------------------------------------------------------
# surface — exact `|` alternation over a closed set, NOT fnmatch
# ---------------------------------------------------------------------------


def test_surface_alternation_exact_segments() -> None:
    m = RuleMatch(surface="qq|telegram")
    assert m.matches(Subject(surface="qq"))
    assert m.matches(Subject(surface="Telegram"))  # case-insensitive
    assert not m.matches(Subject(surface="discord"))


def test_surface_is_not_a_glob() -> None:
    """``qq*`` must NOT accidentally cover both qq and qq_official — the
    design plan singles this out as a foreseeable operations accident."""
    m = RuleMatch(surface="qq*")
    assert not m.matches(Subject(surface="qq"))
    assert not m.matches(Subject(surface="qq_official"))
    # The literal segment still matches itself, nothing else.
    assert m.matches(Subject(surface="qq*"))


def test_missing_surface_never_matches_declared_pattern() -> None:
    """Principle (b): a missing context value never matches a non-empty
    pattern — same contract the legacy user/session/model filters keep."""
    m = RuleMatch(surface="qq")
    assert not m.matches(Subject())
    assert not m.matches(Subject(surface=""))


# ---------------------------------------------------------------------------
# tenant — fnmatch like the other glob scopes
# ---------------------------------------------------------------------------


def test_tenant_scope_glob_and_missing_value() -> None:
    m = RuleMatch(tenant="acme*")
    assert m.matches(Subject(tenant_id="acme"))
    assert m.matches(Subject(tenant_id="acme-eu"))
    assert not m.matches(Subject(tenant_id="globex"))
    assert not m.matches(Subject())  # missing tenant → no match


def test_all_declared_scopes_are_anded() -> None:
    m = RuleMatch(tenant="acme", surface="qq")
    assert m.matches(Subject(tenant_id="acme", surface="qq"))
    assert not m.matches(Subject(tenant_id="acme", surface="web"))
    assert not m.matches(Subject(tenant_id="globex", surface="qq"))


# ---------------------------------------------------------------------------
# rule wiring — scope spelling, memory / note carry-through
# ---------------------------------------------------------------------------


def test_parse_rule_list_scope_spelling_and_new_fields() -> None:
    raw = json.dumps(
        [
            {
                "tool": "mcp:github/*",
                "action": "ask",
                "memory": "session",
                "note": "review first",
                "scope": {"tenant": "acme", "surface": "qq|web", "user": "admin*"},
            }
        ]
    )
    (rule,) = parse_rule_list(raw)
    assert rule.tool == "mcp:github/*"
    assert rule.memory == "session"
    assert rule.note == "review first"
    assert rule.match is not None
    assert rule.match.tenant == "acme"
    assert rule.match.surface == "qq|web"
    assert rule.match.user_pattern == "admin*"


def test_parse_rule_list_scope_wins_over_legacy_match() -> None:
    raw = json.dumps(
        [
            {
                "tool": "run_shell",
                "action": "deny",
                "scope": {"user": "admin*"},
                "match": {"user_pattern": "everyone*"},
            }
        ]
    )
    (rule,) = parse_rule_list(raw)
    assert rule.match is not None and rule.match.user_pattern == "admin*"


def test_parse_rule_list_invalid_memory_degrades_to_none() -> None:
    raw = json.dumps(
        [{"tool": "run_shell", "action": "allow", "memory": "forever"}]
    )
    (rule,) = parse_rule_list(raw)
    assert rule.memory is None


def test_rule_applies_with_scoped_subject_end_to_end() -> None:
    rule = PermissionRule(
        tool="run_shell(rm:*)",
        action=DENY,
        match=RuleMatch(surface="qq"),
    )
    qq = Subject(surface="qq")
    web = Subject(surface="web")
    assert rule.applies_to_args("run_shell", qq, ["rm:rm -rf /"])
    assert not rule.applies_to_args("run_shell", web, ["rm:rm -rf /"])
    assert not rule.applies_to_args("run_shell", qq, ["ls:ls -la"])
    assert rule.action != ALLOW
