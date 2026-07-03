"""``/hooks`` console command — view / test / reload (Dim 9 parity)."""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest
from corlinman_hooks import HookRunner
from corlinman_server.console.brain import BrainSession
from corlinman_server.console.commands import dispatch
from corlinman_server.console.render import Renderer
from corlinman_server.console.router import ModelRouter
from rich.console import Console

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shell hooks are POSIX-flavored")


class _HookBrain:
    descriptor = "stub brain with hooks"

    def __init__(self, runner: HookRunner | None) -> None:
        self._runner = runner

    def get_hook_runner(self) -> HookRunner | None:
        return self._runner

    def run_turn(self, **_kw: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("commands must not run turns")

    async def aclose(self) -> None:  # pragma: no cover - unused
        pass


class _NoHooksBrain:
    descriptor = "stub brain without hooks surface"

    def run_turn(self, **_kw: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("commands must not run turns")

    async def aclose(self) -> None:  # pragma: no cover - unused
        pass


class StubApp:
    def __init__(self, brain: Any) -> None:
        self.session = BrainSession(brain=brain, model="big")
        self.renderer = Renderer(Console(file=io.StringIO(), force_terminal=False))
        self.router = ModelRouter(default_model="big", small_fast_model="small", auto_route=False)
        self.running = True


def _runner_with(declarative: dict, **legacy: str) -> HookRunner:
    return HookRunner({"hooks": {**legacy, "declarative": declarative}})


async def test_hooks_unavailable_without_surface() -> None:
    app = StubApp(_NoHooksBrain())
    text = await dispatch(app, "/hooks") or ""
    assert "unavailable" in text


async def test_hooks_view_lists_all_layers() -> None:
    decl = {
        "PreToolUse": [
            {"matcher": "run_shell", "if": "run_shell(git:*)", "hooks": [{"kind": "command", "command": "true"}]}
        ],
        "SessionStart": [{"hooks": [{"kind": "http", "url": "http://x/h"}]}],
    }
    runner = _runner_with(decl, pre_tool="legacy.sh")
    app = StubApp(_HookBrain(runner))
    text = await dispatch(app, "/hooks") or ""
    assert "pre_tool: legacy.sh" in text
    assert "matcher=run_shell" in text
    assert "if=run_shell(git:*)" in text
    assert "— live" in text  # pre_tool has a live emitter
    assert "no live emitter yet" in text  # session_start does not
    assert "/hooks [test" in text


async def test_hooks_view_surfaces_warnings() -> None:
    runner = _runner_with({"TeleportUser": [{"hooks": [{"kind": "command", "command": "true"}]}]})
    app = StubApp(_HookBrain(runner))
    text = await dispatch(app, "/hooks") or ""
    assert "TeleportUser" in text
    assert "⚠" in text


async def test_hooks_test_pre_tool_deny() -> None:
    runner = _runner_with(
        {"PreToolUse": [{"hooks": [{"kind": "command", "command": "echo test-deny >&2; exit 2"}]}]}
    )
    app = StubApp(_HookBrain(runner))
    text = await dispatch(app, "/hooks test PreToolUse run_shell") or ""
    assert "allow: False" in text
    assert "test-deny" in text


async def test_hooks_test_passes_json_args() -> None:
    runner = _runner_with(
        {"PreToolUse": [{"hooks": [{"kind": "command", "command": "true"}]}]}
    )
    app = StubApp(_HookBrain(runner))
    args = json.dumps({"command": "ls"})
    text = await dispatch(app, f"/hooks test pre_tool run_shell {args}") or ""
    assert "allow: True" in text


async def test_hooks_test_unknown_event() -> None:
    app = StubApp(_HookBrain(_runner_with({})))
    text = await dispatch(app, "/hooks test Teleport") or ""
    assert "unknown event" in text


async def test_hooks_reload_reports_summary(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    runner = _runner_with(
        {"PreToolUse": [{"hooks": [{"kind": "command", "command": "exit 2"}]}]}
    )
    drop = tmp_path / "py-config.json"
    drop.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(drop))
    app = StubApp(_HookBrain(runner))
    text = await dispatch(app, "/hooks reload") or ""
    assert "hooks reloaded" in text
    assert "0 declarative group(s)" in text
    ok, _ = runner.run_pre_tool("run_shell", {})
    assert ok is True  # the deny group is gone after reload


async def test_hooks_reload_refuses_without_config_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CORLINMAN_PY_CONFIG → refuse instead of wiping live hooks (Codex #109)."""
    runner = _runner_with(
        {"PreToolUse": [{"hooks": [{"kind": "command", "command": "exit 2"}]}]}
    )
    monkeypatch.delenv("CORLINMAN_PY_CONFIG", raising=False)
    app = StubApp(_HookBrain(runner))
    text = await dispatch(app, "/hooks reload") or ""
    assert "unavailable" in text
    ok, _ = runner.run_pre_tool("run_shell", {})
    assert ok is False  # the deny group survived the refused reload


async def test_hooks_appears_in_help() -> None:
    app = StubApp(_HookBrain(_runner_with({})))
    text = await dispatch(app, "/help") or ""
    assert "/hooks" in text
