"""Tests for :class:`corlinman_server.system.subagent.AsyncSubagentDispatcher`.

Eight focused cases covering the W1.3 contract:

1. ``dispatch_async`` registers + returns a running row.
2. Background task completes → store flips to succeeded, summary stored.
3. Background task raises → store flips to failed + error populated.
4. Background task exceeds timeout → store flips to ``timeout``.
5. ``kill`` while running cancels the asyncio.Task + flips to killed.
6. ``kill`` after terminal returns None (caller maps to 409).
7. ``max_concurrent_per_tenant`` enforcement raises TenantQuotaExceeded.
8. Synthetic notification injected on terminal — journal append observed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from corlinman_server.system.audit import SystemAuditLog
from corlinman_server.system.subagent import (
    AsyncSubagentDispatcher,
    SubagentRequest,
    SubagentTaskStore,
    TenantQuotaExceeded,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeTaskResult:
    """Duck-typed stand-in for :class:`corlinman_agent.subagent.api.TaskResult`."""

    output_text: str
    finish_reason: str  # raw string; dispatcher reads ``.value`` via getattr
    elapsed_ms: int = 100
    child_session_key: str = "sess-A::child::0"
    error: str | None = None
    tool_calls_made: list[Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tool_calls_made is None:
            self.tool_calls_made = []


class _FakeJournal:
    """Captures synthetic-notification calls for assertion."""

    def __init__(self) -> None:
        self.notifications: list[dict[str, Any]] = []
        self.appended_messages: list[tuple[int, str, str]] = []
        self._next_turn = 100

    async def start_turn_for_subagent_notification(
        self,
        *,
        session_key: str,
        kind: str,
        user_text: str,
        metadata: dict[str, Any],
    ) -> int:
        self.notifications.append(
            {
                "session_key": session_key,
                "kind": kind,
                "user_text": user_text,
                "metadata": metadata,
            }
        )
        self._next_turn += 1
        return self._next_turn

    async def append_message(
        self, turn_id: int, role: str, content: str
    ) -> None:
        self.appended_messages.append((turn_id, role, content))


def _make_req(
    request_id: str = "req-1",
    parent_session_key: str = "sess-A",
    subagent_type: str = "researcher",
) -> SubagentRequest:
    return SubagentRequest(
        request_id=request_id,
        parent_session_key=parent_session_key,
        parent_agent_id="agent-parent",
        subagent_type=subagent_type,
        goal="research stuff",
        description=None,
        requested_at=int(time.time() * 1000),
        requested_by=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_async_registers_running_status(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")

    completed_event = asyncio.Event()

    async def factory(req: SubagentRequest) -> _FakeTaskResult:
        # Wait until the test releases the gate so we can observe the
        # running state before completion.
        await completed_event.wait()
        return _FakeTaskResult(output_text="done", finish_reason="stop")

    dispatcher = AsyncSubagentDispatcher(
        store=store, run_child_factory=factory
    )
    req = _make_req()
    status = await dispatcher.dispatch_async(req)
    assert status.state == "running"
    assert status.started_at is not None

    # Store row should also reflect ``running``.
    persisted = await store.get(req.request_id)
    assert persisted is not None
    assert persisted.state == "running"

    # Let the factory finish so the asyncio.Task settles.
    completed_event.set()
    await asyncio.sleep(0)  # let scheduler run task
    # Wait for the task to drain.
    for _ in range(50):
        check = await store.get(req.request_id)
        if check is not None and check.is_terminal():
            break
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_terminal_success_updates_store(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")

    async def factory(req: SubagentRequest) -> _FakeTaskResult:
        return _FakeTaskResult(
            output_text="result text",
            finish_reason="stop",
            elapsed_ms=250,
        )

    dispatcher = AsyncSubagentDispatcher(
        store=store, run_child_factory=factory
    )
    req = _make_req()
    await dispatcher.dispatch_async(req)

    # Wait for terminal state.
    final = None
    for _ in range(100):
        final = await store.get(req.request_id)
        if final is not None and final.is_terminal():
            break
        await asyncio.sleep(0.01)

    assert final is not None
    assert final.state == "succeeded"
    assert final.elapsed_ms == 250
    assert "result text" in final.summary
    assert final.finish_reason == "stop"
    assert final.child_session_key == "sess-A::child::0"


@pytest.mark.asyncio
async def test_factory_exception_marks_failed(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")

    async def factory(req: SubagentRequest) -> _FakeTaskResult:
        raise RuntimeError("provider blew up")

    dispatcher = AsyncSubagentDispatcher(
        store=store, run_child_factory=factory
    )
    req = _make_req()
    await dispatcher.dispatch_async(req)

    final = None
    for _ in range(100):
        final = await store.get(req.request_id)
        if final is not None and final.is_terminal():
            break
        await asyncio.sleep(0.01)

    assert final is not None
    assert final.state == "failed"
    assert "provider blew up" in (final.error or "")


@pytest.mark.asyncio
async def test_timeout_finish_reason_maps_to_timeout_state(
    tmp_path: Path,
) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")

    async def factory(req: SubagentRequest) -> _FakeTaskResult:
        return _FakeTaskResult(
            output_text="",
            finish_reason="timeout",
            elapsed_ms=60_000,
        )

    dispatcher = AsyncSubagentDispatcher(
        store=store, run_child_factory=factory
    )
    req = _make_req()
    await dispatcher.dispatch_async(req)

    final = None
    for _ in range(100):
        final = await store.get(req.request_id)
        if final is not None and final.is_terminal():
            break
        await asyncio.sleep(0.01)

    assert final is not None
    assert final.state == "timeout"
    assert final.finish_reason == "timeout"


@pytest.mark.asyncio
async def test_kill_while_running_cancels_and_flips(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    gate = asyncio.Event()

    async def factory(req: SubagentRequest) -> _FakeTaskResult:
        # Block until cancelled — never resolves naturally.
        await gate.wait()
        return _FakeTaskResult(output_text="ok", finish_reason="stop")

    dispatcher = AsyncSubagentDispatcher(
        store=store, run_child_factory=factory
    )
    req = _make_req()
    await dispatcher.dispatch_async(req)

    # Let the task start.
    await asyncio.sleep(0)
    killed = await dispatcher.kill(req.request_id, by="alice")
    assert killed is not None
    assert killed.state == "killed"
    assert killed.finish_reason == "killed_by:alice"

    # Make sure the asyncio task has actually drained — cancellation
    # bubbles through; the dispatcher's _run cleans up the registry.
    for _ in range(50):
        if req.request_id not in dispatcher._tasks:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_kill_after_terminal_returns_none(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")

    async def factory(req: SubagentRequest) -> _FakeTaskResult:
        return _FakeTaskResult(output_text="done", finish_reason="stop")

    dispatcher = AsyncSubagentDispatcher(
        store=store, run_child_factory=factory
    )
    req = _make_req()
    await dispatcher.dispatch_async(req)

    # Wait for terminal.
    for _ in range(100):
        cur = await store.get(req.request_id)
        if cur is not None and cur.is_terminal():
            break
        await asyncio.sleep(0.01)

    # Now try to kill — already terminal.
    result = await dispatcher.kill(req.request_id, by="alice")
    assert result is None


@pytest.mark.asyncio
async def test_max_concurrent_per_tenant_enforced(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    gates: list[asyncio.Event] = []

    async def factory(req: SubagentRequest) -> _FakeTaskResult:
        gate = asyncio.Event()
        gates.append(gate)
        await gate.wait()
        return _FakeTaskResult(output_text="ok", finish_reason="stop")

    dispatcher = AsyncSubagentDispatcher(
        store=store,
        run_child_factory=factory,
        max_concurrent_per_tenant=2,
    )

    await dispatcher.dispatch_async(_make_req(request_id="A"))
    await dispatcher.dispatch_async(_make_req(request_id="B"))
    # Third dispatch over the ceiling
    with pytest.raises(TenantQuotaExceeded) as info:
        await dispatcher.dispatch_async(_make_req(request_id="C"))
    assert info.value.active == 2
    assert info.value.ceiling == 2

    # Release one so the test teardown is clean
    for g in gates:
        g.set()
    for _ in range(100):
        in_flight = False
        for rid in ("A", "B"):
            row = await store.get(rid)
            if row is not None and row.is_in_flight():
                in_flight = True
                break
        if not in_flight:
            break
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_terminal_writes_synthetic_notification(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    journal = _FakeJournal()

    async def factory(req: SubagentRequest) -> _FakeTaskResult:
        return _FakeTaskResult(
            output_text="here is the summary",
            finish_reason="stop",
        )

    dispatcher = AsyncSubagentDispatcher(
        store=store,
        run_child_factory=factory,
        journal=journal,
    )
    req = _make_req(parent_session_key="parent-X")
    await dispatcher.dispatch_async(req)

    for _ in range(100):
        cur = await store.get(req.request_id)
        if cur is not None and cur.is_terminal():
            break
        await asyncio.sleep(0.01)
    # Let the notification coroutine settle.
    await asyncio.sleep(0.05)

    assert len(journal.notifications) >= 1
    note = journal.notifications[0]
    assert note["session_key"] == "parent-X"
    assert note["kind"] == "subagent_notification"
    assert "[subagent.completed:" in note["user_text"]
    assert "here is the summary" in note["user_text"]
    assert note["metadata"]["request_id"] == req.request_id
    assert note["metadata"]["kind"] == "subagent_notification"
    # The actual append_message call should also have fired.
    assert journal.appended_messages, "expected append_message to have been called"
    _, role, content = journal.appended_messages[0]
    assert role == "user"
    assert "[subagent.completed:" in content


# ---------------------------------------------------------------------------
# W3.1 — audit log wiring
# ---------------------------------------------------------------------------


async def _drain_audit(audit_log: SystemAuditLog, expected_events: int) -> list:
    """Poll the audit log until we see ``expected_events`` entries.

    The dispatcher writes the audit entry from a background asyncio task
    after the store transition lands, so a synchronous read immediately
    after ``dispatch_async`` returns can race the writer. We tail in a
    short loop with a hard ceiling so a regression surfaces as a test
    failure rather than a hang.
    """
    for _ in range(200):
        entries = await audit_log.tail(limit=50)
        if len(entries) >= expected_events:
            return entries
        await asyncio.sleep(0.01)
    return await audit_log.tail(limit=50)


@pytest.mark.asyncio
async def test_audit_log_emits_dispatched_and_completed(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    audit_log = SystemAuditLog(tmp_path / "system-audit.log")

    async def factory(req: SubagentRequest) -> _FakeTaskResult:
        return _FakeTaskResult(
            output_text="research summary",
            finish_reason="stop",
            elapsed_ms=42,
        )

    dispatcher = AsyncSubagentDispatcher(
        store=store,
        run_child_factory=factory,
        audit_log=audit_log,
    )
    req = _make_req(request_id="aud-1")
    await dispatcher.dispatch_async(req)

    entries = await _drain_audit(audit_log, expected_events=2)
    events = sorted(e.event for e in entries)
    assert "subagent.dispatched" in events
    assert "subagent.completed" in events

    completed = next(e for e in entries if e.event == "subagent.completed")
    assert completed.request_id == "aud-1"
    assert completed.tag == "researcher"
    # `requested_by` is None → actor falls back to "model".
    assert completed.actor == "model"
    assert completed.details.get("subagent_type") == "researcher"
    assert completed.details.get("parent_session_key") == "sess-A"
    assert completed.details.get("finish_reason") == "stop"


@pytest.mark.asyncio
async def test_audit_log_emits_failed_on_factory_exception(
    tmp_path: Path,
) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    audit_log = SystemAuditLog(tmp_path / "system-audit.log")

    async def factory(req: SubagentRequest) -> _FakeTaskResult:
        raise RuntimeError("provider blew up")

    dispatcher = AsyncSubagentDispatcher(
        store=store,
        run_child_factory=factory,
        audit_log=audit_log,
    )
    req = _make_req(request_id="aud-fail")
    await dispatcher.dispatch_async(req)

    entries = await _drain_audit(audit_log, expected_events=2)
    events = [e.event for e in entries]
    assert "subagent.dispatched" in events
    assert "subagent.failed" in events
    failed = next(e for e in entries if e.event == "subagent.failed")
    assert failed.request_id == "aud-fail"
    assert "provider blew up" in failed.details.get("error", "")


@pytest.mark.asyncio
async def test_audit_log_emits_killed_on_operator_kill(tmp_path: Path) -> None:
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    audit_log = SystemAuditLog(tmp_path / "system-audit.log")
    gate = asyncio.Event()

    async def factory(req: SubagentRequest) -> _FakeTaskResult:
        await gate.wait()
        return _FakeTaskResult(output_text="ok", finish_reason="stop")

    dispatcher = AsyncSubagentDispatcher(
        store=store,
        run_child_factory=factory,
        audit_log=audit_log,
    )
    req = _make_req(request_id="aud-kill")
    await dispatcher.dispatch_async(req)

    await asyncio.sleep(0)
    killed = await dispatcher.kill(req.request_id, by="alice")
    assert killed is not None

    entries = await _drain_audit(audit_log, expected_events=2)
    events = [e.event for e in entries]
    assert "subagent.dispatched" in events
    assert "subagent.killed" in events
    kill_entry = next(e for e in entries if e.event == "subagent.killed")
    assert kill_entry.request_id == "aud-kill"
    assert kill_entry.details.get("killed_by") == "alice"


@pytest.mark.asyncio
async def test_audit_log_unwired_dispatcher_is_a_no_op(tmp_path: Path) -> None:
    """``audit_log=None`` (the default) must not raise + must not write."""
    store = SubagentTaskStore(tmp_path / ".subagent-state.json")

    async def factory(req: SubagentRequest) -> _FakeTaskResult:
        return _FakeTaskResult(output_text="ok", finish_reason="stop")

    dispatcher = AsyncSubagentDispatcher(
        store=store, run_child_factory=factory
    )
    req = _make_req(request_id="aud-noop")
    await dispatcher.dispatch_async(req)

    for _ in range(100):
        cur = await store.get(req.request_id)
        if cur is not None and cur.is_terminal():
            break
        await asyncio.sleep(0.01)
    # Reaching here means no audit-write attempt raised — the contract.
    assert (tmp_path / "system-audit.log").exists() is False
