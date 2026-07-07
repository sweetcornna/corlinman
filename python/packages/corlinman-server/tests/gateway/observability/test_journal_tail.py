"""C1 (#108 item 2) — process-wide journal tail feeding the live registry.

``run_journal_subagent_tail`` is the gateway half: a background loop that
polls ``load_subagent_events_since`` and applies each row via
``observe_journal_event``, so /admin/subagents sees grpc_agent-mode
children even when NO session SSE stream is open (previously the only
cross-process feed point).
"""

from __future__ import annotations

import asyncio
from typing import Any

from corlinman_server.gateway.observability.live_subagents import (
    LiveSubagentRegistry,
    run_journal_subagent_tail,
)


def _spawned(turn: str, seq: int, child: str) -> dict[str, Any]:
    return {
        "turn_id": turn,
        "sequence": seq,
        "event_type": "SubagentSpawned",
        "payload": {
            "child_session_key": child,
            "parent_session_key": "corlinman:parent",
            "child_agent_id": "researcher",
            "depth": 1,
            "prompt_preview": "dig",
        },
        "timestamp_ms": 1_700_000_000_000 + seq,
    }


def _completed(turn: str, seq: int, child: str) -> dict[str, Any]:
    return {
        "turn_id": turn,
        "sequence": seq,
        "event_type": "SubagentCompleted",
        "payload": {
            "child_session_key": child,
            "finish_reason": "done",
            "tool_calls_made": 2,
            "elapsed_ms": 10,
            "summary": "ok",
        },
        "timestamp_ms": 1_700_000_000_100 + seq,
    }


class _StubJournal:
    """Feeds one batch of events past the boot cursor, then goes quiet."""

    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self._batches = list(batches)
        self.boot_rowid = 5
        self.polls = 0

    async def latest_event_rowid(self) -> int:
        return self.boot_rowid

    async def load_subagent_events_since(
        self, after_rowid: int, *, limit: int = 500
    ) -> tuple[int, list[dict[str, Any]]]:
        self.polls += 1
        if self._batches:
            batch = self._batches.pop(0)
            return after_rowid + len(batch), batch
        return after_rowid, []


async def test_tail_feeds_registry_and_stops_on_cancel() -> None:
    registry = LiveSubagentRegistry()
    journal = _StubJournal(
        [
            [_spawned("t1", 2, "corlinman:child-1")],
            [_completed("t1", 5, "corlinman:child-1")],
        ]
    )
    cancel = asyncio.Event()
    task = asyncio.create_task(
        run_journal_subagent_tail(
            journal, registry, poll_seconds=0.01, cancel=cancel
        )
    )
    try:
        for _ in range(200):
            rows = registry.list_all()
            if rows and rows[0].state != "running":
                break
            await asyncio.sleep(0.01)
        rows = registry.list_all()
        assert len(rows) == 1
        assert rows[0].child_session_key == "corlinman:child-1"
        assert rows[0].state != "running"
    finally:
        cancel.set()
        await asyncio.wait_for(task, timeout=2)


async def test_tail_survives_journal_errors() -> None:
    class _FlakyJournal(_StubJournal):
        async def load_subagent_events_since(
            self, after_rowid: int, *, limit: int = 500
        ) -> tuple[int, list[dict[str, Any]]]:
            self.polls += 1
            if self.polls == 1:
                raise RuntimeError("db hiccup")
            return await super().load_subagent_events_since(
                after_rowid, limit=limit
            )

    registry = LiveSubagentRegistry()
    journal = _FlakyJournal([[_spawned("t2", 1, "corlinman:child-2")]])
    cancel = asyncio.Event()
    task = asyncio.create_task(
        run_journal_subagent_tail(
            journal, registry, poll_seconds=0.01, cancel=cancel
        )
    )
    try:
        for _ in range(200):
            if registry.list_all():
                break
            await asyncio.sleep(0.01)
        assert [r.child_session_key for r in registry.list_all()] == [
            "corlinman:child-2"
        ]
    finally:
        cancel.set()
        await asyncio.wait_for(task, timeout=2)


async def test_tail_tolerates_missing_backend_methods() -> None:
    """A journal without the tail surface (e.g. an older backend object
    injected in tests) must not crash the loop — it exits cleanly."""

    class _Legacy:
        pass

    registry = LiveSubagentRegistry()
    cancel = asyncio.Event()
    # Must return promptly on its own (no tail surface -> no loop).
    await asyncio.wait_for(
        run_journal_subagent_tail(
            _Legacy(), registry, poll_seconds=0.01, cancel=cancel
        ),
        timeout=2,
    )
    assert registry.list_all() == []
