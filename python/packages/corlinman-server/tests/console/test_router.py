"""Routing heuristics — every ambiguous signal must escalate to complex."""

from __future__ import annotations

import pytest
from corlinman_server.console.router import (
    ModelRouter,
    classify_complexity,
)


@pytest.mark.parametrize(
    "text",
    [
        "What's the capital of France?",
        "几点了",
        "thanks!",
        "今天天气怎么样",
        "explain DNS in one sentence",
    ],
)
def test_simple_prompts(text: str) -> None:
    assert classify_complexity(text) == "simple"


@pytest.mark.parametrize(
    "text",
    [
        "Implement a REST API for the user service",  # work verb
        "修复 src/main.py 里的 bug",  # work verb + path
        "First check the logs, then restart the service",  # multi-step
        "先看日志，然后重启服务，最后验证一下",  # CJK multi-step
        "```python\nprint(1)\n```",  # code fence
        "review ./docs/PLAN.md",  # path-like
        "x" * 300,  # too long
        "a\nb\nc\nd\ne",  # too many lines
    ],
)
def test_complex_prompts(text: str) -> None:
    assert classify_complexity(text) == "complex"


def test_route_explicit_model_always_wins() -> None:
    r = ModelRouter(
        default_model="big", small_fast_model="small", auto_route=True
    )
    d = r.route_turn("hi", explicit_model="other")
    assert (d.model, d.reason) == ("other", "explicit")


def test_route_auto_simple_uses_small_model() -> None:
    r = ModelRouter(
        default_model="big", small_fast_model="small", auto_route=True
    )
    d = r.route_turn("what time is it?")
    assert (d.model, d.reason) == ("small", "auto:simple")


def test_route_auto_off_uses_default() -> None:
    r = ModelRouter(
        default_model="big", small_fast_model="small", auto_route=False
    )
    assert r.route_turn("hi").model == "big"


def test_route_complex_uses_default_even_with_auto() -> None:
    r = ModelRouter(
        default_model="big", small_fast_model="small", auto_route=True
    )
    assert r.route_turn("implement the feature").model == "big"


def test_from_config_reads_console_block() -> None:
    cfg = {"console": {"small_fast_model": "mini", "auto_route": True}}
    r = ModelRouter.from_config(cfg, default_model="big")
    assert r.small_fast_model == "mini"
    assert r.auto_route is True
    assert r.utility_model() == "mini"


def test_from_config_defaults_when_block_missing() -> None:
    r = ModelRouter.from_config({}, default_model="big")
    assert r.small_fast_model is None
    assert r.auto_route is False
    assert r.utility_model() == "big"
