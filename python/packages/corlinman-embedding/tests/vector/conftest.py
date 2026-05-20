"""Vector test resource cleanup."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from corlinman_embedding.vector.bm25_store import SqliteStore


@pytest_asyncio.fixture(autouse=True)
async def close_open_sqlite_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    """Close every SqliteStore opened by a vector test before its loop ends."""

    opened: list[SqliteStore] = []
    original_open = SqliteStore.open.__func__

    async def tracked_open(cls: type[SqliteStore], path: object) -> SqliteStore:
        store = await original_open(cls, path)
        opened.append(store)
        return store

    monkeypatch.setattr(SqliteStore, "open", classmethod(tracked_open))
    try:
        yield
    finally:
        for store in reversed(opened):
            await store.close()
