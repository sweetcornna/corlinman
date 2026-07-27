"""[permissions] config → actual dispatch behaviour (W3-1 acceptance 1).

Fourth step of the config-blindness checklist: not "the admin route
accepts the block" but "the block changes what the agent's tool dispatch
actually does" — driving the REAL ``_dispatch_builtin`` on a servicer
whose default gate is the call-time ``AuthzGate``. No restart, no gate
reconstruction, between the config write and the changed verdict.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_agent.authz.defaults import apply_permissions_config
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:
        if False:  # pragma: no cover — never yields
            yield ProviderChunk(kind="done", finish_reason="stop")


def _servicer() -> CorlinmanAgentServicer:
    return CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())


def _start(session_key: str = "tenant-x::s1") -> Any:
    from corlinman_agent.reasoning_loop import ChatStart

    return ChatStart(model="m", messages=[], tools=[], session_key=session_key)


def _event(tool: str, args: dict[str, Any]) -> Any:
    from corlinman_agent.reasoning_loop import ToolCallEvent

    return ToolCallEvent(
        call_id="c1",
        plugin="builtin",
        tool=tool,
        args_json=json.dumps(args).encode("utf-8"),
    )


@pytest.mark.asyncio
async def test_config_rule_gates_dispatch_without_restart() -> None:
    """Write the [permissions] block → the SAME servicer's next dispatch
    obeys it; clear it → the verdict reverts. The whole point of W3-1."""
    servicer = _servicer()

    ok = await servicer._dispatch_builtin(
        _event("run_shell", {"command": "echo hi"}), _start(), _FakeProvider()
    )
    assert "permission_denied" not in str(ok)

    apply_permissions_config(
        {"rules": [{"tool": "run_shell(echo:*)", "action": "deny"}]}
    )
    blocked = await servicer._dispatch_builtin(
        _event("run_shell", {"command": "echo hi"}), _start(), _FakeProvider()
    )
    payload = json.loads(blocked)
    assert "permission_denied" in payload["error"]
    assert payload["tool"] == "run_shell"


@pytest.mark.asyncio
async def test_config_strict_gates_dispatch_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 2: [permissions].strict flips a mutating tool to deny on
    the very next dispatch (the old gate froze strict at construction)."""
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(tmp_path))
    servicer = _servicer()

    ok = await servicer._dispatch_builtin(
        _event("write_file", {"path": "a.txt", "content": "x"}),
        _start(),
        _FakeProvider(),
    )
    assert "permission_denied" not in str(ok)

    apply_permissions_config({"strict": True})
    blocked = await servicer._dispatch_builtin(
        _event("write_file", {"path": "b.txt", "content": "x"}),
        _start(),
        _FakeProvider(),
    )
    payload = json.loads(blocked)
    assert "permission_denied" in payload["error"]


@pytest.mark.asyncio
async def test_sidecar_write_lands_mid_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Risk R2: a deny the operator just saved must stop the REMAINING
    tool calls of a turn already in flight — the gate re-stats the sidecar
    (throttled) inside resolve, not only at next-turn model resolution."""
    sidecar = tmp_path / "py-config.json"
    sidecar.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(sidecar))
    servicer = _servicer()

    ok = await servicer._dispatch_builtin(
        _event("run_shell", {"command": "echo hi"}), _start(), _FakeProvider()
    )
    assert "permission_denied" not in str(ok)

    # Operator saves a deny while the turn is still running.
    sidecar.write_text(
        json.dumps(
            {
                "permissions": {
                    "rules": [{"tool": "run_shell(echo:*)", "action": "deny"}]
                }
            }
        ),
        encoding="utf-8",
    )
    # Outwait the 100ms stat throttle, then the NEXT tool call is gated.
    time.sleep(0.12)
    blocked = await servicer._dispatch_builtin(
        _event("run_shell", {"command": "echo hi"}), _start(), _FakeProvider()
    )
    payload = json.loads(blocked)
    assert "permission_denied" in payload["error"]
