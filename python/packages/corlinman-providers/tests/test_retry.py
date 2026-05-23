"""Tests for :mod:`corlinman_providers._retry`.

Covers the core ``with_retry`` loop plus the Codex-specific
``default_retryable_codex`` classifier:

* exponential backoff math + jitter cap
* ``Retry-After`` header parsed off an exception is honored
* non-retryable errors short-circuit immediately
* ``max_attempts`` exhausted re-raises the *last* exception
* terminal classifications (4xx, ``insufficient_quota``) don't retry
* ``background=True`` flag converts 529 from retryable → terminal
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from corlinman_providers._retry import default_retryable_codex, with_retry


# ---------------------------------------------------------------------------
# Helpers — minimal fake exception types that mimic the openai/httpx surface
# ---------------------------------------------------------------------------


class _FakeHTTPError(Exception):
    """An exception with a ``response.status_code`` and ``response.headers``.

    Shape matches what the openai SDK builds for ``APIError`` subclasses:
    a ``.response`` attribute carrying status + headers.
    """

    def __init__(
        self, status_code: int, *, headers: dict[str, str] | None = None,
        message: str = "",
    ) -> None:
        super().__init__(message or f"HTTP {status_code}")
        self.response = SimpleNamespace(
            status_code=status_code, headers=headers or {}
        )
        # openai SDK also exposes .status_code directly on the exception.
        self.status_code = status_code


class _FakeRateLimitError(_FakeHTTPError):
    """Mimics ``openai.RateLimitError`` — class name matters for the classifier."""


# Force the class name so the connection-error sniffer recognises it.
_FakeRateLimitError.__name__ = "RateLimitError"


class _FakeConnectError(Exception):
    """Mimics ``httpx.ConnectError`` by class name."""


_FakeConnectError.__name__ = "ConnectError"


# ---------------------------------------------------------------------------
# with_retry — happy paths
# ---------------------------------------------------------------------------


class TestWithRetrySuccess:
    @pytest.mark.asyncio
    async def test_first_attempt_succeeds_no_retry(self) -> None:
        calls = 0

        async def attempt() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        result = await with_retry(
            attempt,
            retryable=lambda e: 0.0,
            sleep=_no_sleep,
        )
        assert result == "ok"
        assert calls == 1

    @pytest.mark.asyncio
    async def test_succeeds_after_one_retry(self) -> None:
        calls = 0

        async def attempt() -> str:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise _FakeHTTPError(429)
            return "ok"

        delays: list[float] = []

        async def _sleep(d: float) -> None:
            delays.append(d)

        result = await with_retry(
            attempt,
            retryable=default_retryable_codex,
            sleep=_sleep,
        )
        assert result == "ok"
        assert calls == 2
        assert len(delays) == 1


# ---------------------------------------------------------------------------
# with_retry — failure paths
# ---------------------------------------------------------------------------


class TestWithRetryFailure:
    @pytest.mark.asyncio
    async def test_non_retryable_short_circuits(self) -> None:
        calls = 0

        async def attempt() -> str:
            nonlocal calls
            calls += 1
            raise _FakeHTTPError(401)

        with pytest.raises(_FakeHTTPError):
            await with_retry(
                attempt,
                retryable=default_retryable_codex,
                sleep=_no_sleep,
            )
        assert calls == 1  # 401 must not retry

    @pytest.mark.asyncio
    async def test_max_attempts_exhausted_reraises(self) -> None:
        calls = 0

        async def attempt() -> str:
            nonlocal calls
            calls += 1
            raise _FakeHTTPError(500, message=f"boom-{calls}")

        with pytest.raises(_FakeHTTPError, match="boom-3"):
            await with_retry(
                attempt,
                max_attempts=3,
                retryable=default_retryable_codex,
                sleep=_no_sleep,
            )
        assert calls == 3

    @pytest.mark.asyncio
    async def test_classifier_returning_none_aborts_mid_loop(self) -> None:
        """If the classifier flips from retry → no-retry, we stop immediately."""
        calls = 0
        verdicts = [0.0, None]  # first retry, second terminal

        async def attempt() -> str:
            nonlocal calls
            calls += 1
            raise _FakeHTTPError(500)

        def classify(_exc: BaseException) -> float | None:
            return verdicts[min(calls - 1, len(verdicts) - 1)]

        with pytest.raises(_FakeHTTPError):
            await with_retry(
                attempt,
                retryable=classify,
                sleep=_no_sleep,
            )
        assert calls == 2  # tried, retried, then classifier said no


# ---------------------------------------------------------------------------
# with_retry — delay math
# ---------------------------------------------------------------------------


class TestWithRetryDelays:
    @pytest.mark.asyncio
    async def test_retry_after_header_honored(self) -> None:
        """``Retry-After: 2`` → next sleep is exactly 2.0s (no jitter)."""
        delays: list[float] = []

        async def _sleep(d: float) -> None:
            delays.append(d)

        calls = 0

        async def attempt() -> str:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise _FakeHTTPError(429, headers={"Retry-After": "2"})
            return "ok"

        result = await with_retry(
            attempt,
            retryable=default_retryable_codex,
            sleep=_sleep,
        )
        assert result == "ok"
        assert delays == [2.0]

    @pytest.mark.asyncio
    async def test_retry_after_clamped_to_max_delay(self) -> None:
        delays: list[float] = []

        async def _sleep(d: float) -> None:
            delays.append(d)

        calls = 0

        async def attempt() -> str:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise _FakeHTTPError(429, headers={"Retry-After": "9999"})
            return "ok"

        await with_retry(
            attempt,
            retryable=default_retryable_codex,
            max_delay=5.0,
            sleep=_sleep,
        )
        assert delays == [5.0]

    @pytest.mark.asyncio
    async def test_exponential_backoff_capped(self) -> None:
        """Without Retry-After, delays follow ``base * 2**(n-1)`` capped at max.

        We seed the jitter RNG so the assertion is deterministic.
        """
        import random as _random

        delays: list[float] = []

        async def _sleep(d: float) -> None:
            delays.append(d)

        calls = 0

        async def attempt() -> str:
            nonlocal calls
            calls += 1
            raise _FakeConnectError("boom")

        _random.seed(0)
        with pytest.raises(_FakeConnectError):
            await with_retry(
                attempt,
                max_attempts=5,
                base_delay=1.0,
                max_delay=4.0,
                retryable=default_retryable_codex,
                sleep=_sleep,
            )
        # Four retries (attempts 1..4 failed, 5th also fails and re-raises).
        # Each delay = random.random() * min(base * 2**(n-1), max_delay).
        # We re-derive expected values with the same seed.
        _random.seed(0)
        expected_caps = [1.0, 2.0, 4.0, 4.0]
        expected = [_random.random() * c for c in expected_caps]
        assert delays == pytest.approx(expected)


# ---------------------------------------------------------------------------
# default_retryable_codex — classifier rules
# ---------------------------------------------------------------------------


class TestDefaultRetryableCodex:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_retryable_statuses(self, status: int) -> None:
        assert default_retryable_codex(_FakeHTTPError(status)) == 0.0

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_terminal_statuses(self, status: int) -> None:
        assert default_retryable_codex(_FakeHTTPError(status)) is None

    def test_insufficient_quota_terminal_even_on_429(self) -> None:
        exc = _FakeHTTPError(429, message="You exceeded insufficient_quota")
        assert default_retryable_codex(exc) is None

    def test_retry_after_header_returned(self) -> None:
        exc = _FakeHTTPError(429, headers={"Retry-After": "3"})
        assert default_retryable_codex(exc) == 3.0

    def test_retry_after_lowercase_header(self) -> None:
        exc = _FakeHTTPError(429, headers={"retry-after": "1.5"})
        assert default_retryable_codex(exc) == 1.5

    def test_retry_after_negative_falls_back(self) -> None:
        exc = _FakeHTTPError(429, headers={"Retry-After": "-1"})
        # Negative values are ignored; we fall through to exponential
        # backoff (signaled by 0.0 from the classifier).
        assert default_retryable_codex(exc) == 0.0

    def test_connection_error_retried(self) -> None:
        assert default_retryable_codex(_FakeConnectError("boom")) == 0.0

    def test_rate_limit_error_retried(self) -> None:
        assert default_retryable_codex(_FakeRateLimitError(429)) == 3.0 * 0  # 0.0 default
        # Re-check without the multiplication trick: a RateLimitError w/o
        # Retry-After should also be retryable.
        assert default_retryable_codex(_FakeRateLimitError(429)) == 0.0

    def test_unknown_exception_not_retried(self) -> None:
        assert default_retryable_codex(ValueError("nope")) is None

    def test_529_overload_retried_foreground(self) -> None:
        assert default_retryable_codex(_FakeHTTPError(529)) == 0.0

    def test_529_overload_terminal_when_background(self) -> None:
        assert default_retryable_codex(
            _FakeHTTPError(529), background=True
        ) is None


# ---------------------------------------------------------------------------
# on_retry hook
# ---------------------------------------------------------------------------


class TestOnRetryHook:
    @pytest.mark.asyncio
    async def test_on_retry_called_with_attempt_delay_exc(self) -> None:
        records: list[tuple[int, float, str]] = []
        calls = 0

        async def attempt() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise _FakeHTTPError(429, headers={"Retry-After": "1"})
            return "ok"

        def hook(attempt: int, delay: float, exc: BaseException) -> None:
            records.append((attempt, delay, type(exc).__name__))

        await with_retry(
            attempt,
            retryable=default_retryable_codex,
            on_retry=hook,
            sleep=_no_sleep,
        )
        assert records == [
            (1, 1.0, "_FakeHTTPError"),
            (2, 1.0, "_FakeHTTPError"),
        ]

    @pytest.mark.asyncio
    async def test_on_retry_hook_exception_swallowed(self) -> None:
        """A buggy logging hook must not abort the retry loop."""
        calls = 0

        async def attempt() -> str:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise _FakeHTTPError(429)
            return "ok"

        def buggy_hook(*_: Any) -> None:
            raise RuntimeError("logger blew up")

        result = await with_retry(
            attempt,
            retryable=default_retryable_codex,
            on_retry=buggy_hook,
            sleep=_no_sleep,
        )
        assert result == "ok"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


async def _no_sleep(_: float) -> None:
    """Stub sleep — used to make retry tests instant."""
    return None
