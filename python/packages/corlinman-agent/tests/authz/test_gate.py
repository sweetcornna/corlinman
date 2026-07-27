"""AuthzGate — the call-time evaluator (W3-1's reason to exist).

The legacy ``PermissionGate`` froze rules/strict/mode at construction and
was built once per process, so no configuration change could ever take
effect without a restart (fact M5). These tests pin the opposite: a
config write via :func:`apply_permissions_config` changes the verdict of
the NEXT ``resolve`` on an already-constructed gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_agent.authz.defaults import (
    apply_permissions_config,
    reset_permissions_defaults,
)
from corlinman_agent.authz.gate import AuthzGate
from corlinman_agent.authz.grants import GrantStore
from corlinman_agent.authz.model import ALLOW, ASK, DENY, PermissionMode, Subject

_CTX = Subject()


@pytest.fixture()
def gate(tmp_path: Path) -> AuthzGate:
    """A gate rooted in tmp so the settings-file layers read nothing."""
    return AuthzGate(
        data_dir=tmp_path,
        project_dir=tmp_path / "proj",
        grant_store=GrantStore(tmp_path),
    )


# ---------------------------------------------------------------------------
# Config takes effect WITHOUT reconstructing the gate (acceptance 1 core)
# ---------------------------------------------------------------------------


def test_config_rule_applies_to_existing_gate(gate: AuthzGate) -> None:
    assert gate.resolve_with_args("run_shell", _CTX, {"command": "rm x"})[0] == ALLOW
    apply_permissions_config(
        {"rules": [{"tool": "run_shell(rm:*)", "action": "deny"}]}
    )
    assert gate.resolve_with_args("run_shell", _CTX, {"command": "rm x"})[0] == DENY
    assert gate.resolve_with_args("run_shell", _CTX, {"command": "ls"})[0] == ALLOW
    # …and removing the block restores the default, still no rebuild.
    reset_permissions_defaults()
    assert gate.resolve_with_args("run_shell", _CTX, {"command": "rm x"})[0] == ALLOW


def test_config_strict_applies_hot(gate: AuthzGate) -> None:
    """Acceptance 2: strict flips a MUTATING tool to deny on the very next
    resolve — the M5 construction-time freeze is gone."""
    assert gate.resolve_with_args("write_file", _CTX, {"path": "x"})[0] == ALLOW
    apply_permissions_config({"strict": True})
    assert gate.resolve_with_args("write_file", _CTX, {"path": "x"})[0] == DENY
    # Read-only tools stay allowed under strict.
    assert gate.resolve_with_args("read_file", _CTX, {"path": "x"})[0] == ALLOW


def test_agent_runtime_strict_mode_applies_hot(gate: AuthzGate) -> None:
    """The [agent_runtime].strict_mode spelling (the #172 knob) flows
    through the SAME deduplicated resolve_strict chain."""
    from corlinman_agent.runtime_defaults import (
        apply_agent_runtime_config,
        reset_agent_runtime_defaults,
    )

    try:
        assert gate.resolve_with_args("write_file", _CTX, {"path": "x"})[0] == ALLOW
        apply_agent_runtime_config({"strict_mode": True})
        assert gate.resolve_with_args("write_file", _CTX, {"path": "x"})[0] == DENY
        # [permissions].strict outranks the legacy spelling (C5 chain).
        apply_permissions_config({"strict": False})
        assert gate.resolve_with_args("write_file", _CTX, {"path": "x"})[0] == ALLOW
    finally:
        reset_agent_runtime_defaults()


def test_config_rules_outrank_env_rules(
    gate: AuthzGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C5: config > env. Both layers name the same call; config wins via
    last-match-wins stacking (config rules are appended after env rules)."""
    monkeypatch.setenv(
        "CORLINMAN_AGENT_PERMISSIONS",
        '[{"tool": "run_shell", "action": "deny"}]',
    )
    assert gate.resolve_with_args("run_shell", _CTX, {"command": "ls"})[0] == DENY
    apply_permissions_config({"rules": [{"tool": "run_shell", "action": "allow"}]})
    assert gate.resolve_with_args("run_shell", _CTX, {"command": "ls"})[0] == ALLOW


def test_env_only_deployment_semantics_preserved(
    gate: AuthzGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 5: an env-only deployment keeps its rule semantics —
    non-overlapping rules verdict identically regardless of scan order."""
    monkeypatch.setenv(
        "CORLINMAN_AGENT_PERMISSIONS",
        '[{"tool": "run_shell", "action": "deny"},'
        ' {"tool": "web_search", "action": "log"}]',
    )
    assert gate.resolve_with_args("run_shell", _CTX, None)[0] == DENY
    assert gate.resolve_with_args("web_search", _CTX, None)[0] == "log"
    assert gate.resolve_with_args("read_file", _CTX, None)[0] == ALLOW


def test_scoped_rule_reaches_gate(gate: AuthzGate) -> None:
    """tenant/surface scopes flow from config to verdict."""
    apply_permissions_config(
        {
            "rules": [
                {
                    "tool": "run_shell",
                    "action": "deny",
                    "scope": {"surface": "qq|telegram"},
                }
            ]
        }
    )
    assert gate.resolve_with_args("run_shell", Subject(surface="qq"), None)[0] == DENY
    assert (
        gate.resolve_with_args("run_shell", Subject(surface="console"), None)[0]
        == ALLOW
    )
    # Missing surface never matches a declared surface scope.
    assert gate.resolve_with_args("run_shell", _CTX, None)[0] == ALLOW


# ---------------------------------------------------------------------------
# Grants — narrow ``ask`` only, never widen past ``deny``
# ---------------------------------------------------------------------------


def test_grant_turns_ask_into_allow(gate: AuthzGate) -> None:
    apply_permissions_config({"rules": [{"tool": "run_shell", "action": "ask"}]})
    ctx = Subject(session_key="t::s1")
    args = {"command": "ls"}
    assert gate.resolve_with_args("run_shell", ctx, args)[0] == ASK
    gate.grant_store.record(ctx, "run_shell", args, "session")
    assert gate.resolve_with_args("run_shell", ctx, args)[0] == ALLOW
    # A different argument shape is NOT covered.
    assert gate.resolve_with_args("run_shell", ctx, {"command": "rm x"})[0] == ASK


def test_grant_never_overrides_deny(gate: AuthzGate) -> None:
    """Hard rule 3 (plan §1.2.4): the rule scan returns deny before the
    grant lookup ever runs."""
    apply_permissions_config(
        {"rules": [{"tool": "run_shell(rm:*)", "action": "deny"}]}
    )
    ctx = Subject(session_key="t::s1")
    args = {"command": "rm -rf /"}
    gate.grant_store.record(ctx, "run_shell", args, "always")
    assert gate.resolve_with_args("run_shell", ctx, args)[0] == DENY


def test_set_mode_clears_session_grants(gate: AuthzGate) -> None:
    """Acceptance 7: entering /plan invalidates session grants — an ask
    rule resolves before the mode override, so a cached grant would
    otherwise bypass plan mode entirely (Codex #104)."""
    apply_permissions_config({"rules": [{"tool": "run_shell", "action": "ask"}]})
    ctx = Subject(session_key="t::s1")
    args = {"command": "ls"}
    gate.grant_store.record(ctx, "run_shell", args, "session")
    assert gate.resolve_with_args("run_shell", ctx, args)[0] == ALLOW
    gate.set_mode(PermissionMode.PLAN)
    assert gate.resolve_with_args("run_shell", ctx, args)[0] == ASK


# ---------------------------------------------------------------------------
# Mode override + coercion noise
# ---------------------------------------------------------------------------


def test_mode_override_outranks_config_mode(gate: AuthzGate) -> None:
    apply_permissions_config({"mode": "bypass"})
    assert gate.mode is PermissionMode.BYPASS
    gate.set_mode("plan")
    assert gate.mode is PermissionMode.PLAN
    assert gate.resolve_with_args("write_file", _CTX, {"path": "x"})[0] == DENY


def test_unknown_config_mode_degrades_loudly_to_default(gate: AuthzGate) -> None:
    """PermissionMode.coerce no longer silently degrades — but it still
    returns DEFAULT (permissions are config-driven; boot must survive)."""
    apply_permissions_config({"mode": "planning"})  # the plan's typo example
    assert gate.mode is PermissionMode.DEFAULT
    assert gate.resolve_with_args("write_file", _CTX, {"path": "x"})[0] == ALLOW


# ---------------------------------------------------------------------------
# C3 migration aid — env-only overlap gets a WARN with the verdict diff
# ---------------------------------------------------------------------------


def test_c3_flip_warns_for_env_only_overlapping_rules(
    gate: AuthzGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 5's WARN half: an env-only deployment whose overlapping
    rules verdict differently under the C3 order flip gets one WARN with
    the per-tool diff. The verdict itself follows the new order."""
    import structlog

    monkeypatch.setenv(
        "CORLINMAN_AGENT_PERMISSIONS",
        # Unique tool name so the module-level memoization from other
        # tests can never have seen this fingerprint.
        '[{"tool": "qzone_publish", "action": "deny"},'
        ' {"tool": "*", "action": "allow"}]',
    )
    with structlog.testing.capture_logs() as logs:
        verdict = gate.resolve_with_args("qzone_publish", _CTX, None)[0]
    # Under last-match-wins the trailing catch-all overrides the deny —
    # exactly the flip the WARN exists to surface.
    assert verdict == ALLOW
    flips = [
        entry
        for entry in logs
        if entry.get("event") == "agent.permission.last_match_wins_flipped"
    ]
    assert flips and "qzone_publish" in flips[0]["diff"]
