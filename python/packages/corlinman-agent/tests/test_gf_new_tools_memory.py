"""gap-fill lane-new-tools — agent-callable memory write/read.

Covers gap ``memory-and-session-search``: the new ``memory_write`` /
``memory_read`` tools that persist + recall durable notes via the
``MemoryHost`` interface (``upsert`` / ``query`` / ``recent``). The
existing ``memory_search`` / ``session_search`` remain untouched.
"""

from __future__ import annotations

import json

from corlinman_agent.memory.tools import (
    MEMORY_READ_TOOL,
    MEMORY_WRITE_TOOL,
    dispatch_memory_read,
    dispatch_memory_write,
    memory_read_tool_schema,
    memory_tool_schemas,
    memory_write_tool_schema,
)


class _StubHost:
    """Minimal in-memory ``MemoryHost`` stand-in exercising the surface
    the dispatchers use: ``upsert`` / ``query`` / ``recent``."""

    def __init__(self) -> None:
        self._docs: list[tuple[str, object]] = []  # (id, MemoryDoc)
        self._counter = 0

    async def upsert(self, doc) -> str:  # noqa: ANN001
        self._counter += 1
        doc_id = str(self._counter)
        self._docs.append((doc_id, doc))
        return doc_id

    async def query(self, req):  # noqa: ANN001
        from corlinman_memory_host.types import MemoryHit

        out = []
        for doc_id, doc in self._docs:
            if req.namespace is not None and doc.namespace != req.namespace:
                continue
            if req.text.lower() in doc.content.lower():
                out.append(
                    MemoryHit(
                        id=doc_id,
                        content=doc.content,
                        score=1.0,
                        source="stub",
                        metadata=doc.metadata,
                    )
                )
        return out[: req.top_k]

    async def recent(self, namespace, limit):  # noqa: ANN001
        from corlinman_memory_host.types import MemoryHit

        out = []
        for doc_id, doc in reversed(self._docs):
            if doc.namespace != namespace:
                continue
            out.append(
                MemoryHit(
                    id=doc_id,
                    content=doc.content,
                    score=1.0,
                    source="stub",
                    metadata=doc.metadata,
                )
            )
            if len(out) >= limit:
                break
        return out


def test_tool_names_wire_stable() -> None:
    assert MEMORY_WRITE_TOOL == "memory_write"
    assert MEMORY_READ_TOOL == "memory_read"


def test_schemas_present_in_bundle() -> None:
    names = {s["function"]["name"] for s in memory_tool_schemas()}
    assert {"memory_search", "session_search", "memory_write", "memory_read"} <= names
    # memory_write requires content; memory_read requires nothing.
    assert memory_write_tool_schema()["function"]["parameters"]["required"] == [
        "content"
    ]
    assert memory_read_tool_schema()["function"]["parameters"]["required"] == []


async def test_write_then_read_roundtrip() -> None:
    host = _StubHost()
    w = await dispatch_memory_write(
        json.dumps(
            {"content": "User prefers metric units", "tag": "preference"}
        ).encode(),
        memory_host=host,
    )
    w_env = json.loads(w)
    assert w_env["ok"] is True
    assert w_env["id"] == "1"
    assert w_env["namespace"] == "agent_notes"
    # Tag folded into metadata.
    _, doc = host._docs[0]
    assert doc.metadata["tag"] == "preference"
    assert doc.metadata["kind"] == "note"

    # Query-based read.
    r = await dispatch_memory_read(
        json.dumps({"query": "metric"}).encode(), memory_host=host
    )
    r_env = json.loads(r)
    assert r_env["total"] == 1
    assert r_env["results"][0]["content"] == "User prefers metric units"


async def test_read_without_query_uses_recent() -> None:
    host = _StubHost()
    await dispatch_memory_write(
        json.dumps({"content": "fact one"}).encode(), memory_host=host
    )
    await dispatch_memory_write(
        json.dumps({"content": "fact two"}).encode(), memory_host=host
    )
    r = await dispatch_memory_read(b"{}", memory_host=host)
    env = json.loads(r)
    assert env["total"] == 2
    # recent() returns newest-first.
    assert env["results"][0]["content"] == "fact two"


async def test_write_requires_content() -> None:
    host = _StubHost()
    out = await dispatch_memory_write(b"{}", memory_host=host)
    env = json.loads(out)
    assert env["ok"] is False
    assert env["error"] == "content_required"


async def test_write_degrades_without_host() -> None:
    out = await dispatch_memory_write(
        json.dumps({"content": "x"}).encode(), memory_host=None
    )
    env = json.loads(out)
    assert env["ok"] is False
    assert env["note"] == "memory_host_not_configured"


async def test_read_degrades_without_host() -> None:
    out = await dispatch_memory_read(b"{}", memory_host=None)
    env = json.loads(out)
    assert env["total"] == 0
    assert env["note"] == "memory_host_not_configured"
