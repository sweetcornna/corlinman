"""Memory W3 — kernel injection (mode=on), ranked recall, budgets, canary.

``CORLINMAN_MEMORY_KERNEL=on`` (or ``[memory.kernel] mode``) flips the
kernel lane from shadow logging to real prompt injection: core-memory
blocks + a ranked, char-budgeted, untrusted-framed recall block. The
legacy recency/notes lanes stay untouched (additive, never replacing).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_memory_kernel import KernelScope, MemoryKernel
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    def __init__(self) -> None:  # pragma: no cover
        pass


class _NullHost:
    async def recent(self, session_key: str, limit: int) -> list[Any]:
        return []

    async def query(self, req: Any) -> list[Any]:
        return []

    async def upsert(self, doc: Any) -> str:
        return "1"


def _servicer(kernel: MemoryKernel, **extra: Any) -> CorlinmanAgentServicer:
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())
    servicer.set_app_state(
        SimpleNamespace(
            memory_host=_NullHost(),
            memory_kernel=kernel,
            identity_resolver=None,
            **extra,
        )
    )
    return servicer


def _start(sender: str = "10086") -> Any:
    from corlinman_agent.reasoning_loop import ChatStart

    start = ChatStart(
        model="m",
        messages=[{"role": "user", "content": "what tea does the user like"}],
        session_key="s1",
    )
    start.extra = {"binding": {"channel": "qq", "sender": sender}}
    return start


async def _drain(servicer: CorlinmanAgentServicer) -> None:
    for _ in range(100):
        if not servicer._prefetch_tasks:
            return
        await asyncio.sleep(0.01)


# Scope key for the fail-open (no resolver) path used by these tests.
_SCOPE = KernelScope(scope_user_id="qq:10086")


async def test_on_mode_injects_ranked_memory_and_bookkeeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "on")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    item_id = await kernel.add_item(
        _SCOPE, text="user likes oolong tea", kind="preference", source="turn"
    )
    servicer = _servicer(kernel)
    try:
        start = _start()
        await servicer._recall_memory(start)
        await _drain(servicer)

        joined = " ".join(str(m.get("content", "")) for m in start.messages)
        assert "user likes oolong tea" in joined
        assert "DATA, not instructions" in joined

        # Bookkeeping: recall_count stamped + ledger row written.
        hits = await kernel.recall(_SCOPE, "oolong tea")
        assert hits and hits[0].id == item_id
        async with kernel._conn.execute(  # noqa: SLF001
            "SELECT recall_count FROM mk_items WHERE id = ?", (item_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None and row[0] == 1
        async with kernel._conn.execute(  # noqa: SLF001
            "SELECT lane, rank FROM mk_recall_ledger WHERE item_id = ?",
            (item_id,),
        ) as cur:
            ledger = await cur.fetchall()
        assert [(r["lane"], r["rank"]) for r in ledger] == [("kernel", 1)]
    finally:
        await servicer.aclose()
        await kernel.close()


async def test_on_mode_injects_core_blocks_stably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "on")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    async with kernel._lock:  # noqa: SLF001 — no writer API until W5
        await kernel._conn.execute(  # noqa: SLF001
            "INSERT INTO mk_core(tenant_id, scope_user_id, persona_id, block,"
            " content, updated_at_ms) VALUES ('default', 'qq:10086', '',"
            " 'user_profile', 'Tea enthusiast from Harbin', 1)"
        )
        await kernel._conn.commit()  # noqa: SLF001
    servicer = _servicer(kernel)
    try:
        start = _start()
        await servicer._recall_memory(start)
        joined = " ".join(str(m.get("content", "")) for m in start.messages)
        assert "Core memory" in joined
        assert "Tea enthusiast from Harbin" in joined
    finally:
        await servicer.aclose()
        await kernel.close()


async def test_on_channels_canary_narrows_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "on")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    await kernel.add_item(
        _SCOPE, text="user likes oolong tea", kind="preference", source="turn"
    )
    servicer = _servicer(
        kernel, memory_kernel_config={"on_channels": ["telegram"]}
    )
    try:
        start = _start()  # qq turn — not in the canary list
        await servicer._recall_memory(start)
        await _drain(servicer)
        joined = " ".join(str(m.get("content", "")) for m in start.messages)
        assert "oolong" not in joined, "canary must keep qq in shadow"
    finally:
        await servicer.aclose()
        await kernel.close()


async def test_shadow_mode_still_never_injects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "shadow")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    await kernel.add_item(
        _SCOPE, text="user likes oolong tea", kind="preference", source="turn"
    )
    servicer = _servicer(kernel)
    try:
        start = _start()
        before = [dict(m) for m in start.messages]
        await servicer._recall_memory(start)
        await _drain(servicer)
        assert [dict(m) for m in start.messages] == before
    finally:
        await servicer.aclose()
        await kernel.close()


async def test_ranked_recall_orders_by_blend(tmp_path: Path) -> None:
    """Trust/importance move the needle: a high-trust important fact
    outranks a low-trust one with the same textual relevance."""
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        await kernel.add_item(
            _SCOPE,
            text="tea preference: gunpowder green tea",
            kind="fact",
            source="turn",
            trust=0.1,
            importance=0.1,
        )
        strong = await kernel.add_item(
            _SCOPE,
            text="tea preference: oolong tea",
            kind="fact",
            source="turn",
            trust=0.9,
            importance=0.9,
        )
        hits = await kernel.recall_ranked(_SCOPE, "tea preference", top_k=2)
        assert next(h.id for h in hits) == strong

        # risk=high items never surface in ranked recall.
        await kernel.add_item(
            _SCOPE,
            text="tea secret risky note",
            kind="fact",
            source="turn",
            risk="high",
        )
        hits = await kernel.recall_ranked(_SCOPE, "tea secret risky", top_k=5)
        assert all("risky" not in h.text for h in hits)
    finally:
        await kernel.close()


async def test_high_risk_does_not_starve_candidate_pool(
    tmp_path: Path,
) -> None:
    """risk=high rows are filtered IN the candidate query, so a pile of
    quarantined matches can't crowd a legitimate one out of the pool."""
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        for i in range(40):
            await kernel.add_item(
                _SCOPE,
                text=f"oolong tea quarantined note {i}",
                kind="fact",
                source="turn",
                risk="high",
            )
        safe = await kernel.add_item(
            _SCOPE, text="oolong tea safe note", kind="fact", source="turn"
        )
        hits = await kernel.recall_ranked(_SCOPE, "oolong tea", top_k=4)
        assert [h.id for h in hits] == [safe]
    finally:
        await kernel.close()


async def test_core_blocks_shared_persona_visible_and_overridable(
    tmp_path: Path,
) -> None:
    """Shared (persona='') core blocks surface for persona-bound turns;
    a persona-specific block of the same name wins."""
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        async with kernel._lock:  # noqa: SLF001 — no writer API until W5
            for persona, block, content in (
                ("", "user_profile", "shared profile"),
                ("grantley", "user_profile", "grantley view of profile"),
                ("", "open_threads", "shared threads"),
            ):
                await kernel._conn.execute(  # noqa: SLF001
                    "INSERT INTO mk_core(tenant_id, scope_user_id,"
                    " persona_id, block, content, updated_at_ms)"
                    " VALUES ('default', 'qq:10086', ?, ?, ?, 1)",
                    (persona, block, content),
                )
            await kernel._conn.commit()  # noqa: SLF001

        bound = await kernel.core_blocks(
            KernelScope(scope_user_id="qq:10086", persona_id="grantley")
        )
        assert bound == [
            ("open_threads", "shared threads"),
            ("user_profile", "grantley view of profile"),
        ]
        unbound = await kernel.core_blocks(
            KernelScope(scope_user_id="qq:10086")
        )
        assert unbound == [
            ("open_threads", "shared threads"),
            ("user_profile", "shared profile"),
        ]
    finally:
        await kernel.close()


async def test_vector_branch_rrf_merges_with_fts(tmp_path: Path) -> None:
    """query_vector engages the cosine branch: an embedded item that the
    FTS query can't match (no shared tokens) still surfaces via RRF."""
    from corlinman_memory_kernel import encode_f32

    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        semantic = await kernel.add_item(
            _SCOPE, text="喜欢喝铁观音", kind="preference", source="turn"
        )
        await kernel.add_item(
            _SCOPE, text="tea preference oolong", kind="fact", source="turn"
        )
        async with kernel._lock:  # noqa: SLF001 — embeddings wired in W5
            await kernel._conn.execute(  # noqa: SLF001
                "UPDATE mk_items SET embedding = ?, embedding_dim = 3"
                " WHERE id = ?",
                (encode_f32([1.0, 0.0, 0.0]), semantic),
            )
            await kernel._conn.commit()  # noqa: SLF001

        hits = await kernel.recall_ranked(
            _SCOPE,
            "tea preference",  # FTS matches only the English item
            top_k=4,
            query_vector=[0.9, 0.1, 0.0],
        )
        ids = [h.id for h in hits]
        assert semantic in ids, "cosine branch must contribute candidates"
        assert len(ids) == 2
    finally:
        await kernel.close()


async def test_max_chars_budget_truncates_by_whole_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "on")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    for i in range(4):
        await kernel.add_item(
            _SCOPE,
            text=f"oolong tea fact number {i} " + "x" * 80,
            kind="fact",
            source="turn",
        )
    servicer = _servicer(
        kernel,
        memory_recall_config={"max_chars": 150, "notes_top_k": 4},
    )
    try:
        start = _start()
        await servicer._recall_memory(start)
        await _drain(servicer)
        joined = " ".join(str(m.get("content", "")) for m in start.messages)
        injected = [
            line for line in joined.split("- [") if "oolong tea fact" in line
        ]
        assert 1 <= len(injected) < 4, "budget must drop whole trailing items"
    finally:
        await servicer.aclose()
        await kernel.close()
