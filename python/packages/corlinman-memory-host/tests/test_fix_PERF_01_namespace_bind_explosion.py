"""Repro for PERF-01: namespace recall inlines every chunk id as an
``AND rowid IN (?,?,...)`` bind list, which exceeds
``SQLITE_MAX_VARIABLE_NUMBER`` once a namespace holds more docs than the
limit, raising ``OperationalError: too many SQL variables``.

The modern SQLite default limit is 32766, so to keep the test fast we
lower the connection's variable limit and upsert a modest number of docs
that comfortably exceeds it. The bug is purely about *how many binds the
query emits*, so a lowered limit reproduces the exact failure mode the
audit describes (and the fix — pushing the namespace predicate into SQL
via a JOIN — emits a fixed, tiny number of binds regardless of namespace
size)."""

from __future__ import annotations

import sqlite3
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


async def test_namespace_recall_no_var_limit_explosion(
    host: LocalSqliteHost,
) -> None:
    # Lower the variable limit on the live connection so a small corpus
    # reproduces the same too-many-variables failure a large namespace
    # would hit against the real 32766 default. aiosqlite drives the real
    # sqlite3 connection on a worker thread, so the limit must be set on
    # that thread via the connection's own ``_execute`` plumbing.
    conn = host.store._conn

    def _lower_limit(raw: sqlite3.Connection) -> None:
        raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 50)

    await conn._execute(_lower_limit, conn._conn)

    # 200 docs in one namespace, all sharing a query token.
    for i in range(200):
        await host.upsert(
            MemoryDoc(content=f"shared token doc number {i}", namespace="ns")
        )

    # Before the fix this raises ``MemoryHostError`` wrapping
    # ``OperationalError: too many SQL variables`` because the namespace
    # filter inlines all 200 ids. After the fix the namespace predicate
    # is a single bind, so this returns normally.
    hits = await host.query(
        MemoryQuery(text="shared", top_k=5, namespace="ns")
    )
    # O(top_k) results, all from the queried namespace.
    assert len(hits) == 5
    assert all(h.metadata["namespace"] == "ns" for h in hits)


async def test_namespace_recall_still_scopes_correctly(
    host: LocalSqliteHost,
) -> None:
    """The JOIN-based pushdown must keep namespace scoping intact."""
    id_a = await host.upsert(
        MemoryDoc(content="alpha document body", namespace="diary")
    )
    await host.upsert(
        MemoryDoc(content="alpha document body", namespace="papers")
    )

    hits = await host.query(
        MemoryQuery(text="alpha", top_k=10, namespace="diary")
    )
    assert len(hits) == 1
    assert hits[0].id == id_a
    assert hits[0].metadata["namespace"] == "diary"
