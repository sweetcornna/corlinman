"""Retry / backoff helpers for provider streaming.

Used by :class:`corlinman_providers.codex_provider.CodexProvider` to retry
the connection + first-event phase of a stream on transient failures
(429, 5xx, connection blips). Mid-stream retries are deliberately not
attempted — once tokens have been emitted, a retry would duplicate text.

The retry classifier is provider-specific so each provider can encode
its own quirks (Codex billing returns 429 with ``insufficient_quota`` —
that's terminal, not transient). The core :func:`with_retry` coroutine
is provider-agnostic: it takes any ``retryable(exc) -> float | None``
callable that maps an exception to a delay (or ``None`` to give up).

Delay strategy:
* If ``retryable`` returns a positive number, that exact delay is used
  (clamped to ``max_delay``). This is how ``Retry-After`` propagates.
* Otherwise the next delay is ``base_delay * 2 ** (attempt - 1)`` with
  full jitter (``random() * delay``), clamped to ``max_delay``.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

# Status codes that are worth retrying. 408 (request timeout) and 425
# (too early) are arguably transient too, but the Codex backend in
# practice surfaces only 429 + 5xx, so we keep the set minimal.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 529 is Anthropic's "overloaded" status; OpenAI doesn't use it but
# Claude Code's foreground/background split treats it as terminal for
# foreground requests when running in background mode (see
# ``CodexProvider.chat_stream(background=True)``).
_OVERLOAD_STATUS = {529}


async def with_retry[T](
    make_attempt: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 16.0,
    retryable: Callable[[BaseException], float | None],
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Run ``make_attempt`` with exponential-backoff retries.

    Parameters
    ----------
    make_attempt:
        Async, no-arg callable; the coroutine that performs one attempt
        and returns the result (or raises).
    max_attempts:
        Total attempts including the first. After this many *retryable*
        failures the last exception is re-raised.
    base_delay:
        First retry's nominal delay in seconds (before jitter).
    max_delay:
        Hard cap on any computed or ``Retry-After``-supplied delay.
    retryable:
        ``(exc) -> float | None``. Return a non-negative float to pin the
        next delay to that value (typically read off ``Retry-After``);
        return ``None`` to give up and re-raise.
    on_retry:
        Optional ``(attempt, delay, exc) -> None`` hook fired right
        before the sleep. ``attempt`` is the 1-based number of the
        attempt that just failed.
    sleep:
        Injectable sleeper, default :func:`asyncio.sleep`. Tests use this
        to assert delay math without actually waiting.

    Returns
    -------
    The result of the first successful ``make_attempt`` call.
    """
    if sleep is None:
        import asyncio
        sleep = asyncio.sleep

    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await make_attempt()
        except BaseException as exc:  # noqa: BLE001 — caller's classifier decides
            last_exc = exc
            override = retryable(exc)
            if override is None or attempt >= max_attempts:
                raise
            delay = _compute_delay(
                attempt=attempt,
                base_delay=base_delay,
                max_delay=max_delay,
                override=override,
            )
            if on_retry is not None:
                try:
                    on_retry(attempt, delay, exc)
                except Exception:  # noqa: BLE001 — logging hook must never break the retry
                    pass
            await sleep(delay)

    # Unreachable — the loop either returns or re-raises. ``raise`` here
    # satisfies type-checkers that don't infer the invariant.
    assert last_exc is not None
    raise last_exc


def _compute_delay(
    *,
    attempt: int,
    base_delay: float,
    max_delay: float,
    override: float,
) -> float:
    """Return the next sleep duration in seconds.

    A positive ``override`` (``Retry-After`` style) wins, clamped to
    ``max_delay``. ``0.0`` falls through to exponential backoff with
    full jitter; this is the default when ``retryable`` returns 0 to
    mean "retry, no specific guidance".
    """
    if override > 0:
        return min(override, max_delay)
    # ``2 ** (attempt - 1)`` with a non-literal int exponent widens to Any
    # (mypy's int.__pow__ overload accounts for negative exponents); coerce
    # back to float so the function's declared return type holds.
    backoff = min(base_delay * float(2 ** (attempt - 1)), max_delay)
    return random.random() * backoff


# ---------------------------------------------------------------------------
# Codex-specific classifier
# ---------------------------------------------------------------------------


def default_retryable_codex(
    exc: BaseException, *, background: bool = False
) -> float | None:
    """Classifier for ``CodexProvider.chat_stream``'s first-event retry.

    Returns the delay in seconds (``0.0`` means "retry, no specific
    guidance — use exponential backoff") or ``None`` for "do not retry".

    Rules:
    * HTTP 400/401/403/404 → ``None`` (auth / not-found / bad-request
      are terminal).
    * ``insufficient_quota`` anywhere in the message → ``None``
      (Codex billing, not transient).
    * HTTP 529 (Anthropic-style overload) → retry unless
      ``background=True``, in which case bail immediately.
    * HTTP 429 / 500 / 502 / 503 / 504 → retry; honor ``Retry-After``
      if present.
    * ``httpx`` connection errors → retry with backoff.
    * ``openai.RateLimitError`` / ``APIConnectionError`` /
      ``APITimeoutError`` / ``InternalServerError`` and any
      ``APIError`` with a retryable ``.status_code`` → retry.
    * Anything else → ``None``.
    """
    # `insufficient_quota` is checked first because the Codex backend
    # frequently surfaces it under a 429 status code, which would
    # otherwise look retryable. Treating it as terminal stops the agent
    # from burning attempts on a billing problem.
    msg = str(exc) if exc is not None else ""
    if "insufficient_quota" in msg:
        return None

    status = _extract_status(exc)
    if status is not None:
        if status in {400, 401, 403, 404}:
            return None
        if status in _OVERLOAD_STATUS:
            if background:
                return None
            return _retry_after_or_backoff(exc)
        if status in _RETRYABLE_STATUS:
            return _retry_after_or_backoff(exc)
        # Unknown status — let it propagate.
        return None

    # Non-HTTP exceptions: openai / httpx network errors.
    if _is_connection_error(exc):
        return 0.0

    return None


def _extract_status(exc: BaseException) -> int | None:
    """Pull an HTTP status code off an openai/httpx exception, if any."""
    # openai 1.x APIError carries a `.status_code` attribute, but the
    # response.status_code on the .response object is the canonical
    # source. Fall through both.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        sc = getattr(response, "status_code", None)
        if isinstance(sc, int):
            return sc
    return None


def _retry_after_or_backoff(exc: BaseException) -> float:
    """Return the parsed ``Retry-After`` value or ``0.0`` for backoff.

    ``Retry-After`` is either a delta-seconds integer or an HTTP-date.
    We only honor the delta-seconds form — HTTP-date is rare in
    practice and parsing it would require a clock-skew tolerance we
    don't want to design here. Falls through to ``0.0`` (exponential
    backoff) on anything we can't parse.
    """
    response = getattr(exc, "response", None)
    headers: Any = None
    if response is not None:
        headers = getattr(response, "headers", None)
    if headers is None:
        # openai SDK sometimes attaches headers directly on the exception.
        headers = getattr(exc, "headers", None)
    if headers is None:
        return 0.0

    raw: str | None = None
    if hasattr(headers, "get"):
        try:
            raw = headers.get("Retry-After") or headers.get("retry-after")
        except Exception:  # noqa: BLE001
            raw = None
    if raw is None:
        return 0.0

    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    if seconds < 0:
        return 0.0
    return seconds


def _is_connection_error(exc: BaseException) -> bool:
    """Best-effort detection of httpx / openai connection failures.

    We pattern-match on class names instead of importing the modules so
    this helper stays cheap (no import-time httpx pull) and so test
    fixtures can shape fake exception classes without monkey-patching
    real httpx internals.
    """
    cls_name = type(exc).__name__
    if cls_name in {
        "ConnectError",
        "ReadError",
        "RemoteProtocolError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteError",
        "WriteTimeout",
        # openai SDK wraps these as:
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
    }:
        return True
    # APIError is the openai SDK root; only retry it when it carries a
    # retryable status (handled by `_extract_status` in the caller).
    return False


__all__ = [
    "default_retryable_codex",
    "with_retry",
]
