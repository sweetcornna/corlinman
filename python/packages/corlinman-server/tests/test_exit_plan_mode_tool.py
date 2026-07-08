"""Wave E — the ``exit_plan_mode`` builtin (claude-code parity).

The model ends plan mode itself: once it has a plan ready it calls
``exit_plan_mode`` to flip the runtime permission mode ``plan`` → ``default``
so the mutating tool surface unlocks again. Exercised through
``_dispatch_builtin`` directly (the unit-level dispatch contract is enough —
mirrors ``test_gf_servicer_tool_wiring``).

Covered:

* registration — ``exit_plan_mode`` in ``BUILTIN_TOOLS`` and advertised by
  ``_builtin_tool_schemas()``.
* dispatch in plan mode — mode becomes ``default``, the interactive approval
  cache is reset exactly once (a grant given while planning must not leak
  past the mode boundary, Codex #104), status ``ok``.
* dispatch outside plan mode — clean ``noop``, mode unchanged, resolver NOT
  reset.
* plan echo — a non-empty ``plan`` arg is echoed back in the result.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import (
    EXIT_PLAN_MODE_TOOL,
    CorlinmanAgentServicer,
    _builtin_tool_schemas,
)


class _FakeProvider:
    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:
        if False:  # pragma: no cover — generator, never yields here
            yield ProviderChunk(kind="done", finish_reason="stop")


class _RecordingResolver:
    """Stand-in for ``ConsoleApprovalResolver`` — records ``reset`` calls."""

    def __init__(self) -> None:
        self.reset_calls = 0
        self.always_allow: set[str] = {"run_shell"}

    def reset(self) -> None:
        self.reset_calls += 1
        self.always_allow.clear()


def _servicer() -> CorlinmanAgentServicer:
    return CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())


def _start(session_key: str = "tenant-x::s1") -> Any:
    from corlinman_agent.reasoning_loop import ChatStart

    return ChatStart(model="m", messages=[], tools=[], session_key=session_key)


def _event(tool: str, args: dict[str, Any] | None = None):
    from corlinman_agent.reasoning_loop import ToolCallEvent

    return ToolCallEvent(
        call_id="c1",
        plugin="builtin",
        tool=tool,
        args_json=json.dumps(args or {}).encode("utf-8"),
    )


def test_exit_plan_mode_registered_and_advertised() -> None:
    from corlinman_server.agent_servicer import BUILTIN_TOOLS

    assert EXIT_PLAN_MODE_TOOL in BUILTIN_TOOLS
    names = {
        s["function"]["name"]
        for s in _builtin_tool_schemas()
        if isinstance(s, dict) and "function" in s
    }
    assert EXIT_PLAN_MODE_TOOL in names


async def test_dispatch_in_plan_mode_switches_and_resets() -> None:
    servicer = _servicer()
    servicer.set_permission_mode("plan")
    resolver = _RecordingResolver()
    servicer.set_approval_resolver(resolver)

    out = await servicer._dispatch_builtin(
        _event(EXIT_PLAN_MODE_TOOL, {}), _start(), _FakeProvider()
    )
    payload = json.loads(out)

    assert payload["status"] == "ok"
    assert payload["mode"] == "default"
    assert servicer.get_permission_mode() == "default"
    # Mode-boundary grants must not leak: the interactive cache is reset once.
    assert resolver.reset_calls == 1
    assert resolver.always_allow == set()


async def test_dispatch_outside_plan_mode_is_noop() -> None:
    servicer = _servicer()
    # Default mode — never entered plan mode.
    assert servicer.get_permission_mode() == "default"
    resolver = _RecordingResolver()
    servicer.set_approval_resolver(resolver)

    out = await servicer._dispatch_builtin(
        _event(EXIT_PLAN_MODE_TOOL, {}), _start(), _FakeProvider()
    )
    payload = json.loads(out)

    assert payload["status"] == "noop"
    assert payload["mode"] == "default"
    assert servicer.get_permission_mode() == "default"
    # A no-op must not touch the interactive approval cache.
    assert resolver.reset_calls == 0


async def test_dispatch_echoes_plan_summary() -> None:
    servicer = _servicer()
    servicer.set_permission_mode("plan")

    out = await servicer._dispatch_builtin(
        _event(EXIT_PLAN_MODE_TOOL, {"plan": "do X then Y"}),
        _start(),
        _FakeProvider(),
    )
    payload = json.loads(out)

    assert payload["status"] == "ok"
    assert payload["mode"] == "default"
    assert payload["plan"] == "do X then Y"


async def test_child_executor_refuses_exit_plan_mode() -> None:
    """Permission mode is servicer-GLOBAL — a subagent spawned during plan
    mode (spawn tools aren't mutating, so plan mode allows them) must not be
    able to flip the whole servicer plan → default on the parent's behalf.
    Only the top-level turn may end plan mode."""
    servicer = _servicer()
    servicer.set_permission_mode("plan")
    resolver = _RecordingResolver()
    servicer.set_approval_resolver(resolver)

    execute = servicer._make_child_tool_executor(_start(), _FakeProvider(), None)
    out = await execute(_event(EXIT_PLAN_MODE_TOOL, {}))
    payload = json.loads(out)

    assert "exit_plan_mode_not_allowed_in_subagent" in payload["error"]
    # The refusal must leave the parent's plan mode + grants untouched.
    assert servicer.get_permission_mode() == "plan"
    assert resolver.reset_calls == 0


async def test_dispatch_resets_app_state_resolver_too() -> None:
    """The approval gate can source its resolver from EITHER
    ``set_approval_resolver`` or ``app_state.approval_resolver`` (CMP-04
    fallback) — the plan-boundary reset must cover both so a gateway-side
    resolver can't dodge the Codex #104 rule."""
    servicer = _servicer()
    servicer.set_permission_mode("plan")
    app_resolver = _RecordingResolver()

    class _AppState:
        approval_resolver = app_resolver

    servicer.set_app_state(_AppState())

    out = await servicer._dispatch_builtin(
        _event(EXIT_PLAN_MODE_TOOL, {}), _start(), _FakeProvider()
    )
    payload = json.loads(out)

    assert payload["status"] == "ok"
    assert servicer.get_permission_mode() == "default"
    assert app_resolver.reset_calls == 1
