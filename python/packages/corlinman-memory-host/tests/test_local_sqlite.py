"""Port of the ``#[cfg(test)] mod tests`` in
``rust/crates/corlinman-memory-host/src/local_sqlite.rs``.

Every Rust test case has a 1:1 Python counterpart with the same
assertions and the same data shape, modulo the Rust ``Arc<SqliteStore>``
plumbing (Python opens the store via :meth:`LocalSqliteHost.open` and
closes it in the fixture's finalizer)."""

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


async def test_upsert_then_query_roundtrip(host: LocalSqliteHost) -> None:
    doc_id = await host.upsert(
        MemoryDoc(
            content="the lazy fox jumps over dogs",
            metadata={"author": "x"},
        )
    )
    assert doc_id

    hits = await host.query(MemoryQuery(text="lazy fox", top_k=3))
    assert len(hits) == 1
    assert hits[0].id == doc_id
    assert hits[0].source == "local-kb"
    assert hits[0].score > 0.0
    assert "lazy fox" in hits[0].content


async def test_recent_returns_newest_first_scoped_by_namespace(
    host: LocalSqliteHost,
) -> None:
    """recent() pulls the latest docs for a namespace, newest first,
    independent of any BM25 query text."""
    await host.upsert(MemoryDoc(content="turn one", namespace="sess-a"))
    await host.upsert(MemoryDoc(content="turn two", namespace="sess-a"))
    await host.upsert(MemoryDoc(content="turn three", namespace="sess-a"))
    await host.upsert(MemoryDoc(content="other session", namespace="sess-b"))

    hits = await host.recent("sess-a", 2)
    assert [h.content for h in hits] == ["turn three", "turn two"]
    assert all(h.metadata["namespace"] == "sess-a" for h in hits)

    # Empty / unknown namespace → no hits, no error.
    assert await host.recent("sess-unknown", 5) == []
    assert await host.recent("sess-a", 0) == []


async def test_namespace_filter_scopes_results(host: LocalSqliteHost) -> None:
    id_a = await host.upsert(
        MemoryDoc(content="alpha document body", namespace="diary")
    )
    _id_b = await host.upsert(
        MemoryDoc(content="alpha document body", namespace="papers")
    )

    hits = await host.query(
        MemoryQuery(text="alpha", top_k=10, namespace="diary")
    )
    assert len(hits) == 1
    assert hits[0].id == id_a


async def test_query_preserves_upserted_metadata(host: LocalSqliteHost) -> None:
    await host.upsert(
        MemoryDoc(
            content="alpha graph node",
            metadata={
                "node_id": "kn-a",
                "title": "Alpha Node",
                "links": ["kn-b"],
                "related_nodes": ["Beta Node"],
            },
            namespace="agent-brain",
        )
    )

    hits = await host.query(
        MemoryQuery(text="alpha", top_k=3, namespace="agent-brain")
    )
    assert len(hits) == 1
    assert hits[0].metadata["node_id"] == "kn-a"
    assert hits[0].metadata["title"] == "Alpha Node"
    assert hits[0].metadata["links"] == ["kn-b"]
    assert hits[0].metadata["related_nodes"] == ["Beta Node"]


async def test_query_expands_one_hop_links_after_bm25_seed(
    host: LocalSqliteHost,
) -> None:
    id_a = await host.upsert(
        MemoryDoc(
            content="alpha seed memory",
            metadata={"node_id": "kn-a", "title": "Alpha", "links": ["kn-b"]},
            namespace="agent-brain",
        )
    )
    id_b = await host.upsert(
        MemoryDoc(
            content="beta linked context without query term",
            metadata={"node_id": "kn-b", "title": "Beta", "links": []},
            namespace="agent-brain",
        )
    )
    id_c = await host.upsert(
        MemoryDoc(
            content="gamma backlink context without query term",
            metadata={"node_id": "kn-c", "title": "Gamma", "links": ["kn-a"]},
            namespace="agent-brain",
        )
    )

    hits = await host.query(
        MemoryQuery(text="alpha", top_k=3, namespace="agent-brain")
    )
    ids = [h.id for h in hits]
    assert ids == [id_a, id_b, id_c]
    assert hits[0].metadata["graph_expanded"] is False
    assert hits[1].metadata["graph_expanded"] is True
    assert hits[2].metadata["graph_expanded"] is True


async def test_query_dedupes_by_node_id_and_host_metadata_wins(
    host: LocalSqliteHost,
) -> None:
    id_a = await host.upsert(
        MemoryDoc(
            content="alpha duplicate first",
            metadata={
                "node_id": "kn-a",
                "title": "Alpha",
                "namespace": "spoofed",
                "graph_expanded": True,
            },
            namespace="agent-brain",
        )
    )
    _id_dup = await host.upsert(
        MemoryDoc(
            content="alpha duplicate second",
            metadata={"node_id": "kn-a", "title": "Alpha duplicate"},
            namespace="agent-brain",
        )
    )

    hits = await host.query(
        MemoryQuery(text="alpha duplicate", top_k=5, namespace="agent-brain")
    )
    assert len(hits) == 1
    assert hits[0].id == id_a
    # Host metadata wins on conflict — the per-call host_base overrides
    # the upserted metadata's ``namespace`` / ``graph_expanded`` keys.
    assert hits[0].metadata["namespace"] == "agent-brain"
    assert hits[0].metadata["graph_expanded"] is False


async def test_query_dedupes_before_applying_top_k_budget(
    host: LocalSqliteHost,
) -> None:
    id_a = await host.upsert(
        MemoryDoc(
            content="alpha duplicate first",
            metadata={"node_id": "kn-a", "title": "Alpha", "links": ["kn-b"]},
            namespace="agent-brain",
        )
    )
    _id_dup = await host.upsert(
        MemoryDoc(
            content="alpha duplicate second",
            metadata={
                "node_id": "kn-a",
                "title": "Alpha duplicate",
                "links": [],
            },
            namespace="agent-brain",
        )
    )
    id_b = await host.upsert(
        MemoryDoc(
            content="beta linked context without query term",
            metadata={"node_id": "kn-b", "title": "Beta", "links": []},
            namespace="agent-brain",
        )
    )

    hits = await host.query(
        MemoryQuery(text="alpha duplicate", top_k=2, namespace="agent-brain")
    )
    ids = [h.id for h in hits]
    assert ids == [id_a, id_b]


async def test_delete_removes_hit(host: LocalSqliteHost) -> None:
    doc_id = await host.upsert(MemoryDoc(content="ephemeral note"))
    await host.delete(doc_id)

    hits = await host.query(MemoryQuery(text="ephemeral", top_k=5))
    assert hits == []


async def test_get_round_trips_upserted_doc(host: LocalSqliteHost) -> None:
    doc_id = await host.upsert(
        MemoryDoc(content="the quick brown fox", namespace="notes")
    )

    hit = await host.get(doc_id)
    assert hit is not None
    assert hit.id == doc_id
    assert hit.content == "the quick brown fox"
    assert hit.source == "local-kb"
    # Score is the "direct lookup" sentinel (1.0).
    assert abs(hit.score - 1.0) < 1e-6
    assert hit.metadata["namespace"] == "notes"


async def test_get_unknown_id_returns_none(host: LocalSqliteHost) -> None:
    # Numeric but unused id.
    assert await host.get("999999") is None
    # Non-numeric id maps to "unknown" too — lenient caller-decides
    # contract, same as the Rust impl.
    assert await host.get("not-a-number") is None


async def test_empty_query_is_empty_result(host: LocalSqliteHost) -> None:
    hits = await host.query(MemoryQuery(text="", top_k=3))
    assert hits == []


async def test_recent_returns_newest_n_with_decaying_scores_over_large_namespace(
    host: LocalSqliteHost,
) -> None:
    """recent() over a big namespace still returns only the newest N,
    newest-first, with rank-decayed scores."""
    n = 50
    for i in range(n):
        await host.upsert(MemoryDoc(content=f"turn {i}", namespace="sess-big"))

    limit = 8
    hits = await host.recent("sess-big", limit)

    # Newest `limit` docs, newest-first: turn 49 .. turn 42.
    assert [h.content for h in hits] == [f"turn {i}" for i in range(49, 41, -1)]
    assert all(h.metadata["namespace"] == "sess-big" for h in hits)
    assert all(h.source == "local-kb" for h in hits)
    # score decays by rank: 1.0 - rank/limit.
    assert [h.score for h in hits] == [1.0 - (rank / limit) for rank in range(limit)]


async def test_recent_does_not_full_scan_the_namespace(
    host: LocalSqliteHost,
) -> None:
    """recent() must push ORDER BY + LIMIT into SQL, not pull every id in
    the namespace and slice the tail. We spy on the store: the full-scan
    ``filter_chunk_ids_by_namespace`` must NOT be hit on the recall path,
    and the bounded recency query must be called with the limit."""
    n = 50
    for i in range(n):
        await host.upsert(MemoryDoc(content=f"turn {i}", namespace="sess-bounded"))

    store = host.store
    full_scan_calls: list[list[str]] = []
    orig_filter = store.filter_chunk_ids_by_namespace

    async def spy_filter(namespaces: list[str]) -> list[int]:
        full_scan_calls.append(list(namespaces))
        return await orig_filter(namespaces)

    bounded_calls: list[tuple[str, int]] = []
    orig_recent = store.recent_chunk_ids_by_namespace

    async def spy_recent(namespace: str, limit: int) -> list[int]:
        bounded_calls.append((namespace, limit))
        return await orig_recent(namespace, limit)

    store.filter_chunk_ids_by_namespace = spy_filter  # type: ignore[method-assign]
    store.recent_chunk_ids_by_namespace = spy_recent  # type: ignore[method-assign]
    try:
        limit = 8
        hits = await host.recent("sess-bounded", limit)
    finally:
        store.filter_chunk_ids_by_namespace = orig_filter  # type: ignore[method-assign]
        store.recent_chunk_ids_by_namespace = orig_recent  # type: ignore[method-assign]

    assert len(hits) == limit
    # The full-namespace scan must be gone from the recall path.
    assert full_scan_calls == []
    # The bounded query carries the limit so work doesn't scale with N.
    assert bounded_calls == [("sess-bounded", limit)]


def _composite_namespace_index_sql(rows: list) -> str | None:
    """Return the DDL of the first ``chunks`` index that leads on
    ``namespace`` then ``id`` (a covering index for the bounded recency
    query), or ``None`` if no such index exists. Name-agnostic on purpose:
    the structural guarantee is the indexed columns, not the index name."""
    for row in rows:
        sql = row["sql"]
        if sql is None:
            continue
        normalized = "".join(sql.lower().split())
        if "on chunks".replace(" ", "") in normalized and "(namespace,id)" in normalized:
            return str(sql)
    return None


async def test_chunks_has_composite_namespace_id_index_after_open(
    host: LocalSqliteHost,
) -> None:
    """The bounded recency query ``WHERE namespace = ? ORDER BY id DESC
    LIMIT ?`` (and the BM25 namespace pre-filter) must be backed by a
    composite ``chunks(namespace, id)`` index rather than a backward table
    scan. Assert the index exists after the store is opened. (#R7-P1
    followup.)

    The bare ``namespace``-only / no index world makes SQLite emit
    ``SCAN chunks`` for this query; the composite index turns it into a
    covering range scan. This test is the structural guard for that.
    """
    conn = host.store._conn
    async with conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'chunks'"
    ) as cur:
        rows = list(await cur.fetchall())

    index_sql = _composite_namespace_index_sql(rows)
    assert index_sql is not None, (
        "expected a composite chunks(namespace, id) index to back the "
        f"bounded recency query; found indexes: {[r['name'] for r in rows]}"
    )


async def test_recency_query_uses_index_not_table_scan(
    host: LocalSqliteHost,
) -> None:
    """The recency query plan must be an index (covering) range scan, not
    a full ``SCAN chunks``. This is the performance guarantee the composite
    namespace index exists to provide (#R7-P1 followup)."""
    await host.upsert(MemoryDoc(content="row", namespace="sess-plan"))

    conn = host.store._conn
    async with conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT id FROM chunks WHERE namespace = ? ORDER BY id DESC LIMIT ?",
        ("sess-plan", 5),
    ) as cur:
        plan = [str(tuple(r)[-1]).upper() for r in await cur.fetchall()]

    detail = " ".join(plan)
    assert "USING" in detail and "INDEX" in detail, (
        f"recency query should use an index range scan, got plan: {plan}"
    )
    assert "SCAN CHUNKS" not in detail, (
        f"recency query must not full-scan the chunks table, got plan: {plan}"
    )


async def test_index_survives_reopen_of_existing_db(tmp_path: Path) -> None:
    """The composite index is created with ``IF NOT EXISTS`` and the schema
    is (re)applied on every open, so an existing DB picks it up on the next
    open. Open, close, reopen the same on-disk DB and confirm the index is
    present and ``recent()``/``query()`` still return correct results
    (regression)."""
    db = tmp_path / "kb.sqlite"

    h1 = await LocalSqliteHost.open("local-kb", db)
    try:
        await h1.upsert(MemoryDoc(content="first turn", namespace="sess"))
        await h1.upsert(MemoryDoc(content="second turn", namespace="sess"))
    finally:
        await h1.close()

    h2 = await LocalSqliteHost.open("local-kb", db)
    try:
        conn = h2.store._conn
        async with conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'chunks'"
        ) as cur:
            rows = list(await cur.fetchall())
        assert _composite_namespace_index_sql(rows) is not None

        # recent() regression: newest-first, namespace-scoped.
        recent = await h2.recent("sess", 2)
        assert [h.content for h in recent] == ["second turn", "first turn"]

        # query() regression: BM25 still matches on content.
        hits = await h2.query(MemoryQuery(text="second", top_k=5))
        assert [h.content for h in hits] == ["second turn"]
    finally:
        await h2.close()
