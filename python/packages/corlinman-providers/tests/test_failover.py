"""Direct unit tests for the failover error hierarchy.

The :mod:`corlinman_providers.failover` module defines the exception
taxonomy that every provider adapter coerces vendor SDK errors into.
The Rust agent-client then maps these onto :class:`FailoverReason`
variants and feeds them to ``ModelRedirect`` to pick the next adapter.

A wrong ``reason`` class attribute, a dropped ``status_code`` kwarg, or
a regressed ``RateLimitError.retry_after_ms`` default would silently
disable failover for the affected error class on *every* provider — the
upstream gRPC ``ErrorInfo`` would still ship, but with the wrong
classification. These tests pin the public contract that the mappers
in ``anthropic_provider``, ``openai_provider``, ``bedrock_provider``
etc. depend on, so a future refactor that breaks the constructor or
class attributes fails loudly here rather than silently in production.

All tests are pure constructor / attribute tests — no network, no SDKs.
"""

from __future__ import annotations

import pytest
from corlinman_providers.failover import (
    AuthError,
    AuthPermanentError,
    BillingError,
    ContextOverflowError,
    CorlinmanError,
    FormatError,
    ModelNotFoundError,
    OverloadedError,
    RateLimitError,
    TimeoutError,
)

# Every subclass that the failover layer routes on. ``CorlinmanError``
# is the shared base — tested separately for its defaults.
_FAILOVER_SUBCLASSES = [
    AuthError,
    AuthPermanentError,
    BillingError,
    ContextOverflowError,
    FormatError,
    ModelNotFoundError,
    OverloadedError,
    RateLimitError,
    TimeoutError,
]


# ---------------------------------------------------------------------------
# Base CorlinmanError contract
# ---------------------------------------------------------------------------


def test_corlinman_error_is_exception_subclass() -> None:
    """Sanity: the base is a real exception so ``raise`` / ``except`` work."""
    assert issubclass(CorlinmanError, Exception)


def test_corlinman_error_default_field_values() -> None:
    """A bare ``CorlinmanError("msg")`` carries the documented defaults.

    ``status_code=0`` and ``provider/model = None`` are the sentinel
    values the gRPC ``ErrorInfo`` payload encodes when the adapter
    couldn't recover them. Regressing the defaults to e.g. ``-1`` or
    raising on missing kwargs would break every adapter that relies on
    ``BillingError(str(exc))`` (single-arg call) — a real pattern in
    ``_map_openai_error`` and ``_map_anthropic_error``.
    """
    err = CorlinmanError("boom")
    assert str(err) == "boom"
    assert err.status_code == 0
    assert err.provider is None
    assert err.model is None
    assert err.reason == "unknown"


def test_corlinman_error_kwargs_are_plumbed_through() -> None:
    """Provider mappers rely on the (status_code, provider, model) kwargs
    landing on the instance unchanged — the gRPC ``ErrorInfo`` shaping
    later reads them as-is."""
    err = CorlinmanError(
        "boom", status_code=418, provider="anthropic", model="claude-sonnet-4-5"
    )
    assert err.status_code == 418
    assert err.provider == "anthropic"
    assert err.model == "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# Reason class attribute — the one the gRPC layer reads to classify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls,expected_reason",
    [
        (AuthError, "auth"),
        (AuthPermanentError, "auth_permanent"),
        (BillingError, "billing"),
        (ContextOverflowError, "context_overflow"),
        (FormatError, "format"),
        (ModelNotFoundError, "model_not_found"),
        (OverloadedError, "overloaded"),
        (RateLimitError, "rate_limit"),
        (TimeoutError, "timeout"),
        (CorlinmanError, "unknown"),
    ],
)
def test_reason_class_attribute_matches_failover_enum(
    cls: type[CorlinmanError], expected_reason: str
) -> None:
    """Each error class declares a stable ``reason`` string that mirrors
    a variant of the Rust ``FailoverReason`` enum (see
    ``proto/corlinman/v1/common.proto::FailoverReason``).

    A typo here would silently degrade failover decisions — the agent
    client would route on ``"unknown"`` and apply the wrong retry
    schedule. Pinning every value catches accidental renames.
    """
    assert cls.reason == expected_reason


# ---------------------------------------------------------------------------
# All subclasses honour the shared CorlinmanError constructor contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _FAILOVER_SUBCLASSES)
def test_subclass_accepts_single_message_arg(cls: type[CorlinmanError]) -> None:
    """Every subclass must accept ``cls("message")`` — the most common
    call shape in the provider mappers (``return BillingError(str(exc),
    status_code=402, **ctx)``).
    """
    err = cls("payment required")
    assert str(err) == "payment required"
    assert isinstance(err, CorlinmanError)


@pytest.mark.parametrize("cls", _FAILOVER_SUBCLASSES)
def test_subclass_plumbs_provider_and_model(cls: type[CorlinmanError]) -> None:
    """The (provider, model) context must reach the instance for any
    subclass — the gRPC ``ErrorInfo`` ships them downstream. ``TimeoutError``
    has a default ``message=`` arg but still accepts the keyword
    parameters; ``RateLimitError`` has an extra ``retry_after_ms`` kwarg
    but mustn't shadow the base parameters."""
    err = cls("boom", provider="openai", model="gpt-4o")
    assert err.provider == "openai"
    assert err.model == "gpt-4o"


# ---------------------------------------------------------------------------
# RateLimitError-specific extension
# ---------------------------------------------------------------------------


def test_rate_limit_default_status_code_is_429() -> None:
    """``RateLimitError`` ships with ``status_code=429`` by default — the
    OpenAI/Anthropic mappers explicitly pass ``status_code=429`` already,
    but providers that catch a vendor SDK ``RateLimitError`` instance
    rely on the default for the safety net. Bedrock, for example,
    constructs ours from a ThrottlingException without specifying the
    status, depending on this default landing as 429 in ``ErrorInfo``."""
    err = RateLimitError("rate exceeded")
    assert err.status_code == 429


def test_rate_limit_retry_after_ms_defaults_to_none() -> None:
    """Default ``retry_after_ms`` is ``None`` — the absence of the
    header is signalled with ``None``, not ``0`` (``0`` would mean
    "retry immediately"). The Rust backoff layer falls back to its
    ``DEFAULT_SCHEDULE`` only when the field is ``None``."""
    err = RateLimitError("rate exceeded")
    assert err.retry_after_ms is None


def test_rate_limit_retry_after_ms_keyword_is_preserved() -> None:
    """When the adapter does parse a ``retry-after`` header (e.g. from a
    custom OpenAI-compatible endpoint), the value must land on the
    instance unchanged."""
    err = RateLimitError(
        "throttled",
        status_code=429,
        provider="openai",
        model="gpt-4o",
        retry_after_ms=4500,
    )
    assert err.retry_after_ms == 4500
    assert err.status_code == 429
    assert err.provider == "openai"
    assert err.model == "gpt-4o"


def test_rate_limit_accepts_custom_status_code() -> None:
    """Some vendors (Anthropic gateway, throttling intermediaries) use
    non-429 status for rate limits — the adapter must be allowed to
    override the default."""
    err = RateLimitError("throttled", status_code=503)
    assert err.status_code == 503
    assert err.reason == "rate_limit"


# ---------------------------------------------------------------------------
# TimeoutError-specific defaults
# ---------------------------------------------------------------------------


def test_timeout_error_message_defaults_to_upstream_timeout() -> None:
    """``TimeoutError`` has a default ``message`` arg — the adapters
    construct ``TimeoutError(**ctx)`` without a message in a few hot
    paths. Regressing the default would crash with a missing-arg
    TypeError instead of failing over."""
    err = TimeoutError()
    assert str(err) == "upstream timeout"
    assert err.reason == "timeout"
    assert err.status_code == 0


def test_timeout_error_accepts_custom_message_and_context() -> None:
    """A more specific timeout message (e.g. ``"stream idle 30s"``)
    survives the constructor along with the provider/model context."""
    err = TimeoutError("idle 30s", provider="bedrock", model="claude-3-5-sonnet")
    assert str(err) == "idle 30s"
    assert err.provider == "bedrock"
    assert err.model == "claude-3-5-sonnet"


# ---------------------------------------------------------------------------
# Hierarchy guarantees — needed for blanket ``except CorlinmanError``
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _FAILOVER_SUBCLASSES)
def test_every_subclass_is_corlinman_error(cls: type[CorlinmanError]) -> None:
    """``chat_stream`` and the registry use ``except CorlinmanError`` as
    a blanket re-raise — every failover class must descend from it so
    nothing leaks raw vendor exceptions to the gRPC layer."""
    assert issubclass(cls, CorlinmanError)
    assert issubclass(cls, Exception)


def test_timeout_error_shadows_builtin_intentionally() -> None:
    """We deliberately shadow :class:`builtins.TimeoutError` (see the
    ``noqa: A001`` in failover.py). The provider mappers always import
    *our* class via ``from corlinman_providers.failover import
    TimeoutError`` and rely on that import-binding. This test pins the
    semantics: an instance of our TimeoutError is *not* an instance of
    the stdlib TimeoutError, which prevents an over-broad
    ``except OSError`` (the stdlib TimeoutError's parent) from
    accidentally swallowing a provider failover signal."""
    err = TimeoutError("upstream slow")
    # Our class lives in failover.py — it does not inherit from OSError
    # like ``builtins.TimeoutError`` does.
    assert not isinstance(err, OSError)
    # And it IS a CorlinmanError for the failover layer.
    assert isinstance(err, CorlinmanError)
