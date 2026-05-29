# TEST-003 — discovered defects (not fixed under this ticket)

## 1. Anthropic mapper drops the `retry-after` header on 429

**Location:** `python/packages/corlinman-providers/src/corlinman_providers/anthropic_provider.py`,
`_map_anthropic_error`, the `AnthRateLimit` branch:

```python
if isinstance(exc, AnthRateLimit):
    return RateLimitError(str(exc), status_code=429, **ctx)
```

**Problem.** `failover.RateLimitError` carries a dedicated
`retry_after_ms: int | None` field for exactly this case. The
Anthropic SDK preserves the upstream `retry-after` header on
`exc.response.headers` (verified with the SDK installed locally —
`anthropic==0.104.1` keeps the field via `httpx.Response.headers`), so
the mapper has the value in hand. It just doesn't read it. The
resulting `RateLimitError.retry_after_ms` is always `None`, and the
Rust agent client's backoff layer falls back to the generic
`DEFAULT_SCHEDULE` instead of honouring the vendor-suggested wait.

Same bug in `openai_provider.py::_map_openai_error` — the OaRateLimit
branch is structurally identical.

**Impact.** Functional, not security-critical. Failover still picks
the next provider correctly (the *class* is right). What's lost is the
chance to wait exactly long enough on the current provider to come
back. In the worst case (long retry-after, e.g. 30s on a vendor-side
queue), Corlinman either retries too early and gets re-throttled (if
the backoff schedule's first step is shorter) or moves to a more
expensive fallback model when it could have waited cheaply.

**Test coverage status.** `test_anthropic_provider_error_mapping.py
::test_429_with_retry_after_header` pins the *current shipped*
behaviour with an assertion of `err.retry_after_ms is None` plus a
TODO-style comment. When this bug is fixed, the test asserts the
extracted value (`7000` for the fixture's `retry-after: 7`), and this
discovered.md entry can be removed.

**No xfail.** The test of the current behaviour passes — there's no
xfail needed. The bug is in the production code's lack of extraction,
not in any failing assertion.

---

(No other defects discovered while writing TEST-003.)
