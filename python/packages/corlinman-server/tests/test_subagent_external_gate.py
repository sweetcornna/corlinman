"""Subagent tool calls under the unified gate (audit W3-2, acceptance 5).

The child executor closure reuses the parent's dispatch path, so:

* the child's EXTERNAL tool calls face the same gate, resolved under
  ``surface="subagent"`` with the parent's surface as ``parent_surface``
  (both land in the audit log with ``initiator=subagent``);
* the three child-specific hard refusals (recursive spawn /
  ``exit_plan_mode`` / backgrounded ``run_shell``) stay BEFORE the gate —
  not even ``bypass`` mode re-enables them.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import structlog
from corlinman_agent.authz.defaults import apply_permissions_config
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:
        if False:  # pragma: no cover — never yields
            yield ProviderChunk(kind="done", finish_reason="stop")


def _servicer() -> CorlinmanAgentServicer:
    return CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())


def _start() -> Any:
    from corlinman_agent.reasoning_loop import ChatStart

    # ``console:`` prefix → parent surface resolves to "console".
    return ChatStart(model="m", messages=[], tools=[], session_key="console:s1")


def _event(tool: str, args: dict[str, Any], plugin: str | None = None) -> Any:
    from corlinman_agent.reasoning_loop import ToolCallEvent

    return ToolCallEvent(
        call_id="child-1",
        plugin=plugin if plugin is not None else tool,
        tool=tool,
        args_json=json.dumps(args).encode("utf-8"),
    )


def _child_executor(servicer: CorlinmanAgentServicer) -> Any:
    return servicer._make_child_tool_executor(_start(), _FakeProvider(), None)


@pytest.mark.asyncio
async def test_child_external_tool_is_gated_and_audited() -> None:
    apply_permissions_config({"rules": [{"tool": "*", "action": "deny"}]})
    execute = _child_executor(_servicer())

    captured: list[dict[str, Any]] = []

    def _capture(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        if event_dict.get("event") == "agent.authz.external_decision":
            captured.append(dict(event_dict))
        return event_dict

    structlog.configure(
        processors=[_capture, *structlog.get_config()["processors"]]
    )
    try:
        result = await execute(_event("github_create_issue", {"title": "hi"}))
    finally:
        cfg = structlog.get_config()
        structlog.configure(processors=cfg["processors"][1:])

    assert "permission_denied" in json.loads(result)["error"]
    # The decision landed in the gate audit log with the subagent identity.
    assert captured, "external child call missing from the authz audit log"
    entry = captured[-1]
    assert entry["initiator"] == "subagent"
    assert entry["surface"] == "subagent"
    assert entry["parent_surface"] == "console"
    assert entry["decision"] == "deny"


@pytest.mark.asyncio
async def test_child_external_tool_allowed_when_rules_allow() -> None:
    apply_permissions_config(
        {"rules": [{"tool": "mcp:github/*", "action": "allow"}]}
    )
    execute = _child_executor(_servicer())
    result = await execute(_event("github_create_issue", {"title": "hi"}))
    # Allowed past the gate; the child executor itself cannot run external
    # tools, so the envelope is the unknown-builtin fallback, NOT a
    # permission denial.
    assert "permission_denied" not in result
    assert "unknown_builtin_tool" in result


@pytest.mark.asyncio
async def test_child_external_ask_fail_closes_without_resolver() -> None:
    """An external ``ask`` rule must reach the approval gate under its
    EXTERNAL keys — the fail-closed denial proves the escalation did not
    silently degrade to the default action when re-resolving (the
    ``ApprovalGate.decide`` re-resolution bug W3-2 fixed)."""
    apply_permissions_config(
        {"rules": [{"tool": "mcp:github/*", "action": "ask"}]}
    )
    execute = _child_executor(_servicer())
    result = await execute(_event("github_create_issue", {"title": "hi"}))
    assert "approval_denied" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_child_external_ask_approved_by_resolver_proceeds() -> None:
    apply_permissions_config(
        {"rules": [{"tool": "mcp:github/*", "action": "ask"}]}
    )
    servicer = _servicer()

    async def _approve(_tool: str, _args: dict, _ctx: Any) -> bool:
        return True

    servicer.set_approval_resolver(_approve)
    execute = servicer._make_child_tool_executor(_start(), _FakeProvider(), None)
    result = await execute(_event("github_create_issue", {"title": "hi"}))
    assert "approval_denied" not in result
    assert "unknown_builtin_tool" in result  # past the gate, at the executor wall


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args", "marker"),
    [
        ("subagent_spawn", {"task": "x"}, "subagent_no_recursive_spawn"),
        ("exit_plan_mode", {}, "exit_plan_mode_not_allowed_in_subagent"),
        (
            "run_shell",
            {"command": "sleep 1", "run_in_background": True},
            "background_not_allowed_in_subagent",
        ),
    ],
)
async def test_child_hard_refusals_fire_before_gate(
    tool: str, args: dict[str, Any], marker: str
) -> None:
    """Bypass mode allows EVERYTHING at the gate — so seeing the refusal
    envelope proves the three child-specific denials run before it."""
    apply_permissions_config({"mode": "bypass"})
    execute = _child_executor(_servicer())
    result = await execute(_event(tool, args))
    assert marker in result
