"""Property test: ``[approvals]`` translation preserves verdicts (W3-2,
acceptance 4).

For randomly generated ``[[approvals.rules]]`` lists and random calls, the
legacy gateway matcher (``gateway/middleware/approval.py:match_rule`` —
most-specific-rule-first-declaration-wins) and the unified gate evaluating
the TRANSLATED rules (last-match-wins over canonical ``plugin:<p>/<t>``
keys) must agree sample by sample.

Verdict mapping (decisions C1/C6): ``MATCHED_AUTO``/``MATCHED_WHITELIST``
→ allow, ``MATCHED_DENY`` → deny, ``MATCHED_PROMPT`` → ask, ``NO_MATCH``
→ the gate's ``default_action`` (allow) — the legacy ``NO_MATCH → allow``
polarity is expressed by the same knob, not an independent branch.
"""

from __future__ import annotations

import json

from corlinman_agent.authz import Subject
from corlinman_agent.authz.approvals_compat import translate_approvals_rules
from corlinman_agent.permission import PermissionGate, parse_rule_list
from corlinman_server.gateway.middleware.approval import (
    ApprovalMode,
    ApprovalRule,
    RuleMatchKind,
    match_rule,
)
from hypothesis import given, settings
from hypothesis import strategies as st

_PLUGINS = ["file-ops", "net", "deploy"]
_TOOLS = ["read", "write", "fetch"]
_SESSIONS = ["", "trusted-1", "tenant::chan", "other"]

_rule_dicts = st.fixed_dictionaries(
    {
        "plugin": st.sampled_from(_PLUGINS),
        "mode": st.sampled_from(["auto", "prompt", "deny"]),
    },
    optional={
        "tool": st.sampled_from(_TOOLS),
        "allow_session_keys": st.lists(
            st.sampled_from([s for s in _SESSIONS if s]), max_size=2
        ),
    },
)

_calls = st.tuples(
    st.sampled_from(_PLUGINS),
    st.sampled_from(_TOOLS),
    st.sampled_from(_SESSIONS),
)


def _legacy_verdict(rules: list[dict], plugin: str, tool: str, session: str) -> str:
    typed = [
        ApprovalRule(
            plugin=r["plugin"],
            tool=r.get("tool"),
            mode=ApprovalMode(r["mode"]),
            allow_session_keys=tuple(r.get("allow_session_keys") or ()),
        )
        for r in rules
    ]
    kind = match_rule(typed, plugin, tool, session).kind
    if kind in (RuleMatchKind.MATCHED_AUTO, RuleMatchKind.MATCHED_WHITELIST):
        return "allow"
    if kind is RuleMatchKind.MATCHED_DENY:
        return "deny"
    if kind is RuleMatchKind.MATCHED_PROMPT:
        return "ask"
    return "allow"  # NO_MATCH → legacy fail-open == default_action allow


def _translated_verdict(rules: list[dict], plugin: str, tool: str, session: str) -> str:
    translated = translate_approvals_rules({"rules": rules})
    gate = PermissionGate(parse_rule_list(json.dumps(translated)))
    action, _ = gate.resolve_external_with_args(
        (f"plugin:{plugin}/{tool}",),
        Subject(session_key=session or None),
        None,
    )
    return action


@settings(max_examples=300, deadline=None)
@given(rules=st.lists(_rule_dicts, max_size=6), call=_calls)
def test_translated_rules_agree_with_legacy_matcher(
    rules: list[dict], call: tuple[str, str, str]
) -> None:
    plugin, tool, session = call
    assert _translated_verdict(rules, plugin, tool, session) == _legacy_verdict(
        rules, plugin, tool, session
    ), f"divergence on rules={rules} call={call}"
