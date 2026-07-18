"""Per-model-family reasoning-effort tier registry.

Single source of truth for "which thinking depths does this model actually
accept". The admin models API decorates each alias with the resolved
model's tier list (so the web composer renders the real options instead of
a hardcoded three) and the provider adapters use the same table to
translate + clamp the canonical tier onto each vendor's wire format.

Canonical tier vocabulary (superset across vendors, low → high):

    none < minimal < low < on ≈ medium < high < xhigh < max

``on`` exists for pure-toggle families (GLM-4.x, Kimi k2.x) where the only
choice is thinking on/off at provider-default depth.

Sources (verified 2026-07-18): OpenAI reasoning guide + latest-model notes,
Google Gemini 3 / thinking docs, xAI reasoning docs, DeepSeek thinking_mode,
Alibaba Model Studio deep-thinking, Z.AI thinking-mode, Moonshot platform
docs, Anthropic API reference. Patterns match on the *resolved* upstream
model id (relay aliases must be resolved by the caller first) and tolerate
date/channel suffixes like ``-2025-12-11`` or ``-sol``.
"""

from __future__ import annotations

import re

__all__ = [
    "CANONICAL_REASONING_TIERS",
    "clamp_reasoning_tier",
    "reasoning_tiers_for_model",
]

#: Every canonical tier, in clamp order.
CANONICAL_REASONING_TIERS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "on",
    "medium",
    "high",
    "xhigh",
    "max",
)

#: Rank used for nearest-tier clamping. ``on`` sits beside ``medium`` so a
#: graded request lands on the toggle (and vice versa) instead of dropping.
_TIER_RANK: dict[str, int] = {
    "none": 0,
    "minimal": 1,
    "low": 2,
    "on": 3,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 6,
}

_FIVE_TIER_CLAUDE = ("low", "medium", "high", "xhigh", "max")
_GEMINI_LEVELS = ("minimal", "low", "medium", "high")

#: (pattern, tiers, default) — first match wins, so keep the more specific
#: generation patterns above their family catch-alls. An EMPTY tier tuple
#: means "this family is known to have no effort knob" (the parameter is
#: dropped); an id matching NO row at all is passed through untouched so
#: unknown relay models keep working.
_FAMILY_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...], str | None], ...] = tuple(
    (re.compile(p), tiers, default)
    for p, tiers, default in (
        # ── OpenAI ────────────────────────────────────────────────────
        # gpt-5.6 introduced `max` (and the standard/pro request modes).
        (r"gpt-5\.6", ("none", "low", "medium", "high", "xhigh", "max"), "medium"),
        (r"gpt-5\.2-pro", ("medium", "high", "xhigh"), "high"),
        # 5.2 / 5.4 / 5.5 (incl. -codex variants): xhigh generation.
        (r"gpt-5\.[245]", ("none", "low", "medium", "high", "xhigh"), "medium"),
        (r"gpt-5\.1-codex-max", ("low", "medium", "high", "xhigh"), "medium"),
        (r"gpt-5\.1-codex", ("low", "medium", "high"), "medium"),
        # 5.1 replaced gpt-5's `minimal` with `none`.
        (r"gpt-5\.1", ("none", "low", "medium", "high"), "medium"),
        (r"gpt-5-pro", ("high",), "high"),
        # First-gen gpt-5 / -mini / -nano (not the dotted successors).
        (r"gpt-5(?![.\d])", ("minimal", "low", "medium", "high"), "medium"),
        (r"(?:^|[/_-])o[134](?:$|[.-])", ("low", "medium", "high"), "medium"),
        (r"codex", ("low", "medium", "high", "xhigh"), "medium"),
        # ── Anthropic (output_config.effort; 4.6+ only) ───────────────
        (r"claude-(?:fable|mythos)", _FIVE_TIER_CLAUDE, "high"),
        (r"claude-opus-4-[78]", _FIVE_TIER_CLAUDE, "high"),
        (r"claude-sonnet-5", _FIVE_TIER_CLAUDE, "high"),
        (r"claude-(?:opus|sonnet)-4-6", ("low", "medium", "high", "max"), "high"),
        # budget_tokens era (4.5 and below, all haiku): no effort knob.
        (r"claude-(?:opus|sonnet)-4-5", (), None),
        (r"claude-(?:haiku|3)", (), None),
        # Forward-compat: unrecognised newer claude gets the full ladder.
        (r"claude-", _FIVE_TIER_CLAUDE, "high"),
        # ── Google Gemini ─────────────────────────────────────────────
        (r"gemini-3-pro", ("low", "high"), "high"),
        (r"gemini-3-flash", _GEMINI_LEVELS, "high"),
        (r"gemini-3\.1-pro", ("low", "medium", "high"), "high"),
        (r"gemini-3\.1-flash-lite", _GEMINI_LEVELS, "minimal"),
        (r"gemini-3\.1-flash", _GEMINI_LEVELS, "medium"),
        (r"gemini-3\.5-flash", _GEMINI_LEVELS, "medium"),
        # 2.5: numeric thinkingBudget; Pro can't disable thinking.
        (r"gemini-2\.5-pro", ("low", "medium", "high"), "medium"),
        (r"gemini-2\.5-flash", ("none", "low", "medium", "high"), "medium"),
        (r"gemini-[3-9]", ("low", "medium", "high"), "high"),
        (r"gemini-", (), None),
        # ── xAI ───────────────────────────────────────────────────────
        (r"grok-3-mini", ("low", "high"), "low"),
        (r"grok-4\.5", ("low", "medium", "high"), "high"),
        (r"grok-4\.20", ("low", "medium", "high", "xhigh"), "high"),
        # grok-4 / 4.1: always-on reasoning, the parameter 400s.
        (r"grok-", (), None),
        # ── DeepSeek (V4: thinking.type + effort high|max) ────────────
        (r"deepseek-(?:v4|chat|reasoner)", ("none", "high", "max"), "high"),
        (r"deepseek", (), None),
        # ── Qwen (enable_thinking bool + thinking_budget) ─────────────
        (r"qwq|qwen3-[\w.-]*thinking", ("low", "medium", "high"), "high"),
        (
            r"qwen3[.\d-]|qwen3-max|qwen-plus|qwen-flash|qwen-turbo",
            ("none", "low", "medium", "high"),
            "medium",
        ),
        (r"qwen", (), None),
        # ── Zhipu GLM ─────────────────────────────────────────────────
        (r"glm-5", ("none", "high", "max"), "max"),
        (r"glm-4\.[567]", ("none", "on"), "on"),
        (r"glm", (), None),
        # ── Moonshot Kimi ─────────────────────────────────────────────
        (r"kimi-k2\.[56]", ("none", "on"), "on"),
        # k2.7-code / k3 / k2-thinking: thinking locked on, no knob.
        (r"kimi|moonshot", (), None),
    )
)


def _normalise(model: str) -> str:
    """Lowercase + strip a vendor path prefix (``openai/gpt-5.6`` → ``gpt-5.6``)."""
    id_ = (model or "").strip().lower()
    if "/" in id_:
        id_ = id_.rsplit("/", 1)[-1]
    return id_


def reasoning_tiers_for_model(
    model: str,
) -> tuple[tuple[str, ...] | None, str | None]:
    """Return ``(tiers, default)`` for a resolved model id.

    ``((), None)`` means the family is known to carry no effort knob;
    ``(None, None)`` means the id is unknown — callers should pass any
    requested value through untouched and hide the picker.
    """
    id_ = _normalise(model)
    if not id_:
        return (), None
    for pattern, tiers, default in _FAMILY_RULES:
        if pattern.search(id_):
            return tiers, default
    return None, None


def clamp_reasoning_tier(model: str, requested: str) -> str | None:
    """Snap ``requested`` onto the model's supported ladder.

    Returns the nearest supported tier (ties resolve downward, the
    cost-conservative side), the request untouched for unknown models, or
    ``None`` when the family has no knob (callers drop the parameter).
    """
    req = (requested or "").strip().lower()
    if req not in _TIER_RANK:
        return None
    tiers, _default = reasoning_tiers_for_model(model)
    if tiers is None:
        return req
    if not tiers:
        return None
    if req in tiers:
        return req
    want = _TIER_RANK[req]
    best = min(
        tiers,
        key=lambda t: (abs(_TIER_RANK[t] - want), _TIER_RANK[t]),
    )
    return best
