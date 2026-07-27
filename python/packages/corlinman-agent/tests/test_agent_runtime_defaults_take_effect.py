"""Operator config must reach the agent's own runtime knobs.

Everything in ``[agent_runtime]`` — round budgets, compaction thresholds,
the ``execute_code`` opt-in, the shell sandbox backend — was previously
reachable only through a ``CORLINMAN_*`` environment variable, and the
agent's systemd unit deliberately carries no ``EnvironmentFile`` (it sets
exactly ``HOME`` / ``CORLINMAN_EXECUTION_STATE_DIR`` /
``CORLINMAN_PY_CONFIG`` / ``CORLINMAN_PY_SOCKET``). So in a native
deployment every one of these sat pinned at its built-in default with no
way to change it.

These tests drive **behaviour** — the loop, the tool dispatcher, the
environment factory — rather than the resolvers in isolation. A resolver
that returns the right number while the caller still reads a
module constant frozen at import would pass a unit test and fail in
production; that is the exact bug this section exists to close.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_agent import ChatStart, ReasoningLoop, ToolResult
from corlinman_agent.coding.environment import DockerEnvironment, LocalEnvironment, get_environment
from corlinman_agent.coding.repl import _SESSIONS, dispatch_execute_code
from corlinman_agent.runtime_defaults import (
    AgentRuntimeDefaults,
    agent_runtime_defaults_from_config,
    apply_agent_runtime_config,
    max_rounds,
    require_read_before_edit,
    sandbox_image,
    shell_task_max_lifetime_s,
    tool_result_cap,
    web_fetch_allow_private,
)
from corlinman_providers.base import ProviderChunk

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Precedence — config > env > built-in
# ---------------------------------------------------------------------------


def test_config_outranks_a_stale_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env vars predate the config section.

    An operator who sets a value in ``config.toml`` must not be silently
    overridden by an old export left on the host.
    """
    monkeypatch.setenv("CORLINMAN_AGENT_MAX_ROUNDS", "11")
    assert max_rounds() == 11  # env alone still works

    apply_agent_runtime_config({"max_rounds": 40})
    assert max_rounds() == 40


def test_unset_keys_fall_through_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configuring one knob must not pin the other two dozen.

    ``AgentRuntimeDefaults`` uses ``None`` for "not configured" precisely so
    a block that sets ``max_rounds`` leaves ``tool_result_cap`` on whatever
    the env/built-in layer says.
    """
    monkeypatch.setenv("CORLINMAN_TOOL_RESULT_CAP", "12345")
    apply_agent_runtime_config({"max_rounds": 40})

    assert max_rounds() == 40
    assert tool_result_cap() == 12345


def test_clamps_are_shared_by_both_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A knob must not mean one thing from config and another from env."""
    monkeypatch.setenv("CORLINMAN_AGENT_MAX_ROUNDS", "1")
    assert max_rounds() == 8  # floored

    apply_agent_runtime_config({"max_rounds": 1})
    assert max_rounds() == 8  # same floor from config


def test_malformed_values_degrade_to_unconfigured() -> None:
    """One bad key must not take the agent process down at sidecar load."""
    parsed = agent_runtime_defaults_from_config(
        {
            "max_rounds": "not-a-number",
            "context_reserve_fraction": [],
            "enable_execute_code": "yes",
            "sandbox_backend": "  DOCKER  ",
        }
    )
    assert parsed.max_rounds is None
    assert parsed.context_reserve_fraction is None
    assert parsed.enable_execute_code is True
    assert parsed.sandbox_backend == "docker"


def test_a_true_where_a_number_belongs_is_ignored() -> None:
    """``bool`` is an ``int`` subclass — reading ``true`` as ``1`` would
    silently set a round budget of 1."""
    assert agent_runtime_defaults_from_config({"max_rounds": True}).max_rounds is None


def test_defaults_match_the_values_they_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Moving these knobs into one module must not shift any default."""
    for var in (
        "CORLINMAN_AGENT_MAX_ROUNDS",
        "CORLINMAN_TOOL_RESULT_CAP",
        "CORLINMAN_SHELL_TASK_MAX_LIFETIME_S",
        "CORLINMAN_REQUIRE_READ_BEFORE_EDIT",
        "CORLINMAN_WEB_FETCH_ALLOW_PRIVATE",
    ):
        monkeypatch.delenv(var, raising=False)

    assert max_rounds() == 60
    assert tool_result_cap() == 8_000
    assert shell_task_max_lifetime_s() == 1_800.0
    assert require_read_before_edit() is True  # default ON — the safe side
    assert web_fetch_allow_private() is False


# ---------------------------------------------------------------------------
# Effect — the loop
# ---------------------------------------------------------------------------


class _EndlessToolCaller:
    """Never finishes on its own; only the round ceiling stops it."""

    def __init__(self) -> None:
        self.rounds = 0

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.rounds += 1
        cid = f"call{self.rounds}"
        yield ProviderChunk(kind="tool_call_start", tool_call_id=cid, tool_name="t")
        yield ProviderChunk(kind="tool_call_delta", tool_call_id=cid, arguments_delta="{}")
        yield ProviderChunk(kind="tool_call_end", tool_call_id=cid)
        yield ProviderChunk(kind="done", finish_reason="tool_calls")


async def test_max_rounds_bounds_the_live_loop() -> None:
    """The ceiling the operator configured is the one the loop enforces.

    Driving the real loop is the point: the previous ``_MAX_ROUNDS`` was a
    module constant resolved at import, so the sidecar could set this to
    anything and the loop would still run 60 rounds.
    """
    apply_agent_runtime_config({"max_rounds": 9})

    provider = _EndlessToolCaller()
    loop = ReasoningLoop(provider, tool_result_timeout=1.0)
    async for event in loop.run(ChatStart(model="x", messages=[])):
        if hasattr(event, "call_id"):
            loop.feed_tool_result(ToolResult(call_id=event.call_id, content="ok"))

    assert provider.rounds == 9


async def test_tool_result_cap_bounds_what_reaches_history() -> None:
    from corlinman_agent.reasoning_loop import _truncate_tool_result

    apply_agent_runtime_config({"tool_result_cap": 3_000})
    capped = _truncate_tool_result("x" * 50_000)
    assert len(capped) < 8_000  # the built-in default no longer applies
    assert "elided" in capped


# ---------------------------------------------------------------------------
# Effect — capability switches
# ---------------------------------------------------------------------------


async def test_execute_code_opt_in_is_reachable_from_config() -> None:
    """``execute_code`` had no enable path at all in a native deployment."""
    apply_agent_runtime_config({})
    disabled = json.loads(await dispatch_execute_code(args_json=b'{"code":"1"}'))
    assert disabled["error"] == "execute_code_disabled"

    # Own the session key so the live interpreter is closed here rather
    # than left parked in the module-global registry (the leaked-thread
    # backstop in the root conftest exists because of exactly this).
    session_key = "agent-runtime-config-test"
    try:
        apply_agent_runtime_config({"enable_execute_code": True})
        enabled = json.loads(
            await dispatch_execute_code(
                args_json=b'{"code":"print(6*7)","timeout":10}',
                session_key=session_key,
            )
        )
        # Assert on the real output, not merely "no longer disabled" — the
        # point is that the tool runs, not that one error string changed.
        assert enabled.get("output", "").strip() == "42"
    finally:
        session = _SESSIONS.pop(session_key, None)
        if session is not None:
            await session.close()


def test_sandbox_backend_selects_the_environment() -> None:
    """The agent's own shell was locked to ``LocalEnvironment`` forever."""
    apply_agent_runtime_config({})
    assert isinstance(get_environment(), LocalEnvironment)

    apply_agent_runtime_config({"sandbox_backend": "docker"})
    assert isinstance(get_environment(), DockerEnvironment)


def test_unknown_sandbox_backend_fails_loudly() -> None:
    """A typo must not silently run tools on the host."""
    apply_agent_runtime_config({"sandbox_backend": "kubernetes"})
    with pytest.raises(RuntimeError, match="unknown"):
        get_environment()


def test_sandbox_image_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_SANDBOX_IMAGE", "from-env:1")
    assert sandbox_image() == "from-env:1"
    apply_agent_runtime_config({"sandbox_image": "registry.example/agent:2"})
    assert sandbox_image() == "registry.example/agent:2"


def test_read_before_edit_can_be_relaxed_from_config() -> None:
    apply_agent_runtime_config({"require_read_before_edit": False})
    assert require_read_before_edit() is False


def test_web_fetch_private_override_is_reachable() -> None:
    apply_agent_runtime_config({"web_fetch_allow_private": True})
    assert web_fetch_allow_private() is True


def test_as_dict_reports_only_configured_keys() -> None:
    """The apply-time log line must not print two dozen ``None``s."""
    reported = AgentRuntimeDefaults(max_rounds=12, sandbox_backend="docker").as_dict()
    assert reported == {"max_rounds": 12, "sandbox_backend": "docker"}
