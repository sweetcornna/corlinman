"""W6 EPA affect lens — axes, projection, resonance, mood state."""

from __future__ import annotations

from pathlib import Path

from corlinman_memory_kernel import (
    KernelScope,
    MemoryKernel,
    affect_from_embedding,
    build_anchors,
    resonance,
)
from corlinman_memory_kernel.affect import ANCHOR_WORDS, REPAIR_THRESHOLD_E

# Deterministic toy embedding space: axis 0 = valence, 1 = potency,
# 2 = activity. Word lists map onto the axes by membership.
_POS = {w for w, _ in [(w, None) for w in ANCHOR_WORDS["e"][0]]}


def _toy_embed_word(word: str) -> list[float]:
    e_pos, e_neg = ANCHOR_WORDS["e"]
    p_pos, p_neg = ANCHOR_WORDS["p"]
    a_pos, a_neg = ANCHOR_WORDS["a"]
    return [
        1.0 if word in e_pos else -1.0 if word in e_neg else 0.0,
        1.0 if word in p_pos else -1.0 if word in p_neg else 0.0,
        1.0 if word in a_pos else -1.0 if word in a_neg else 0.0,
    ]


async def _toy_embed(text: str) -> list[float]:
    return _toy_embed_word(text)


async def test_anchors_and_projection_roundtrip() -> None:
    anchors = await build_anchors(_toy_embed)
    assert anchors is not None
    happy = affect_from_embedding([1.0, 0.0, 0.0], anchors)
    sad = affect_from_embedding([-1.0, 0.0, 0.0], anchors)
    neutral = affect_from_embedding([0.0, 0.0, 0.0], anchors)
    assert happy.e > 0.5 and sad.e < -0.5
    assert happy.salience > 0.5
    assert neutral.salience == 0.0


async def test_anchors_none_when_embed_unavailable() -> None:
    async def broken(_text: str) -> None:
        return None

    assert await build_anchors(broken) is None


def test_resonance_congruence_and_flashbulb() -> None:
    sad_item = (-0.8, 0.0, 0.2, 0.8)
    happy_item = (0.8, 0.0, 0.2, 0.8)
    neutral_item = (0.0, 0.0, 0.0, 0.0)
    mild_sad_mood = (-0.4, 0.0, 0.0)

    # Mood congruence: sad mood resonates more with sad memories.
    assert resonance(mild_sad_mood, sad_item) > resonance(
        mild_sad_mood, happy_item
    )
    # Salience gates: neutral memories never resonate.
    assert resonance(mild_sad_mood, neutral_item) == 0.0
    # Flashbulb floor: salient memories resonate a little regardless.
    assert resonance((0.0, 0.0, 0.0), happy_item) > 0.0


def test_resonance_mood_repair_flips_deep_negative() -> None:
    sad_item = (-0.8, 0.0, 0.2, 0.8)
    happy_item = (0.8, 0.0, 0.2, 0.8)
    deep_sad = (REPAIR_THRESHOLD_E - 0.1, 0.0, 0.0)
    # Anti-spiral: deep sadness prefers POSITIVE memories.
    assert resonance(deep_sad, happy_item) > resonance(deep_sad, sad_item)


async def test_mood_ema_and_persistence(tmp_path: Path) -> None:
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        assert await kernel.get_affect_state("grantley") == (0.0, 0.0, 0.0)
        mood = await kernel.update_affect_state(
            "grantley", (1.0, 0.0, 0.0), alpha=0.1
        )
        assert abs(mood[0] - 0.1) < 1e-9  # one nudge, not a yank
        for _ in range(50):
            mood = await kernel.update_affect_state(
                "grantley", (1.0, 0.0, 0.0), alpha=0.1
            )
        assert mood[0] > 0.9  # converges under sustained affect
        # Personas are independent.
        assert await kernel.get_affect_state("other") == (0.0, 0.0, 0.0)
    finally:
        await kernel.close()


async def test_affect_stamp_reaches_ranked_recall(tmp_path: Path) -> None:
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        scope = KernelScope(scope_user_id="u1")
        sad = await kernel.add_item(
            scope, text="rainy day event note", kind="event", source="turn"
        )
        happy = await kernel.add_item(
            scope, text="sunny day event note", kind="event", source="turn"
        )
        await kernel.set_affect(sad, -0.8, 0.0, 0.2, 0.8)
        await kernel.set_affect(happy, 0.8, 0.0, 0.2, 0.8)

        hits = await kernel.recall_ranked(
            scope,
            "day event note",
            top_k=1,
            weights={"w_aff": 1.5},
            mood=(-0.4, 0.0, 0.0),
        )
        assert [h.id for h in hits] == [sad]  # congruence
        hits = await kernel.recall_ranked(
            scope,
            "day event note",
            top_k=1,
            weights={"w_aff": 1.5},
            mood=(-0.9, 0.0, 0.0),
        )
        assert [h.id for h in hits] == [happy]  # repair
    finally:
        await kernel.close()
