"""Interactive console approval resolver (ABSORB_MATRIX Dim 3, slice 2).

W3-1 rewrote the resolver around the shared :class:`GrantStore` and the
unified memory vocabulary (C2 — once / session / always):

* grants are **argument-scoped** — approving ``run_shell ls`` no longer
  silences the prompt for ``run_shell rm -rf /`` (the old
  ``always_allow: set[str]`` cache did exactly that, agent-gate §8.7);
* ``p`` records a durable GrantStore row instead of appending a global
  unconditional allow rule to ``settings.json``.

Covers the y/session/always/No semantics (deny-by-default on anything
unexpected), grant arg-specificity, the renderer-pausing prompter, and —
the load-bearing check — the resolver driving the REAL ``ApprovalGate``
so an ``ask`` permission rule becomes interactive.
"""

from __future__ import annotations

import io
from typing import Any

from corlinman_agent.authz.grants import GrantStore
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


def _resolver(
    prompter: _ScriptedPrompter, tmp_path: Any = None
) -> ConsoleApprovalResolver:
    """Resolver with an ISOLATED store — tests must not share the global."""
    return ConsoleApprovalResolver(prompter, grant_store=GrantStore(tmp_path))


async def test_yes_allows_once_and_asks_again() -> None:
    prompter = _ScriptedPrompter("y", "yes")
    resolver = _resolver(prompter)
    assert await resolver("run_shell", {"command": "rm x"}, None) is True
    assert await resolver("run_shell", {"command": "rm y"}, None) is True
    assert len(prompter.calls) == 2  # "y" is once-only — asked again


async def test_session_grant_is_arg_scoped() -> None:
    """Acceptance 6 (design plan W3-1): 'a' grants THIS call's argument
    shape for the session — a different command still prompts."""
    prompter = _ScriptedPrompter("a", "n")
    resolver = _resolver(prompter)
    assert await resolver("run_shell", {"command": "ls"}, None) is True
    # Identical call: covered by the session grant, no prompt.
    assert await resolver("run_shell", {"command": "ls"}, None) is True
    assert len(prompter.calls) == 1
    # DIFFERENT args: the grant does not stretch — prompts again ("n").
    assert await resolver("run_shell", {"command": "rm -rf /"}, None) is False
    assert len(prompter.calls) == 2
    assert resolver.session_grant_tools() == {"run_shell"}


async def test_anything_else_denies() -> None:
    prompter = _ScriptedPrompter("n", "", "whatever", "  NO  ")
    resolver = _resolver(prompter)
    for _ in range(4):
        assert await resolver("write_file", {"path": "x"}, None) is False


async def test_prompter_failure_fails_closed() -> None:
    async def _boom(desc: str) -> str:
        raise RuntimeError("tty gone")

    resolver = ConsoleApprovalResolver(_boom, grant_store=GrantStore())
    assert await resolver("run_shell", {}, None) is False


async def test_always_answer_is_durable_across_stores(tmp_path: Any) -> None:
    """'p' records a durable always grant: a FRESH store on the same
    data_dir (a new process, in production) still honours it — and the
    rules file is not involved at all (the old persist path appended a
    global unconditional allow rule to settings.json)."""
    resolver = _resolver(_ScriptedPrompter("p"), tmp_path)
    assert await resolver("run_shell", {"command": "ls"}, None) is True

    fresh = ConsoleApprovalResolver(
        _ScriptedPrompter(), grant_store=GrantStore(tmp_path)
    )
    # No prompt — the durable grant covers the identical call.
    assert await fresh("run_shell", {"command": "ls"}, None) is True
    # …but only that argument shape.
    assert (tmp_path / "authz" / "grants.sqlite3").exists()
    assert not (tmp_path / "settings.json").exists()


async def test_always_answer_stays_arg_scoped(tmp_path: Any) -> None:
    prompter = _ScriptedPrompter("p", "n")
    resolver = _resolver(prompter, tmp_path)
    assert await resolver("run_shell", {"command": "ls"}, None) is True
    assert await resolver("run_shell", {"command": "rm -rf /"}, None) is False
    assert len(prompter.calls) == 2


async def test_args_preview_truncates_and_survives_bad_args() -> None:
    seen: list[str] = []

    async def _capture(desc: str) -> str:
        seen.append(desc)
        return "n"

    resolver = ConsoleApprovalResolver(_capture, grant_store=GrantStore())
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
    interactive allow/deny through the REAL ApprovalGate. The 'a' grant
    covers the exact argument shape only."""
    from corlinman_agent.approval_gate import ApprovalGate, ApprovalVerdict
    from corlinman_agent.permission import ASK, PermissionGate, PermissionRule

    resolver = _resolver(_ScriptedPrompter("y", "n", "a", "n"))
    gate = ApprovalGate(
        PermissionGate([PermissionRule(tool="run_shell", action=ASK)]),
        resolver=resolver,
    )

    first = await gate.decide("run_shell", args={"command": "ls"})
    assert first.verdict is ApprovalVerdict.ALLOW and first.asked is True

    second = await gate.decide("run_shell", args={"command": "rm x"})
    assert second.verdict is ApprovalVerdict.DENY

    third = await gate.decide("run_shell", args={"command": "ls"})
    assert third.verdict is ApprovalVerdict.ALLOW  # "a" → session grant…
    fourth = await gate.decide("run_shell", args={"command": "ls"})
    assert fourth.verdict is ApprovalVerdict.ALLOW  # …same args: no prompt
    # Different args: the grant does not stretch — prompts again ("n").
    fifth = await gate.decide("run_shell", args={"command": "ls -la"})
    assert fifth.verdict is ApprovalVerdict.DENY


def test_permissions_command_lists_session_grants(monkeypatch: Any) -> None:
    """/permissions (no args) surfaces the session's granted tool names."""
    import asyncio

    from corlinman_server.console.commands import dispatch

    from .test_commands import StubApp, _PermBrain

    app = StubApp()
    app.session.brain = _PermBrain()
    store = GrantStore()
    resolver = ConsoleApprovalResolver(_ScriptedPrompter(), grant_store=store)
    store.record(None, "run_shell", {"command": "ls"}, "session")
    app.approval_resolver = resolver

    text = asyncio.run(dispatch(app, "/permissions")) or ""
    assert "always-allowed this session: run_shell" in text


async def test_reset_clears_session_grants() -> None:
    """resolver.reset() drops the session grants (Codex #104 — a grant
    must not outlive the session it was granted in)."""
    prompter = _ScriptedPrompter("a", "n")
    resolver = _resolver(prompter)
    assert await resolver("run_shell", {}, None) is True
    resolver.reset()
    assert resolver.session_grant_tools() == set()
    assert await resolver("run_shell", {}, None) is False  # asked again


async def test_new_and_clear_reset_session_grants() -> None:
    """/new and /clear start a fresh session — session grants must not
    leak across the boundary (Codex #104)."""
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
            self.approval_resolver = _resolver(_ScriptedPrompter("a"))
            self.renderer = type("R", (), {"console": _Console(file=_io.StringIO())})()

    app = _App()
    await app.approval_resolver("run_shell", {}, None)
    assert app.approval_resolver.session_grant_tools() == {"run_shell"}
    await dispatch(app, "/new")
    assert app.approval_resolver.session_grant_tools() == set()

    app2 = _App()
    await app2.approval_resolver("run_shell", {}, None)
    await dispatch(app2, "/clear")
    assert app2.approval_resolver.session_grant_tools() == set()


async def test_permission_mode_switch_resets_session_grants() -> None:
    """Switching permission modes (notably into /plan) clears the session
    grants (Codex #104) — a cached run_shell grant must not keep mutating
    the workspace in plan mode. Acceptance 7 of the W3-1 plan."""
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
            self.approval_resolver = _resolver(_ScriptedPrompter("a"))
            self.renderer = type("R", (), {"console": _Console(file=_io.StringIO())})()

    app = _App()
    await app.approval_resolver("run_shell", {}, None)
    assert app.approval_resolver.session_grant_tools() == {"run_shell"}
    out = await dispatch(app, "/plan")
    assert isinstance(out, str) and "plan" in out
    assert app.approval_resolver.session_grant_tools() == set()


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

    resolver = ConsoleApprovalResolver(_slow_prompter, grant_store=GrantStore())
    results = await asyncio.gather(
        resolver("run_shell", {"command": "a"}, None),
        resolver("write_file", {"path": "b"}, None),
    )
    assert results == [True, True]
    assert max_in_flight == 1
