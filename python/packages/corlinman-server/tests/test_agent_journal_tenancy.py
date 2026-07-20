"""W8 — tenant isolation on the per-turn journal.

The ``turns`` table historically had no tenant column, so every
journal-backed admin surface (session list / delete / meta patch /
replay) was cross-tenant readable and writable by whoever could name a
``session_key``. These tests pin the fix:

* ``begin_turn`` stamps the originating tenant (``""`` = legacy row,
  owned by the default tenant);
* every read/mutation surface accepts an optional ``tenant_id`` filter
  — ``None`` preserves the single-tenant fast path, a concrete tenant
  scopes the operation and cross-tenant access behaves as "not found";
* pre-tenant journals migrate additively on open (gated ALTER, same
  pattern as ``user_id`` / ``channel``).
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest
from corlinman_server.agent_journal import AgentJournal
from corlinman_server.agent_journal_backend import (
    DEFAULT_TENANT_ID,
    SqliteJournalBackend,
)

pytestmark = pytest.mark.asyncio


async def _seed(journal: AgentJournal, session_key: str, tenant_id: str) -> int:
    turn_id = await journal.begin_turn(
        session_key,
        f"hello from {tenant_id or 'legacy'}",
        tenant_id=tenant_id,
    )
    assert turn_id is not None
    await journal.append_message(turn_id, "user", "hi")
    await journal.append_message(turn_id, "assistant", "hello!")
    await journal.complete_turn(turn_id)
    return turn_id


# ---------------------------------------------------------------------------
# Constant hygiene — the backend's legacy-row owner must be the same
# tenant id the tenancy package calls "default".
# ---------------------------------------------------------------------------


async def test_default_tenant_constant_matches_tenancy_package() -> None:
    from corlinman_server.tenancy import DEFAULT_TENANT_ID as TENANCY_DEFAULT

    assert DEFAULT_TENANT_ID == TENANCY_DEFAULT


# ---------------------------------------------------------------------------
# Write path — begin_turn stamps the tenant.
# ---------------------------------------------------------------------------


async def test_begin_turn_stamps_tenant_id(tmp_path: Path) -> None:
    journal = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        turn_id = await journal.begin_turn(
            "sess-a", "hi", tenant_id="acme"
        )
        assert turn_id is not None
    finally:
        await journal.close()
    async with aiosqlite.connect(tmp_path / "j.sqlite") as conn:
        cur = await conn.execute(
            "SELECT tenant_id FROM turns WHERE turn_id = ?", (turn_id,)
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "acme"


async def test_begin_turn_default_is_legacy_empty(tmp_path: Path) -> None:
    """Callers that don't thread a tenant keep writing ``''`` rows —
    the legacy shape, owned by the default tenant on the read side."""
    journal = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        turn_id = await journal.begin_turn("sess-a", "hi")
        assert turn_id is not None
    finally:
        await journal.close()
    async with aiosqlite.connect(tmp_path / "j.sqlite") as conn:
        cur = await conn.execute(
            "SELECT tenant_id FROM turns WHERE turn_id = ?", (turn_id,)
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == ""


# ---------------------------------------------------------------------------
# Read path — list_session_summaries scoping.
# ---------------------------------------------------------------------------


async def test_list_session_summaries_scopes_by_tenant(tmp_path: Path) -> None:
    journal = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        await _seed(journal, "sess-acme", "acme")
        await _seed(journal, "sess-globex", "globex")
        await _seed(journal, "sess-legacy", "")

        acme = await journal.list_session_summaries(tenant_id="acme")
        assert [s.session_key for s in acme] == ["sess-acme"]

        globex = await journal.list_session_summaries(tenant_id="globex")
        assert [s.session_key for s in globex] == ["sess-globex"]
    finally:
        await journal.close()


async def test_default_tenant_owns_legacy_rows(tmp_path: Path) -> None:
    journal = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        await _seed(journal, "sess-acme", "acme")
        await _seed(journal, "sess-legacy", "")
        await _seed(journal, "sess-default", DEFAULT_TENANT_ID)

        rows = await journal.list_session_summaries(
            tenant_id=DEFAULT_TENANT_ID
        )
        keys = {s.session_key for s in rows}
        assert keys == {"sess-legacy", "sess-default"}
    finally:
        await journal.close()


async def test_list_session_summaries_unscoped_returns_all(
    tmp_path: Path,
) -> None:
    """``tenant_id=None`` keeps the pre-W8 behaviour for single-tenant
    callers that never resolve a tenant."""
    journal = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        await _seed(journal, "sess-acme", "acme")
        await _seed(journal, "sess-legacy", "")
        rows = await journal.list_session_summaries()
        assert {s.session_key for s in rows} == {"sess-acme", "sess-legacy"}
    finally:
        await journal.close()


# ---------------------------------------------------------------------------
# Mutations — delete / meta-patch respect the tenant scope.
# ---------------------------------------------------------------------------


async def test_delete_session_cross_tenant_deletes_nothing(
    tmp_path: Path,
) -> None:
    journal = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        await _seed(journal, "sess-acme", "acme")
        n = await journal.delete_session("sess-acme", tenant_id="globex")
        assert n == 0
        remaining = await journal.list_session_summaries(tenant_id="acme")
        assert [s.session_key for s in remaining] == ["sess-acme"]
    finally:
        await journal.close()


async def test_delete_session_same_tenant_deletes(tmp_path: Path) -> None:
    journal = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        await _seed(journal, "sess-acme", "acme")
        n = await journal.delete_session("sess-acme", tenant_id="acme")
        assert n == 1
        assert await journal.list_session_summaries(tenant_id="acme") == []
    finally:
        await journal.close()


async def test_delete_session_unscoped_still_deletes(tmp_path: Path) -> None:
    journal = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        await _seed(journal, "sess-acme", "acme")
        n = await journal.delete_session("sess-acme")
        assert n == 1
    finally:
        await journal.close()


async def test_session_exists_scoped(tmp_path: Path) -> None:
    journal = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        await _seed(journal, "sess-acme", "acme")
        assert await journal.session_exists("sess-acme", tenant_id="acme")
        assert not await journal.session_exists(
            "sess-acme", tenant_id="globex"
        )
        # Unscoped keeps legacy semantics.
        assert await journal.session_exists("sess-acme")
    finally:
        await journal.close()


async def test_update_session_meta_cross_tenant_is_not_found(
    tmp_path: Path,
) -> None:
    journal = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        await _seed(journal, "sess-acme", "acme")
        out = await journal.update_session_meta(
            "sess-acme", title="stolen", tenant_id="globex"
        )
        assert out is None
        ok = await journal.update_session_meta(
            "sess-acme", title="mine", tenant_id="acme"
        )
        assert ok is not None
        assert ok.title == "mine"
    finally:
        await journal.close()


async def test_list_session_turns_scoped(tmp_path: Path) -> None:
    journal = await AgentJournal.open(tmp_path / "j.sqlite")
    try:
        await _seed(journal, "sess-acme", "acme")
        own = await journal.list_session_turns("sess-acme", tenant_id="acme")
        assert len(own) == 1
        foreign = await journal.list_session_turns(
            "sess-acme", tenant_id="globex"
        )
        assert foreign == []
    finally:
        await journal.close()


# ---------------------------------------------------------------------------
# Migration — pre-tenant journals gain the column additively on open.
# ---------------------------------------------------------------------------


async def test_pre_tenant_journal_migrates_on_open(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    # Build a journal with the pre-W8 ``turns`` shape (no tenant_id).
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "CREATE TABLE turns ("
            "  turn_id INTEGER PRIMARY KEY,"
            "  session_key TEXT NOT NULL,"
            "  status TEXT NOT NULL,"
            "  started_at_ms INTEGER NOT NULL,"
            "  ended_at_ms INTEGER,"
            "  user_text TEXT,"
            "  user_id TEXT,"
            "  channel TEXT NOT NULL DEFAULT '',"
            "  pending_question_json TEXT,"
            "  error TEXT)"
        )
        await conn.execute(
            "INSERT INTO turns (turn_id, session_key, status, started_at_ms,"
            " user_text) VALUES (1, 'sess-old', 'completed', 1, 'hi')"
        )
        await conn.commit()

    backend = await SqliteJournalBackend.open(path)
    try:
        # The legacy row is now owned by the default tenant …
        rows = await backend.list_session_summaries(
            tenant_id=DEFAULT_TENANT_ID
        )
        assert [s.session_key for s in rows] == ["sess-old"]
        # … and invisible to any other tenant.
        assert await backend.list_session_summaries(tenant_id="acme") == []
    finally:
        await backend.close()
