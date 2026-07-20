"""Production evaluators for prompt/agent-kind declarative hooks (Dim 9).

v1.25.0 shipped the declarative-hooks engine with ``prompt`` and
``agent`` hook *kinds*, but both HookRunner construction sites passed
only ``rule_matcher`` — the evaluators stayed ``None`` and every
prompt/agent hook silently failed open (``_exec_evaluator``'s
``unwired:`` warn-once). This module supplies them:

* :func:`build_prompt_evaluator` — a single-shot LLM judge over an
  injected provider resolver. The hook's ``prompt`` string is the
  judging instruction; the event payload is the evidence; the model
  must answer with a JSON verdict (``{"ok": bool, "reason": str}`` —
  the shape ``_verdict_to_decision`` already accepts). Any failure
  (no model configured, provider error, non-JSON answer) returns
  ``None`` → the engine fails open, unchanged from today.
* :func:`build_agent_evaluator` — wraps the module-level late-binding
  slot (:func:`register_hook_agent_runner`). Agent-kind hooks need an
  out-of-turn subagent entry point; the runtime that owns one (the
  background subagent dispatcher) registers a runner here. Until a
  runner is registered the evaluator returns ``None`` (fail-open with
  the engine's own warn-once) — the seam is one registration call
  away instead of unreachable.

Model choice for the prompt judge, first match wins:

1. ``hooks.evaluator_model`` in the config the caller passes;
2. ``CORLINMAN_HOOK_EVAL_MODEL`` env;
3. the caller-supplied ``default_model`` (typically ``models.default``).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

__all__ = [
    "build_agent_evaluator",
    "build_prompt_evaluator",
    "register_hook_agent_runner",
    "resolve_evaluator_model",
]

#: ``(instruction, payload) -> awaitable verdict`` — the engine's shape.
Evaluator = Callable[[str, dict[str, Any]], Awaitable[Any]]

#: An out-of-turn agent runner: ``(instruction, payload, timeout_s) ->
#: final answer text``. Registered by the runtime that owns a subagent
#: entry point (background dispatcher); consumed by the agent evaluator.
AgentRunner = Callable[[str, dict[str, Any], float], Awaitable[str]]

_JUDGE_SYSTEM = (
    "You are a lifecycle-hook evaluator inside an agent runtime. Judge the "
    "EVENT PAYLOAD against the INSTRUCTION and answer with ONLY a JSON "
    'object: {"ok": true|false, "reason": "<one line>"} — ok=false blocks '
    "the gated action. No prose, no markdown fence."
)

#: Cap the payload evidence so a huge tool result can't blow the judge's
#: context (the payload is evidence, not the subject under edit).
_PAYLOAD_CAP = 8_000

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class _Msg:
    role: str
    content: str


def resolve_evaluator_model(
    config: dict[str, Any] | None, default_model: str = ""
) -> str:
    """The judge model: hooks.evaluator_model > env > default_model."""
    hooks_cfg = (config or {}).get("hooks")
    if isinstance(hooks_cfg, dict):
        raw = hooks_cfg.get("evaluator_model")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    env = os.environ.get("CORLINMAN_HOOK_EVAL_MODEL", "").strip()
    if env:
        return env
    return default_model


def _parse_verdict(text: str) -> dict[str, Any] | None:
    """Best-effort verdict extraction — the first JSON object with an
    ``ok`` key wins; anything else is ``None`` (fail open)."""
    m = _JSON_RE.search(text or "")
    if m is None:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if isinstance(obj, dict) and "ok" in obj:
        return obj
    return None


def build_prompt_evaluator(
    resolve: Callable[[str], tuple[Any, str]],
    model: str,
) -> Evaluator | None:
    """Build the prompt-kind evaluator over a provider resolver.

    ``resolve(model)`` returns ``(provider, upstream_model)`` — each
    call site adapts its own registry/resolver shape. Called lazily per
    hook fire so provider hot-reload is picked up. ``model`` empty →
    ``None`` (nothing to judge with; the engine keeps its unwired
    warn-once).
    """
    if not model:
        return None

    async def _evaluate(instruction: str, payload: dict[str, Any]) -> Any:
        provider, upstream = resolve(model)
        evidence = json.dumps(payload, ensure_ascii=False, default=str)
        if len(evidence) > _PAYLOAD_CAP:
            evidence = evidence[:_PAYLOAD_CAP] + "…(truncated)"
        messages = [
            _Msg("system", _JUDGE_SYSTEM),
            _Msg(
                "user",
                f"INSTRUCTION:\n{instruction}\n\nEVENT PAYLOAD:\n{evidence}",
            ),
        ]
        parts: list[str] = []
        async for chunk in provider.chat_stream(
            model=upstream,
            messages=messages,
            temperature=0.0,
            max_tokens=256,
        ):
            if (
                getattr(chunk, "kind", None) == "token"
                and chunk.text
                and not chunk.is_reasoning
            ):
                parts.append(chunk.text)
        verdict = _parse_verdict("".join(parts))
        if verdict is None:
            logger.warning(
                "hooks.prompt_evaluator.unparseable_verdict", model=model
            )
        return verdict  # None → engine fails open

    return _evaluate


# ---------------------------------------------------------------------------
# Agent-kind — late-binding seam.
# ---------------------------------------------------------------------------

_AGENT_RUNNER: AgentRunner | None = None


def register_hook_agent_runner(runner: AgentRunner | None) -> None:
    """Register (or clear, with ``None``) the process-wide out-of-turn
    agent runner backing agent-kind hooks.

    Called by the runtime that owns a subagent entry point once it is
    constructed (the HookRunner is built earlier in boot, so this is a
    late-binding slot rather than a constructor argument).
    """
    global _AGENT_RUNNER  # noqa: PLW0603 — process-wide seam by design
    _AGENT_RUNNER = runner
    logger.info("hooks.agent_runner_registered", wired=runner is not None)


def build_agent_evaluator() -> Evaluator:
    """Agent-kind evaluator over the late-binding runner slot.

    Unregistered runner → ``None`` verdict (fail open; the engine's
    warn-once fires so the operator can see agent hooks are configured
    but the runtime has no out-of-turn subagent entry point).
    """

    async def _evaluate(instruction: str, payload: dict[str, Any]) -> Any:
        runner = _AGENT_RUNNER
        if runner is None:
            return None
        evidence = json.dumps(payload, ensure_ascii=False, default=str)
        if len(evidence) > _PAYLOAD_CAP:
            evidence = evidence[:_PAYLOAD_CAP] + "…(truncated)"
        text = await runner(
            f"{_JUDGE_SYSTEM}\n\nINSTRUCTION:\n{instruction}\n\n"
            f"EVENT PAYLOAD:\n{evidence}",
            payload,
            60.0,
        )
        verdict = _parse_verdict(text)
        if verdict is None:
            logger.warning("hooks.agent_evaluator.unparseable_verdict")
        return verdict

    return _evaluate
