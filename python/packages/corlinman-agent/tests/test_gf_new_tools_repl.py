"""gap-fill lane-new-tools — opt-in ``execute_code`` REPL.

Covers gap ``code-execution-repl``: a persistent-session Python REPL
that is DISABLED by default and only spawns an interpreter when the
operator opts in via ``CORLINMAN_ENABLE_EXECUTE_CODE``.
"""

from __future__ import annotations

import json

from corlinman_agent.coding import (
    EXECUTE_CODE_TOOL,
    dispatch_execute_code,
    execute_code_tool_schema,
)
from corlinman_agent.coding.repl import _ENABLE_ENV, _SESSIONS

_ENABLE = _ENABLE_ENV


def test_tool_name_wire_stable() -> None:
    assert EXECUTE_CODE_TOOL == "execute_code"


def test_schema_shape() -> None:
    schema = execute_code_tool_schema()
    assert schema["function"]["name"] == "execute_code"
    props = schema["function"]["parameters"]["properties"]
    assert "code" in props
    assert schema["function"]["parameters"]["required"] == ["code"]


async def test_disabled_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(_ENABLE, raising=False)
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(tmp_path))
    out = await dispatch_execute_code(
        args_json=json.dumps({"code": "print(1)"}).encode()
    )
    env = json.loads(out)
    assert env["error"] == "execute_code_disabled"
    # No interpreter session was created while disabled.
    assert "__anon__" not in _SESSIONS


async def test_enabled_runs_and_persists_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(_ENABLE, "1")
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(tmp_path))
    session_key = "gf-repl-test"
    try:
        out1 = await dispatch_execute_code(
            args_json=json.dumps({"code": "x = 41", "timeout": 10}).encode(),
            session_key=session_key,
        )
        assert "error" not in json.loads(out1)

        # State persists across calls in the same session.
        out2 = await dispatch_execute_code(
            args_json=json.dumps({"code": "print(x + 1)", "timeout": 10}).encode(),
            session_key=session_key,
        )
        env2 = json.loads(out2)
        assert env2.get("error") is None
        assert "42" in env2["output"]
    finally:
        sess = _SESSIONS.pop(session_key, None)
        if sess is not None:
            await sess.close()


async def test_enabled_captures_traceback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(_ENABLE, "true")
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(tmp_path))
    session_key = "gf-repl-err"
    try:
        out = await dispatch_execute_code(
            args_json=json.dumps(
                {"code": "raise ValueError('boom')", "timeout": 10}
            ).encode(),
            session_key=session_key,
        )
        env = json.loads(out)
        # A raising snippet must not kill the dispatcher; the traceback
        # is surfaced as captured output.
        assert env.get("error") is None
        assert "ValueError" in env["output"]
        assert "boom" in env["output"]
    finally:
        sess = _SESSIONS.pop(session_key, None)
        if sess is not None:
            await sess.close()


async def test_enabled_missing_code(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(_ENABLE, "on")
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(tmp_path))
    out = await dispatch_execute_code(args_json=b"{}")
    env = json.loads(out)
    assert "args_invalid" in env["error"]
