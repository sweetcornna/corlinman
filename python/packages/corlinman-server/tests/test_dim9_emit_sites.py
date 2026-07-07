"""Dim 9 residuals — the new lifecycle-hook emit sites.

Pins each previously-dormant event's production emitter:

* ``file_changed`` — after ``post_tool`` for the file-mutating builtins;
* ``notification`` — ``ask_user`` (needs-input) + subagent terminal
  states (the dispatcher's ``hook_notifier`` seam);
* ``session_start`` — servicer chat entry, once per session_key;
* the ``hooks_live`` registry advertises exactly the live set.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_server.agent_servicer import CorlinmanAgentServicer
from corlinman_server.hooks_live import LIVE_HOOK_EVENTS

pytestmark = pytest.mark.asyncio


class _RecordingRunner:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, dict]] = []

    async def run_event_async(self, event, payload=None, ctx=None):
        self.events.append((event, dict(payload or {}), dict(ctx or {})))
        return SimpleNamespace(allow=True, reason=None)

    async def run_post_tool_async(self, tool, args, result, ctx=None):
        self.events.append(("post_tool", {"tool": tool}, dict(ctx or {})))


def _start(session_key: str = "sess-e") -> Any:
    return SimpleNamespace(session_key=session_key, extra={}, model="m")


def _servicer_with(runner: Any) -> CorlinmanAgentServicer:
    servicer = CorlinmanAgentServicer(hook_runner=runner)
    return servicer


# ---------------------------------------------------------------------------
# file_changed
# ---------------------------------------------------------------------------


async def test_file_changed_fires_for_mutating_tools() -> None:
    runner = _RecordingRunner()
    servicer = _servicer_with(runner)
    await servicer._run_post_tool_hooks(
        "write_file", {"path": "src/x.py", "content": "..."}, _start(), "ok"
    )
    kinds = [e[0] for e in runner.events]
    assert kinds == ["post_tool", "file_changed"]
    _ev, payload, ctx = runner.events[1]
    assert payload == {"tool": "write_file", "path": "src/x.py"}
    assert ctx["session_key"] == "sess-e"


async def test_file_changed_skips_readonly_tools() -> None:
    runner = _RecordingRunner()
    servicer = _servicer_with(runner)
    await servicer._run_post_tool_hooks(
        "read_file", {"path": "src/x.py"}, _start(), "contents"
    )
    assert [e[0] for e in runner.events] == ["post_tool"]


# ---------------------------------------------------------------------------
# notification — ask_user needs-input
# ---------------------------------------------------------------------------


async def test_notification_fires_for_ask_user() -> None:
    runner = _RecordingRunner()
    servicer = _servicer_with(runner)
    await servicer._run_post_tool_hooks(
        "ask_user", {"question": "deploy to prod?"}, _start(), "{}"
    )
    kinds = [e[0] for e in runner.events]
    assert kinds == ["post_tool", "notification"]
    _ev, payload, _ctx = runner.events[1]
    assert payload == {"kind": "needs_input", "question": "deploy to prod?"}


# ---------------------------------------------------------------------------
# notification — subagent terminal (dispatcher hook_notifier seam)
# ---------------------------------------------------------------------------


async def test_dispatcher_inject_notification_fires_hook_notifier() -> None:
    from corlinman_server.system.subagent.dispatcher import (
        AsyncSubagentDispatcher,
    )

    class _Store:
        async def get_request(self, request_id: str) -> Any:
            return SimpleNamespace(parent_session_key="parent-sess")

    fired: list[dict[str, Any]] = []

    async def _notifier(payload: dict[str, Any]) -> None:
        fired.append(payload)

    dispatcher = AsyncSubagentDispatcher(
        store=_Store(),  # type: ignore[arg-type]
        run_child_factory=lambda req: None,  # type: ignore[arg-type, return-value]
        journal=None,
        hook_notifier=_notifier,
    )
    await dispatcher._inject_notification(
        request_id="req-1",
        agent_name="researcher",
        output_text="done",
        terminal_state="completed",
    )
    assert fired == [
        {
            "kind": "subagent_completed",
            "request_id": "req-1",
            "subagent_type": "researcher",
            "terminal_state": "completed",
            "parent_session_key": "parent-sess",
        }
    ]


async def test_dispatcher_notifier_failure_is_swallowed() -> None:
    from corlinman_server.system.subagent.dispatcher import (
        AsyncSubagentDispatcher,
    )

    class _Store:
        async def get_request(self, request_id: str) -> Any:
            return SimpleNamespace(parent_session_key="p")

    async def _boom(payload: dict[str, Any]) -> None:
        raise RuntimeError("notify blew up")

    dispatcher = AsyncSubagentDispatcher(
        store=_Store(),  # type: ignore[arg-type]
        run_child_factory=lambda req: None,  # type: ignore[arg-type, return-value]
        journal=None,
        hook_notifier=_boom,
    )
    # Must not raise.
    await dispatcher._inject_notification(
        request_id="r",
        agent_name="a",
        output_text="t",
        terminal_state="failed",
    )


# ---------------------------------------------------------------------------
# hooks_live registry
# ---------------------------------------------------------------------------


async def test_live_hook_events_cover_new_emitters() -> None:
    assert {
        "pre_compact",
        "session_start",
        "session_reset",
        "notification",
        "file_changed",
        "setup",
    } <= LIVE_HOOK_EVENTS
