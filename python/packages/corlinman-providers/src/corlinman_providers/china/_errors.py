"""China-vendor error re-mapping shared by DeepSeek / Qwen / GLM.

The three china-bucket adapters speak the OpenAI wire format, so the
inherited :func:`corlinman_providers.openai_provider._map_openai_error`
already classifies SDK-level exceptions. But each vendor reports several
failure classes through bodies the generic mapping cannot see through:

* DeepSeek phrases quota exhaustion as HTTP 402 ``"Insufficient Balance"``
  — the generic mapping has no 402 branch, so it lands as a bare
  :class:`CorlinmanError` instead of :class:`BillingError`;
* DashScope (Qwen) throttles with ``Throttling.RateQuota`` /
  ``Throttling.AllocationQuota`` bodies and bounds prompts with
  ``"Range of input length should be [1, N]"``;
* GLM (智谱) multiplexes HTTP 429 across distinct business codes —
  ``1113`` is *arrears* (billing, never retry) while ``1302``/``1303``/
  ``1305`` are genuine concurrency/rate limits.

:func:`map_china_error` re-classifies an already-normalised
:class:`CorlinmanError` using those vendor markers and returns either a
better-fitting subclass instance or the original error untouched (the
inherited mapping is the fallback). :class:`ChinaOpenAIProvider` is the
shared base that applies it around the inherited stream.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

from corlinman_providers.base import ProviderChunk
from corlinman_providers.failover import (
    AuthError,
    BillingError,
    ContextOverflowError,
    CorlinmanError,
    RateLimitError,
)
from corlinman_providers.openai_provider import OpenAIProvider

# Lowercase substrings that identify each failure class across the three
# vendors. Matched against ``str(err).lower()`` — the openai SDK folds the
# JSON error body into the exception message, so body markers are visible
# here. Kept deliberately specific (full vendor phrases, not single common
# words) to avoid re-classifying unrelated errors.
_BILLING_MARKERS: tuple[str, ...] = (
    "insufficient balance",  # DeepSeek 402
    "arrearage",  # DashScope account in arrears
    "余额不足",  # GLM / common CN body
    "欠费",  # GLM 1113 prose
)
_RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "throttling",  # DashScope Throttling.RateQuota / .AllocationQuota
    "too many requests",
    "rate limit",
    "并发数过高",  # GLM 1302 prose
    "频率过高",  # GLM 1303 prose
)
_AUTH_MARKERS: tuple[str, ...] = (
    "invalid api key",
    "invalid api-key",
    "invalidapikey",  # DashScope code
    "incorrect api key",
    "鉴权失败",  # GLM auth prose
)
_CONTEXT_MARKERS: tuple[str, ...] = (
    "range of input length",  # DashScope prompt-length bound
    "maximum context length",
    "context length",
    "输入长度超过",  # CN prompt-too-long prose
)

# GLM business codes — more stable than the Chinese prose. The body shape is
# ``{'error': {'code': '1302', 'message': ...}}`` folded into the exception
# message; match the quoted ``code`` field only so token counts and other
# free integers in the message can never false-positive.
_CODE_RE = re.compile(r"['\"]code['\"]\s*[:=]\s*['\"]?(\d{4})['\"]?")
_BILLING_CODES: frozenset[str] = frozenset({"1113"})
_RATE_LIMIT_CODES: frozenset[str] = frozenset({"1302", "1303", "1305"})


def _vendor_code(message: str) -> str | None:
    """Extract the quoted vendor business ``code`` from an error body, if any."""
    match = _CODE_RE.search(message)
    return match.group(1) if match else None


def map_china_error(err: CorlinmanError) -> CorlinmanError:
    """Re-classify ``err`` using china-vendor markers; identity on no match.

    Checks billing first — GLM reports arrears (code 1113) under HTTP 429,
    so a billing marker must out-rank an existing ``RateLimitError``
    classification (retrying an unpaid account is pure waste). Context,
    auth, and rate-limit markers follow. When the marker agrees with the
    existing classification the original instance is returned unchanged
    (preserving extras like ``retry_after_ms``); when nothing matches the
    inherited mapping stands.
    """
    message = str(err)
    lowered = message.lower()
    code = _vendor_code(message)
    ctx: dict[str, Any] = {
        "status_code": err.status_code,
        "provider": err.provider,
        "model": err.model,
    }

    if (code in _BILLING_CODES) or any(m in lowered for m in _BILLING_MARKERS):
        if isinstance(err, BillingError):
            return err
        return BillingError(message, **ctx)
    if any(m in lowered for m in _CONTEXT_MARKERS):
        if isinstance(err, ContextOverflowError):
            return err
        return ContextOverflowError(message, **ctx)
    if any(m in lowered for m in _AUTH_MARKERS):
        if isinstance(err, AuthError):
            return err
        return AuthError(message, **ctx)
    if (code in _RATE_LIMIT_CODES) or any(m in lowered for m in _RATE_LIMIT_MARKERS):
        if isinstance(err, RateLimitError):
            return err
        return RateLimitError(message, **ctx)
    return err


class ChinaOpenAIProvider(OpenAIProvider):
    """Shared base for the china-bucket adapters.

    Wraps the inherited OpenAI-wire stream and passes every raised
    :class:`CorlinmanError` through :func:`map_china_error` so vendor
    bodies the generic mapping cannot classify (DeepSeek 402 balance,
    DashScope throttling, GLM business codes) surface as the right
    failover class. Errors the helper leaves untouched re-raise verbatim
    — the inherited mapping is the fallback, never overridden blindly.
    """

    async def chat_stream(
        self,
        *,
        model: str,
        messages: Sequence[Any],
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        try:
            async for chunk in super().chat_stream(
                model=model,
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                extra=extra,
            ):
                yield chunk
        except CorlinmanError as err:
            mapped = map_china_error(err)
            if mapped is err:
                raise
            raise mapped from err
