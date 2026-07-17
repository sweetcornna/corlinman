"""CJK-aware token similarity + reply attribution (W7 trust loop).

The tokenizer treats spaced scripts word-wise and contiguous CJK runs as
character bigrams — a word-level Jaccard would collapse an unspaced
Chinese sentence into one token and score any two sentences 1-vs-1.
(Moved here from the reconcile builtin so the trust loop and the
reconciler share one implementation.)

``attribute_reply`` is the tier-0 attribution: given the assistant's
reply and one injected memory, decide **used / ignored / ambiguous**
from token overlap plus negation cues. Pure function, zero LLM — the
sampled tier-1 judge only sees the ambiguous slice.
"""

from __future__ import annotations

import re

#: Tier-0 thresholds. Overlap ≥ USED → the reply plausibly drew on the
#: memory; ≤ IGNORED → it clearly didn't; between → ambiguous (judge).
USED_OVERLAP = 0.22
IGNORED_OVERLAP = 0.05

#: Negation/correction cues: HIGH overlap plus one of these near-misses
#: means the reply may be *contradicting* the memory, not using it —
#: always ambiguous, never auto-`used`.
_NEGATION_RE = re.compile(
    # CJK: negated copulas/verbs + correction verbs. 不[住在去来是再对]
    # over-matches phrases like 忍不住 — acceptable: a false cue only
    # routes the row to the judge (one sampled call), never auto-flips.
    r"(?:不[是再对住在去来]|没有|并非|错了|已经不|其实|记错|"
    r"搬[去到家走]|换成|换了|改成|"
    r"\bnot\b|\bno longer\b|\bactually\b|\bincorrect\b|\bwrong\b|"
    r"\brather than\b|\binstead\b|\bmoved\b|\bchanged\b)",
    re.IGNORECASE,
)


def tokens(text: str) -> set[str]:
    out: set[str] = set()
    for word in text.lower().split():
        if any("一" <= ch <= "鿿" for ch in word):
            out.update(word[i : i + 2] for i in range(max(len(word) - 1, 1)))
        else:
            out.add(word)
    return out


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def overlap_containment(reply: str, memory: str) -> float:
    """How much of the MEMORY's content appears in the reply.

    Containment, not symmetric Jaccard: a long reply that fully restates
    a short memory should score ~1.0, which Jaccard would dilute by the
    reply's extra tokens.
    """
    tm = tokens(memory)
    if not tm:
        return 0.0
    tr = tokens(reply)
    return len(tm & tr) / len(tm)


def attribute_reply(reply: str, memory: str) -> tuple[str, float]:
    """Tier-0 verdict for one injected memory: (verdict, score).

    verdict ∈ {"used", "ignored", "ambiguous"}; score is the containment
    overlap that produced it (recorded in the ledger for tuning).
    """
    score = overlap_containment(reply, memory)
    if score >= USED_OVERLAP:
        if _NEGATION_RE.search(reply):
            # The reply engages with the memory's content while negating
            # something — possibly correcting it. Human-grade call: judge.
            return ("ambiguous", score)
        return ("used", score)
    if score <= IGNORED_OVERLAP:
        return ("ignored", score)
    return ("ambiguous", score)
