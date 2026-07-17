"""``LocalSqliteHost.delete`` must reap the synthetic ``files`` row.

Before this fix ``delete`` removed the chunk only (Rust-parity), so every
delete leaked one ``files`` row forever — a real problem once the
conversational store gains decay/archival sweeps that delete in bulk.
The fix deletes the file row too, but only when no other chunk still
references it (multi-chunk files from other writers are left alone).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from corlinman_memory_host import LocalSqliteHost, MemoryDoc


@pytest.fixture
async def host(tmp_path: Path) -> AsyncIterator[LocalSqliteHost]:
    h = await LocalSqliteHost.open("local-kb", tmp_path / "kb.sqlite")
    try:
        yield h
    finally:
        await h.close()


async def _count(host: LocalSqliteHost, table: str) -> int:
    async with host.store._conn.execute(  # noqa: SLF001 — test peeks at storage
        f"SELECT COUNT(*) AS n FROM {table}"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row["n"])


async def test_delete_reaps_orphaned_file_row(host: LocalSqliteHost) -> None:
    doc_id = await host.upsert(MemoryDoc(content="ephemeral note"))
    keep_id = await host.upsert(MemoryDoc(content="durable note"))
    assert await _count(host, "files") == 2
    assert await _count(host, "chunks") == 2

    await host.delete(doc_id)

    assert await _count(host, "chunks") == 1
    assert await _count(host, "files") == 1
    # The surviving doc is untouched.
    kept = await host.get(keep_id)
    assert kept is not None and kept.content == "durable note"


async def test_delete_keeps_file_row_with_remaining_chunks(
    host: LocalSqliteHost,
) -> None:
    """A file referenced by more than one chunk survives a single-chunk
    delete — only fully-orphaned file rows are reaped."""
    doc_id = await host.upsert(MemoryDoc(content="chunk zero"))
    store = host.store
    async with store._conn.execute(  # noqa: SLF001
        "SELECT file_id FROM chunks WHERE id = ?", (int(doc_id),)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    file_id = int(row["file_id"])
    sibling_id = await store.insert_chunk(file_id, 1, "chunk one", None, "general")

    await host.delete(doc_id)

    assert await _count(host, "files") == 1
    async with store._conn.execute(  # noqa: SLF001
        "SELECT id FROM chunks WHERE file_id = ?", (file_id,)
    ) as cur:
        rows = await cur.fetchall()
    assert [int(r["id"]) for r in rows] == [sibling_id]


async def test_delete_cascades_metadata_and_links(host: LocalSqliteHost) -> None:
    doc_id = await host.upsert(
        MemoryDoc(
            content="linked note",
            metadata={"node_id": "n1", "links": ["n2"]},
        )
    )
    assert await _count(host, "memory_host_docs") == 1
    assert await _count(host, "memory_host_links") == 1

    await host.delete(doc_id)

    assert await _count(host, "memory_host_docs") == 0
    assert await _count(host, "memory_host_links") == 0
    assert await _count(host, "files") == 0
