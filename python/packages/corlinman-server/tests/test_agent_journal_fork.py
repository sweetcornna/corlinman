"""E3 --fork-session: :meth:`AgentJournal.fork_session` semantics.

claude-code ``--fork-session`` branches a conversation under a fresh
session key so the user can explore an alternate continuation without
mutating the original. These tests pin the load-bearing rules:

- only ``completed`` turns are copied (an ``in_progress`` turn is live
  elsewhere; an ``errored`` turn carries T4.4 breadcrumbs that must not
  replay as clean history),
- messages round-trip faithfully (roles / contents / ``tool_calls`` in
  chronological order) under the new key,
- the source session is strictly read-only — its turn ledger is
  unchanged and later writes to the fork never leak back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from corlinman_server.agent_journal import AgentJournal


async def _session_messages(
    journal: AgentJournal, key: str
) -> list[dict[str, Any]]:
    """Flatten a session's messages in chronological (oldest-first) order.

    ``get_session_turn_ids`` returns started_at_ms DESC, so we reverse to
    read the way the conversation actually happened, then concatenate each
    turn's ``_load_messages`` payload.
    """
    turn_ids = await journal.get_session_turn_ids(key, limit=500)
    out: list[dict[str, Any]] = []
    for turn_id in reversed(turn_ids):
        out.extend(await journal._load_messages(turn_id))  # noqa: SLF001
    return out


async def _seed_completed_turn(
    journal: AgentJournal, key: str, user_text: str, tool_name: str
) -> int:
    """A realistic multi-message completed turn: user → assistant(tool_call)
    → tool result, then completed."""
    turn = await journal.begin_turn(key, user_text)
    assert turn is not None
    await journal.append_message(turn, "user", user_text)
    await journal.append_message(
        turn,
        "assistant",
        "",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": tool_name, "arguments": "{}"},
            }
        ],
    )
    await journal.append_message(
        turn, "tool", f"{tool_name} result", tool_call_id="call_1"
    )
    await journal.complete_turn(turn)
    return turn


async def test_fork_copies_completed_turns_faithfully(tmp_path: Path) -> None:
    src = "console:source"
    dst = "console:fork"
    journal = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
    try:
        await _seed_completed_turn(journal, src, "first question", "web_search")
        await _seed_completed_turn(journal, src, "second question", "read_file")

        src_before = await _session_messages(journal, src)

        copied = await journal.fork_session(src, dst)
        assert copied == 2

        forked = await _session_messages(journal, dst)
        # Same roles / contents / tool_calls, chronological order preserved.
        assert forked == src_before

        # Source is untouched — same two turns, same messages.
        assert len(await journal.get_session_turn_ids(src, limit=500)) == 2
        assert await _session_messages(journal, src) == src_before
    finally:
        await journal.close()


async def test_fork_skips_in_progress_turn(tmp_path: Path) -> None:
    src = "console:source"
    dst = "console:fork"
    journal = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
    try:
        await _seed_completed_turn(journal, src, "done", "web_search")
        # A live turn — journaled but never completed.
        live = await journal.begin_turn(src, "still thinking")
        assert live is not None
        await journal.append_message(live, "user", "still thinking")

        copied = await journal.fork_session(src, dst)
        assert copied == 1  # only the completed turn

        forked = await _session_messages(journal, dst)
        contents = [m["content"] for m in forked]
        assert "done" in contents
        assert "still thinking" not in contents
    finally:
        await journal.close()


async def test_fork_skips_errored_turn(tmp_path: Path) -> None:
    src = "console:source"
    dst = "console:fork"
    journal = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
    try:
        await _seed_completed_turn(journal, src, "clean", "web_search")
        bad = await journal.begin_turn(src, "boom")
        assert bad is not None
        await journal.append_message(bad, "user", "boom")
        await journal.error_turn(bad, "traceback: something exploded")

        copied = await journal.fork_session(src, dst)
        assert copied == 1

        forked = await _session_messages(journal, dst)
        contents = [m["content"] for m in forked]
        assert "clean" in contents
        assert "boom" not in contents
    finally:
        await journal.close()


async def test_fork_empty_source_returns_zero(tmp_path: Path) -> None:
    journal = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
    try:
        assert await journal.fork_session("console:nope", "console:fork") == 0
        # Guard: empty keys are a no-op too.
        assert await journal.fork_session("", "console:fork") == 0
        assert await journal.fork_session("console:src", "") == 0
    finally:
        await journal.close()


async def test_fork_same_key_is_noop(tmp_path: Path) -> None:
    key = "console:source"
    journal = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
    try:
        await _seed_completed_turn(journal, key, "hello", "web_search")
        before = await _session_messages(journal, key)

        # source_key == new_key must not duplicate the history onto itself.
        assert await journal.fork_session(key, key) == 0

        assert len(await journal.get_session_turn_ids(key, limit=500)) == 1
        assert await _session_messages(journal, key) == before
    finally:
        await journal.close()


async def test_writes_to_fork_do_not_leak_into_source(tmp_path: Path) -> None:
    src = "console:source"
    dst = "console:fork"
    journal = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
    try:
        await _seed_completed_turn(journal, src, "original", "web_search")
        assert await journal.fork_session(src, dst) == 1

        # Continue the FORK with a brand-new turn.
        extra = await journal.begin_turn(dst, "fork-only follow-up")
        assert extra is not None
        await journal.append_message(extra, "user", "fork-only follow-up")
        await journal.append_message(extra, "assistant", "sure")
        await journal.complete_turn(extra)

        # The source stays a single turn; the fork's continuation is invisible.
        assert len(await journal.get_session_turn_ids(src, limit=500)) == 1
        src_contents = [m["content"] for m in await _session_messages(journal, src)]
        assert "fork-only follow-up" not in src_contents
        # The fork carries both the copied turn and the new one.
        dst_contents = [m["content"] for m in await _session_messages(journal, dst)]
        assert "original" in dst_contents
        assert "fork-only follow-up" in dst_contents
    finally:
        await journal.close()
