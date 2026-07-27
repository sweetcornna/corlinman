"""W3-3 / decision C1: the voice approval bridge is fail-closed.

The pre-W3-3 bridge auto-APPROVED every tool call when no queue was
wired — the only fail-open ask path in the product, and an accident of
``routes_voice/mod.py`` constructing ``VoiceState`` without an
``approval_queue`` rather than a design decision. These tests pin the
flip: no queue → deny + spoken explanation + upstream interrupt; a wired
queue still approves normally.
"""

from __future__ import annotations

from typing import Any

import pytest
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
async def test_no_queue_denies_fail_closed() -> None:
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
    bridge = VoiceApprovalBridge.with_queue(_AllowQueue(), "sess-1")
    outcome = await bridge.handle_tool_call("appr-2", "run_shell", {"command": "ls"})
    assert outcome.decision == ApprovalDecisionKind.APPROVED
