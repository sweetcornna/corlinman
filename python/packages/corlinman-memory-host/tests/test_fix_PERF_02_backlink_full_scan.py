"""Repro for PERF-02: graph back-link expansion scans + JSON-decodes
EVERY ``memory_host_docs`` row in the namespace on every successful
recall (``_backlinked_node_ids`` -> ``list_memory_host_metadata`` ->
``json.loads`` per row + linear link scan), so backlink cost is
O(namespace) per query instead of O(seed/result count).

We assert the cost scales with seed/result count by counting how many
metadata rows the backlink resolution touches. Before the fix the count
equals the whole namespace (minus seeds); after the fix it is bounded by
the seed set."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from corlinman_memory_host import (
    LocalSqliteHost,
    MemoryDoc,
    MemoryQuery,
)


@pytest.fixture
async def host(tmp_path: Path) -> AsyncIterator[LocalSqliteHost]:
    h = await LocalSqliteHost.open("local-kb", tmp_path / "kb.sqlite")
    try:
        yield h
    finally:
        await h.close()


async def test_backlink_cost_scales_with_seeds_not_namespace(
    host: LocalSqliteHost,
) -> None:
    # One node the query will hit ("seed"), plus a large amount of noise
    # in the same namespace whose metadata must NOT be scanned per query.
    await host.upsert(
        MemoryDoc(
            content="unique seed token here",
            metadata={"node_id": "seed", "links": []},
            namespace="ns",
        )
    )
    # A real back-linker: links to "seed", so it must be returned.
    await host.upsert(
        MemoryDoc(
            content="some other content backlinker",
            metadata={"node_id": "backlinker", "links": ["seed"]},
            namespace="ns",
        )
    )
    # Noise: 500 unrelated docs with metadata + links in the same
    # namespace. Old code json.loads()es every one of these per recall.
    for i in range(500):
        await host.upsert(
            MemoryDoc(
                content=f"noise document {i}",
                metadata={"node_id": f"noise-{i}", "links": [f"x-{i}"]},
                namespace="ns",
            )
        )

    # Instrument the full-namespace scan path. After the fix this should
    # never be called (back-links resolved via an indexed edge lookup),
    # or at minimum must not scan the whole namespace.
    scan_row_counts: list[int] = []
    orig = host.store.list_memory_host_metadata

    async def _counting_scan(namespace: str | None):  # type: ignore[no-untyped-def]
        rows = await orig(namespace)
        scan_row_counts.append(len(rows))
        return rows

    host.store.list_memory_host_metadata = _counting_scan  # type: ignore[assignment]

    hits = await host.query(MemoryQuery(text="seed", top_k=10, namespace="ns"))

    # Functional contract preserved: the seed plus its real backlinker.
    contents = {h.content for h in hits}
    assert "unique seed token here" in contents
    assert "some other content backlinker" in contents

    # Cost contract: backlink resolution must NOT scan the whole
    # namespace. Before the fix scan_row_counts == [502] (every row).
    scanned = max(scan_row_counts) if scan_row_counts else 0
    assert scanned < 100, (
        f"backlink resolution scanned {scanned} metadata rows "
        "(should scale with seed/result count, not namespace size)"
    )


async def test_backlinks_backfilled_for_preexisting_docs(tmp_path: Path) -> None:
    """A DB whose docs+links were written before the edge table existed
    must still resolve back-links after upgrade (one-time backfill)."""
    db = tmp_path / "legacy.sqlite"

    # Simulate a "legacy" DB: write docs/metadata WITHOUT the edge table
    # by inserting directly into memory_host_docs after stripping the edge
    # table, mimicking an on-disk DB created by the pre-fix code.
    h = await LocalSqliteHost.open("local-kb", db)
    try:
        await h.upsert(
            MemoryDoc(
                content="unique seed token here",
                metadata={"node_id": "seed", "links": []},
                namespace="ns",
            )
        )
        await h.upsert(
            MemoryDoc(
                content="legacy backlinker content",
                metadata={"node_id": "backlinker", "links": ["seed"]},
                namespace="ns",
            )
        )
        # Drop the edge table contents to emulate a DB migrated in place
        # (docs/metadata present, edges never recorded).
        conn = h.store._conn
        await conn.execute("DELETE FROM memory_host_links")
        await conn.commit()
        # Force the next ensure() to re-run the backfill on this connection.
        h.store._memory_host_schema_ready = False
    finally:
        await h.close()

    # Reopen (fresh connection) → schema ensure + backfill kicks in.
    h2 = await LocalSqliteHost.open("local-kb", db)
    try:
        hits = await h2.query(
            MemoryQuery(text="seed", top_k=10, namespace="ns")
        )
        contents = {hit.content for hit in hits}
        assert "unique seed token here" in contents
        assert "legacy backlinker content" in contents
    finally:
        await h2.close()
