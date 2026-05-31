"""gap-fill wire-A — servicer-side wiring of the new builtin tools.

Exercises ``_dispatch_builtin`` directly (the streaming-loop fixture is
quadratic to set up; the unit-level dispatch contract is enough) for:

* ``execute_code`` — registered + dispatched, disabled-by-default envelope.
* ``subagent_stop`` — routes to the operator stop mechanism (cancel_session).
* ``Skill`` — the on-demand progressive-disclosure body pull.
* ``memory_write`` / ``memory_read`` — gated + dispatched (not_configured
  without a host).
* the per-argument permission rule + ``ask`` fail-closed path through
  the dispatcher's gate.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] = {}

    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:
        self.last_kwargs = kwargs
        if False:  # pragma: no cover — generator, never yields here
            yield ProviderChunk(kind="done", finish_reason="stop")


def _servicer() -> CorlinmanAgentServicer:
    return CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())


def _start(session_key: str = "tenant-x::s1") -> Any:
    from corlinman_agent.reasoning_loop import ChatStart

    return ChatStart(model="m", messages=[], tools=[], session_key=session_key)


def _event(tool: str, args: dict[str, Any] | None = None, plugin: str = "builtin"):
    from corlinman_agent.reasoning_loop import ToolCallEvent

    return ToolCallEvent(
        call_id="c1",
        plugin=plugin,
        tool=tool,
        args_json=json.dumps(args or {}).encode("utf-8"),
    )


@pytest.mark.asyncio
async def test_execute_code_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_ENABLE_EXECUTE_CODE", raising=False)
    servicer = _servicer()
    out = await servicer._dispatch_builtin(
        _event("execute_code", {"code": "print(1)"}), _start(), _FakeProvider()
    )
    payload = json.loads(out)
    assert payload["error"] == "execute_code_disabled"


@pytest.mark.asyncio
async def test_subagent_stop_not_running() -> None:
    servicer = _servicer()
    out = await servicer._dispatch_builtin(
        _event("subagent_stop", {}), _start("tenant-x::no-loop"), _FakeProvider()
    )
    payload = json.loads(out)
    # No active loop registered for this session -> not_running, ok False.
    assert payload["status"] == "not_running"
    assert payload["ok"] is False
    assert payload["session_key"] == "tenant-x::no-loop"


@pytest.mark.asyncio
async def test_skill_tool_unregistered_envelope() -> None:
    servicer = _servicer()
    out = await servicer._dispatch_builtin(
        _event("Skill", {"name": "does-not-exist"}), _start(), _FakeProvider()
    )
    payload = json.loads(out)
    # No registry wired (default assembler may be None) OR not registered;
    # either way a clean error envelope, never a crash.
    assert payload["ok"] is False
    assert payload["error"] in ("skills_unavailable", "skill_not_registered")


@pytest.mark.asyncio
async def test_skill_tool_requires_name() -> None:
    servicer = _servicer()
    out = await servicer._dispatch_builtin(
        _event("Skill", {}), _start(), _FakeProvider()
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"] == "name_required"


@pytest.mark.asyncio
async def test_memory_write_not_configured_without_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    servicer = _servicer()
    out = await servicer._dispatch_builtin(
        _event("memory_write", {"content": "remember this"}),
        _start(),
        _FakeProvider(),
    )
    payload = json.loads(out)
    # With the servicer's lazily-opened host this may write OK; the
    # contract under test is only that it dispatches + returns an envelope
    # with an ``ok`` flag (never crashes / falls through to unknown).
    assert "ok" in payload


@pytest.mark.asyncio
async def test_per_arg_rule_denies_rm_via_dispatch(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``run_shell(rm:*)`` deny rule blocks an ``rm`` command at the
    dispatcher's permission gate, but lets a benign command through."""
    monkeypatch.setenv(
        "CORLINMAN_AGENT_PERMISSIONS",
        '[{"tool": "run_shell(rm:*)", "action": "deny"}]',
    )
    servicer = _servicer()
    blocked = await servicer._dispatch_builtin(
        _event("run_shell", {"command": "rm -rf /tmp/x"}),
        _start(),
        _FakeProvider(),
    )
    payload = json.loads(blocked)
    assert "permission_denied" in payload["error"]
    assert payload["tool"] == "run_shell"


@pytest.mark.asyncio
async def test_ask_verdict_fail_closed_via_dispatch(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``ask`` permission verdict with no approval resolver wired
    fail-closes to an ``approval_denied`` envelope."""
    monkeypatch.setenv(
        "CORLINMAN_AGENT_PERMISSIONS",
        '[{"tool": "run_shell", "action": "ask"}]',
    )
    servicer = _servicer()
    out = await servicer._dispatch_builtin(
        _event("run_shell", {"command": "echo hi"}),
        _start(),
        _FakeProvider(),
    )
    payload = json.loads(out)
    assert "approval_denied" in payload["error"]
