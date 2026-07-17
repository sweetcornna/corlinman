"""W7 — textsim attribution + kernel trust-verdict primitives."""

from __future__ import annotations

from pathlib import Path

from corlinman_memory_kernel import KernelScope, LedgerEntry, MemoryKernel
from corlinman_memory_kernel.textsim import attribute_reply, jaccard


def test_attribute_reply_tiers() -> None:
    memory = "用户的家乡是哈尔滨"
    # Reply restating the memory → used.
    assert attribute_reply("你的家乡是哈尔滨,冬天很美", memory)[0] == "used"
    # Unrelated reply → ignored.
    assert attribute_reply("今天天气不错,适合散步", memory)[0] == "ignored"
    # Engaged-but-negating reply → ambiguous, never auto-used.
    assert (
        attribute_reply("你的家乡不是哈尔滨,其实是长春", memory)[0]
        == "ambiguous"
    )
    # English negation cue.
    assert (
        attribute_reply(
            "actually the user works at Globex, not at Initech anymore",
            "user works at Initech",
        )[0]
        == "ambiguous"
    )


def test_jaccard_cjk_bigrams() -> None:
    # Different Chinese sentences must NOT score as identical singletons.
    assert jaccard("我爱吃苹果", "他讨厌香蕉") < 0.2
    assert jaccard("用户的家乡是哈尔滨", "用户的家乡是哈尔滨") == 1.0


async def _seed_injection(
    kernel: MemoryKernel, *, text: str, turn_key: str = "s1:1000"
) -> tuple[str, int]:
    scope = KernelScope(scope_user_id="u1")
    item_id = await kernel.add_item(scope, text=text, kind="fact", source="turn")
    await kernel.record_injection(
        turn_key,
        [LedgerEntry(item_id=item_id, lane="kernel", rank=1, score=1.0, shown_chars=10)],
    )
    rows = await kernel.ledger_rows_for_turn(turn_key)
    assert len(rows) == 1
    return item_id, rows[0][0]


async def test_verdicts_move_trust_and_utility(tmp_path: Path) -> None:
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        item_id, ledger_id = await _seed_injection(kernel, text="fact one")
        counts = await kernel.apply_trust_verdicts(
            [(ledger_id, item_id, "used", 0.8, 0)], move_trust=True
        )
        assert counts["used"] == 1
        hits = await kernel.recall(KernelScope(scope_user_id="u1"), "fact one")
        assert hits[0].trust > 0.5 and hits[0].utility > 0.5

        # Verdict recorded → the same rows never re-attribute.
        assert await kernel.ledger_rows_for_turn("s1:1000") == []
    finally:
        await kernel.close()


async def test_trust_floor_invalidates_after_two_contradictions(
    tmp_path: Path,
) -> None:
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        scope = KernelScope(scope_user_id="u1")
        item_id = await kernel.add_item(
            scope, text="dubious claim", kind="fact", source="turn", trust=0.5
        )
        for i, expect_invalidated in ((0, 0), (1, 1)):
            turn = f"s1:{2000 + i}"
            await kernel.record_injection(
                turn,
                [
                    LedgerEntry(
                        item_id=item_id,
                        lane="kernel",
                        rank=1,
                        score=1.0,
                        shown_chars=5,
                    )
                ],
            )
            rows = await kernel.ledger_rows_for_turn(turn)
            counts = await kernel.apply_trust_verdicts(
                [(rows[0][0], item_id, "contradicted", 0.5, 0)],
                move_trust=True,
            )
            assert counts["invalidated"] == expect_invalidated
        # Archived, not deleted; and no longer recallable.
        stats = await kernel.stats()
        assert stats["items_invalidated"] == 1
        assert await kernel.recall(scope, "dubious claim") == []
    finally:
        await kernel.close()


async def test_dry_run_records_verdicts_without_moves(tmp_path: Path) -> None:
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        item_id, ledger_id = await _seed_injection(kernel, text="fact two")
        await kernel.apply_trust_verdicts(
            [(ledger_id, item_id, "contradicted", 0.5, 1)], move_trust=False
        )
        hits = await kernel.recall(KernelScope(scope_user_id="u1"), "fact two")
        assert hits[0].trust == 0.5, "dry run must not move trust"
        # But the verdict IS recorded (telemetry) — row consumed.
        assert await kernel.ledger_rows_for_turn("s1:1000") == []
        async with kernel._conn.execute(  # noqa: SLF001
            "SELECT verdict, verdict_tier FROM mk_recall_ledger WHERE id = ?",
            (ledger_id,),
        ) as cur:
            row = await cur.fetchone()
        assert (row["verdict"], row["verdict_tier"]) == ("contradicted", 1)
    finally:
        await kernel.close()
