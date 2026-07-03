"""gap-fill wire-A — unified permissions + approval gate.

Covers the gaps:

* ``permissions-no-ask-action`` — the new ``ask`` verdict routed through
  :class:`~corlinman_agent.approval_gate.ApprovalGate` (prompt-and-wait,
  fail-closed when no resolver).
* ``permissions-no-per-arg-rules`` — per-argument / command-pattern rules
  (``run_shell(rm:*)``) with first- and last-match-wins ordering and
  layered rule sources.
* ``permissions-no-permission-mode`` — the :class:`PermissionMode` enum
  (acceptEdits / plan / bypass / default) layered above the rule list.
"""

from __future__ import annotations

import asyncio

from corlinman_agent.approval_gate import ApprovalGate, ApprovalVerdict
from corlinman_agent.permission import (
    ALLOW,
    ASK,
    DENY,
    PermissionContext,
    PermissionGate,
    PermissionMode,
    PermissionRule,
    extract_primary_arg,
    parse_rule_list,
)

_CTX = PermissionContext()


# --------------------------------------------------------------------------
# Per-argument / command-pattern rules
# --------------------------------------------------------------------------


def test_command_pattern_sugar_parses_arg_pattern() -> None:
    rule = PermissionRule(tool="run_shell(rm:*)", action=DENY)
    assert rule.tool == "run_shell"
    assert rule.arg_pattern == "rm:*"


def test_run_shell_rm_pattern_denies_only_rm() -> None:
    gate = PermissionGate(
        [
            PermissionRule(tool="run_shell(rm:*)", action=DENY),
            PermissionRule(tool="*", action=ALLOW),
        ]
    )
    rm = gate.resolve_with_args("run_shell", _CTX, {"command": "rm -rf /tmp/x"})
    ls = gate.resolve_with_args("run_shell", _CTX, {"command": "ls -la"})
    assert rm[0] == DENY
    assert ls[0] == ALLOW


def test_arg_unaware_resolve_does_not_overmatch_arg_rule() -> None:
    # A narrowed rule must NOT fire on the args-unaware ``resolve``.
    gate = PermissionGate(
        [
            PermissionRule(tool="run_shell(rm:*)", action=DENY),
            PermissionRule(tool="*", action=ALLOW),
        ]
    )
    # Without args, the rm:* rule is skipped; the catch-all allow wins.
    assert gate.resolve("run_shell", _CTX)[0] == ALLOW


def test_extract_primary_arg_shapes() -> None:
    assert extract_primary_arg(
        "run_shell", {"command": "rm -rf x"}
    ).startswith("rm:")
    assert extract_primary_arg("write_file", {"path": "/a/b.txt"}) == "/a/b.txt"
    assert extract_primary_arg("run_shell", {}) is None


def test_last_match_wins_lets_later_rule_override() -> None:
    gate = PermissionGate(
        [
            PermissionRule(tool="run_shell", action=ALLOW),
            PermissionRule(tool="run_shell(rm:*)", action=DENY),
        ],
        last_match_wins=True,
    )
    out = gate.resolve_with_args("run_shell", _CTX, {"command": "rm x"})
    assert out[0] == DENY


def test_first_match_wins_default() -> None:
    gate = PermissionGate(
        [
            PermissionRule(tool="run_shell", action=ALLOW),
            PermissionRule(tool="run_shell(rm:*)", action=DENY),
        ]
    )
    out = gate.resolve_with_args("run_shell", _CTX, {"command": "rm x"})
    assert out[0] == ALLOW


def test_layered_sources_project_beats_global() -> None:
    gate = PermissionGate.from_layered_sources(
        '[{"tool": "*", "action": "allow"}]',  # global
        '[{"tool": "run_shell(rm:*)", "action": "deny"}]',  # project overlay
    )
    rm = gate.resolve_with_args("run_shell", _CTX, {"command": "rm -rf /"})
    assert rm[0] == DENY


# --------------------------------------------------------------------------
# Permission modes
# --------------------------------------------------------------------------


def test_mode_plan_denies_mutating_allows_read() -> None:
    gate = PermissionGate(mode=PermissionMode.PLAN)
    assert gate.resolve("write_file", _CTX)[0] == DENY
    assert gate.resolve("run_shell", _CTX)[0] == DENY
    assert gate.resolve("notebook_edit", _CTX)[0] == DENY  # mutates a file
    assert gate.resolve("read_file", _CTX)[0] == ALLOW


def test_mode_accept_edits_auto_allows_edit_tools() -> None:
    gate = PermissionGate(mode=PermissionMode.ACCEPT_EDITS)
    assert gate.resolve("write_file", _CTX)[0] == ALLOW
    assert gate.resolve("edit_file", _CTX)[0] == ALLOW
    assert gate.resolve("notebook_edit", _CTX)[0] == ALLOW


def test_mode_bypass_overrides_deny_rules() -> None:
    gate = PermissionGate(
        [PermissionRule(tool="*", action=DENY)], mode=PermissionMode.BYPASS
    )
    assert gate.resolve("write_file", _CTX)[0] == ALLOW


def test_mode_coerce_unknown_is_default() -> None:
    assert PermissionMode.coerce("nonsense") is PermissionMode.DEFAULT
    assert PermissionMode.coerce("acceptEdits") is PermissionMode.ACCEPT_EDITS


def test_parse_rule_list_skips_bad_entries() -> None:
    rules = parse_rule_list(
        '[{"tool": "run_shell", "action": "ask"}, '
        '{"tool": "", "action": "allow"}, '
        '{"tool": "x", "action": "bogus"}, '
        '"not-a-dict"]'
    )
    assert len(rules) == 1
    assert rules[0].action == ASK


# --------------------------------------------------------------------------
# ask verdict + approval gate
# --------------------------------------------------------------------------


def test_ask_verdict_fail_closed_without_resolver() -> None:
    gate = ApprovalGate(
        PermissionGate([PermissionRule(tool="run_shell", action=ASK)])
    )
    out = asyncio.run(gate.decide("run_shell", args={"command": "rm x"}))
    assert out.verdict is ApprovalVerdict.DENY
    assert out.asked is True
    assert out.allowed is False


def test_ask_verdict_resolver_approves() -> None:
    async def _yes(tool, args, ctx):  # noqa: ANN001
        return True

    gate = ApprovalGate(
        PermissionGate([PermissionRule(tool="run_shell", action=ASK)]),
        resolver=_yes,
    )
    out = asyncio.run(gate.decide("run_shell", args={"command": "rm x"}))
    assert out.verdict is ApprovalVerdict.ALLOW
    assert out.allowed is True


def test_ask_verdict_resolver_error_fails_closed() -> None:
    async def _boom(tool, args, ctx):  # noqa: ANN001
        raise RuntimeError("prompt surface down")

    gate = ApprovalGate(
        PermissionGate([PermissionRule(tool="run_shell", action=ASK)]),
        resolver=_boom,
    )
    out = asyncio.run(gate.decide("run_shell", args={"command": "rm x"}))
    assert out.allowed is False


def test_allow_path_passes_without_prompt() -> None:
    gate = ApprovalGate(PermissionGate())  # allow-all default
    out = asyncio.run(gate.decide("read_file", args={"path": "a"}))
    assert out.verdict is ApprovalVerdict.ALLOW
    assert out.asked is False


def test_set_mode_swaps_at_runtime_and_coerces() -> None:
    """Dim 3: the console /permissions command swaps the gate mode at runtime;
    set_mode normalizes via PermissionMode.coerce and takes effect on the next
    resolve (the gate re-reads _mode per call)."""
    gate = PermissionGate(mode=PermissionMode.DEFAULT)
    assert gate.resolve("write_file", _CTX)[0] != DENY  # default: not plan-denied

    assert gate.set_mode("plan") is PermissionMode.PLAN
    assert gate.resolve("write_file", _CTX)[0] == DENY  # now plan-denied

    assert gate.set_mode("acceptEdits") is PermissionMode.ACCEPT_EDITS
    assert gate.resolve("edit_file", _CTX)[0] == ALLOW

    # Unknown strings coerce to DEFAULT rather than raising.
    assert gate.set_mode("no-such-mode") is PermissionMode.DEFAULT
