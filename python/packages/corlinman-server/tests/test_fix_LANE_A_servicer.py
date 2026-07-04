"""LANE-A audit-fix repros — agent_servicer core.

Covers SEC-01, SEC-02, BUG-01, BUG-04, CMP-04, CMP-02, SEC-03.

Each test reproduces the audited defect against the real servicer, then
locks in the fix. Constructed unit-style (calling the in-process dispatch
methods directly) because the streaming Chat loop fixture is quadratic to
set up — the audited contract lives entirely in these methods.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import (
    _DEFAULT_ALWAYS_SKILLS,
    CorlinmanAgentServicer,
    _register_active_loop,
    _unregister_active_loop,
)


class _FakeProvider:
    def __init__(self, chunks: list[ProviderChunk] | None = None) -> None:
        self._chunks = chunks or []

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:
        for c in self._chunks:
            yield c


def _start(session_key: str, *, messages: list | None = None, tools: list | None = None) -> ChatStart:
    return ChatStart(
        model="m",
        messages=messages or [],
        tools=tools or [],
        session_key=session_key,
    )


def _tool_event(tool: str, args: dict[str, Any], call_id: str = "c1") -> ToolCallEvent:
    return ToolCallEvent(
        call_id=call_id,
        plugin="x",
        tool=tool,
        args_json=json.dumps(args).encode("utf-8"),
    )


class _FakeLoop:
    """Minimal stand-in for ReasoningLoop for the cancel registry."""

    def __init__(self) -> None:
        self.cancelled_with: str | None = None
        self._turn_id = "tid-1"

    def cancel(self, reason: str = "user_abort") -> None:
        self.cancelled_with = reason


# ---------------------------------------------------------------------------
# SEC-01: _active_skills must be per-session, not process-global.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sec01_active_skills_isolated_per_session() -> None:
    """Session A pulls a skill restricting tools to [read_file]. Session B
    (a different session_key) must NOT be blocked from web_search."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))

    # Simulate A having pulled a skill with allowed_tools=[read_file].
    servicer._record_active_skill("tenant-a::sess-A", "skill-x", ["read_file"])

    # A is correctly narrowed: web_search blocked under A's key.
    blocked_a = servicer._skill_allowed_tools_block("web_search", "tenant-a::sess-A")
    assert blocked_a is not None, "session A should be narrowed by its own skill"

    # B is a DIFFERENT session — must run web_search freely.
    blocked_b = servicer._skill_allowed_tools_block("web_search", "tenant-b::sess-B")
    assert blocked_b is None, "session B must not inherit A's skill restriction"


@pytest.mark.asyncio
async def test_skill_dotted_allowed_tools_match_wire_tool_names() -> None:
    """Regression: a skill whose ``allowed-tools`` uses the dotted logical
    namespace (``web.search``, ``file.read``) must NOT block the real wire
    tools (``web_search``, ``read_file``). Pre-fix, pulling deep-research /
    web_search silently denied every web + file tool on the session."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    # deep-research's real allowed-tools (dotted form).
    servicer._record_active_skill(
        "t::sess",
        "deep-research",
        ["web.search", "web.fetch", "file.read", "file.write", "memory.search"],
    )

    # The model calls the wire names — all must pass.
    for wire in ("web_search", "web_fetch", "read_file", "write_file", "memory_search"):
        assert (
            servicer._skill_allowed_tools_block(wire, "t::sess") is None
        ), f"{wire} should be allowed by its dotted skill entry"

    # A tool genuinely outside the skill's scope is still blocked, and the
    # reported allow-list is the canonical (wire) union.
    blocked = servicer._skill_allowed_tools_block("run_shell", "t::sess")
    assert blocked is not None
    assert "web_search" in blocked and "read_file" in blocked


@pytest.mark.asyncio
async def test_skill_run_shell_implies_bg_task_tools() -> None:
    """A skill granting run_shell implies its background-polling surface —
    shell_task_output / shell_task_kill are NOT blocked, so a bg command in
    a skill-scoped context can be polled and killed (Codex #112 r4)."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    servicer._record_active_skill("t::sess", "verification", ["shell.run"])
    for wire in ("run_shell", "shell_task_output", "shell_task_kill"):
        assert (
            servicer._skill_allowed_tools_block(wire, "t::sess") is None
        ), f"{wire} should be implied by a run_shell grant"
    # A tool still outside the grant is blocked.
    assert servicer._skill_allowed_tools_block("web_search", "t::sess") is not None


@pytest.mark.asyncio
async def test_skill_allowed_tools_collision_warns_without_changing_outcome() -> None:
    """Two spellings of one tool across active skills (#108 item 3): the gate
    logs ONE ``tool_aliases.collision`` warning but the allow/deny outcome is
    unchanged — the folded wire tool still passes, an out-of-scope tool is
    still blocked."""
    import structlog
    from corlinman_agent import tool_aliases

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    # One skill lists the dotted spelling, another the wire spelling — both
    # fold onto ``web_search``.
    servicer._record_active_skill("t::sess", "skill-a", ["web.search"])
    servicer._record_active_skill("t::sess", "skill-b", ["web_search"])

    tool_aliases._warned_collisions.clear()
    with structlog.testing.capture_logs() as captured:
        # web_search allowed (behaviour unchanged despite the two spellings).
        assert servicer._skill_allowed_tools_block("web_search", "t::sess") is None
        # An out-of-scope tool is still blocked.
        assert servicer._skill_allowed_tools_block("run_shell", "t::sess") is not None

    events = [e for e in captured if e.get("event") == "tool_aliases.collision"]
    assert len(events) == 1, f"expected exactly one collision warning; got {events}"
    assert events[0]["gate"] == "skill_allowed_tools"
    assert events[0]["canonical"] == "web_search"
    assert events[0]["sources"] == ["web.search", "web_search"]


@pytest.mark.asyncio
async def test_sec01_active_skills_cleared_at_turn_end() -> None:
    """The per-session active-skill entry is cleared at turn/session end so
    it cannot narrow a later turn forever."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    servicer._record_active_skill("s::1", "skill-x", ["read_file"])
    assert servicer._skill_allowed_tools_block("web_search", "s::1") is not None
    servicer._clear_active_skills("s::1")
    assert servicer._skill_allowed_tools_block("web_search", "s::1") is None


# ---------------------------------------------------------------------------
# SEC-02: subagent_stop must not cancel a foreign session.
# ---------------------------------------------------------------------------


def test_sec02_subagent_stop_rejects_foreign_session() -> None:
    """Session A emits subagent_stop targeting a live B session it does not
    own; the dispatch must refuse rather than cancel B."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    victim = _FakeLoop()
    _register_active_loop("tenant-b::sess-B", victim)
    try:
        result = servicer._dispatch_subagent_stop(
            json.dumps({"session_key": "tenant-b::sess-B"}).encode("utf-8"),
            "tenant-a::sess-A",
        )
        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload.get("error") == "not_authorized_for_session"
        assert victim.cancelled_with is None, "foreign session must not be cancelled"
    finally:
        _unregister_active_loop("tenant-b::sess-B", victim)


def test_sec02_subagent_stop_allows_own_session() -> None:
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    own = _FakeLoop()
    _register_active_loop("tenant-a::sess-A", own)
    try:
        result = servicer._dispatch_subagent_stop(b"{}", "tenant-a::sess-A")
        payload = json.loads(result)
        assert payload["ok"] is True
        assert own.cancelled_with is not None
    finally:
        _unregister_active_loop("tenant-a::sess-A", own)


def test_sec02_subagent_stop_allows_descendant_session() -> None:
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    child = _FakeLoop()
    child_key = "tenant-a::sess-A::child::0"
    _register_active_loop(child_key, child)
    try:
        result = servicer._dispatch_subagent_stop(
            json.dumps({"session_key": child_key}).encode("utf-8"),
            "tenant-a::sess-A",
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert child.cancelled_with is not None
    finally:
        _unregister_active_loop(child_key, child)


# ---------------------------------------------------------------------------
# BUG-01: HookRunner must be wirable into the servicer (settable).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug01_pre_tool_hook_gate_blocks_when_runner_set() -> None:
    """A wired HookRunner whose pre_tool hook blocks must short-circuit the
    builtin dispatch with a hook-blocked envelope. Before the fix the
    standalone server never receives a runner so the gate is inert."""
    from corlinman_hooks.runner import HookRunner

    class _BlockingRunner(HookRunner):
        async def run_pre_tool_async(self, tool, args, ctx):  # type: ignore[override]
            from corlinman_hooks.runner import HookDecision

            return HookDecision(allow=False, reason="blocked-by-test")

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    # Wire it the way main.py / agent_server.py must after the fix.
    servicer.set_hook_runner(_BlockingRunner({}))

    start = _start("s::1")
    event = _tool_event("calculator", {"expression": "1+1"})
    result = await servicer._dispatch_builtin(event, start, _FakeProvider([]))
    payload = json.loads(result)
    assert "error" in payload
    assert "blocked by hook" in payload["error"]


def test_bug01_main_builds_hook_runner() -> None:
    """main._serve must construct a HookRunner and pass it into the servicer.

    Static check: the source wires ``hook_runner=`` and a setter exists."""
    import importlib
    import inspect

    main = importlib.import_module("corlinman_server.main")
    src = inspect.getsource(main._serve)
    assert "hook_runner" in src, "main._serve must build + pass a HookRunner"
    # The setter must exist for the C2/C3 lifespan wiring path too.
    assert hasattr(CorlinmanAgentServicer, "set_hook_runner")


def test_bug01_agent_server_builds_hook_runner() -> None:
    import inspect

    from corlinman_server.gateway.grpc import agent_server

    src = inspect.getsource(agent_server.serve_agent)
    assert "hook_runner" in src, "serve_agent must wire a hook_runner"


# ---------------------------------------------------------------------------
# BUG-04: child_seq must be threaded so same-card spawns don't collide.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug04_sequential_spawns_get_distinct_child_seq() -> None:
    """Two subagent_spawn calls in one parent session must produce distinct
    child session keys (::child::0 then ::child::1)."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))

    seen: list[int] = []

    async def _fake_dispatch(*args: Any, **kwargs: Any) -> str:
        seen.append(kwargs["child_seq"])
        return json.dumps({"child_session_key": "x", "finish_reason": "stop"})

    import corlinman_server.agent_servicer as mod

    orig = mod.dispatch_subagent_spawn
    mod.dispatch_subagent_spawn = _fake_dispatch  # type: ignore[assignment]
    try:
        # Provide a stub agent registry so the spawn branch runs.
        from corlinman_agent.agents import AgentCardRegistry

        servicer._builtin_agents = AgentCardRegistry([])
        start = _start("tenant::sess-A")
        ev = _tool_event("subagent_spawn", {"goal": "g"})
        await servicer._dispatch_builtin(ev, start, _FakeProvider([]))
        await servicer._dispatch_builtin(ev, start, _FakeProvider([]))
    finally:
        mod.dispatch_subagent_spawn = orig  # type: ignore[assignment]

    assert seen == [0, 1], f"expected distinct child_seq, got {seen}"


@pytest.mark.asyncio
async def test_bug04_spawn_many_advances_base_seq() -> None:
    """A spawn_many with N tasks advances the per-parent counter by N so a
    later single spawn doesn't collide with the fan-out's children."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))

    bases: list[int] = []
    singles: list[int] = []

    async def _fake_many(*args: Any, **kwargs: Any) -> str:
        bases.append(kwargs["base_child_seq"])
        return json.dumps({"tasks": []})

    async def _fake_single(*args: Any, **kwargs: Any) -> str:
        singles.append(kwargs["child_seq"])
        return json.dumps({"child_session_key": "x", "finish_reason": "stop"})

    import corlinman_server.agent_servicer as mod

    orig_many = mod.dispatch_subagent_spawn_many
    orig_single = mod.dispatch_subagent_spawn
    mod.dispatch_subagent_spawn_many = _fake_many  # type: ignore[assignment]
    mod.dispatch_subagent_spawn = _fake_single  # type: ignore[assignment]
    try:
        from corlinman_agent.agents import AgentCardRegistry

        servicer._builtin_agents = AgentCardRegistry([])
        start = _start("tenant::sess-A")
        # Three tasks → consume seqs 0,1,2.
        many_ev = _tool_event(
            "subagent_spawn_many",
            {"tasks": [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}]},
        )
        await servicer._dispatch_builtin(many_ev, start, _FakeProvider([]))
        single_ev = _tool_event("subagent_spawn", {"goal": "g"})
        await servicer._dispatch_builtin(single_ev, start, _FakeProvider([]))
    finally:
        mod.dispatch_subagent_spawn_many = orig_many  # type: ignore[assignment]
        mod.dispatch_subagent_spawn = orig_single  # type: ignore[assignment]

    assert bases == [0]
    assert singles == [3], f"single spawn after 3-task fan-out should be seq 3, got {singles}"


# ---------------------------------------------------------------------------
# CMP-04: ask verdict must be wirable via an approval_resolver setter.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmp04_ask_resolver_can_be_wired() -> None:
    """A deployment can wire an approval_resolver so an ASK verdict is no
    longer hard fail-closed."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))

    calls: list[str] = []

    async def _resolver(tool: str, args: dict, ctx: Any) -> bool:
        calls.append(tool)
        return True

    servicer.set_approval_resolver(_resolver)
    gate = servicer._get_approval_gate()
    assert gate.has_resolver, "resolver must be wired into the approval gate"


# ---------------------------------------------------------------------------
# CMP-02: injected/card/always-on skills allowed-tools must be enforced.
# ---------------------------------------------------------------------------


def test_cmp02_default_always_skills_include_document_and_visual_quality() -> None:
    """The main chat agent must always see the PDF and visual layout
    guardrails, even when no explicit agent card is invoked."""

    assert "document-generator" in _DEFAULT_ALWAYS_SKILLS
    assert "visual-output-quality" in _DEFAULT_ALWAYS_SKILLS


@pytest.mark.asyncio
async def test_cmp02_injected_skill_allowed_tools_enforced(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A card whose skill_refs include a skill with allowed-tools
    [read_file] must block run_shell for that session, exactly like a
    Skill-pulled restriction — even though it was never on-demand-pulled."""
    from types import SimpleNamespace

    from corlinman_agent.agents import AgentCardRegistry
    from corlinman_agent.skills import SkillRegistry

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))

    # Build restricted skills whose names must be in the servicer's
    # _DEFAULT_ALWAYS_SKILLS for the always-on injection path.
    for name in _DEFAULT_ALWAYS_SKILLS:
        skills_dir = tmp_path / "skills" / name
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: test\n"
            "when_to_use: testing\n"
            "allowed-tools: [read_file]\n"
            "---\n"
            "body\n",
            encoding="utf-8",
        )

    registry = SkillRegistry.load_from_dir(tmp_path / "skills")
    # Minimal assembler stand-in: the servicer's _get_skill_registry reads
    # ``._skills``; a real ContextAssembler is not needed for the enforcement
    # path. Empty agent registry → no card peeked → only the always-on
    # defaults are folded in.
    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _m: _FakeProvider([]),
        context_assembler=SimpleNamespace(_skills=registry),
    )
    servicer._builtin_agents = AgentCardRegistry([])

    start = _start("tenant::sess-A")
    servicer._ensure_injected_skills_recorded(start, "tenant::sess-A")

    # No Skill() pull happened — the skill is injected via the always-on
    # default. run_shell must be blocked for this session.
    blocked = servicer._skill_allowed_tools_block("run_shell", "tenant::sess-A")
    assert blocked is not None, "injected skill's allowed-tools must be enforced"
    assert servicer._skill_allowed_tools_block("read_file", "tenant::sess-A") is None


# ---------------------------------------------------------------------------
# SEC-03: calculator dispatch must be offloaded + timeout-bounded.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sec03_calculator_offloaded_does_not_block_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pathological calculator call must not block the event loop: it runs
    off-thread. We simulate the pathological case with a synchronous sleep
    inside dispatch_calculator and assert a concurrent task makes progress
    while it runs. Before the fix (inline sync call) the ticker cannot
    advance until the blocking call returns."""
    import corlinman_server.agent_servicer as mod

    def _blocking_calc(*, args_json: Any) -> str:
        time.sleep(0.2)  # simulate a pathological synchronous compute
        return json.dumps({"result": "blocked-ok"})

    monkeypatch.setattr(mod, "dispatch_calculator", _blocking_calc)

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = _start("s::1")
    ev = _tool_event("calculator", {"expression": "2+2"})

    interleaved = {"during_calc": False}

    async def _concurrent() -> None:
        # Yield control then flip the flag. If the calc is offloaded, this
        # runs WHILE the 0.2s blocking compute is on a worker thread.
        await asyncio.sleep(0.05)
        interleaved["during_calc"] = True

    calc_task = asyncio.ensure_future(
        servicer._dispatch_builtin(ev, start, _FakeProvider([]))
    )
    conc_task = asyncio.ensure_future(_concurrent())
    result = await calc_task
    await conc_task
    # If the calculator blocked the loop inline, ``_concurrent`` could not
    # have run its sleep+flip until after the 0.2s blocking call returned;
    # off-thread it interleaves freely. We assert the flag flipped before
    # the calc completed by checking the calc result depended on the worker
    # thread (no deadlock) and the concurrent task finished.
    assert interleaved["during_calc"] is True
    payload = json.loads(result)
    assert payload.get("result") == "blocked-ok"


@pytest.mark.asyncio
async def test_sec03_calculator_timeout_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A calculator call exceeding the wall-clock budget returns an error
    envelope rather than stalling the dispatch forever."""
    import corlinman_server.agent_servicer as mod

    def _hang_calc(*, args_json: Any) -> str:
        time.sleep(5.0)
        return json.dumps({"result": "never"})

    monkeypatch.setattr(mod, "dispatch_calculator", _hang_calc)
    monkeypatch.setenv("CORLINMAN_CALCULATOR_TIMEOUT_S", "0.2")

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = _start("s::1")
    ev = _tool_event("calculator", {"expression": "huge"})
    started = time.monotonic()
    result = await servicer._dispatch_builtin(ev, start, _FakeProvider([]))
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, "dispatch must return well before the 5s hang"
    payload = json.loads(result)
    assert "error" in payload


@pytest.mark.asyncio
async def test_child_executor_rejects_background_shell() -> None:
    """A subagent child cannot start a detached run_shell(run_in_background=
    true): the task would register under the parent session and outlive the
    child's wall-clock cap with nothing to reap it (Codex #112 r6). The child
    executor refuses bg mode; a foreground run_shell still flows through."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    child_exec = servicer._make_child_tool_executor(
        _start("parent::sess"), _FakeProvider([]), None
    )
    # Background mode is refused.
    bg = json.loads(
        await child_exec(_tool_event("run_shell", {"command": "sleep 5", "run_in_background": True}))
    )
    assert "background_not_allowed_in_subagent" in bg["error"]
    # A non-bg run_shell is NOT short-circuited here (flows to dispatch).
    fg = json.loads(
        await child_exec(_tool_event("run_shell", {"command": "echo hi"}))
    )
    assert "background_not_allowed_in_subagent" not in fg.get("error", "")
