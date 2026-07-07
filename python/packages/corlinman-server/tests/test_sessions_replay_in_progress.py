"""Regression: an in-progress turn must NOT appear in the settled
transcript.

The ``/chat`` page renders a still-generating turn LIVE via
``resumeInFlight`` (a separate pending bubble that tails the journal). A
multi-step agentic turn journals its intermediate assistant/tool message
rows AS IT RUNS, so if ``_replay_from_journal`` also emitted them into the
settled transcript the turn double-rendered — a frozen
"已隐藏 N 个工具调用" bubble stacked above the live one (the bug the user
reported on the ``feat/multi-agent-live-panel`` branch).
"""

from __future__ import annotations

from pathlib import Path

from corlinman_replay import ReplayMode
from corlinman_server.agent_journal import AgentJournal
from corlinman_server.gateway.routes_admin_a._sessions_lib import (
    _replay_from_journal,
)
from corlinman_server.tenancy import default_tenant


async def test_in_progress_turn_excluded_from_transcript(tmp_path: Path) -> None:
    data_dir = tmp_path
    session_key = "corlinman:sess-resume"
    journal = await AgentJournal.open(data_dir / "agent_journal.sqlite")
    try:
        # A completed turn — present in the transcript.
        t_done = await journal.begin_turn(session_key, "first question")
        await journal.append_message(t_done, "user", "first question")
        await journal.append_message(t_done, "assistant", "first answer")
        await journal.complete_turn(t_done)

        # An in-progress turn — a tool round is already journaled but no
        # final answer yet. Deliberately NOT completed.
        t_live = await journal.begin_turn(session_key, "second question")
        await journal.append_message(t_live, "user", "second question")
        await journal.append_message(
            t_live,
            "assistant",
            "",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        )
        await journal.append_message(
            t_live, "tool", "search result", tool_call_id="call_1"
        )
    finally:
        await journal.close()

    out = await _replay_from_journal(
        data_dir, default_tenant(), session_key, ReplayMode.TRANSCRIPT
    )
    assert out is not None
    transcript = out["transcript"]
    contents = [m["content"] for m in transcript]

    # Completed turn survives.
    assert "first question" in contents
    assert "first answer" in contents
    # In-progress turn is excluded wholesale — neither its user prompt nor
    # any assistant bubble carrying its tool_calls leaks into the settled
    # transcript (the live resume bubble owns it).
    assert "second question" not in contents
    assert all("tool_calls" not in m for m in transcript)


async def test_older_stale_in_progress_turn_is_included(tmp_path: Path) -> None:
    """L-103 — a crashed in-progress turn that is NOT the newest turn must
    appear in the settled transcript. Only the NEWEST turn can be live (a
    session runs one turn at a time), so an older ``in_progress`` row is a
    crash artifact: skipping it silently vanished the user's message and
    any partial answer from the thread forever."""
    import asyncio

    data_dir = tmp_path
    session_key = "corlinman:sess-crashed"
    journal = await AgentJournal.open(data_dir / "agent_journal.sqlite")
    try:
        # Crashed turn — journaled a user message + partial answer, never
        # completed. Deliberately older than the next turn.
        t_crash = await journal.begin_turn(session_key, "lost question")
        await journal.append_message(t_crash, "user", "lost question")
        await journal.append_message(t_crash, "assistant", "partial answer")
        # Ensure a strictly-later started_at_ms for the next turn (the
        # newest-turn detection orders by started_at_ms DESC).
        await asyncio.sleep(0.01)
        t_done = await journal.begin_turn(session_key, "later question")
        await journal.append_message(t_done, "user", "later question")
        await journal.append_message(t_done, "assistant", "later answer")
        await journal.complete_turn(t_done)
    finally:
        await journal.close()

    out = await _replay_from_journal(
        data_dir, default_tenant(), session_key, ReplayMode.TRANSCRIPT
    )
    assert out is not None
    contents = [m["content"] for m in out["transcript"]]
    # The crashed turn's rows are real history now — both survive.
    assert "lost question" in contents
    assert "partial answer" in contents
    assert "later question" in contents
    # And chronological order holds: crashed turn precedes the newer one.
    assert contents.index("lost question") < contents.index("later question")


async def test_only_in_progress_turn_yields_empty_transcript(
    tmp_path: Path,
) -> None:
    """A session whose ONLY turn is in-progress returns an empty (but
    non-None) transcript — the caller renders a clean thread + reattaches
    the live stream rather than 404'ing."""
    data_dir = tmp_path
    session_key = "corlinman:sess-fresh"
    journal = await AgentJournal.open(data_dir / "agent_journal.sqlite")
    try:
        t_live = await journal.begin_turn(session_key, "only question")
        await journal.append_message(t_live, "user", "only question")
        await journal.append_message(
            t_live,
            "assistant",
            "",
            tool_calls=[
                {
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        )
    finally:
        await journal.close()

    out = await _replay_from_journal(
        data_dir, default_tenant(), session_key, ReplayMode.TRANSCRIPT
    )
    assert out is not None
    assert out["transcript"] == []
