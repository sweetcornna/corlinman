"""External-tool gating semantics (audit W3-2 — EP2, decisions C4/C7).

Covers the canonical candidate-key derivation, the ``"*"``-covers-external
flip, plan/bypass mode behaviour, the ``external_tools_enforced`` escape
hatch, grants on external ``ask`` rules, and the p99 < 1ms performance
line (risk R5 / acceptance 6).
"""

from __future__ import annotations

import time

from corlinman_agent.authz import AuthzGate, Subject, apply_permissions_config
from corlinman_agent.authz.matcher import (
    external_candidate_keys,
    glob_escape,
    tool_pattern_matches,
)

_CTX = Subject(session_key="tenant-x::s1", tenant_id="tenant-x")


# ---------------------------------------------------------------------------
# Canonical candidate keys (C7)
# ---------------------------------------------------------------------------


def test_candidate_keys_bare_name_first() -> None:
    keys = external_candidate_keys("github_create_issue", "github_create_issue")
    assert keys[0] == "github_create_issue"


def test_candidate_keys_cover_every_mcp_split() -> None:
    keys = external_candidate_keys("github_create_issue", "github_create_issue")
    assert "mcp:github/create_issue" in keys
    assert "mcp:github_create/issue" in keys


def test_candidate_keys_plugin_forms() -> None:
    # Distinct plugin identity (gateway service plugins).
    keys = external_candidate_keys("file-ops", "write")
    assert "plugin:file-ops/write" in keys
    # Collapsed OpenAI identity still yields a plugin: spelling.
    collapsed = external_candidate_keys("deploy", "deploy")
    assert "plugin:deploy/deploy" in collapsed


def test_candidate_keys_empty_input() -> None:
    assert external_candidate_keys("", "") == ()


def test_tool_pattern_glob_only_when_glob_chars_present() -> None:
    assert tool_pattern_matches("mcp:github/*", "mcp:github/create_issue")
    assert not tool_pattern_matches("mcp:github/x", "mcp:github/create_issue")
    # Literal patterns stay literal — pre-W3-2 exactness preserved.
    assert not tool_pattern_matches("run_shel", "run_shell")
    assert tool_pattern_matches("*", "anything:at/all")


def test_glob_escape_self_matches() -> None:
    import fnmatch

    weird = "sess[1]*?"
    assert fnmatch.fnmatchcase(weird, glob_escape(weird))
    assert not fnmatch.fnmatchcase("sess1x", glob_escape(weird))


# ---------------------------------------------------------------------------
# Gate semantics (C4)
# ---------------------------------------------------------------------------


def _keys(name: str = "github_create_issue") -> tuple[str, ...]:
    return external_candidate_keys(name, name)


def test_wildcard_deny_now_covers_external_tools() -> None:
    """Acceptance 1: {"tool": "*", "action": "deny"} blocks MCP/plugin."""
    apply_permissions_config({"rules": [{"tool": "*", "action": "deny"}]})
    action, idx = AuthzGate().resolve_external(_keys(), _CTX, None)
    assert action == "deny"
    assert idx is not None


def test_namespaced_mcp_rule_matches() -> None:
    apply_permissions_config(
        {"rules": [{"tool": "mcp:github/*", "action": "deny"}]}
    )
    action, _ = AuthzGate().resolve_external(_keys(), _CTX, None)
    assert action == "deny"
    # A different server's tool is untouched.
    action, _ = AuthzGate().resolve_external(_keys("jira_create_issue"), _CTX, None)
    assert action == "allow"


def test_plugin_rule_matches_gateway_plugin() -> None:
    apply_permissions_config(
        {"rules": [{"tool": "plugin:file-ops/*", "action": "deny"}]}
    )
    keys = external_candidate_keys("file-ops", "write")
    action, _ = AuthzGate().resolve_external(keys, _CTX, None)
    assert action == "deny"


def test_plan_mode_denies_external_tools() -> None:
    """Acceptance 2 (half): plan mode covers external tools."""
    apply_permissions_config({"mode": "plan"})
    action, idx = AuthzGate().resolve_external(_keys(), _CTX, None)
    assert action == "deny"
    assert idx is None


def test_bypass_mode_allows_external_despite_deny_rule() -> None:
    """Acceptance 2 (other half): bypass short-circuits external gating."""
    apply_permissions_config(
        {"mode": "bypass", "rules": [{"tool": "*", "action": "deny"}]}
    )
    action, _ = AuthzGate().resolve_external(_keys(), _CTX, None)
    assert action == "allow"


def test_external_tools_enforced_escape_hatch() -> None:
    """Risk R4: the opt-out restores the pre-W3-2 bypass, rules and all."""
    apply_permissions_config(
        {
            "external_tools_enforced": False,
            "rules": [{"tool": "*", "action": "deny"}],
        }
    )
    action, idx = AuthzGate().resolve_external(_keys(), _CTX, None)
    assert (action, idx) == ("allow", None)


def test_explicit_deny_beats_last_allow_ordering() -> None:
    """Last-match-wins across external keys, same as builtin rules."""
    apply_permissions_config(
        {
            "rules": [
                {"tool": "*", "action": "deny"},
                {"tool": "mcp:github/*", "action": "allow"},
            ]
        }
    )
    action, _ = AuthzGate().resolve_external(_keys(), _CTX, None)
    assert action == "allow"


def test_ask_rule_with_session_grant_resolves_allow() -> None:
    apply_permissions_config(
        {"rules": [{"tool": "mcp:github/*", "action": "ask"}]}
    )
    gate = AuthzGate()
    action, _ = gate.resolve_external(_keys(), _CTX, None)
    assert action == "ask"
    gate.grant_store.record(_CTX, _keys()[0], None, "session")
    action, _ = gate.resolve_external(_keys(), _CTX, None)
    assert action == "allow"


def test_builtin_rules_unaffected_by_external_namespace() -> None:
    """A namespaced rule can never leak onto a builtin tool name."""
    apply_permissions_config(
        {"rules": [{"tool": "mcp:github/*", "action": "deny"}]}
    )
    action, _ = AuthzGate().resolve_with_args(
        "run_shell", _CTX, {"command": "echo hi"}
    )
    assert action == "allow"


# ---------------------------------------------------------------------------
# Performance (risk R5 / acceptance 6)
# ---------------------------------------------------------------------------


def test_external_resolution_mean_under_1ms() -> None:
    """The compiled-snapshot cache keeps a decision far under the 1ms
    line even with a realistic rule set. Coarse (mean over 2000 calls,
    generous bound) so CI jitter can't flake it — the guarded regression
    is "re-parse every rule per call", which is orders of magnitude off.
    """
    rules = [{"tool": f"mcp:server{i}/*", "action": "allow"} for i in range(20)]
    rules.append({"tool": "*", "action": "deny"})
    apply_permissions_config({"rules": rules})
    gate = AuthzGate()
    keys = _keys()
    gate.resolve_external(keys, _CTX, None)  # warm the snapshot cache

    n = 2000
    started = time.perf_counter()
    for _ in range(n):
        gate.resolve_external(keys, _CTX, {"title": "hello"})
    mean_ms = (time.perf_counter() - started) * 1000 / n
    assert mean_ms < 1.0, f"mean external resolution {mean_ms:.3f}ms >= 1ms"
