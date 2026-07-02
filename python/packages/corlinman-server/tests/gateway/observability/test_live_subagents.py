"""Unit tests for :class:`LiveSubagentRegistry` (W2.x multi-agent panel).

Drives the registry with the same ``SubagentSpawned`` / ``SubagentEvent`` /
``SubagentCompleted`` envelopes the emitter tees to it, and asserts the
lifecycle row transitions the ``/admin/subagents`` overview renders.
"""

from __future__ import annotations

from corlinman_agent.events import (
    BlockStart,
    EventEnvelope,
    SubagentCompleted,
    SubagentEvent,
    SubagentSpawned,
    ToolStateRunning,
)
from corlinman_server.gateway.observability import LiveSubagentRegistry


def _env(session_key: str, event: object, ts: int = 1000) -> EventEnvelope:
    return EventEnvelope(
        turn_id="t1",
        session_key=session_key,
        sequence=0,
        timestamp_ms=ts,
        event=event,  # type: ignore[arg-type]
    )


def _spawn(child: str, *, parent: str = "p", depth: int = 0, ts: int = 1000) -> EventEnvelope:
    return _env(
        parent,
        SubagentSpawned(
            parent_session_key=parent,
            child_session_key=child,
            child_agent_id="researcher",
            depth=depth,
            prompt_preview="find the answer",
        ),
        ts=ts,
    )


def test_spawn_creates_running_row() -> None:
    reg = LiveSubagentRegistry()
    reg.observe(_spawn("c1", depth=1))

    rows = reg.list_active()
    assert len(rows) == 1
    row = rows[0]
    assert row.request_id == "c1"
    assert row.parent_session_key == "p"
    assert row.subagent_type == "researcher"
    assert row.state == "running"
    assert row.depth == 1
    assert row.source == "inline"
    assert row.started_at == 1000
    assert row.description == "find the answer"


def test_child_tool_event_sets_activity_and_counts() -> None:
    reg = LiveSubagentRegistry()
    reg.observe(_spawn("c1"))
    inner = _env(
        "c1",
        ToolStateRunning(
            tool_call_id="tc1",
            tool_name="web_search",
            args_json="{}",
            started_at_ms=1100,
        ),
    )
    reg.observe(_env("p", SubagentEvent(child_session_key="c1", envelope=inner)))

    row = reg.list_active()[0]
    assert row.tool_calls_made == 1
    assert "web_search" in row.activity

    # A reasoning block flips the activity line to "thinking".
    block = _env("c1", BlockStart(index=0, block_type="reasoning"))
    reg.observe(_env("p", SubagentEvent(child_session_key="c1", envelope=block)))
    assert reg.list_active()[0].activity == "思考中…"


def test_tool_calls_deduped_by_tool_call_id_across_paths() -> None:
    """Regression (L-102): the shared registry is fed once per open SSE client
    (poll) AND via the emitter observer, so the same ``ToolStateRunning`` frame
    arrives many times. Counting must be idempotent by ``tool_call_id`` — not
    ``+= 1`` per delivery — or the live panel shows an inflated tool count."""
    reg = LiveSubagentRegistry()
    reg.observe(_spawn("c1"))

    def _running_env(tc: str) -> object:
        inner = _env(
            "c1",
            ToolStateRunning(
                tool_call_id=tc, tool_name="web_search", args_json="{}", started_at_ms=1100
            ),
        )
        return _env("p", SubagentEvent(child_session_key="c1", envelope=inner))

    # Emitter + several SSE-client poll re-deliveries of the SAME tool call.
    for _ in range(3):
        reg.observe(_running_env("tc1"))
    # The cross-process journal poll re-delivers the very same tool call too.
    reg.observe_journal_event(
        {
            "event_type": "SubagentEvent",
            "timestamp_ms": 1100,
            "payload": {
                "child_session_key": "c1",
                "envelope": {
                    "event_type": "ToolStateRunning",
                    "payload": {"tool_name": "web_search", "tool_call_id": "tc1"},
                },
            },
        }
    )
    assert reg.list_active()[0].tool_calls_made == 1  # deduped, not 4

    # A genuinely new tool call advances the count.
    reg.observe(_running_env("tc2"))
    assert reg.list_active()[0].tool_calls_made == 2


def test_completed_marks_terminal_and_clears_activity() -> None:
    reg = LiveSubagentRegistry()
    reg.observe(_spawn("c1", ts=1000))
    reg.observe(
        _env(
            "p",
            SubagentCompleted(
                child_session_key="c1",
                finish_reason="completed",
                tool_calls_made=3,
                elapsed_ms=4200,
                summary="done: 42",
            ),
            ts=5200,
        )
    )
    assert reg.list_active() == []
    row = reg.list_all()[0]
    assert row.state == "succeeded"
    assert row.finished_at == 5200
    assert row.tool_calls_made == 3
    assert row.elapsed_ms == 4200
    assert row.summary == "done: 42"
    assert row.activity == ""


def test_completed_error_and_timeout_states() -> None:
    reg = LiveSubagentRegistry()
    reg.observe(_spawn("e1"))
    reg.observe(
        _env("p", SubagentCompleted(child_session_key="e1", finish_reason="error", tool_calls_made=0, elapsed_ms=10, summary=""))
    )
    reg.observe(_spawn("t1"))
    reg.observe(
        _env("p", SubagentCompleted(child_session_key="t1", finish_reason="timeout", tool_calls_made=0, elapsed_ms=99, summary=""))
    )
    by_id = {r.request_id: r for r in reg.list_all()}
    assert by_id["e1"].state == "failed"
    assert by_id["t1"].state == "timeout"


def test_terminal_rows_are_capped() -> None:
    reg = LiveSubagentRegistry(terminal_cap=2)
    for i in range(5):
        cid = f"c{i}"
        reg.observe(_spawn(cid))
        reg.observe(
            _env("p", SubagentCompleted(child_session_key=cid, finish_reason="completed", tool_calls_made=0, elapsed_ms=1, summary=""))
        )
    # Only the cap's worth of terminal rows are retained (oldest dropped).
    assert len(reg.list_all()) == 2
    assert {r.request_id for r in reg.list_all()} == {"c3", "c4"}


def test_observe_never_raises_on_garbage() -> None:
    reg = LiveSubagentRegistry()
    # Non-subagent / malformed envelopes are ignored, not fatal.
    reg.observe(object())
    reg.observe(_env("p", BlockStart(index=0, block_type="text")))
    assert reg.list_all() == []


# --------------------------------------------------------------------- #
# Cross-process journal-dict path (grpc_agent mode): the gateway feeds   #
# the registry from journal rows the agent process wrote.                #
# --------------------------------------------------------------------- #


def test_observe_journal_event_lifecycle() -> None:
    reg = LiveSubagentRegistry()
    reg.observe_journal_event({
        "event_type": "SubagentSpawned",
        "timestamp_ms": 1000,
        "payload": {
            "parent_session_key": "sess",
            "child_session_key": "sess::child::0",
            "child_agent_id": "researcher",
            "depth": 1,
            "prompt_preview": "find the answer",
        },
    })
    rows = reg.list_active()
    assert len(rows) == 1
    assert rows[0].subagent_type == "researcher"
    assert rows[0].source == "inline"
    assert rows[0].depth == 1
    assert rows[0].state == "running"

    reg.observe_journal_event({
        "event_type": "SubagentEvent",
        "timestamp_ms": 1100,
        "payload": {
            "child_session_key": "sess::child::0",
            "envelope": {
                "event_type": "ToolStateRunning",
                "payload": {"tool_name": "web_search"},
            },
        },
    })
    assert "web_search" in reg.list_active()[0].activity
    assert reg.list_active()[0].tool_calls_made == 1

    reg.observe_journal_event({
        "event_type": "SubagentCompleted",
        "timestamp_ms": 5200,
        "payload": {
            "child_session_key": "sess::child::0",
            "finish_reason": "completed",
            "tool_calls_made": 3,
            "elapsed_ms": 4200,
            "summary": "done: 42",
        },
    })
    assert reg.list_active() == []
    row = reg.list_all()[0]
    assert row.state == "succeeded"
    assert row.summary == "done: 42"
    assert row.elapsed_ms == 4200


def test_observe_journal_event_idempotent_respawn() -> None:
    """Poll re-delivery of the same spawn must not reset an advancing row."""
    reg = LiveSubagentRegistry()
    spawn = {
        "event_type": "SubagentSpawned",
        "timestamp_ms": 1000,
        "payload": {
            "parent_session_key": "sess",
            "child_session_key": "c1",
            "child_agent_id": "w",
            "depth": 0,
            "prompt_preview": "",
        },
    }
    reg.observe_journal_event(spawn)
    reg.observe_journal_event({
        "event_type": "SubagentEvent",
        "payload": {
            "child_session_key": "c1",
            "envelope": {"event_type": "ToolStateRunning", "payload": {"tool_name": "x"}},
        },
    })
    reg.observe_journal_event(spawn)  # re-delivered by the poll
    assert reg.list_active()[0].tool_calls_made == 1  # not reset


def test_observe_journal_event_ignores_non_subagent() -> None:
    reg = LiveSubagentRegistry()
    reg.observe_journal_event({"event_type": "TextDelta", "payload": {"text": "hi"}})
    reg.observe_journal_event({"event_type": "SubagentSpawned", "payload": {}})
    assert reg.list_all() == []
