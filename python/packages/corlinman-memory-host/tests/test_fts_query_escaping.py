"""FTS5 MATCH escaping — operator characters in user text must not blank
the search.

Raw user text used to be passed to ``MATCH`` unescaped, so FTS5 query
syntax (``-``, ``:``, quotes, stray operators) raised and the adapter
swallowed the error into a silent empty result — recall returned nothing
for messages like ``"corlinman - help:"``. ``_fts_match_query`` quotes
each whitespace token so operator characters match literally while plain
multi-word queries keep their implicit-AND semantics.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from corlinman_memory_host import LocalSqliteHost, MemoryDoc, MemoryQuery
from corlinman_memory_host.local_sqlite import _fts_match_query


@pytest.fixture
async def host(tmp_path: Path) -> AsyncIterator[LocalSqliteHost]:
    h = await LocalSqliteHost.open("local-kb", tmp_path / "kb.sqlite")
    try:
        yield h
    finally:
        await h.close()


def test_fts_match_query_quotes_tokens() -> None:
    assert _fts_match_query("lazy fox") == '"lazy" "fox"'
    assert _fts_match_query('say "hi"') == '"say" """hi"""'
    assert _fts_match_query("  ") == ""
    # Tokens that are only quote characters vanish rather than producing
    # a malformed empty phrase.
    assert _fts_match_query('"') == ""


async def test_operator_characters_match_literally(host: LocalSqliteHost) -> None:
    doc_id = await host.upsert(
        MemoryDoc(content="deploy failed with error code 137")
    )
    # Every one of these used to raise inside FTS5 (swallowed → []).
    # Multi-word queries keep the legacy implicit-AND semantics, so each
    # case only uses words present in the stored content.
    for query in (
        "deploy - error:",
        '"error code 137"',
        "(error",
        "error!",
        "code: 137",
    ):
        hits = await host.query(MemoryQuery(text=query, top_k=5))
        assert hits, f"query {query!r} should match literally"
        assert hits[0].id == doc_id
    # Queries made of operators/quotes alone degrade to empty, not raise.
    assert await host.query(MemoryQuery(text='"""', top_k=5)) == []


async def test_plain_multiword_keeps_implicit_and(host: LocalSqliteHost) -> None:
    id_both = await host.upsert(MemoryDoc(content="alpha beta gamma"))
    await host.upsert(MemoryDoc(content="alpha only here"))

    hits = await host.query(MemoryQuery(text="alpha beta", top_k=10))
    assert [h.id for h in hits] == [id_both]


async def test_namespace_search_is_escaped_too(host: LocalSqliteHost) -> None:
    doc_id = await host.upsert(
        MemoryDoc(content="note about c: drive", namespace="notes")
    )
    hits = await host.query(
        MemoryQuery(text="c: drive", top_k=5, namespace="notes")
    )
    assert [h.id for h in hits] == [doc_id]
