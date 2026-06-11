# R1-003 — Discovered (queued for next round)

While fixing the OpenAI + Anthropic client-close leak, several sibling
providers were inspected and exhibit the same lifecycle bug pattern.
Per spec for R1-003 these are NOT in scope for this commit — they need
their own audit rows + their own TDD pass. Filed here so round 2 can
batch them.

## Same leak shape — third-party SDK client never closed

### Google (Gemini) — `google_provider.py:114-145`
- `_open()` constructs `genai.Client(api_key=…)` per chat call.
- After the open, `client` falls out of scope without `client.close()` or `async with`.
- `google.genai.Client` wraps an `httpx.AsyncClient` internally — same fd / TLS-session leak surface.
- Also leaks on the 401-retry path inside `with_401_recovery` (first client abandoned).

### Codex (`gpt-5` family) — `codex_provider.py:296-451`
- `chat_stream` calls `self._make_client()` at line 328 and a SECOND time at line 426 inside the `_attempt_token_recovery` branch.
- Neither client is `.close()`d. Both abandoned `AsyncOpenAI` instances leak their httpx pools.
- More acute than OpenAI/Anthropic because the retry branch deterministically constructs a second client every time a `token_invalidated` fires (every 5–10 minutes during Codex auth-server hiccups, observed in field).

### Bedrock — `bedrock_provider.py` (around `_open()`)
- Uses `httpx.AsyncClient` directly (not a vendor SDK). Per-request construction visible at the `_open()` factory used inside `with_401_recovery`.
- Needs the same `try/finally: await client.aclose()` treatment. The AWS-SigV4 retry path will create multiple clients on key rotation.

## Recommendation for next round
Open three new audit rows:
- **R2-NEW-A** (high) — Google provider client leak, exact same fix shape as R1-003 (one helper, `try/finally`).
- **R2-NEW-B** (high) — Codex provider double-client leak (success + recovery paths).
- **R2-NEW-C** (med) — Bedrock httpx.AsyncClient leak.

All three are mechanically similar enough to share one `_safe_close`
helper hoisted into a small `_lifecycle.py` module — worth a refactor
once round 2 confirms the pattern is universal.

## Out of scope — already correct
- **Azure** (`azure_provider.py`): subclasses `OpenAIProvider` and only overrides `_make_client()`. Inherits the now-fixed `chat_stream` verbatim — close runs through the parent's `try/finally`. Verified by running the existing `test_azure_provider.py` regression (8 tests pass).
- **OpenAI-compatible market siblings** (Mistral, Cohere, Together, Groq, Replicate, DeepSeek/Qwen/GLM): all `OpenAICompatibleProvider` subclasses, inherit the fix. Verified by `test_auth_refresh.py::test_openai_compatible_reactive_401_refresh` still passing.
- **Anthropic OAuth file-watcher refresh path**: orthogonal to client lifecycle — runs at credential resolution time before the client is built, so no leak interaction.

## Out of scope — perf follow-up (already tracked)
- **PERF-001 / PERF-002** — client-per-request construction itself.
  The fix here closes correctly; it does NOT cache the client across
  requests. Per spec, hoisting a credential-keyed cache onto the
  provider instance is a separate larger change. The lifecycle fix is
  a prerequisite (you can't safely reuse a leaked client either way).
