"""MCP sampling approval normalized onto the unified gate (audit W3-2).

``[mcp.sampling].mode = "ask"`` used to deny every request in production
because the entrypoint never wired an approval hook. The gate-backed hook
evaluates the canonical ``sampling:<server>`` key — an explicit allow rule
(or bypass mode) approves; everything else, including "no rule matched",
stays fail-closed (polarity C1).
"""

from __future__ import annotations

from typing import Any

import pytest
from corlinman_agent.authz import AuthzGate
from corlinman_agent.authz.defaults import apply_permissions_config
from corlinman_mcp_server import SamplingConfig, SamplingResponder
from corlinman_mcp_server.sampling import SamplingRequest, SamplingResult
from corlinman_server.gateway.mcp.sampling_authz import build_sampling_approval_hook


def _request(model: str = "claude-x") -> SamplingRequest:
    return SamplingRequest(model=model, messages=[], max_tokens=16)


@pytest.mark.asyncio
async def test_no_rule_denies() -> None:
    """``ask`` mode is an explicit opt-in — default_action=allow must NOT
    silently auto-approve sampling (fail-closed, C1)."""
    apply_permissions_config(None)
    hook = build_sampling_approval_hook(AuthzGate())
    assert await hook("some-server", _request()) is False


@pytest.mark.asyncio
async def test_explicit_allow_rule_approves() -> None:
    apply_permissions_config(
        {"rules": [{"tool": "sampling:trusted", "action": "allow"}]}
    )
    hook = build_sampling_approval_hook(AuthzGate())
    assert await hook("trusted", _request()) is True
    assert await hook("other", _request()) is False


@pytest.mark.asyncio
async def test_glob_rule_and_deny_rule() -> None:
    apply_permissions_config(
        {
            "rules": [
                {"tool": "sampling:*", "action": "allow"},
                {"tool": "sampling:evil", "action": "deny"},
            ]
        }
    )
    hook = build_sampling_approval_hook(AuthzGate())
    assert await hook("nice", _request()) is True
    assert await hook("evil", _request()) is False


@pytest.mark.asyncio
async def test_bypass_mode_approves() -> None:
    apply_permissions_config({"mode": "bypass"})
    hook = build_sampling_approval_hook(AuthzGate())
    assert await hook("anything", _request()) is True


@pytest.mark.asyncio
async def test_responder_ask_mode_end_to_end() -> None:
    """The responder's ``ask`` branch actually consults the gate hook."""
    apply_permissions_config(
        {"rules": [{"tool": "sampling:trusted", "action": "allow"}]}
    )

    async def _completer(req: SamplingRequest) -> SamplingResult:
        return SamplingResult(text="ok", model=req.model)

    responder = SamplingResponder(
        SamplingConfig(mode="ask", allowed_models=["claude-x"]),
        _completer,
        approval_hook=build_sampling_approval_hook(AuthzGate()),
    )
    params: dict[str, Any] = {
        "messages": [{"role": "user", "content": "hi"}],
        "modelPreferences": {"hints": [{"name": "claude-x"}]},
    }
    result, error = await responder.handle("trusted", params)
    assert error is None and result["content"]["text"] == "ok"

    result, error = await responder.handle("untrusted", params)
    assert result is None and "denied" in error["message"]
