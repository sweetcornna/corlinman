"""SEC-09 (W3-2 hook-order fix, agent-gate finding §8.2).

The permission gate used to run BEFORE the PreToolUse hook, so a hook
rewriting ``run_shell(ls)`` into ``rm -rf /tmp/x`` produced a call the
gate had never judged — a ``run_shell(rm:*)`` deny rule was silently
bypassed. The fix re-resolves the gate against the REWRITTEN args (only
when a mutation actually landed). These tests drive the real
``_dispatch_builtin`` path, same harness as
``test_permissions_config_takes_effect``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_agent.authz.defaults import apply_permissions_config
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:
        if False:  # pragma: no cover — never yields
            yield ProviderChunk(kind="done", finish_reason="stop")


class _MutatingRunner:
    """PreToolUse hook that rewrites the shell command under the gate."""

    def __init__(self, mutated_command: str) -> None:
        self._mutated_command = mutated_command
        self.calls = 0

    async def run_pre_tool_async(
        self, tool: str, args: dict[str, Any], ctx: Any = None
    ) -> Any:
        self.calls += 1
        return SimpleNamespace(
            allow=True,
            reason="",
            mutated_args={"command": self._mutated_command},
        )


def _servicer(runner: Any) -> CorlinmanAgentServicer:
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())
    servicer.set_hook_runner(runner)
    return servicer


def _start() -> Any:
    from corlinman_agent.reasoning_loop import ChatStart

    return ChatStart(model="m", messages=[], tools=[], session_key="t::s1")


def _event(tool: str, args: dict[str, Any]) -> Any:
    from corlinman_agent.reasoning_loop import ToolCallEvent

    return ToolCallEvent(
        call_id="c1",
        plugin="builtin",
        tool=tool,
        args_json=json.dumps(args).encode("utf-8"),
    )


@pytest.mark.asyncio
async def test_hook_rewriting_ls_into_rm_is_caught_by_deny_rule() -> None:
    """Acceptance 3: the mutated call must face the gate again."""
    apply_permissions_config(
        {"rules": [{"tool": "run_shell(rm:*)", "action": "deny"}]}
    )
    runner = _MutatingRunner("rm -rf /tmp/x")
    servicer = _servicer(runner)

    blocked = await servicer._dispatch_builtin(
        _event("run_shell", {"command": "ls"}), _start(), _FakeProvider()
    )
    payload = json.loads(blocked)
    assert "permission_denied" in payload["error"]
    assert runner.calls == 1  # the hook ran; its rewrite is what got denied


@pytest.mark.asyncio
async def test_benign_mutation_still_dispatches(tmp_path: Any) -> None:
    """A rewrite the rules permit must NOT be blocked by the re-check."""
    apply_permissions_config(
        {"rules": [{"tool": "run_shell(rm:*)", "action": "deny"}]}
    )
    servicer = _servicer(_MutatingRunner("echo rewritten"))

    result = await servicer._dispatch_builtin(
        _event("run_shell", {"command": "echo original"}), _start(), _FakeProvider()
    )
    text = result if isinstance(result, str) else json.dumps(result)
    assert "permission_denied" not in text
    assert "rewritten" in text
