"""Anthropic vendor-error → failover taxonomy mapping tests.

The Anthropic provider's ``_map_anthropic_error`` helper is the only
thing standing between an upstream HTTP failure and the
``ModelRedirect`` failover layer. A wrong mapping (e.g. 529 → AuthError
instead of OverloadedError) silently disables failover for one of the
most-used providers — the Rust agent client would see the wrong
``FailoverReason`` and either burn an auth-retry budget on a transient
overload or skip a recoverable rate-limit entirely.

Before this file existed, the only end-to-end assertion of the
contract was a single ``HTTP 429 → RateLimitError`` check in
``test_bedrock_provider.py`` — not even on the Anthropic adapter.

Strategy: drive the *real* ``anthropic.AsyncAnthropic`` SDK through
``respx`` so the SDK does its own status → exception class promotion
(``_make_status_error``), then exercise ``AnthropicProvider.chat_stream``
end-to-end. We assert the exact :class:`CorlinmanError` subclass and
the key fields the gRPC ``ErrorInfo`` payload depends on
(``status_code``, ``provider``, ``model``, and — for rate limits —
``retry_after_ms``).

Why respx and not just calling ``_map_anthropic_error`` directly:
constructing the SDK exception classes manually is brittle (the
constructor signature varies across SDK minor versions and requires a
real ``httpx.Response`` anyway). Driving through the HTTP transport
also catches regressions where the SDK starts upgrading a status to a
*different* subclass (e.g. the SDK already promotes 529 →
``anthropic.OverloadedError`` via ``_make_status_error``; if a future
version stops doing that, the ``isinstance(APIStatusError)`` fallback
in our mapper must still recognise the status code).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from corlinman_providers import AnthropicProvider, ProviderChunk
from corlinman_providers.failover import (
    AuthError,
    BillingError,
    ContextOverflowError,
    CorlinmanError,
    FormatError,
    ModelNotFoundError,
    OverloadedError,
    RateLimitError,
)
from corlinman_providers.failover import TimeoutError as ProviderTimeoutError

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_MODEL = "claude-sonnet-4-5"


def _anthropic_error_body(error_type: str, message: str) -> dict[str, Any]:
    """Shape the JSON body Anthropic returns for any error status.

    Source: https://docs.anthropic.com/en/api/errors — every error
    response carries ``{"type": "error", "error": {"type": ..., "message":
    ...}}``. The SDK reads the inner ``message`` for the exception string.
    """
    return {"type": "error", "error": {"type": error_type, "message": message}}


async def _drive_chat_stream(prov: AnthropicProvider) -> list[ProviderChunk]:
    """Drive ``chat_stream`` to completion (or raise).

    A separate helper so the parametrize cases stay focused on the
    error-mapping assertion. Any chunks yielded before an exception are
    discarded — the assertion in the test is on the raised exception.
    """
    chunks: list[ProviderChunk] = []
    async for c in prov.chat_stream(
        model=_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=16,
    ):
        chunks.append(c)
    return chunks


# ---------------------------------------------------------------------------
# Vendor-response cases — one row per upstream status + body variant.
# ---------------------------------------------------------------------------
#
# Each row drives a single Anthropic HTTP response through the SDK and
# asserts the resulting :class:`CorlinmanError` subclass + the
# ``status_code`` that lands on the instance (the field that ships in
# ``ErrorInfo``). Cases that need extra body-level assertions (the 429
# ``retry-after`` header, the 400 BadRequest sub-discriminator on
# context-overflow vs format) get their own dedicated tests below.
#
# Format: (case_id, http_status, body_json, expected_cls, expected_status_code)
_VENDOR_CASES: list[tuple[str, int, dict[str, Any], type[CorlinmanError], int]] = [
    (
        "401_invalid_api_key",
        401,
        _anthropic_error_body("authentication_error", "invalid x-api-key"),
        AuthError,
        401,
    ),
    (
        "403_permission_denied_to_model",
        403,
        _anthropic_error_body("permission_error", "your account is not permitted"),
        # Per failover.py: AuthPermanentError exists; the Anthropic mapper
        # also exposes it for PermissionDeniedError. AuthError is the
        # *transient* sibling — 403 should never be transient.
        # NB: assertion validated in dedicated test below; here we only
        # cover via the status-code rollup so the parametrize stays
        # focused on the high-traffic 401/429/529/400/404 path.
        # Skipped from this parametrize, covered in test_403_*.
        AuthError,  # placeholder — see dedicated 403 test, this row excluded below.
        403,
    ),
    (
        "429_rate_limited",
        429,
        _anthropic_error_body("rate_limit_error", "rate limit exceeded"),
        RateLimitError,
        429,
    ),
    (
        "404_unknown_model",
        404,
        _anthropic_error_body("not_found_error", "model not found"),
        ModelNotFoundError,
        404,
    ),
    (
        "529_overloaded",
        529,
        _anthropic_error_body("overloaded_error", "Anthropic is overloaded"),
        OverloadedError,
        529,
    ),
    (
        "503_service_unavailable",
        503,
        _anthropic_error_body("api_error", "service unavailable"),
        OverloadedError,
        503,
    ),
]

# Strip the 403 row — covered in a dedicated test where we assert the
# distinct AuthPermanentError class explicitly. Keeping it in the rollup
# would muddy the per-status assertion above.
_VENDOR_CASES = [row for row in _VENDOR_CASES if row[0] != "403_permission_denied_to_model"]


@pytest.mark.parametrize(
    "case_id,status,body,expected_cls,expected_status_code",
    _VENDOR_CASES,
    ids=[row[0] for row in _VENDOR_CASES],
)
async def test_vendor_status_maps_to_failover_class(
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    status: int,
    body: dict[str, Any],
    expected_cls: type[CorlinmanError],
    expected_status_code: int,
) -> None:
    """Each upstream HTTP status produces the expected
    :class:`CorlinmanError` subclass with the right ``status_code``,
    ``provider``, and ``model`` populated."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    # Force max_retries=0 inside the SDK via the constructor patch — the
    # real adapter constructs AsyncAnthropic without that kwarg, so we
    # wrap the constructor to inject it. Otherwise the SDK retries
    # 429/503/529 with exponential backoff and the test takes seconds.
    _suppress_sdk_retries(monkeypatch)

    with respx.mock(
        base_url="https://api.anthropic.com", assert_all_called=True
    ) as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(status, json=body)
        )

        prov = AnthropicProvider()
        with pytest.raises(expected_cls) as exc_info:
            await _drive_chat_stream(prov)

    err = exc_info.value
    assert err.status_code == expected_status_code, (
        f"{case_id}: expected status_code={expected_status_code}, got {err.status_code}"
    )
    assert err.provider == "anthropic"
    assert err.model == _MODEL


# ---------------------------------------------------------------------------
# Dedicated 403 → AuthPermanentError test
# ---------------------------------------------------------------------------


async def test_403_maps_to_auth_permanent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 means a revoked / wrong-tenant key — never retry, fail over
    immediately. The Anthropic SDK promotes 403 to
    :class:`anthropic.PermissionDeniedError`, which our mapper catches
    explicitly and maps to :class:`AuthPermanentError` (distinct from
    the transient :class:`AuthError` that 401 maps to)."""
    from corlinman_providers.failover import AuthPermanentError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    _suppress_sdk_retries(monkeypatch)

    with respx.mock(
        base_url="https://api.anthropic.com", assert_all_called=True
    ) as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(
                403,
                json=_anthropic_error_body(
                    "permission_error", "your account is not permitted"
                ),
            )
        )

        prov = AnthropicProvider()
        with pytest.raises(AuthPermanentError) as exc_info:
            await _drive_chat_stream(prov)

    assert exc_info.value.status_code == 403
    assert exc_info.value.provider == "anthropic"


# ---------------------------------------------------------------------------
# 400 BadRequest discrimination — context-overflow / billing / format
# ---------------------------------------------------------------------------
#
# Anthropic returns 400 for several distinct problems; the mapper
# discriminates by substring-matching the error message. This is
# brittle (a vendor message tweak would re-route the error) but it's
# the only signal available — so the substrings are part of the
# tested contract.

# Row format: (case_id, upstream_message, expected_cls, expected_status_code).
#
# Note: BillingError-mapped rows ship with ``status_code=402`` (Payment
# Required), not 400 — the mapper explicitly overrides the status when
# it recognises a credit / quota / billing message, since the inbound
# HTTP status is technically 400 BadRequest but the semantics are
# "out of credit" which the gRPC layer surfaces as 402. ContextOverflow
# and Format rows keep status_code=400.
_BAD_REQUEST_CASES: list[tuple[str, str, type[CorlinmanError], int]] = [
    (
        "context_length_exceeded",
        "prompt is too long: 200000 tokens exceed context length",
        ContextOverflowError,
        400,
    ),
    (
        "tokens_exceeded_alias",
        "tokens exceed model limit",
        ContextOverflowError,
        400,
    ),
    (
        "credit_balance_exhausted",
        "your credit balance is too low",
        BillingError,
        402,
    ),
    (
        "quota_exceeded",
        "monthly quota exhausted",
        BillingError,
        402,
    ),
    (
        "malformed_tool_schema",
        "tools.0.input_schema: Required",
        FormatError,
        400,
    ),
    (
        "invalid_message_role",
        "messages.0.role: must be one of user, assistant",
        FormatError,
        400,
    ),
]


@pytest.mark.parametrize(
    "case_id,message,expected_cls,expected_status_code",
    _BAD_REQUEST_CASES,
    ids=[row[0] for row in _BAD_REQUEST_CASES],
)
async def test_400_bad_request_discriminates_by_message(
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    message: str,
    expected_cls: type[CorlinmanError],
    expected_status_code: int,
) -> None:
    """The 400 BadRequest mapper inspects the error message substring to
    decide between :class:`ContextOverflowError` (recoverable: drop the
    oldest turn, retry on a larger-context model), :class:`BillingError`
    (terminal: skip to the next provider) and :class:`FormatError`
    (caller bug: surface to the agent, no failover).

    NB: BillingError-mapped rows arrive over the wire as HTTP 400 but
    the production mapper deliberately re-stamps them with
    ``status_code=402`` so the gRPC ``ErrorInfo`` surface reflects the
    semantic ("payment required") rather than the vendor's literal HTTP
    code. ContextOverflow and Format rows keep the literal 400. The
    parametrize captures both invariants per case.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    _suppress_sdk_retries(monkeypatch)

    with respx.mock(
        base_url="https://api.anthropic.com", assert_all_called=True
    ) as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(
                400, json=_anthropic_error_body("invalid_request_error", message)
            )
        )

        prov = AnthropicProvider()
        with pytest.raises(expected_cls) as exc_info:
            await _drive_chat_stream(prov)

    assert exc_info.value.status_code == expected_status_code, (
        f"{case_id}: expected status_code={expected_status_code} "
        f"(BillingError re-stamps to 402; the other 400 variants keep 400)"
    )
    assert exc_info.value.provider == "anthropic"


# ---------------------------------------------------------------------------
# 429 retry-after extraction — the one place ``RateLimitError.retry_after_ms``
# matters.
# ---------------------------------------------------------------------------


async def test_429_with_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 response with a ``retry-after`` header is still mapped to
    :class:`RateLimitError`.

    NB (discovered while writing this test, recorded in
    ``audit/evidence/cleanup/TEST-003/discovered.md``): the Anthropic
    mapper does *not* extract the ``retry-after`` header into
    ``RateLimitError.retry_after_ms``. The field is left at its
    default ``None`` even when the upstream told us exactly how long to
    wait. This test pins the *current* shipped behaviour so a future
    fix (which should set ``retry_after_ms`` from the header) breaks
    here loudly — at which point this assertion should be updated to
    ``assert err.retry_after_ms == 7000`` and the discovered.md note
    can be removed.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    _suppress_sdk_retries(monkeypatch)

    with respx.mock(
        base_url="https://api.anthropic.com", assert_all_called=True
    ) as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(
                429,
                headers={"retry-after": "7"},
                json=_anthropic_error_body("rate_limit_error", "rate limit exceeded"),
            )
        )

        prov = AnthropicProvider()
        with pytest.raises(RateLimitError) as exc_info:
            await _drive_chat_stream(prov)

    err = exc_info.value
    assert err.status_code == 429
    assert err.provider == "anthropic"
    # ===== Pin current behaviour: header is NOT extracted =====
    # See discovered.md — the mapper drops ``retry-after`` on the floor.
    assert err.retry_after_ms is None, (
        "Anthropic mapper currently drops the Retry-After header. "
        "If this assertion starts failing, the mapper now extracts the "
        "header (good!) and this test should be updated to assert the "
        "extracted value."
    )


# ---------------------------------------------------------------------------
# Transport-level (pre-HTTP-status) failures
# ---------------------------------------------------------------------------


async def test_connect_timeout_maps_to_provider_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``httpx.ConnectTimeout`` is promoted by the Anthropic SDK to
    :class:`anthropic.APITimeoutError`, which our mapper catches and
    maps to :class:`failover.TimeoutError` (note: *our* TimeoutError,
    not ``builtins.TimeoutError`` — see ``test_failover.py``)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    _suppress_sdk_retries(monkeypatch)

    with respx.mock(
        base_url="https://api.anthropic.com", assert_all_called=True
    ) as router:
        router.post("/v1/messages").mock(
            side_effect=httpx.ConnectTimeout("connect timed out")
        )

        prov = AnthropicProvider()
        with pytest.raises(ProviderTimeoutError) as exc_info:
            await _drive_chat_stream(prov)

    assert exc_info.value.provider == "anthropic"
    assert exc_info.value.model == _MODEL


async def test_unknown_5xx_falls_through_to_base_corlinman_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 with no specialised vendor class lands on the
    ``APIStatusError`` catch-all branch, which yields a bare
    :class:`CorlinmanError` carrying the upstream status. This is the
    "we don't know what to do — let the upper layer decide" path; it
    must still be a :class:`CorlinmanError` (so the blanket
    ``except CorlinmanError`` in ``chat_stream`` doesn't double-wrap)
    and never a raw vendor exception."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    _suppress_sdk_retries(monkeypatch)

    with respx.mock(
        base_url="https://api.anthropic.com", assert_all_called=True
    ) as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(
                500, json=_anthropic_error_body("api_error", "internal server error")
            )
        )

        prov = AnthropicProvider()
        with pytest.raises(CorlinmanError) as exc_info:
            await _drive_chat_stream(prov)

    # Must NOT be one of the specialised subclasses — if a future SDK
    # bump starts promoting 500 to a known class we want this test to
    # fail loudly so the mapper can be updated to take advantage.
    err = exc_info.value
    assert type(err) is CorlinmanError, (
        f"Expected the base CorlinmanError for the 500 fall-through, got "
        f"{type(err).__name__} — the SDK may have started classifying 500s "
        "more precisely; review the mapper."
    )
    assert err.status_code == 500


# ---------------------------------------------------------------------------
# SDK retry suppression
# ---------------------------------------------------------------------------


def _suppress_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap ``anthropic.AsyncAnthropic.__init__`` so every adapter-built
    client uses ``max_retries=0``.

    The adapter constructs ``AsyncAnthropic(api_key=...)`` (or
    ``auth_token=...``) without specifying ``max_retries``, so the SDK
    falls back to its default of 2 — meaning every 429/503/529 response
    in this file would trigger two ~1s sleeps before the exception
    reaches our mapper. That bloats the test run from <1s to >10s and,
    worse, leads to ``respx`` consuming the registered mock multiple
    times. We patch the constructor to force ``max_retries=0`` so each
    test sees exactly one upstream call.

    We MUST NOT change the production code (TEST-003 rule); the patch
    is scoped to the test process via monkeypatch.
    """
    import anthropic  # type: ignore[import-not-found]

    real_init = anthropic.AsyncAnthropic.__init__

    def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("max_retries", 0)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", _patched_init)
