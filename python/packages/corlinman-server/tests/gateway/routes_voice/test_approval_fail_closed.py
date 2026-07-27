"""W3-3 / decision C1: the voice approval bridge is fail-closed on ASK.

The pre-W3-3 bridge auto-APPROVED every tool call when no queue was
wired — the only fail-open ask path in the product, and an accident of
``routes_voice/mod.py`` constructing ``VoiceState`` without an
``approval_queue`` rather than a design decision. C1's exact wording:
distinguish "no rule matched" (still ``default_action``, allow) from
"an ask rule matched but there is no channel" (deny). The bridge sees
EVERY provider tool call, so it resolves the unified gate first —
without that, the fail-closed flip would deny every voice tool call in
every real deployment (the queue is never wired in production).
"""

from __future__ import annotations

from typing import Any

import pytest
from corlinman_agent.authz.defaults import apply_permissions_config
from corlinman_server.gateway.routes_voice.approval import (
    APPROVAL_DENIED_TEXT,
    ApprovalDecisionKind,
    VoiceApprovalBridge,
)
from corlinman_server.gateway.routes_voice.framing import ServerControl


class _AllowQueue:
    async def enqueue_and_wait(self, request: Any, *, timeout: float | None = None) -> str:
        return "allow"


@pytest.mark.asyncio
async def test_no_queue_denies_fail_closed_on_ask() -> None:
    """An ASK verdict + no channel = deny (C1's second half)."""
    apply_permissions_config({"rules": [{"tool": "run_shell", "action": "ask"}]})
    bridge = VoiceApprovalBridge.no_gate("sess-1")
    outcome = await bridge.handle_tool_call("appr-1", "run_shell", {"command": "ls"})

    assert outcome.decision == ApprovalDecisionKind.DENIED
    # The user hears WHY the tool went silent (never a silent failure).
    texts = [
        f.text
        for f in outcome.server_frames
        if getattr(f, "type", "") == ServerControl.AGENT_TEXT
    ]
    assert APPROVAL_DENIED_TEXT in texts
    # Upstream gets the refusal + an interrupt to flush buffered TTS.
    kinds = [getattr(c, "kind", None) for c in outcome.provider_commands]
    approve_cmds = [
        c for c in outcome.provider_commands if getattr(c, "approve", None) is not None
    ]
    assert approve_cmds and all(c.approve is False for c in approve_cmds)
    assert len(outcome.provider_commands) >= 2, kinds


@pytest.mark.asyncio
async def test_wired_queue_still_approves() -> None:
    apply_permissions_config({"rules": [{"tool": "run_shell", "action": "ask"}]})
    bridge = VoiceApprovalBridge.with_queue(_AllowQueue(), "sess-1")
    outcome = await bridge.handle_tool_call("appr-2", "run_shell", {"command": "ls"})
    assert outcome.decision == ApprovalDecisionKind.APPROVED


@pytest.mark.asyncio
async def test_default_allow_proceeds_without_prompt() -> None:
    """C1's first half: NO rule matched → default_action (allow) — the
    bridge must not park or deny an ordinary tool call just because no
    queue is wired. This was the review-caught regression: unconditional
    fail-closed would have denied every voice tool call in production."""
    bridge = VoiceApprovalBridge.no_gate("sess-1")
    outcome = await bridge.handle_tool_call("appr-3", "run_shell", {"command": "ls"})

    assert outcome.decision == ApprovalDecisionKind.APPROVED
    approve_cmds = [
        c for c in outcome.provider_commands if getattr(c, "approve", None) is not None
    ]
    assert approve_cmds and all(c.approve is True for c in approve_cmds)
    # No approval banner for a call that never needed approval.
    assert outcome.server_frames == []


@pytest.mark.asyncio
async def test_deny_rule_blocks_without_prompt() -> None:
    apply_permissions_config(
        {"rules": [{"tool": "run_shell(rm:*)", "action": "deny"}]}
    )
    bridge = VoiceApprovalBridge.no_gate("sess-1")
    outcome = await bridge.handle_tool_call(
        "appr-4", "run_shell", {"command": "rm -rf /tmp/x"}
    )
    assert outcome.decision == ApprovalDecisionKind.DENIED
