"""Dim 9 — production evaluators for prompt/agent-kind declarative hooks.

The engine-side plumbing (verdict coercion, timeouts, fail-open) is
covered by ``corlinman-hooks``' own tests; these pin the NEW production
implementations: the LLM prompt judge (provider stream → verdict JSON,
reasoning-delta hygiene, unparseable → fail open), the model-resolution
precedence, and the agent-kind late-binding runner slot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from corlinman_hooks import HookRunner
from corlinman_server import hooks_evaluators as he
from corlinman_server.hooks_evaluators import (
    build_agent_evaluator,
    build_prompt_evaluator,
    register_hook_agent_runner,
    resolve_evaluator_model,
)

pytestmark = pytest.mark.asyncio


@dataclass
class _Chunk:
    kind: str
    text: str | None = None
    is_reasoning: bool = False
    finish_reason: str | None = None


@dataclass
class _JudgeProvider:
    chunks: list[_Chunk]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def chat_stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        async def _gen() -> Any:
            for c in self.chunks:
                yield c

        return _gen()


def _resolver(provider: _JudgeProvider) -> Any:
    return lambda m: (provider, f"upstream/{m}")


# ---------------------------------------------------------------------------
# Model resolution precedence
# ---------------------------------------------------------------------------


async def test_resolve_evaluator_model_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_HOOK_EVAL_MODEL", raising=False)
    assert resolve_evaluator_model(None, "fallback") == "fallback"
    monkeypatch.setenv("CORLINMAN_HOOK_EVAL_MODEL", "env-model")
    assert resolve_evaluator_model(None, "fallback") == "env-model"
    cfg = {"hooks": {"evaluator_model": "cfg-model"}}
    assert resolve_evaluator_model(cfg, "fallback") == "cfg-model"


# ---------------------------------------------------------------------------
# Prompt judge
# ---------------------------------------------------------------------------


async def test_prompt_evaluator_parses_verdict_and_drops_reasoning() -> None:
    provider = _JudgeProvider(
        [
            _Chunk(kind="token", text='{"ok": false', is_reasoning=True),
            _Chunk(kind="token", text='{"ok": false, "reason": "blocked"}'),
            _Chunk(kind="done", finish_reason="stop"),
        ]
    )
    evaluate = build_prompt_evaluator(_resolver(provider), "judge-model")
    assert evaluate is not None

    verdict = await evaluate("no rm allowed", {"tool_name": "run_shell"})

    assert verdict == {"ok": False, "reason": "blocked"}
    sent = provider.calls[0]
    assert sent["model"] == "upstream/judge-model"
    # Instruction + payload both reach the judge.
    user_msg = sent["messages"][1].content
    assert "no rm allowed" in user_msg
    assert "run_shell" in user_msg


async def test_prompt_evaluator_unparseable_fails_open() -> None:
    provider = _JudgeProvider(
        [_Chunk(kind="token", text="sure thing!"), _Chunk(kind="done")]
    )
    evaluate = build_prompt_evaluator(_resolver(provider), "judge-model")
    assert evaluate is not None
    assert await evaluate("judge this", {}) is None


async def test_prompt_evaluator_requires_model() -> None:
    assert build_prompt_evaluator(_resolver(_JudgeProvider([])), "") is None


async def test_prompt_evaluator_gates_through_runner() -> None:
    """End-to-end: a prompt-kind Stop hook backed by the real judge
    denies the stop through HookRunner."""
    provider = _JudgeProvider(
        [
            _Chunk(kind="token", text='{"ok": false, "reason": "not done"}'),
            _Chunk(kind="done"),
        ]
    )
    evaluate = build_prompt_evaluator(_resolver(provider), "judge-model")
    runner = HookRunner(
        {
            "hooks": {
                "declarative": {
                    "Stop": [
                        {"hooks": [{"kind": "prompt", "prompt": "done?"}]}
                    ]
                }
            }
        },
        prompt_evaluator=evaluate,
    )
    decision = await runner.run_stop_async({"session_key": "s"})
    assert decision.allow is False
    assert decision.reason == "not done"


# ---------------------------------------------------------------------------
# Agent-kind late-binding slot
# ---------------------------------------------------------------------------


async def test_agent_evaluator_unregistered_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(he, "_AGENT_RUNNER", None)
    evaluate = build_agent_evaluator()
    assert await evaluate("verify this", {}) is None


async def test_agent_evaluator_uses_registered_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(he, "_AGENT_RUNNER", None)
    seen: list[tuple[str, float]] = []

    async def _runner(instruction: str, payload: dict, timeout: float) -> str:
        seen.append((instruction, timeout))
        return 'verdict: {"ok": true, "reason": "verified"}'

    register_hook_agent_runner(_runner)
    try:
        evaluate = build_agent_evaluator()
        verdict = await evaluate("verify the fix", {"tool_name": "x"})
        assert verdict == {"ok": True, "reason": "verified"}
        assert "verify the fix" in seen[0][0]
    finally:
        register_hook_agent_runner(None)
