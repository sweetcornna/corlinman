"""W3 — ``attachments_json`` round-trip on the journal message store.

Covers the additive ``turn_messages.attachments_json`` column: write via
``append_message`` / ``append_messages``, read back via
``load_messages``, and the gated-ALTER migration path for journals
created before the column existed.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio
from corlinman_server.agent_journal import AgentJournal

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def journal(tmp_path: Path) -> AgentJournal:
    j = await AgentJournal.open(tmp_path / "journal.sqlite")
    yield j
    await j.close()


META = [{"kind": "image", "url": "/v1/files/" + "a" * 26, "mime": "image/png"}]


async def test_append_message_attachments_round_trip(
    journal: AgentJournal,
) -> None:
    tid = await journal.begin_turn("sess-att", "look at this")
    await journal.append_message(
        tid, role="user", content="look at this", attachments=META
    )
    msgs = await journal._load_messages(tid)
    assert msgs[-1]["attachments"] == META


async def test_append_message_without_attachments_omits_key(
    journal: AgentJournal,
) -> None:
    tid = await journal.begin_turn("sess-plain", "no files")
    await journal.append_message(tid, role="user", content="no files")
    msgs = await journal._load_messages(tid)
    assert "attachments" not in msgs[-1]


async def test_append_messages_batch_carries_attachments(
    journal: AgentJournal,
) -> None:
    tid = await journal.begin_turn("sess-batch", "batch")
    await journal.append_messages(
        tid,
        [
            {"role": "user", "content": "with file", "attachments": META},
            {"role": "assistant", "content": "got it"},
        ],
    )
    msgs = await journal._load_messages(tid)
    assert msgs[0]["attachments"] == META
    assert "attachments" not in msgs[1]


async def test_migration_adds_column_to_pre_w3_journal(tmp_path: Path) -> None:
    """A journal whose ``turn_messages`` predates the column must gain it
    transparently on open (gated ALTER, same pattern as turns.user_id)."""
    path = tmp_path / "old.sqlite"
    conn = await aiosqlite.connect(path)
    await conn.executescript(
        """
        CREATE TABLE turns (
            turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_key TEXT NOT NULL,
            user_text TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at_ms INTEGER NOT NULL,
            completed_at_ms INTEGER,
            error TEXT
        );
        CREATE TABLE turn_messages (
            turn_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_call_id TEXT,
            tool_calls_json TEXT,
            PRIMARY KEY (turn_id, seq)
        );
        """
    )
    await conn.commit()
    await conn.close()

    j = await AgentJournal.open(path)
    try:
        tid = await j.begin_turn("sess-mig", "after migration")
        await j.append_message(
            tid, role="user", content="after migration", attachments=META
        )
        msgs = await j._load_messages(tid)
        assert msgs[-1]["attachments"] == META
    finally:
        await j.close()
