"""EPA affect lens — emotion as a first-class recall signal (W6, N1).

Human memory is mood-dependent: emotionally salient events are easier to
recall (flashbulb effect) and the current mood biases which memories
surface (mood congruence). No mainstream memory system models this;
corlinman can, because every memory item can carry an Osgood EPA vector
(Evaluation: pleasant↔unpleasant, Potency: strong↔weak, Activity:
excited↔calm) and every persona carries a live mood in the same space.

Affect is stamped with ZERO LLM calls via the semantic-differential
trick: embed small antonym word sets once, take normalized mean
differences as fixed axes, then any text's affect is its embedding's
cosine against each axis. Salience gates everything — affect-diffuse
text (code, logistics) gets ≈0 and is untouched by the affect term.

The ranking term is bounded and reversible (weight 0 restores classic
ranking), and carries a **mood-repair guard**: when the persona's mood
is strongly negative, congruence flips toward positively-valenced
memories — matching the human mood-repair bias and preventing the
depressive spiral (sad → recalls sad → sadder) that naive mood
congruence would create.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from corlinman_memory_kernel.vector import cosine

#: Antonym sets defining the three Osgood axes in embedding space.
#: Small on purpose: anchors are direction estimates, not classifiers.
ANCHOR_WORDS: dict[str, tuple[list[str], list[str]]] = {
    "e": (
        ["good", "pleasant", "kind", "happy", "wonderful", "开心", "快乐", "美好"],
        ["bad", "painful", "cruel", "sad", "terrible", "难过", "痛苦", "糟糕"],
    ),
    "p": (
        ["strong", "dominant", "confident", "powerful", "坚强", "自信"],
        ["weak", "helpless", "timid", "powerless", "无助", "软弱"],
    ),
    "a": (
        ["excited", "frantic", "energetic", "lively", "兴奋", "激动"],
        ["calm", "sleepy", "still", "quiet", "平静", "困倦"],
    ),
}

#: Below this |cosine| an axis reading is treated as noise.
_AXIS_NOISE_FLOOR = 0.02

#: Mood-repair threshold: at or below this evaluation the congruence
#: term flips toward positive memories instead of amplifying negatives.
REPAIR_THRESHOLD_E = -0.6


@dataclass(frozen=True)
class AffectAnchors:
    """The three fixed axes, each a unit-ish embedding-space direction."""

    e: list[float]
    p: list[float]
    a: list[float]


@dataclass(frozen=True)
class Affect:
    e: float
    p: float
    a: float
    salience: float


EmbedFn = Callable[[str], Awaitable[list[float] | None]]


def _mean(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    dim = len(vectors[0])
    return [sum(v[i] for v in vectors) / n for i in range(dim)]


def _diff_axis(pos: list[list[float]], neg: list[list[float]]) -> list[float]:
    mp, mn = _mean(pos), _mean(neg)
    return [a - b for a, b in zip(mp, mn, strict=True)]


async def build_anchors(embed_fn: EmbedFn) -> AffectAnchors | None:
    """Embed the antonym sets once and derive the three axes.

    ~40 short embedding calls, done once per process (callers cache the
    result). Returns None when the embed seam is unavailable or any
    word fails to embed — affect then simply stays off.
    """
    axes: dict[str, list[float]] = {}
    for axis, (pos_words, neg_words) in ANCHOR_WORDS.items():
        pos: list[list[float]] = []
        neg: list[list[float]] = []
        for word in pos_words:
            vec = await embed_fn(word)
            if not vec:
                return None
            pos.append(list(vec))
        for word in neg_words:
            vec = await embed_fn(word)
            if not vec:
                return None
            neg.append(list(vec))
        axes[axis] = _diff_axis(pos, neg)
    return AffectAnchors(e=axes["e"], p=axes["p"], a=axes["a"])


def affect_from_embedding(
    embedding: list[float], anchors: AffectAnchors
) -> Affect:
    """Project a text embedding onto the EPA axes.

    Salience = the strongest |axis| reading — affect-diffuse text scores
    near zero on every axis and is effectively exempt from the affect
    ranking term.
    """
    e = cosine(embedding, anchors.e)
    p = cosine(embedding, anchors.p)
    a = cosine(embedding, anchors.a)
    e = 0.0 if abs(e) < _AXIS_NOISE_FLOOR else e
    p = 0.0 if abs(p) < _AXIS_NOISE_FLOOR else p
    a = 0.0 if abs(a) < _AXIS_NOISE_FLOOR else a
    salience = max(abs(e), abs(p), abs(a))
    return Affect(e=e, p=p, a=a, salience=salience)


def resonance(
    mood: tuple[float, float, float],
    item: tuple[float, float, float, float],
    *,
    congruence: float = 0.6,
) -> float:
    """Emotional resonance of one memory given the current mood, in [0, 1].

    ``salience`` (item[3]) scales everything: neutral memories resonate 0
    regardless of mood. The congruence half rewards mood-aligned affect;
    the constant half is the flashbulb term (salient memories are always
    a bit easier to recall).

    Mood repair: when mood evaluation ≤ REPAIR_THRESHOLD_E, the mood's E
    component is mirrored positive before the congruence dot — a deeply
    sad persona preferentially resurfaces positive memories instead of
    spiralling.
    """
    e, p, a, salience = item
    if salience <= 0.0:
        return 0.0
    me, mp, ma = mood
    if me <= REPAIR_THRESHOLD_E:
        me = abs(me)
    congruence_score = cosine([me, mp, ma], [e, p, a])
    base = (1.0 - congruence) + congruence * max(congruence_score, 0.0)
    return max(0.0, min(1.0, salience * base))
