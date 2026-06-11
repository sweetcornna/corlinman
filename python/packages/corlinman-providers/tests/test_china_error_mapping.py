"""China-vendor error re-mapping (P6 — DeepSeek / Qwen / GLM).

Unit-tests :func:`corlinman_providers.china.map_china_error` against the
vendor bodies the generic OpenAI-wire mapping cannot classify, plus one
end-to-end run through :class:`DeepSeekProvider` driving the real openai
SDK exception types so the wrap-around-the-inherited-stream path is
exercised for real.
"""

from __future__ import annotations

from typing import Any

import pytest
from corlinman_providers.china import DeepSeekProvider, map_china_error
from corlinman_providers.failover import (
    AuthError,
    BillingError,
    ContextOverflowError,
    CorlinmanError,
    FormatError,
    RateLimitError,
)

# ---------------------------------------------------------------------------
# Pure mapping helper.
# ---------------------------------------------------------------------------


def test_deepseek_insufficient_balance_maps_to_billing() -> None:
    err = CorlinmanError(
        "Error code: 402 - {'error': {'message': 'Insufficient Balance'}}",
        status_code=402,
        provider="deepseek",
        model="deepseek-chat",
    )
    mapped = map_china_error(err)
    assert isinstance(mapped, BillingError)
    assert mapped.status_code == 402
    assert mapped.provider == "deepseek"
    assert mapped.model == "deepseek-chat"


def test_glm_arrears_code_1113_outranks_rate_limit() -> None:
    """GLM reports arrears under HTTP 429 — billing must win over the
    SDK's rate-limit classification (retrying an unpaid account is waste)."""
    err = RateLimitError(
        "Error code: 429 - {'error': {'code': '1113', 'message': '您的账户已欠费'}}",
        status_code=429,
        provider="glm",
        model="glm-4",
    )
    mapped = map_china_error(err)
    assert isinstance(mapped, BillingError)


def test_glm_concurrency_code_1302_maps_to_rate_limit() -> None:
    err = CorlinmanError(
        "Error code: 429 - {'error': {'code': '1302', 'message': '并发数过高'}}",
        status_code=429,
        provider="glm",
    )
    assert isinstance(map_china_error(err), RateLimitError)


def test_dashscope_throttling_maps_to_rate_limit() -> None:
    err = CorlinmanError(
        "Error code: 400 - {'error': {'code': 'Throttling.RateQuota', "
        "'message': 'Requests rate limit exceeded'}}",
        status_code=400,
        provider="qwen",
    )
    assert isinstance(map_china_error(err), RateLimitError)


def test_dashscope_input_length_maps_to_context_overflow() -> None:
    err = FormatError(
        "Range of input length should be [1, 30720]",
        status_code=400,
        provider="qwen",
        model="qwen-turbo",
    )
    assert isinstance(map_china_error(err), ContextOverflowError)


def test_invalid_api_key_maps_to_auth() -> None:
    err = CorlinmanError(
        "Error code: 400 - {'error': {'code': 'InvalidApiKey', "
        "'message': 'Invalid API-key provided.'}}",
        status_code=400,
        provider="qwen",
    )
    assert isinstance(map_china_error(err), AuthError)


def test_already_specific_error_is_returned_unchanged() -> None:
    """A matching classification keeps the original instance (and with it
    extras like ``retry_after_ms``)."""
    err = RateLimitError("rate limit exceeded", retry_after_ms=1500, provider="deepseek")
    assert map_china_error(err) is err


def test_unrelated_error_passes_through_untouched() -> None:
    err = FormatError("messages[1] is missing 'content'", status_code=400)
    assert map_china_error(err) is err


def test_free_integers_never_false_positive_as_vendor_codes() -> None:
    """Token counts and other bare numbers must not match the GLM code
    regex — only a quoted ``code`` field does."""
    err = CorlinmanError("processed 1113 tokens in 1302 ms", status_code=500)
    assert map_china_error(err) is err


# ---------------------------------------------------------------------------
# End-to-end through the provider wrapper + real SDK exception types.
# ---------------------------------------------------------------------------


async def test_deepseek_stream_raises_billing_on_402_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    import openai  # type: ignore[import-not-found]

    request = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
    response = httpx.Response(402, request=request)
    exc = openai.APIStatusError(
        "Error code: 402 - {'error': {'message': 'Insufficient Balance'}}",
        response=response,
        body=None,
    )

    class _FailingCompletions:
        async def create(self, **_: Any) -> Any:
            raise exc

    class _FakeClient:
        def __init__(self, **_: Any) -> None:
            self.chat = type("C", (), {"completions": _FailingCompletions()})()

    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeClient)

    prov = DeepSeekProvider(api_key="sk-test")
    with pytest.raises(BillingError) as exc_info:
        async for _ in prov.chat_stream(
            model="deepseek-chat", messages=[{"role": "user", "content": "hi"}]
        ):
            pass

    assert exc_info.value.status_code == 402
    assert exc_info.value.provider == "deepseek"
    assert exc_info.value.model == "deepseek-chat"
