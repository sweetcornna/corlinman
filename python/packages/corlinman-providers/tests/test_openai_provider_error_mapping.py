"""OpenAI vendor-error → failover taxonomy mapping tests.

Companion to ``test_anthropic_provider_error_mapping.py``. The focus
here is the ``Retry-After`` extraction on 429 (audit R4-D3 / PERF
sibling): ``OpenAIProvider._map_openai_error`` must read the upstream
``retry-after`` header off ``exc.response.headers`` and stamp it onto
:attr:`failover.RateLimitError.retry_after_ms` (delta-seconds → ms) so
the Rust agent client can honour the vendor-suggested wait instead of
its generic ``DEFAULT_SCHEDULE``.

Strategy mirrors the Anthropic file: drive the *real*
``openai.AsyncOpenAI`` SDK through ``respx`` so the SDK does its own
status → exception-class promotion, then exercise
``OpenAIProvider.chat_stream`` end-to-end and assert the resulting
:class:`RateLimitError` fields. Driving through the transport (rather
than constructing SDK exceptions by hand) keeps the test robust to SDK
minor-version constructor churn and exercises the same
``exc.response.headers`` path production relies on.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from corlinman_providers import ProviderChunk
from corlinman_providers.failover import RateLimitError
from corlinman_providers.openai_provider import OpenAIProvider

_MODEL = "gpt-4o"


async def _drive_chat_stream(prov: OpenAIProvider) -> list[ProviderChunk]:
    """Drive ``chat_stream`` to completion (or raise)."""
    chunks: list[ProviderChunk] = []
    async for c in prov.chat_stream(
        model=_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=16,
    ):
        chunks.append(c)
    return chunks


def _openai_error_body(message: str, code: str = "rate_limit_exceeded") -> dict[str, Any]:
    """Shape the JSON body OpenAI returns for an error status.

    Source: https://platform.openai.com/docs/guides/error-codes — every
    error response carries ``{"error": {"message": ..., "type": ...,
    "code": ...}}``.
    """
    return {"error": {"message": message, "type": "rate_limit_error", "code": code}}


def _suppress_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``max_retries=0`` on every adapter-built ``AsyncOpenAI``.

    The adapter constructs ``AsyncOpenAI(api_key=...)`` without
    specifying ``max_retries``, so the SDK falls back to its default of
    2 — meaning a 429 would trigger two backoff sleeps before the
    exception reaches our mapper (and respx would see the mock consumed
    multiple times). We patch the constructor to force ``max_retries=0``
    so each test sees exactly one upstream call. Scoped to the test
    process via monkeypatch; production code is untouched.
    """
    import openai  # type: ignore[import-not-found]

    real_init = openai.AsyncOpenAI.__init__

    def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("max_retries", 0)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", _patched_init)


async def test_429_with_retry_after_header_extracts_ms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 with a ``retry-after`` header maps to :class:`RateLimitError`
    with ``retry_after_ms`` extracted from the header.

    Fixed under audit R4-D3: the OpenAI mapper reads the delta-seconds
    ``retry-after`` header off ``exc.response.headers`` and converts it
    to milliseconds. The fixture sends ``retry-after: 7`` →
    ``retry_after_ms == 7000``.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    _suppress_sdk_retries(monkeypatch)

    with respx.mock(
        base_url="https://api.openai.com/v1", assert_all_called=True
    ) as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                429,
                headers={"retry-after": "7"},
                json=_openai_error_body("rate limit exceeded"),
            )
        )

        prov = OpenAIProvider()
        with pytest.raises(RateLimitError) as exc_info:
            await _drive_chat_stream(prov)

    err = exc_info.value
    assert err.status_code == 429
    assert err.provider == "openai"
    assert err.model == _MODEL
    assert err.retry_after_ms == 7000, (
        "OpenAI mapper must extract the Retry-After header "
        "(7 delta-seconds → 7000 ms) into RateLimitError.retry_after_ms."
    )


async def test_429_without_retry_after_header_leaves_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 lacking a ``retry-after`` header maps to
    :class:`RateLimitError` with ``retry_after_ms`` left at ``None`` —
    the failover layer then uses its default backoff schedule."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    _suppress_sdk_retries(monkeypatch)

    with respx.mock(
        base_url="https://api.openai.com/v1", assert_all_called=True
    ) as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                429, json=_openai_error_body("rate limit exceeded")
            )
        )

        prov = OpenAIProvider()
        with pytest.raises(RateLimitError) as exc_info:
            await _drive_chat_stream(prov)

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_ms is None


async def test_429_with_http_date_retry_after_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP-date ``retry-after`` (rare) is ignored gracefully — the
    mapper only honours the delta-seconds form and leaves
    ``retry_after_ms`` ``None`` rather than crashing or guessing a
    clock-skew-sensitive absolute time."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    _suppress_sdk_retries(monkeypatch)

    with respx.mock(
        base_url="https://api.openai.com/v1", assert_all_called=True
    ) as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                429,
                headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
                json=_openai_error_body("rate limit exceeded"),
            )
        )

        prov = OpenAIProvider()
        with pytest.raises(RateLimitError) as exc_info:
            await _drive_chat_stream(prov)

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_ms is None
