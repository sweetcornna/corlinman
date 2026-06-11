"""Model routing — send simple/utility work to a cheaper model.

claude-code's two routing ideas, ported:

* ``getSmallFastModel()`` — utility subtasks (session-title generation,
  window summaries) always use a configured cheap model.
* runtime main-loop routing — when the operator opts in
  (``[console].auto_route``), a *deterministic* classifier sends simple
  turns to the small model. No LLM is burned on classification, and an
  explicit user choice (``--model`` / ``/model``) always wins — routing
  never overrides the human (claude-code rule).

Config (``config.toml``)::

    [console]
    small_fast_model = "gpt-4o-mini"   # registry-resolvable id or alias
    auto_route = false
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["ModelRouter", "RouteDecision", "classify_complexity"]

# Markers that indicate real work: code, files, multi-step instructions.
_CODE_FENCE = re.compile(r"```")
_PATH_LIKE = re.compile(r"(^|[\s'\"(])(/|\./|~/)[\w.\-/]+", re.MULTILINE)
_MULTI_STEP = re.compile(
    r"\b(first|then|after that|step \d|finally)\b|然后|接着|第[一二三四五六七八九十]步|最后",
    re.IGNORECASE,
)
_WORK_VERBS = re.compile(
    r"\b(implement|refactor|debug|fix|build|deploy|analy[sz]e|migrate|optimi[sz]e|"
    r"review|test|write code|design)\b|实现|重构|调试|修复|部署|分析|迁移|优化|审查|写代码|设计",
    re.IGNORECASE,
)

_SIMPLE_MAX_CHARS = 240
_SIMPLE_MAX_LINES = 3


def classify_complexity(text: str) -> str:
    """``"simple"`` or ``"complex"`` — deterministic, conservative.

    Anything that *might* need tools, code, or multi-step reasoning is
    complex; only short, single-intent, marker-free prompts route small.
    False-"complex" costs a few tokens; false-"simple" costs answer
    quality — so every ambiguous signal escalates.
    """
    stripped = text.strip()
    if len(stripped) > _SIMPLE_MAX_CHARS:
        return "complex"
    if stripped.count("\n") + 1 > _SIMPLE_MAX_LINES:
        return "complex"
    if _CODE_FENCE.search(stripped) is not None:
        return "complex"
    if _PATH_LIKE.search(stripped) is not None:
        return "complex"
    if _MULTI_STEP.search(stripped) is not None:
        return "complex"
    if _WORK_VERBS.search(stripped) is not None:
        return "complex"
    return "simple"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Outcome of one routing decision, with the why for the status line."""

    model: str
    reason: str  # "explicit" | "auto:simple" | "default"


class ModelRouter:
    """Per-session routing state."""

    def __init__(
        self,
        *,
        default_model: str,
        small_fast_model: str | None = None,
        auto_route: bool = False,
    ) -> None:
        self.default_model = default_model
        self.small_fast_model = small_fast_model
        self.auto_route = auto_route

    @classmethod
    def from_config(cls, config: dict, *, default_model: str) -> ModelRouter:
        """Build from a parsed ``config.toml`` dict (``[console]`` block)."""
        console_cfg = config.get("console") if isinstance(config, dict) else None
        if not isinstance(console_cfg, dict):
            console_cfg = {}
        small = console_cfg.get("small_fast_model")
        return cls(
            default_model=default_model,
            small_fast_model=str(small) if isinstance(small, str) and small else None,
            auto_route=bool(console_cfg.get("auto_route", False)),
        )

    def route_turn(self, text: str, *, explicit_model: str | None = None) -> RouteDecision:
        """Pick the model for a user turn."""
        if explicit_model:
            return RouteDecision(model=explicit_model, reason="explicit")
        if (
            self.auto_route
            and self.small_fast_model
            and classify_complexity(text) == "simple"
        ):
            return RouteDecision(model=self.small_fast_model, reason="auto:simple")
        return RouteDecision(model=self.default_model, reason="default")

    def utility_model(self) -> str:
        """Model for internal utility calls (title gen, summaries) —
        always the small one when configured."""
        return self.small_fast_model or self.default_model
