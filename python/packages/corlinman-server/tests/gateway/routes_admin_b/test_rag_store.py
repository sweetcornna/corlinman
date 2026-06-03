"""Tests for the RAG corpus store + ``/admin/rag/*`` wiring.

Covers the two halves of the rag_store gap fix:

* :class:`corlinman_server.gateway.rag_store.RagStore` implements every
  method the admin routes call (``count_*`` / ``search_bm25`` /
  ``query_chunks_by_ids`` / ``rebuild_fts`` for ``rag.py``;
  ``reset_chunk_decay`` / ``get_chunk_decay_state`` for ``memory.py``).
* Wiring the store onto ``AdminState.rag_store`` un-503s
  ``GET /admin/rag/stats`` + ``GET /admin/rag/query`` (previously every
  call returned 503 ``rag_disabled`` because the slot was never assigned).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from corlinman_server.gateway.rag_store import RagStore
from corlinman_server.gateway.routes_admin_b import rag as rag_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth


async def _seed_corpus(store: RagStore) -> dict[str, int]:
    """Insert two files + three chunks (+ two tags) directly via SQL.

    Returns the chunk ids so tests can assert on the decay routes.
    """
    conn = store._conn  # noqa: SLF001 — test-only direct seed
    await conn.execute(
        "INSERT INTO files(path, diary_name) VALUES ('a.md', 'kb'), ('b.md', 'kb')"
    )
    await conn.execute(
        "INSERT INTO chunks(file_id, chunk_index, content) VALUES "
        "(1, 0, 'the quick brown fox jumps'), "
        "(1, 1, 'lazy dog sleeps'), "
        "(2, 0, 'fox and hound')"
    )
    await conn.execute("INSERT INTO tags(name) VALUES ('animals'), ('test')")
    await conn.commit()
    async with conn.execute("SELECT id FROM chunks ORDER BY id") as cur:
        rows = await cur.fetchall()
    return {"first": int(rows[0]["id"]), "last": int(rows[-1]["id"])}


async def test_rag_store_counts_search_and_decay(tmp_path: Path) -> None:
    store = await RagStore.open(tmp_path / "kb.sqlite")
    try:
        # Fresh corpus → zero everywhere; empty search.
        assert await store.count_files() == 0
        assert await store.count_chunks() == 0
        assert await store.count_tags() == 0
        assert await store.search_bm25("fox", 10) == []

        ids = await _seed_corpus(store)

        assert await store.count_files() == 2
        assert await store.count_chunks() == 3
        assert await store.count_tags() == 2

        # BM25 finds the two 'fox' chunks, higher-is-better orientation.
        hits = await store.search_bm25("fox", 10)
        hit_ids = {cid for cid, _ in hits}
        assert hit_ids == {ids["first"], ids["last"]}
        assert all(isinstance(score, float) for _, score in hits)

        # Hydration returns id + content.
        chunks = await store.query_chunks_by_ids([ids["first"]])
        assert len(chunks) == 1
        assert chunks[0].id == ids["first"]
        assert "quick brown fox" in chunks[0].content

        # A malformed FTS query degrades to empty rather than raising.
        assert await store.search_bm25("", 10) == []
        assert await store.search_bm25('"', 10) == []

        # Rebuild is a no-op on a healthy index but must not raise.
        await store.rebuild_fts()
        assert {cid for cid, _ in await store.search_bm25("fox", 10)} == hit_ids

        # Decay reset: known chunk → 1 row affected; unknown → 0.
        assert await store.reset_chunk_decay(ids["first"]) == 1
        assert await store.reset_chunk_decay(999_999) == 0
        state = await store.get_chunk_decay_state(ids["first"])
        assert state == {"chunk_id": ids["first"], "decay_score": 1.0}
        assert await store.get_chunk_decay_state(999_999) is None
    finally:
        await store.close()


@pytest.fixture()
def admin_state(tmp_path: Path) -> Iterator[AdminState]:
    state = AdminState()
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(admin_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(rag_routes.router())
    return authenticated_test_client(app)


def test_rag_stats_503_when_store_unwired(client: TestClient) -> None:
    resp = client.get("/admin/rag/stats")
    assert resp.status_code == 503
    assert resp.json()["error"] == "rag_disabled"


async def test_rag_routes_serve_once_store_wired(
    admin_state: AdminState, tmp_path: Path
) -> None:
    store = await RagStore.open(tmp_path / "kb.sqlite")
    await _seed_corpus(store)
    admin_state.rag_store = store
    try:
        app = FastAPI()
        app.include_router(rag_routes.router())
        client = authenticated_test_client(app)

        stats = client.get("/admin/rag/stats")
        assert stats.status_code == 200
        body = stats.json()
        assert body == {"ready": True, "files": 2, "chunks": 3, "tags": 2}

        query = client.get("/admin/rag/query", params={"q": "fox", "k": 5})
        assert query.status_code == 200
        qbody = query.json()
        assert qbody["backend"] == "bm25"
        assert qbody["q"] == "fox"
        assert len(qbody["hits"]) == 2
        assert any("fox" in h["content_preview"] for h in qbody["hits"])

        rebuild = client.post("/admin/rag/rebuild")
        assert rebuild.status_code == 200
        assert rebuild.json() == {"status": "ok", "target": "chunks_fts"}
    finally:
        await store.close()
