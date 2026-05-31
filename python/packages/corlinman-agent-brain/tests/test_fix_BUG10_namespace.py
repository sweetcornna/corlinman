"""Repro for BUG-10: IndexSync namespace mismatch.

``node_to_memory_doc`` hardcoded the namespace ``"agent-brain"`` in the upsert
payload, but ``IndexSyncClient.query`` sends ``self._config.namespace``. Under a
custom namespace (e.g. ``tenant-x``) the upsert lands in one namespace while the
query targets another, so the round-trip returns zero hits.
"""

from __future__ import annotations

from typing import Any

import pytest

from corlinman_agent_brain.index_sync import (
    IndexSyncClient,
    IndexSyncConfig,
    node_to_memory_doc,
)
from corlinman_agent_brain.models import (
    KnowledgeNode,
    KnowledgeNodeFrontmatter,
    MemoryKind,
    NodeScope,
    NodeStatus,
    RiskLevel,
)


def _make_node() -> KnowledgeNode:
    fm = KnowledgeNodeFrontmatter(
        id="node-1",
        tenant_id="tenant-x",
        agent_id="agent-1",
        scope=NodeScope.AGENT,
        kind=MemoryKind.CONCEPT,
        status=NodeStatus.ACTIVE,
        confidence=0.9,
        risk=RiskLevel.LOW,
    )
    return KnowledgeNode(
        node_id="node-1",
        title="Hyperdrive calibration procedure",
        path="",
        kind=MemoryKind.CONCEPT,
        frontmatter=fm,
        summary="How to calibrate the hyperdrive.",
    )


class NamespacedStubTransport:
    """Stub MemoryHost that honours namespaces like a real vector index.

    Docs are stored under the namespace declared in their upsert payload, and
    queries only match docs stored under the query's namespace.
    """

    def __init__(self) -> None:
        # namespace -> list of stored docs
        self.store: dict[str, list[dict[str, Any]]] = {}

    async def post(
        self, url: str, *, json_body: dict[str, Any], headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        if url.endswith("/upsert"):
            ns = json_body.get("namespace", "")
            self.store.setdefault(ns, []).append(json_body)
            return 200, {"id": json_body.get("metadata", {}).get("node_id", "x")}
        if url.endswith("/query"):
            ns = json_body.get("namespace", "")
            text = json_body.get("text", "")
            hits = []
            for doc in self.store.get(ns, []):
                if text and text in doc.get("content", ""):
                    hits.append(
                        {
                            "id": doc.get("metadata", {}).get("node_id", ""),
                            "content": doc.get("content", ""),
                            "metadata": doc.get("metadata", {}),
                        }
                    )
            return 200, {"hits": hits}
        return 404, {}

    async def delete(
        self, url: str, *, headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        return 200, {}

    async def get(
        self, url: str, *, headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        return 200, {}


def test_node_to_memory_doc_uses_configured_namespace() -> None:
    """The upsert payload must carry the configured namespace, not a literal."""
    node = _make_node()
    doc = node_to_memory_doc(node, namespace="tenant-x")
    assert doc["namespace"] == "tenant-x"


@pytest.mark.asyncio
async def test_custom_namespace_roundtrip_returns_hit() -> None:
    """Upsert + query under a custom namespace must round-trip to a hit."""
    transport = NamespacedStubTransport()
    config = IndexSyncConfig(namespace="tenant-x", batch_delay_ms=0)
    client = IndexSyncClient(transport, config)

    node = _make_node()
    result = await client.upsert_node(node)
    assert result.action == "upserted"

    # Confirm the doc actually landed in the configured namespace.
    assert "tenant-x" in transport.store
    assert transport.store["tenant-x"][0]["namespace"] == "tenant-x"

    hits = await client.query("Hyperdrive calibration", limit=5)
    assert len(hits) == 1
    assert hits[0].title == "Hyperdrive calibration procedure"
