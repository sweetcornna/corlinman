"""Repro for SEC-05 — per-arg run_shell deny bypassable via shell-shape.

A deny rule ``run_shell(rm:*)`` must deny ANY ``run_shell`` invocation that
ultimately runs ``rm`` — including compound (``cd … && rm``), sh-dash-c
wrappers, env-prefixed forms, and path basenames — because ``run_shell``
executes the whole string through ``create_subprocess_shell``.

Before the fix, ``extract_primary_arg`` only keyed on the FIRST shlex token,
so every form below except the plain ``rm`` slipped through to ``allow``.
"""

from __future__ import annotations

from corlinman_agent.permission import (
    ALLOW,
    DENY,
    PermissionContext,
    PermissionGate,
    PermissionRule,
)

_CTX = PermissionContext()


def _gate() -> PermissionGate:
    return PermissionGate(
        [
            PermissionRule(tool="run_shell(rm:*)", action=DENY),
            PermissionRule(tool="*", action=ALLOW),
        ]
    )


def _decide(gate: PermissionGate, command: str) -> str:
    return gate.resolve_with_args("run_shell", _CTX, {"command": command})[0]


def test_plain_rm_is_denied() -> None:
    # Baseline — this already worked before the fix.
    assert _decide(_gate(), "rm -rf /tmp/x") == DENY


def test_compound_cd_then_rm_is_denied() -> None:
    # cd then rm in one command — was ALLOWED before the fix.
    assert _decide(_gate(), "cd /tmp && rm -rf x") == DENY


def test_pipe_chained_rm_is_denied() -> None:
    assert _decide(_gate(), "echo hi | rm -rf x") == DENY


def test_semicolon_chained_rm_is_denied() -> None:
    assert _decide(_gate(), "true ; rm -rf x") == DENY


def test_sh_dash_c_wrapper_is_denied() -> None:
    # sh -c "rm -rf x" — was ALLOWED before the fix (head was 'sh').
    assert _decide(_gate(), 'sh -c "rm -rf x"') == DENY
    assert _decide(_gate(), "bash -c 'rm -rf x'") == DENY


def test_env_prefixed_rm_is_denied() -> None:
    # env FOO=bar rm … — was ALLOWED before the fix (head was 'env').
    assert _decide(_gate(), "env FOO=bar rm -rf x") == DENY


def test_path_basename_rm_is_denied() -> None:
    # /bin/rm — was ALLOWED before the fix (head was '/bin/rm').
    assert _decide(_gate(), "/bin/rm -rf x") == DENY


def test_non_rm_command_still_allowed() -> None:
    # The deny rule must not over-match an innocent command.
    assert _decide(_gate(), "ls -la") == ALLOW
    assert _decide(_gate(), "cd /tmp && ls") == ALLOW
    assert _decide(_gate(), "echo rm") == ALLOW  # 'rm' only as an argument
