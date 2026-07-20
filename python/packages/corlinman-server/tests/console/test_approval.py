"""Interactive console approval resolver (ABSORB_MATRIX Dim 3, slice 2).

Covers the resolver's y/always/No semantics (deny-by-default on anything
unexpected), the session-scoped always-allow cache, the renderer-pausing
prompter, and — the load-bearing check — the resolver driving the REAL
``ApprovalGate`` so an ``ask`` permission rule becomes interactive.
"""

from __future__ import annotations

import io
from typing import Any

from corlinman_server.console.approval import (
    ConsoleApprovalResolver,
    build_console_prompter,
)
from rich.console import Console


class _ScriptedPrompter:
    """Returns queued answers; records how often it was asked."""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.calls: list[str] = []

    async def __call__(self, desc: str) -> str:
        self.calls.append(desc)
        return self._answers.pop(0)


async def test_yes_allows_once_and_asks_again() -> None:
    prompter = _ScriptedPrompter("y", "yes")
    resolver = ConsoleApprovalResolver(prompter)
    assert await resolver("run_shell", {"command": "rm x"}, None) is True
    assert await resolver("run_shell", {"command": "rm y"}, None) is True
    assert len(prompter.calls) == 2  # "y" is once-only — asked again


async def test_always_caches_for_the_session() -> None:
    prompter = _ScriptedPrompter("a")
    resolver = ConsoleApprovalResolver(prompter)
    assert await resolver("run_shell", {"command": "ls"}, None) is True
    # Second call: no prompt at all.
    assert await resolver("run_shell", {"command": "rm -rf /"}, None) is True
    assert len(prompter.calls) == 1
    assert resolver.always_allow == {"run_shell"}


async def test_anything_else_denies() -> None:
    prompter = _ScriptedPrompter("n", "", "whatever", "  NO  ")
    resolver = ConsoleApprovalResolver(prompter)
    for _ in range(4):
        assert await resolver("write_file", {"path": "x"}, None) is False


async def test_prompter_failure_fails_closed() -> None:
    async def _boom(desc: str) -> str:
        raise RuntimeError("tty gone")

    resolver = ConsoleApprovalResolver(_boom)
    assert await resolver("run_shell", {}, None) is False


async def test_persist_answer_grants_and_persists() -> None:
    """'p' allows, caches for the session, AND calls the persist hook (E1
    — the durable variant of 'always')."""
    persisted: list[str] = []
    resolver = ConsoleApprovalResolver(
        _ScriptedPrompter("p"), persist=persisted.append
    )
    assert await resolver("run_shell", {"command": "ls"}, None) is True
    assert persisted == ["run_shell"]
    assert resolver.always_allow == {"run_shell"}
    # No further prompt this session.
    assert await resolver("run_shell", {"command": "pwd"}, None) is True


async def test_persist_answer_failure_degrades_to_session_grant() -> None:
    """A persist hook that raises must not turn an approval into a deny —
    the grant degrades to session-scoped."""

    def _boom(tool: str) -> None:
        raise OSError("disk full")

    resolver = ConsoleApprovalResolver(_ScriptedPrompter("persist"), persist=_boom)
    assert await resolver("run_shell", {}, None) is True
    assert resolver.always_allow == {"run_shell"}


async def test_persist_answer_without_hook_acts_like_always() -> None:
    resolver = ConsoleApprovalResolver(_ScriptedPrompter("p"))
    assert await resolver("write_file", {}, None) is True
    assert resolver.always_allow == {"write_file"}


async def test_args_preview_truncates_and_survives_bad_args() -> None:
    seen: list[str] = []

    async def _capture(desc: str) -> str:
        seen.append(desc)
        return "n"

    resolver = ConsoleApprovalResolver(_capture)
    await resolver("run_shell", {"command": "x" * 500}, None)
    assert len(seen[0]) < 260  # preview capped
    # Non-JSON-serializable args must not break the prompt.
    await resolver("run_shell", {"weird": object()}, None)
    assert len(seen) == 2


async def test_console_prompter_pauses_live_and_prints_request() -> None:
    class _Renderer:
        def __init__(self) -> None:
            self.console = Console(file=io.StringIO(), force_terminal=False)
            self.live_stopped = 0

        def _stop_live(self) -> None:
            self.live_stopped += 1

    renderer = _Renderer()

    async def _reader(suffix: str) -> str:
        assert "allow?" in suffix
        return "y"

    prompt = build_console_prompter(renderer, reader=_reader)
    answer = await prompt('run_shell {"command": "ls"}')
    assert answer == "y"
    assert renderer.live_stopped == 1  # spinner paused before prompting
    out = renderer.console.file.getvalue()
    assert "approval needed" in out and "run_shell" in out


async def test_resolver_drives_the_real_approval_gate() -> None:
    """End-to-end contract: an ``ask`` permission rule + this resolver =
    interactive allow/deny through the REAL ApprovalGate (which previously
    fail-closed everywhere because nothing wired a resolver)."""
    from corlinman_agent.approval_gate import ApprovalGate, ApprovalVerdict
    from corlinman_agent.permission import ASK, PermissionGate, PermissionRule

    resolver = ConsoleApprovalResolver(_ScriptedPrompter("y", "n", "a"))
    gate = ApprovalGate(
        PermissionGate([PermissionRule(tool="run_shell", action=ASK)]),
        resolver=resolver,
    )

    first = await gate.decide("run_shell", args={"command": "ls"})
    assert first.verdict is ApprovalVerdict.ALLOW and first.asked is True

    second = await gate.decide("run_shell", args={"command": "rm x"})
    assert second.verdict is ApprovalVerdict.DENY

    third = await gate.decide("run_shell", args={"command": "ls"})
    assert third.verdict is ApprovalVerdict.ALLOW  # "a" → cached…
    fourth = await gate.decide("run_shell", args={"command": "ls -la"})
    assert fourth.verdict is ApprovalVerdict.ALLOW  # …no further prompt


def test_permissions_command_lists_always_allowed(monkeypatch: Any) -> None:
    """/permissions (no args) surfaces the session's always-allow set."""
    import asyncio

    from corlinman_server.console.commands import dispatch

    from .test_commands import StubApp, _PermBrain

    app = StubApp()
    app.session.brain = _PermBrain()
    resolver = ConsoleApprovalResolver(_ScriptedPrompter())
    resolver.always_allow.add("run_shell")
    app.approval_resolver = resolver

    text = asyncio.run(dispatch(app, "/permissions")) or ""
    assert "always-allowed this session: run_shell" in text


async def test_reset_clears_always_cache() -> None:
    """resolver.reset() drops the always grants (Codex #104 — the cache
    must not outlive the session it was granted in)."""
    prompter = _ScriptedPrompter("a", "n")
    resolver = ConsoleApprovalResolver(prompter)
    assert await resolver("run_shell", {}, None) is True
    resolver.reset()
    assert resolver.always_allow == set()
    assert await resolver("run_shell", {}, None) is False  # asked again


async def test_new_and_clear_reset_always_cache() -> None:
    """/new and /clear start a fresh session — always grants must not leak
    across the boundary (Codex #104)."""
    import io as _io

    from corlinman_server.console.brain import BrainSession
    from corlinman_server.console.commands import dispatch
    from rich.console import Console as _Console

    class _IdleBrain:
        descriptor = "stub"

        async def aclose(self) -> None:  # pragma: no cover
            pass

    class _App:
        def __init__(self) -> None:
            self.session = BrainSession(brain=_IdleBrain(), model="m")
            self.approval_resolver = ConsoleApprovalResolver(_ScriptedPrompter("a"))
            self.renderer = type("R", (), {"console": _Console(file=_io.StringIO())})()

    app = _App()
    await app.approval_resolver("run_shell", {}, None)
    assert app.approval_resolver.always_allow == {"run_shell"}
    await dispatch(app, "/new")
    assert app.approval_resolver.always_allow == set()

    app2 = _App()
    await app2.approval_resolver("run_shell", {}, None)
    await dispatch(app2, "/clear")
    assert app2.approval_resolver.always_allow == set()


async def test_permission_mode_switch_resets_always_cache() -> None:
    """Switching permission modes (notably into /plan) clears the always
    cache (Codex #104) — a cached run_shell grant must not keep mutating
    the workspace in plan mode."""
    import io as _io

    from corlinman_server.console.brain import BrainSession
    from corlinman_server.console.commands import dispatch
    from rich.console import Console as _Console

    class _GatedBrain:
        descriptor = "stub"

        def __init__(self) -> None:
            self.mode = "default"

        def get_permission_mode(self) -> str:
            return self.mode

        def set_permission_mode(self, mode: str) -> str:
            self.mode = mode
            return mode

        async def aclose(self) -> None:  # pragma: no cover
            pass

    class _App:
        def __init__(self) -> None:
            self.session = BrainSession(brain=_GatedBrain(), model="m")
            self.approval_resolver = ConsoleApprovalResolver(_ScriptedPrompter("a"))
            self.renderer = type("R", (), {"console": _Console(file=_io.StringIO())})()

    app = _App()
    await app.approval_resolver("run_shell", {}, None)
    assert app.approval_resolver.always_allow == {"run_shell"}
    out = await dispatch(app, "/plan")
    assert isinstance(out, str) and "plan" in out
    assert app.approval_resolver.always_allow == set()


async def test_concurrent_approvals_are_serialized() -> None:
    """Two overlapping approval calls must never prompt concurrently
    (Codex #104 — competing prompt_toolkit sessions corrupt the
    terminal)."""
    import asyncio

    in_flight = 0
    max_in_flight = 0

    async def _slow_prompter(desc: str) -> str:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return "y"

    resolver = ConsoleApprovalResolver(_slow_prompter)
    results = await asyncio.gather(
        resolver("run_shell", {"command": "a"}, None),
        resolver("write_file", {"path": "b"}, None),
    )
    assert results == [True, True]
    assert max_in_flight == 1


async def test_persist_answer_is_durable_across_gate_rebuild(tmp_path: Any) -> None:
    """End-to-end E1: a 'persist' answer, wired to the real
    ``persist_allow_rule``, must make a freshly-built permission gate decide
    ``allow`` for the tool — the grant survives the session that granted it."""
    from corlinman_agent.permission import ALLOW
    from corlinman_agent.permission_settings import (
        build_permission_gate,
        persist_allow_rule,
    )

    resolver = ConsoleApprovalResolver(
        _ScriptedPrompter("p"),
        persist=lambda tool: persist_allow_rule(tool, data_dir=tmp_path),
    )
    assert await resolver("run_shell", {"command": "ls"}, None) is True

    gate = build_permission_gate(data_dir=tmp_path, project_dir=tmp_path / "proj")
    assert gate.decide("run_shell") == ALLOW
