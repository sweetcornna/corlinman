"""MemoryKernel W1 — observe queue, scoped bi-temporal recall, mode gate."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from corlinman_memory_kernel import (
    KernelScope,
    MemoryKernel,
    Observation,
    kernel_mode,
    now_ms,
)


@pytest.fixture
async def kernel(tmp_path: Path) -> AsyncIterator[MemoryKernel]:
    k = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        yield k
    finally:
        await k.close()


def _obs(session: str = "s1", **kw: object) -> Observation:
    defaults: dict[str, object] = {
        "session_key": session,
        "user_text": "hello",
        "reply_text": "hi there",
        "ts_ms": now_ms(),
    }
    defaults.update(kw)
    return Observation(**defaults)  # type: ignore[arg-type]


# ---- mode gate --------------------------------------------------------------


def test_kernel_mode_default_and_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_MEMORY_KERNEL", raising=False)
    assert kernel_mode() == "shadow"
    for value in ("off", "shadow", "on", "  ON "):
        monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", value)
        assert kernel_mode() == value.strip().lower()
    # A typo cannot silently disable observation accrual.
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "onn")
    assert kernel_mode() == "shadow"


# ---- observe queue ----------------------------------------------------------


async def test_observe_queues_and_drains(kernel: MemoryKernel) -> None:
    a = await kernel.observe(_obs(user_text="I live in Harbin"))
    b = await kernel.observe(_obs(user_text="x" * 10_000))  # truncated
    assert a and b and a != b

    pending = await kernel.pending_observations()
    assert [o.id for o in pending] == [a, b]
    assert len(pending[1].user_text) == 4000

    await kernel.mark_observations_processed([a])
    remaining = await kernel.pending_observations()
    assert [o.id for o in remaining] == [b]

    stats = await kernel.stats()
    assert stats["observations_total"] == 2
    assert stats["observations_pending"] == 1


async def test_observe_carries_binding_fields(kernel: MemoryKernel) -> None:
    await kernel.observe(
        _obs(channel="qq", channel_user_id="10086", persona_id="grantley")
    )
    (obs,) = await kernel.pending_observations()
    assert (obs.channel, obs.channel_user_id, obs.persona_id) == (
        "qq",
        "10086",
        "grantley",
    )


# ---- scoped recall ----------------------------------------------------------


async def test_recall_is_scope_isolated(kernel: MemoryKernel) -> None:
    alice = KernelScope(scope_user_id="alice")
    bob = KernelScope(scope_user_id="bob")
    await kernel.add_item(
        alice, text="favorite tea is oolong", kind="preference", source="turn"
    )

    hits_alice = await kernel.recall(alice, "favorite tea")
    hits_bob = await kernel.recall(bob, "favorite tea")
    assert [h.text for h in hits_alice] == ["favorite tea is oolong"]
    assert hits_bob == [], "cross-user leak"


async def test_recall_shares_agent_and_persona_shared_items(
    kernel: MemoryKernel,
) -> None:
    scope = KernelScope(scope_user_id="alice", persona_id="grantley")
    # Agent-scoped (no user) + persona-shared ('') items are visible.
    await kernel.add_item(
        KernelScope(), text="server timezone is UTC", kind="fact", source="operator"
    )
    # Other-persona item is not.
    await kernel.add_item(
        KernelScope(scope_user_id="alice", persona_id="other"),
        text="secret persona timezone note",
        kind="fact",
        source="turn",
    )
    hits = await kernel.recall(scope, "timezone")
    assert [h.text for h in hits] == ["server timezone is UTC"]


async def test_invalidate_is_bitemporal_not_delete(kernel: MemoryKernel) -> None:
    scope = KernelScope(scope_user_id="alice")
    item_id = await kernel.add_item(
        scope, text="works at Initech", kind="fact", source="turn"
    )
    assert await kernel.invalidate_item(
        item_id, reason="contradiction", by="reconcile"
    )
    # Idempotent: second call is a no-op.
    assert not await kernel.invalidate_item(item_id, reason="again")

    assert await kernel.recall(scope, "Initech") == []
    stats = await kernel.stats()
    assert stats["items"] == 0
    assert stats["items_invalidated"] == 1  # archived, not deleted


async def test_recall_matches_cjk_substrings(kernel: MemoryKernel) -> None:
    """Chinese recall must not require a verbatim whole-string match.

    The trigram tokenizer + sliding CJK trigram query units make a chatty
    question ("我的家乡是哪里") hit a stored fact ("家乡是哈尔滨") on the
    shared 家乡是 substring — with unicode61 the whole run was one token
    and CJK recall silently never matched.
    """
    scope = KernelScope(scope_user_id="alice")
    await kernel.add_item(
        scope, text="用户的家乡是哈尔滨", kind="fact", source="turn"
    )
    await kernel.add_item(
        scope, text="喜欢喝乌龙茶", kind="preference", source="turn"
    )

    hits = await kernel.recall(scope, "我的家乡是哪里?")
    assert [h.text for h in hits] == ["用户的家乡是哈尔滨"]

    # Mixed CJK + ASCII in one query.
    hits = await kernel.recall(scope, "乌龙茶 or coffee?")
    assert [h.text for h in hits] == ["喜欢喝乌龙茶"]


def test_fts_match_query_units() -> None:
    from corlinman_memory_kernel.kernel import _fts_match_query

    # Plain English: quoted units joined with OR.
    assert _fts_match_query("lazy fox") == '"lazy" OR "fox"'
    # CJK runs become sliding trigram phrases.
    assert _fts_match_query("家乡是哪里") == '"家乡是" OR "乡是哪" OR "是哪里"'
    # Short CJK runs (< 3 chars) pass through as-is.
    assert _fts_match_query("家乡") == '"家乡"'
    # Embedded quotes are doubled; quote-only tokens vanish.
    assert _fts_match_query('say "hi"') == '"say" OR """hi"""'
    assert _fts_match_query('"') == ""
    assert _fts_match_query("  ") == ""


async def test_recall_escapes_fts_operators(kernel: MemoryKernel) -> None:
    scope = KernelScope(scope_user_id="alice")
    await kernel.add_item(
        scope, text="deploy failed with error code 137", kind="event", source="turn"
    )
    hits = await kernel.recall(scope, "deploy - error:")
    assert len(hits) == 1
    assert hits[0].score > 0.0
    assert await kernel.recall(scope, '"""') == []


async def test_cohabits_with_legacy_memory_host(tmp_path: Path) -> None:
    """The kernel's mk_* DDL must coexist with LocalSqliteHost's schema in
    the SAME memory.sqlite file — both handles read/write concurrently."""
    from corlinman_memory_host import LocalSqliteHost, MemoryDoc, MemoryQuery

    path = tmp_path / "memory.sqlite"
    host = await LocalSqliteHost.open("local", path)
    kernel = await MemoryKernel.open(path)
    try:
        await host.upsert(MemoryDoc(content="legacy turn dump", namespace="s1"))
        await kernel.observe(_obs())
        await kernel.add_item(
            KernelScope(), text="kernel fact", kind="fact", source="turn"
        )

        legacy_hits = await host.query(MemoryQuery(text="legacy", top_k=5))
        assert len(legacy_hits) == 1
        kernel_hits = await kernel.recall(KernelScope(), "kernel fact")
        assert len(kernel_hits) == 1
    finally:
        await kernel.close()
        await host.close()
