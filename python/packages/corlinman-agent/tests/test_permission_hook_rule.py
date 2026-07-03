"""``match_hook_rule`` — the permission-rule grammar as a hook `if` matcher.

The declarative-hooks layer injects this callable into ``HookRunner`` so
hook groups can be gated with the same rule spellings the permission gate
uses (``run_shell(git:*)``, ``write_file(*.ts)``, ``*``). Grammar is
designed once and shared (parity-matrix contract).
"""

from __future__ import annotations

from corlinman_agent.permission import match_hook_rule


def test_bare_tool_rule_matches_any_args():
    assert match_hook_rule("run_shell", "run_shell", {"command": "ls"}) is True
    assert match_hook_rule("run_shell", "read_file", {"path": "x"}) is False


def test_wildcard_rule_matches_any_tool():
    assert match_hook_rule("*", "read_file", {"path": "x"}) is True


def test_arg_pattern_rule_gates_on_command_basename():
    # The permission grammar matches "<basename>:<full command>" candidates.
    assert match_hook_rule("run_shell(git:*)", "run_shell", {"command": "git push --force"}) is True
    assert match_hook_rule("run_shell(git:*)", "run_shell", {"command": "ls -la"}) is False


def test_claude_style_command_prefix_pattern_matches_raw_command():
    # Codex #109 round 2: the documented claude-code spelling
    # ``run_shell(git push*)`` must fire on the raw command string too —
    # hook if-rules accept BOTH the ``basename:*`` form and the natural
    # command-prefix form.
    assert match_hook_rule("run_shell(git push*)", "run_shell", {"command": "git push --force"}) is True
    assert match_hook_rule("run_shell(git push*)", "run_shell", {"command": "git pull"}) is False
    assert match_hook_rule("run_shell(rm *)", "run_shell", {"command": "rm -rf /tmp/x"}) is True


def test_arg_pattern_catches_compound_commands():
    rule = "run_shell(rm:*)"
    assert match_hook_rule(rule, "run_shell", {"command": "cd /tmp && rm -rf x"}) is True


def test_file_tool_pattern_matches_path():
    assert match_hook_rule("write_file(*.ts)", "write_file", {"path": "src/app.ts"}) is True
    assert match_hook_rule("write_file(*.ts)", "write_file", {"path": "src/app.py"}) is False


def test_arg_pattern_with_no_args_does_not_match():
    assert match_hook_rule("run_shell(git:*)", "run_shell", {}) is False
    assert match_hook_rule("run_shell(git:*)", "run_shell", None) is False


def test_empty_or_garbage_rule_is_false():
    assert match_hook_rule("", "run_shell", {"command": "ls"}) is False
    assert match_hook_rule("   ", "run_shell", {}) is False
