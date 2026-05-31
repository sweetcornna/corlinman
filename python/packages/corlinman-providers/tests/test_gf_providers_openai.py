"""Gap-fill (lane-providers) OpenAI provider context-overflow tests.

Mirrors the Anthropic provider's richer context-overflow error so the
reasoning loop can shrink-and-retry: a 400 ``BadRequestError`` whose body
describes a context overflow is mapped to :class:`ContextOverflowError` with
the parsed numeric ``limit`` / ``input_tokens`` attached.

The pure parse helper is tested directly (SDK-independent); the end-to-end
mapping is exercised via ``respx`` driving the real ``openai`` SDK so the
``BadRequestError`` promotion happens for real.
"""

from __future__ import annotations

from typing import Any

import pytest
from corlinman_providers.failover import ContextOverflowError
from corlinman_providers.openai_provider import (
    OpenAIProvider,
    _parse_openai_context_overflow,
)


# ---------------------------------------------------------------------------
# Pure parse helper.
# ---------------------------------------------------------------------------


def test_parse_openai_prose_form() -> None:
    msg = (
        "This model's maximum context length is 128000 tokens. However, your "
        "messages resulted in 130500 tokens. Please reduce the length."
    )
    used, max_tokens, limit = _parse_openai_context_overflow(msg)
    assert limit == 128000
    assert used == 130500
    assert max_tokens is None


def test_parse_openai_triple_form() -> None:
    used, max_tokens, limit = _parse_openai_context_overflow("8000 + 4096 > 8192")
    assert (used, max_tokens, limit) == (8000, 4096, 8192)


def test_parse_openai_commas() -> None:
    msg = "maximum context length is 1,047,576 tokens ... resulted in 1,050,000 tokens"
    used, _max_tokens, limit = _parse_openai_context_overflow(msg)
    assert limit == 1047576
    assert used == 1050000


def test_parse_openai_no_match() -> None:
    assert _parse_openai_context_overflow("invalid request") == (None, None, None)


# ---------------------------------------------------------------------------
# End-to-end mapping via the real SDK + respx.
# ---------------------------------------------------------------------------


async def test_context_overflow_error_carries_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    import respx

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    import openai  # type: ignore[import-not-found]

    orig = openai.AsyncOpenAI

    def _no_retry(*a: Any, **kw: Any) -> Any:
        kw.setdefault("max_retries", 0)
        return orig(*a, **kw)

    monkeypatch.setattr(openai, "AsyncOpenAI", _no_retry)

    body_msg = (
        "This model's maximum context length is 128000 tokens. However, your "
        "messages resulted in 130500 tokens."
    )
    with respx.mock(base_url="https://api.openai.com/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                400,
                json={"error": {"message": body_msg, "type": "invalid_request_error"}},
            )
        )
        prov = OpenAIProvider()
        with pytest.raises(ContextOverflowError) as exc_info:
            async for _ in prov.chat_stream(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=8,
            ):
                pass

    err = exc_info.value
    assert err.status_code == 400
    assert getattr(err, "limit", None) == 128000
    assert getattr(err, "input_tokens", None) == 130500


def test_build_context_overflow_error_without_limit_is_safe() -> None:
    """When the body has no parseable numbers the error still constructs —
    the numeric attrs are simply absent (loop falls back to its own budget)."""
    from corlinman_providers.openai_provider import _build_openai_context_overflow_error

    err = _build_openai_context_overflow_error(
        ValueError("prompt too long"), ctx={"provider": "openai", "model": "gpt-4o"}
    )
    assert isinstance(err, ContextOverflowError)
    assert getattr(err, "limit", None) is None
