"""Tests for the declarative hooks layer (claude-code parity, Dim 9).

Covers:
- ``parse_declarative``: claude-code + snake_case event names, unknown
  events/kinds collected as warnings (never raised), per-hook timeout and
  async-default resolution;
- ``match_tool``: ``*`` / exact / ``A|B`` alternation / ``run_*`` prefix,
  case-sensitive;
- command kind exit-code table: 0 = allow, 0 + stdout JSON
  ``{"decision":"block"}`` = deny, 2 = deny (stderr reason), other = fail-open,
  timeout = fail-open;
- http kind verdict shapes + network fail-open (injected transport);
- prompt / agent kinds wired + unwired;
- ``if`` rule via injected matcher (unset matcher → group skipped);
- fold: first deny short-circuits, mutation last-write-wins, async-designated
  hooks never block;
- HookRunner integration: legacy shell → discovered → declarative order,
  ``run_stop`` / ``run_event_async`` / ``run_post_tool_async``, ``reload``
  diff, introspection props, ``supported_events`` gains
  ``user_prompt_submit``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from corlinman_hooks import HookRunner
from corlinman_hooks.declarative import (
    DeclarativeEngine,
    match_tool,
    parse_declarative,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shell hooks are POSIX-flavored")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine(section: dict, **kwargs) -> DeclarativeEngine:
    return DeclarativeEngine(parse_declarative(section), **kwargs)


def _cmd_group(event: str, command: str, **hook_extra) -> dict:
    return {event: [{"hooks": [{"kind": "command", "command": command, **hook_extra}]}]}


# ---------------------------------------------------------------------------
# parse_declarative
# ---------------------------------------------------------------------------


def test_parse_maps_claude_code_event_names():
    cfg = parse_declarative(
        {
            "PreToolUse": [{"hooks": [{"kind": "command", "command": "true"}]}],
            "PostToolUse": [{"hooks": [{"kind": "command", "command": "true"}]}],
            "Stop": [{"hooks": [{"kind": "command", "command": "true"}]}],
            "UserPromptSubmit": [{"hooks": [{"kind": "command", "command": "true"}]}],
            "SessionStart": [{"hooks": [{"kind": "command", "command": "true"}]}],
            "PreCompact": [{"hooks": [{"kind": "command", "command": "true"}]}],
        }
    )
    assert set(cfg.groups) == {
        "pre_tool",
        "post_tool",
        "stop",
        "user_prompt_submit",
        "session_start",
        "pre_compact",
    }
    assert cfg.warnings == []


def test_parse_accepts_snake_case_names():
    cfg = parse_declarative(_cmd_group("pre_tool", "true"))
    assert "pre_tool" in cfg.groups


def test_parse_unknown_event_warns_and_drops():
    cfg = parse_declarative(_cmd_group("TeleportUser", "true"))
    assert cfg.groups == {}
    assert any("TeleportUser" in w for w in cfg.warnings)


def test_parse_unknown_kind_warns_and_drops_hook():
    cfg = parse_declarative({"PreToolUse": [{"hooks": [{"kind": "carrier_pigeon"}]}]})
    assert cfg.groups.get("pre_tool", []) == [] or not cfg.groups["pre_tool"][0].hooks
    assert any("carrier_pigeon" in w for w in cfg.warnings)


def test_parse_command_kind_requires_command_field():
    cfg = parse_declarative({"PreToolUse": [{"hooks": [{"kind": "command"}]}]})
    assert any("command" in w for w in cfg.warnings)


def test_parse_defensive_on_garbage_shapes():
    cfg = parse_declarative({"PreToolUse": "not-a-list", "Stop": [42]})
    assert cfg.groups == {}
    assert len(cfg.warnings) >= 2


def test_parse_timeout_and_async_flags():
    cfg = parse_declarative(
        {
            "PreToolUse": [
                {
                    "hooks": [
                        {"kind": "command", "command": "true", "timeout": 11.5},
                        {"kind": "command", "command": "true", "async": True},
                    ]
                }
            ],
            "PostToolUse": [{"hooks": [{"kind": "command", "command": "true"}]}],
        }
    )
    pre = cfg.groups["pre_tool"][0].hooks
    assert pre[0].timeout == 11.5
    assert pre[0].fire_async is False  # pre_tool default = sync/blocking
    assert pre[1].fire_async is True  # explicit override
    post = cfg.groups["post_tool"][0].hooks
    assert post[0].fire_async is True  # post_tool default = async


def test_parse_none_section_is_empty():
    cfg = parse_declarative(None)
    assert cfg.groups == {}
    assert cfg.warnings == []


# ---------------------------------------------------------------------------
# match_tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "tool", "expected"),
    [
        ("*", "run_shell", True),
        ("", "run_shell", True),
        ("run_shell", "run_shell", True),
        ("run_shell", "read_file", False),
        ("read_file|write_file", "write_file", True),
        ("read_file|write_file", "run_shell", False),
        ("run_*", "run_shell", True),
        ("run_*", "read_file", False),
        ("Run_shell", "run_shell", False),  # case-sensitive
        ("*", "", True),  # tool-less events always match the wildcard
        ("run_shell", "", False),
    ],
)
def test_match_tool(pattern: str, tool: str, expected: bool):
    assert match_tool(pattern, tool) is expected


# ---------------------------------------------------------------------------
# command kind — exit-code table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_exit_zero_allows():
    eng = _engine(_cmd_group("PreToolUse", "true"))
    decision = await eng.run("pre_tool", "run_shell", {"cmd": "ls"}, {})
    assert decision.allow is True


@pytest.mark.asyncio
async def test_command_exit_zero_with_block_json_denies():
    cmd = """echo '{"decision": "block", "reason": "policy says no"}'"""
    eng = _engine(_cmd_group("PreToolUse", cmd))
    decision = await eng.run("pre_tool", "run_shell", {}, {})
    assert decision.allow is False
    assert decision.reason == "policy says no"


@pytest.mark.asyncio
async def test_command_exit_zero_with_mutated_args_json():
    cmd = """echo '{"decision": "allow", "mutated_args": {"cmd": "ls -la"}}'"""
    eng = _engine(_cmd_group("PreToolUse", cmd))
    decision = await eng.run("pre_tool", "run_shell", {"cmd": "ls"}, {})
    assert decision.allow is True
    assert decision.mutated_args == {"cmd": "ls -la"}


@pytest.mark.asyncio
async def test_command_exit_two_blocks_with_stderr_reason():
    eng = _engine(_cmd_group("PreToolUse", "echo 'nope from stderr' >&2; exit 2"))
    decision = await eng.run("pre_tool", "run_shell", {}, {})
    assert decision.allow is False
    assert "nope from stderr" in (decision.reason or "")


@pytest.mark.asyncio
async def test_command_other_nonzero_exit_fails_open():
    eng = _engine(_cmd_group("PreToolUse", "echo whatever; exit 7"))
    decision = await eng.run("pre_tool", "run_shell", {}, {})
    assert decision.allow is True


@pytest.mark.asyncio
async def test_command_timeout_fails_open():
    eng = _engine(_cmd_group("PreToolUse", "sleep 30", timeout=0.2))
    decision = await eng.run("pre_tool", "run_shell", {}, {})
    assert decision.allow is True


@pytest.mark.asyncio
async def test_command_receives_payload_on_stdin(tmp_path: Path):
    out = tmp_path / "payload.json"
    eng = _engine(_cmd_group("PreToolUse", f"cat > {out}"))
    await eng.run("pre_tool", "run_shell", {"cmd": "ls"}, {"session_key": "s1"})
    data = json.loads(out.read_text())
    assert data["event"] == "pre_tool"
    assert data["tool_name"] == "run_shell"
    assert data["tool_input"] == {"cmd": "ls"}
    assert data["session_key"] == "s1"


def test_command_sync_path_matches_async():
    eng = _engine(_cmd_group("PreToolUse", "echo deny >&2; exit 2"))
    decision = eng.run_sync("pre_tool", "run_shell", {}, {})
    assert decision.allow is False


# ---------------------------------------------------------------------------
# http kind (injected transport)
# ---------------------------------------------------------------------------


def _http_group(url: str = "http://127.0.0.1:1/hook") -> dict:
    return {"PreToolUse": [{"hooks": [{"kind": "http", "url": url}]}]}


@pytest.mark.asyncio
async def test_http_block_verdict_denies():
    def fake_post(url, body, timeout):
        assert body["tool_name"] == "run_shell"
        return 200, json.dumps({"decision": "block", "reason": "http says no"})

    eng = _engine(_http_group(), http_post=fake_post)
    decision = await eng.run("pre_tool", "run_shell", {}, {})
    assert decision.allow is False
    assert decision.reason == "http says no"


@pytest.mark.asyncio
async def test_http_allow_verdict_with_mutation():
    def fake_post(url, body, timeout):
        return 200, json.dumps({"decision": "allow", "mutated_args": {"x": 1}})

    eng = _engine(_http_group(), http_post=fake_post)
    decision = await eng.run("pre_tool", "run_shell", {}, {})
    assert decision.allow is True
    assert decision.mutated_args == {"x": 1}


@pytest.mark.asyncio
async def test_http_non_2xx_fails_open():
    eng = _engine(_http_group(), http_post=lambda u, b, t: (500, "boom"))
    decision = await eng.run("pre_tool", "run_shell", {}, {})
    assert decision.allow is True


@pytest.mark.asyncio
async def test_http_transport_error_fails_open():
    def broken(url, body, timeout):
        raise OSError("connection refused")

    eng = _engine(_http_group(), http_post=broken)
    decision = await eng.run("pre_tool", "run_shell", {}, {})
    assert decision.allow is True


@pytest.mark.asyncio
async def test_http_missing_url_warns_at_parse():
    cfg = parse_declarative({"PreToolUse": [{"hooks": [{"kind": "http"}]}]})
    assert any("url" in w for w in cfg.warnings)


# ---------------------------------------------------------------------------
# prompt / agent kinds
# ---------------------------------------------------------------------------


def _prompt_group() -> dict:
    return {"Stop": [{"hooks": [{"kind": "prompt", "prompt": "did it finish?"}]}]}


@pytest.mark.asyncio
async def test_prompt_evaluator_deny():
    async def judge(prompt, payload):
        assert prompt == "did it finish?"
        return {"ok": False, "reason": "task incomplete"}

    eng = _engine(_prompt_group(), prompt_evaluator=judge)
    decision = await eng.run("stop", "", {}, {})
    assert decision.allow is False
    assert decision.reason == "task incomplete"


@pytest.mark.asyncio
async def test_prompt_evaluator_allow():
    async def judge(prompt, payload):
        return {"ok": True}

    eng = _engine(_prompt_group(), prompt_evaluator=judge)
    decision = await eng.run("stop", "", {}, {})
    assert decision.allow is True


@pytest.mark.asyncio
async def test_prompt_unwired_fails_open():
    eng = _engine(_prompt_group())
    decision = await eng.run("stop", "", {}, {})
    assert decision.allow is True


@pytest.mark.asyncio
async def test_agent_evaluator_deny():
    async def verifier(instructions, payload):
        return {"ok": False, "reason": "verifier rejected"}

    section = {"PreToolUse": [{"hooks": [{"kind": "agent", "instructions": "verify the diff"}]}]}
    eng = _engine(section, agent_evaluator=verifier)
    decision = await eng.run("pre_tool", "write_file", {}, {})
    assert decision.allow is False
    assert decision.reason == "verifier rejected"


@pytest.mark.asyncio
async def test_agent_unwired_fails_open():
    section = {"PreToolUse": [{"hooks": [{"kind": "agent", "instructions": "verify"}]}]}
    eng = _engine(section)
    decision = await eng.run("pre_tool", "write_file", {}, {})
    assert decision.allow is True


# ---------------------------------------------------------------------------
# matcher + if-rule gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matcher_skips_non_matching_tool(tmp_path: Path):
    marker = tmp_path / "ran"
    section = {
        "PreToolUse": [
            {"matcher": "run_shell", "hooks": [{"kind": "command", "command": f"touch {marker}; exit 2"}]}
        ]
    }
    eng = _engine(section)
    decision = await eng.run("pre_tool", "read_file", {}, {})
    assert decision.allow is True
    assert not marker.exists()


@pytest.mark.asyncio
async def test_if_rule_gates_group():
    seen: list[tuple[str, str]] = []

    def rule_matcher(rule, tool, args):
        seen.append((rule, tool))
        return args.get("cmd", "").startswith("git push")

    section = {
        "PreToolUse": [
            {
                "matcher": "run_shell",
                "if": "run_shell(git push*)",
                "hooks": [{"kind": "command", "command": "exit 2"}],
            }
        ]
    }
    eng = _engine(section, rule_matcher=rule_matcher)
    ok = await eng.run("pre_tool", "run_shell", {"cmd": "ls"}, {})
    assert ok.allow is True
    blocked = await eng.run("pre_tool", "run_shell", {"cmd": "git push --force"}, {})
    assert blocked.allow is False
    assert seen and seen[0][0] == "run_shell(git push*)"


@pytest.mark.asyncio
async def test_if_rule_without_matcher_wired_skips_group():
    section = {
        "PreToolUse": [
            {"if": "run_shell(git *)", "hooks": [{"kind": "command", "command": "exit 2"}]}
        ]
    }
    eng = _engine(section)  # no rule_matcher injected
    decision = await eng.run("pre_tool", "run_shell", {"cmd": "git st"}, {})
    assert decision.allow is True


# ---------------------------------------------------------------------------
# fold semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_deny_short_circuits(tmp_path: Path):
    marker = tmp_path / "second_ran"
    section = {
        "PreToolUse": [
            {"hooks": [{"kind": "command", "command": "exit 2"}]},
            {"hooks": [{"kind": "command", "command": f"touch {marker}"}]},
        ]
    }
    eng = _engine(section)
    decision = await eng.run("pre_tool", "run_shell", {}, {})
    assert decision.allow is False
    assert not marker.exists()


@pytest.mark.asyncio
async def test_mutation_last_write_wins():
    section = {
        "PreToolUse": [
            {"hooks": [{"kind": "command", "command": """echo '{"mutated_args": {"v": 1}}'"""}]},
            {"hooks": [{"kind": "command", "command": """echo '{"mutated_args": {"v": 2}}'"""}]},
        ]
    }
    eng = _engine(section)
    decision = await eng.run("pre_tool", "run_shell", {}, {})
    assert decision.allow is True
    assert decision.mutated_args == {"v": 2}


@pytest.mark.asyncio
async def test_async_designated_hook_never_blocks(tmp_path: Path):
    marker = tmp_path / "async_ran"
    section = {
        "PreToolUse": [
            {
                "hooks": [
                    {"kind": "command", "command": f"touch {marker}; exit 2", "async": True}
                ]
            }
        ]
    }
    eng = _engine(section)
    decision = await eng.run("pre_tool", "run_shell", {}, {})
    assert decision.allow is True  # deny from an async hook is ignored
    await eng.drain()
    assert marker.exists()  # but the hook did run


# ---------------------------------------------------------------------------
# HookRunner integration
# ---------------------------------------------------------------------------


def _runner_config(declarative: dict, **legacy: str) -> dict:
    return {"hooks": {**legacy, "declarative": declarative}}


@pytest.mark.asyncio
async def test_runner_declarative_deny_blocks_pre_tool():
    runner = HookRunner(_runner_config({"PreToolUse": [{"hooks": [{"kind": "command", "command": "exit 2"}]}]}))
    decision = await runner.run_pre_tool_async("run_shell", {"cmd": "ls"})
    assert decision.allow is False


@pytest.mark.asyncio
async def test_runner_legacy_deny_short_circuits_declarative(tmp_path: Path):
    marker = tmp_path / "decl_ran"
    runner = HookRunner(
        _runner_config(
            {"PreToolUse": [{"hooks": [{"kind": "command", "command": f"touch {marker}"}]}]},
            pre_tool="echo legacy-block; exit 1",
        )
    )
    decision = await runner.run_pre_tool_async("run_shell", {})
    assert decision.allow is False
    assert "legacy-block" in (decision.reason or "")
    assert not marker.exists()


@pytest.mark.asyncio
async def test_runner_declarative_mutation_applies():
    decl = {"PreToolUse": [{"hooks": [{"kind": "command", "command": """echo '{"mutated_args": {"cmd": "safe"}}'"""}]}]}
    runner = HookRunner(_runner_config(decl))
    decision = await runner.run_pre_tool_async("run_shell", {"cmd": "rm -rf /"})
    assert decision.allow is True
    assert decision.mutated_args == {"cmd": "safe"}


def test_runner_sync_pre_tool_consults_declarative():
    runner = HookRunner(_runner_config({"PreToolUse": [{"hooks": [{"kind": "command", "command": "exit 2"}]}]}))
    ok, _msg = runner.run_pre_tool("run_shell", {})
    assert ok is False


def test_runner_declarative_key_not_a_shell_hook():
    runner = HookRunner(_runner_config({"PreToolUse": [{"hooks": [{"kind": "command", "command": "true"}]}]}))
    assert "declarative" not in runner.registered


def test_runner_stop_declarative_veto():
    decl = {"Stop": [{"hooks": [{"kind": "command", "command": "echo keep going; exit 2"}]}]}
    runner = HookRunner(_runner_config(decl))
    decision = runner.run_stop({"session_key": "s1"})
    assert decision.allow is False


@pytest.mark.asyncio
async def test_runner_stop_async_supports_prompt_kind():
    async def judge(prompt, payload):
        return {"ok": False, "reason": "not done"}

    decl = {"Stop": [{"hooks": [{"kind": "prompt", "prompt": "done?"}]}]}
    runner = HookRunner(_runner_config(decl), prompt_evaluator=judge)
    decision = await runner.run_stop_async({"session_key": "s1"})
    assert decision.allow is False
    assert decision.reason == "not done"


@pytest.mark.asyncio
async def test_runner_post_tool_async_fires_declarative(tmp_path: Path):
    marker = tmp_path / "post_ran"
    decl = {"PostToolUse": [{"hooks": [{"kind": "command", "command": f"cat > {marker}"}]}]}
    runner = HookRunner(_runner_config(decl))
    await runner.run_post_tool_async("run_shell", {"cmd": "ls"}, '{"ok": true}')
    await runner.drain()
    assert marker.exists()
    data = json.loads(marker.read_text())
    assert data["tool_name"] == "run_shell"
    assert data["tool_result"] == '{"ok": true}'


@pytest.mark.asyncio
async def test_runner_event_async_runs_discovered_and_declarative(tmp_path: Path):
    marker = tmp_path / "sess"
    decl = {"SessionStart": [{"hooks": [{"kind": "command", "command": f"touch {marker}"}]}]}
    runner = HookRunner(_runner_config(decl))
    calls: list[dict] = []
    runner.register_handler("session_start", lambda ev, payload: calls.append(payload))
    decision = await runner.run_event_async("session_start", {"source": "startup"})
    await runner.drain()
    assert decision.allow is True
    assert calls and calls[0]["source"] == "startup"
    assert marker.exists()


@pytest.mark.asyncio
async def test_runner_event_async_sync_hook_verdict_returned():
    decl = {
        "UserPromptSubmit": [
            {"hooks": [{"kind": "command", "command": "echo prompt-note; exit 2", "async": False}]}
        ]
    }
    runner = HookRunner(_runner_config(decl))
    decision = await runner.run_event_async("user_prompt_submit", {"user_text": "hi"})
    assert decision.allow is False
    assert "prompt-note" in (decision.reason or "")


@pytest.mark.asyncio
async def test_if_rule_reevaluated_against_mutated_args():
    """A later group's ``if`` rule must gate on the EFFECTIVE args after an
    earlier hook's mutation (Codex #109) — not the original call args."""

    def rule_matcher(rule, tool, args):
        return "danger" in str(args.get("cmd", ""))

    section = {
        "PreToolUse": [
            # Group 1 rewrites the dangerous command to a safe one.
            {"hooks": [{"kind": "command", "command": """echo '{"mutated_args": {"cmd": "safe"}}'"""}]},
            # Group 2 blocks — but its if-rule matches only "danger" args,
            # which no longer exist after group 1's rewrite.
            {"if": "any-danger", "hooks": [{"kind": "command", "command": "exit 2"}]},
        ]
    }
    eng = _engine(section, rule_matcher=rule_matcher)
    decision = await eng.run("pre_tool", "run_shell", {"cmd": "danger --now"}, {})
    assert decision.allow is True
    assert decision.mutated_args == {"cmd": "safe"}

    # Inverse: mutation INTO a matching value must trigger the later group.
    section2 = {
        "PreToolUse": [
            {"hooks": [{"kind": "command", "command": """echo '{"mutated_args": {"cmd": "danger"}}'"""}]},
            {"if": "any-danger", "hooks": [{"kind": "command", "command": "echo caught >&2; exit 2"}]},
        ]
    }
    eng2 = _engine(section2, rule_matcher=rule_matcher)
    blocked = await eng2.run("pre_tool", "run_shell", {"cmd": "innocent"}, {})
    assert blocked.allow is False


@pytest.mark.asyncio
async def test_runner_post_tool_async_runs_discovered_handlers():
    """HOOK.yaml handlers registered for post_tool fire on the async path
    (Codex #109 — they were advertised but never invoked)."""
    runner = HookRunner({})
    seen: list[dict] = []
    runner.register_handler("post_tool", lambda ev, payload: seen.append(payload))
    await runner.run_post_tool_async("run_shell", {"cmd": "ls"}, '{"ok": true}')
    await runner.drain()
    assert len(seen) == 1
    assert seen[0]["tool"] == "run_shell"
    assert seen[0]["result"] == '{"ok": true}'


@pytest.mark.asyncio
async def test_runner_post_tool_async_returns_before_slow_handler():
    """A slow discovered post handler must not delay the tool result —
    handlers run off the dispatch path (Codex #109 round 2)."""
    import time as _time

    runner = HookRunner({})
    runner.register_handler("post_tool", lambda ev, payload: _time.sleep(0.5))
    started = _time.perf_counter()
    await runner.run_post_tool_async("run_shell", {}, "{}")
    elapsed = _time.perf_counter() - started
    assert elapsed < 0.3, f"dispatch path blocked for {elapsed:.2f}s by a post handler"
    await runner.drain()


def test_runner_reload_preserves_programmatic_handlers():
    """reload() rebuilds discovered handlers but must keep handlers added
    via the public register_handler API (Codex #109 round 2)."""
    runner = HookRunner({})
    denials: list[str] = []

    def guard(event: str, payload: dict) -> dict:
        denials.append(event)
        return {"allow": False, "reason": "manual guard"}

    runner.register_handler("pre_tool", guard)
    ok, msg = runner.run_pre_tool("run_shell", {})
    assert ok is False and msg == "manual guard"
    runner.reload({"hooks": {}})
    ok2, msg2 = runner.run_pre_tool("run_shell", {})
    assert ok2 is False and msg2 == "manual guard"
    assert len(denials) == 2


def test_runner_reload_swaps_declarative_config():
    runner = HookRunner(_runner_config({"PreToolUse": [{"hooks": [{"kind": "command", "command": "exit 2"}]}]}))
    ok, _ = runner.run_pre_tool("run_shell", {})
    assert ok is False
    summary = runner.reload({"hooks": {"declarative": {}}})
    ok2, _ = runner.run_pre_tool("run_shell", {})
    assert ok2 is True
    assert summary["declarative_groups"] == 0


def test_runner_reload_swaps_shell_hooks():
    runner = HookRunner({"hooks": {"pre_tool": "exit 1"}})
    ok, _ = runner.run_pre_tool("run_shell", {})
    assert ok is False
    summary = runner.reload({"hooks": {}})
    ok2, _ = runner.run_pre_tool("run_shell", {})
    assert ok2 is True
    assert summary["shell_hooks"] == 0


def test_runner_declarative_introspection():
    decl = {
        "PreToolUse": [
            {"matcher": "run_shell", "if": "run_shell(git *)", "hooks": [{"kind": "command", "command": "true"}, {"kind": "http", "url": "http://x/h"}]}
        ]
    }
    runner = HookRunner(_runner_config(decl))
    groups = runner.declarative_groups
    assert groups[0]["event"] == "pre_tool"
    assert groups[0]["matcher"] == "run_shell"
    assert groups[0]["if"] == "run_shell(git *)"
    assert groups[0]["kinds"] == ["command", "http"]
    assert runner.declarative_warnings == []


def test_supported_events_includes_user_prompt_submit():
    runner = HookRunner({})
    assert "user_prompt_submit" in runner.supported_events()


def test_legacy_flat_contract_unchanged():
    # The pre-existing shell contract must not shift: 0=allow, 1=deny.
    runner = HookRunner({"hooks": {"pre_tool": "echo no; exit 1"}})
    ok, msg = runner.run_pre_tool("run_shell", {})
    assert ok is False
    assert msg == "no"
