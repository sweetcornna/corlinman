"""Gap-fill (lane-providers) Anthropic provider tests — all offline.

Covers the provider-reliability + caching + auth work for the Anthropic
adapter:

* prompt caching: ``cache_control: {type: ephemeral}`` on the trailing tools
  entry / system block / trailing user turns, with the TOTAL marker count
  kept ``<= 4``;
* cache-usage accounting: ``cache_read_input_tokens`` /
  ``cache_creation_input_tokens`` surface on the ``done`` chunk's usage;
* ``is_error`` on the emitted ``tool_result`` block (contract C4 — the
  reasoning loop marks the ``role="tool"`` message with ``_is_error``);
* the ``anthropic-ratelimit-unified-reset`` header parsed onto the
  ``RateLimitError`` as ``reset_at_ms``;
* :class:`ContextOverflowError` carrying the parsed numeric ``limit`` /
  ``input_tokens`` / ``max_tokens``;
* the async single-flight OAuth ``_ensure_fresh`` refresh.

Strategy mirrors ``test_anthropic_provider.py``: a capturing fake client for
the stream-shape assertions, and direct calls into the small pure helpers
(``_inject_tools_cache_control``, ``_parse_context_overflow_limits``,
``_unified_reset_ms_from_exc``, ``_split_system``) so the SDK is not required.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_providers import AnthropicProvider, ProviderChunk
from corlinman_providers.anthropic_provider import (
    _inject_tools_cache_control,
    _parse_context_overflow_limits,
    _split_system,
    _unified_reset_ms_from_exc,
)
from corlinman_providers.failover import RateLimitError

# ---------------------------------------------------------------------------
# Capturing fake client — records the kwargs passed to ``messages.stream``.
# ---------------------------------------------------------------------------


class _CapturingStream:
    def __init__(self, captured: dict[str, Any], usage: Any) -> None:
        self._captured = captured
        self._usage = usage

    async def __aenter__(self) -> _CapturingStream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[Any]:
        async def _gen() -> AsyncIterator[Any]:
            if False:  # pragma: no cover — empty event stream
                yield None

        return _gen()

    async def get_final_message(self) -> Any:
        return SimpleNamespace(stop_reason="end_turn", usage=self._usage)


class _CapturingMessages:
    def __init__(self, captured: dict[str, Any], usage: Any) -> None:
        self._captured = captured
        self._usage = usage

    def stream(self, **kwargs: Any) -> _CapturingStream:
        self._captured.clear()
        self._captured.update(kwargs)
        return _CapturingStream(self._captured, self._usage)


class _CapturingClient:
    def __init__(self, captured: dict[str, Any], usage: Any = None) -> None:
        self.messages = _CapturingMessages(captured, usage)

    async def close(self) -> None:
        return None


def _patch_capture(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any], usage: Any = None
) -> None:
    import anthropic  # type: ignore[import-not-found]

    monkeypatch.setattr(
        anthropic, "AsyncAnthropic", lambda **_: _CapturingClient(captured, usage)
    )


def _count_cache_markers(obj: Any) -> int:
    """Count every ``cache_control`` key reachable through nested dicts/lists."""
    n = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "cache_control":
                n += 1
            else:
                n += _count_cache_markers(v)
    elif isinstance(obj, list):
        for item in obj:
            n += _count_cache_markers(item)
    return n


# ---------------------------------------------------------------------------
# Prompt caching — cache_control placement + the <= 4 marker cap.
# ---------------------------------------------------------------------------


async def test_cache_control_on_trailing_tools_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    captured: dict[str, Any] = {}
    _patch_capture(monkeypatch, captured)

    tools = [
        {"name": "a", "input_schema": {"type": "object"}},
        {"name": "b", "input_schema": {"type": "object"}},
    ]
    prov = AnthropicProvider()
    async for _ in prov.chat_stream(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
    ):
        pass

    sent_tools = captured["tools"]
    # Only the trailing tools entry carries the marker.
    assert "cache_control" not in sent_tools[0]
    assert sent_tools[-1]["cache_control"] == {"type": "ephemeral"}
    # The caller's list is not mutated.
    assert "cache_control" not in tools[-1]


async def test_total_cache_markers_capped_at_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    captured: dict[str, Any] = {}
    _patch_capture(monkeypatch, captured)

    big_system = "S" * 2048  # >= 1024 chars so user-turn caching kicks in
    tools = [{"name": "t", "input_schema": {"type": "object"}}]
    messages = [{"role": "system", "content": big_system}]
    # Several user turns so the user-turn injection has candidates to mark.
    for i in range(5):
        messages.append({"role": "user", "content": f"u{i}"})

    prov = AnthropicProvider()
    async for _ in prov.chat_stream(
        model="claude-sonnet-4-5", messages=messages, tools=tools
    ):
        pass

    total = (
        _count_cache_markers(captured.get("system"))
        + _count_cache_markers(captured.get("tools"))
        + _count_cache_markers(captured.get("messages"))
    )
    assert total <= 4, f"expected <= 4 cache_control markers, got {total}"
    # And at least the system + tools markers landed.
    assert _count_cache_markers(captured.get("system")) == 1
    assert _count_cache_markers(captured.get("tools")) == 1


async def test_no_cache_control_for_unsupported_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    captured: dict[str, Any] = {}
    _patch_capture(monkeypatch, captured)

    tools = [{"name": "t", "input_schema": {"type": "object"}}]
    prov = AnthropicProvider()
    async for _ in prov.chat_stream(
        model="claude-2.1",  # not in the caching prefix set
        messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}],
        tools=tools,
    ):
        pass

    total = (
        _count_cache_markers(captured.get("system"))
        + _count_cache_markers(captured.get("tools"))
        + _count_cache_markers(captured.get("messages"))
    )
    assert total == 0


def test_inject_tools_cache_control_idempotent_passthrough() -> None:
    assert _inject_tools_cache_control([]) == []
    # Non-dict trailing entry is returned unchanged.
    weird: list[Any] = ["not-a-dict"]
    assert _inject_tools_cache_control(weird) is weird


# ---------------------------------------------------------------------------
# Cache-usage accounting on the done chunk.
# ---------------------------------------------------------------------------


async def test_done_chunk_carries_cache_usage_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    captured: dict[str, Any] = {}
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=80,
        cache_creation_input_tokens=15,
    )
    _patch_capture(monkeypatch, captured, usage=usage)

    prov = AnthropicProvider()
    done: ProviderChunk | None = None
    async for chunk in prov.chat_stream(
        model="claude-sonnet-4-5", messages=[{"role": "user", "content": "hi"}]
    ):
        if chunk.kind == "done":
            done = chunk
    assert done is not None
    assert done.usage is not None
    assert done.usage["cache_read_input_tokens"] == 80
    assert done.usage["cache_creation_input_tokens"] == 15
    assert done.usage["input_tokens"] == 100


# ---------------------------------------------------------------------------
# is_error on the tool_result block (contract C4).
# ---------------------------------------------------------------------------


def test_split_system_sets_is_error_on_errored_tool_result() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "boom", "_is_error": True},
    ]
    _system, chat = _split_system(messages)
    # Find the user turn carrying the tool_result block.
    tool_results = [
        blk
        for m in chat
        if m["role"] == "user" and isinstance(m["content"], list)
        for blk in m["content"]
        if isinstance(blk, dict) and blk.get("type") == "tool_result"
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["is_error"] is True


def test_split_system_omits_is_error_on_successful_tool_result() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    _system, chat = _split_system(messages)
    tool_results = [
        blk
        for m in chat
        if m["role"] == "user" and isinstance(m["content"], list)
        for blk in m["content"]
        if isinstance(blk, dict) and blk.get("type") == "tool_result"
    ]
    assert len(tool_results) == 1
    assert "is_error" not in tool_results[0]


# ---------------------------------------------------------------------------
# anthropic-ratelimit-unified-reset header parse.
# ---------------------------------------------------------------------------


class _FakeHeaders(dict):
    """A header mapping with case-insensitive ``get`` is not needed — the
    provider tries both cases explicitly, so a plain dict suffices."""


def _exc_with_headers(headers: dict[str, str]) -> Exception:
    resp = SimpleNamespace(headers=headers)
    return SimpleNamespace(response=resp)  # type: ignore[return-value]


def test_unified_reset_absolute_epoch_seconds() -> None:
    future = int(time.time()) + 300
    exc = _exc_with_headers({"anthropic-ratelimit-unified-reset": str(future)})
    ms = _unified_reset_ms_from_exc(exc)  # type: ignore[arg-type]
    assert ms == future * 1000


def test_unified_reset_relative_delta_seconds() -> None:
    before = time.time()
    exc = _exc_with_headers({"anthropic-ratelimit-unified-reset": "30"})
    ms = _unified_reset_ms_from_exc(exc)  # type: ignore[arg-type]
    assert ms is not None
    # 30s delta → roughly now + 30s in ms.
    assert (before + 29) * 1000 <= ms <= (time.time() + 31) * 1000


def test_unified_reset_absent_returns_none() -> None:
    exc = _exc_with_headers({"retry-after": "7"})
    assert _unified_reset_ms_from_exc(exc) is None  # type: ignore[arg-type]


async def test_429_unified_reset_attached_to_rate_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a 429 carrying the unified-reset header surfaces
    ``reset_at_ms`` on the raised :class:`RateLimitError`."""
    import httpx
    import respx

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    # Force the SDK to not retry internally.
    import anthropic  # type: ignore[import-not-found]

    orig = anthropic.AsyncAnthropic

    def _no_retry(*a: Any, **kw: Any) -> Any:
        kw.setdefault("max_retries", 0)
        return orig(*a, **kw)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _no_retry)

    future = int(time.time()) + 120
    with respx.mock(base_url="https://api.anthropic.com") as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(
                429,
                headers={
                    "retry-after": "5",
                    "anthropic-ratelimit-unified-reset": str(future),
                },
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
            )
        )
        prov = AnthropicProvider()
        with pytest.raises(RateLimitError) as exc_info:
            async for _ in prov.chat_stream(
                model="claude-sonnet-4-5",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=8,
            ):
                pass

    err = exc_info.value
    assert err.retry_after_ms == 5000
    assert getattr(err, "reset_at_ms", None) == future * 1000


# ---------------------------------------------------------------------------
# ContextOverflowError carries the parsed numeric limit.
# ---------------------------------------------------------------------------


def test_parse_context_overflow_limits_triple() -> None:
    msg = "input length and `max_tokens` exceed context limit: 200000 + 8192 > 200000"
    parsed = _parse_context_overflow_limits(msg)
    assert parsed == (200000, 8192, 200000)


def test_parse_context_overflow_limits_with_commas() -> None:
    parsed = _parse_context_overflow_limits("tokens: 195,000 + 8,192 > 200,000")
    assert parsed == (195000, 8192, 200000)


def test_parse_context_overflow_limits_no_match() -> None:
    assert _parse_context_overflow_limits("prompt is too long") is None


async def test_context_overflow_error_carries_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    import respx
    from corlinman_providers.failover import ContextOverflowError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    import anthropic  # type: ignore[import-not-found]

    orig = anthropic.AsyncAnthropic

    def _no_retry(*a: Any, **kw: Any) -> Any:
        kw.setdefault("max_retries", 0)
        return orig(*a, **kw)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _no_retry)

    body_msg = "input length and max_tokens exceed context limit: 199000 + 8192 > 200000"
    with respx.mock(base_url="https://api.anthropic.com") as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(
                400,
                json={"type": "error", "error": {"type": "invalid_request_error", "message": body_msg}},
            )
        )
        prov = AnthropicProvider()
        with pytest.raises(ContextOverflowError) as exc_info:
            async for _ in prov.chat_stream(
                model="claude-sonnet-4-5",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=8,
            ):
                pass

    err = exc_info.value
    assert getattr(err, "limit", None) == 200000
    assert getattr(err, "input_tokens", None) == 199000
    assert getattr(err, "max_tokens", None) == 8192


# ---------------------------------------------------------------------------
# OAuth _ensure_fresh single-flight.
# ---------------------------------------------------------------------------


def _write_oauth_file(tmp_path: Any, *, expires_at_ms: int, refresh: str = "rt-old") -> Any:
    oauth_dir = tmp_path / ".oauth"
    oauth_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "provider": "anthropic",
        "access_token": "at-old",
        "refresh_token": refresh,
        "expires_at_ms": expires_at_ms,
        "scope": None,
        "obtained_at_ms": int(time.time() * 1000),
    }
    (oauth_dir / "anthropic.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


async def test_ensure_fresh_single_flight_one_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Two concurrent ``_ensure_fresh`` calls on an expiring credential issue
    exactly ONE token-endpoint POST (single-flight via the asyncio.Lock)."""
    import asyncio

    import corlinman_providers.anthropic_provider as ap

    # Credential expired (in the past) so a refresh is due.
    data_dir = _write_oauth_file(tmp_path, expires_at_ms=int(time.time() * 1000) - 1000)

    calls = {"n": 0}

    async def _fake_refresh(*, refresh_token: str) -> dict[str, Any]:
        calls["n"] += 1
        await asyncio.sleep(0.01)  # widen the race window
        return {
            "access_token": "at-new",
            "refresh_token": "rt-new",
            "expires_at_ms": int(time.time() * 1000) + 3_600_000,
        }

    monkeypatch.setattr(ap, "refresh_anthropic_token", _fake_refresh)

    prov = AnthropicProvider(data_dir=data_dir)
    await asyncio.gather(prov._ensure_fresh(), prov._ensure_fresh())

    assert calls["n"] == 1
    # The refreshed token is now what the resolution chain returns.
    token, style = prov._credential_resolution()
    assert token == "at-new"
    assert style == "bearer"


async def test_ensure_fresh_noop_when_token_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import corlinman_providers.anthropic_provider as ap

    data_dir = _write_oauth_file(
        tmp_path, expires_at_ms=int(time.time() * 1000) + 3_600_000
    )

    calls = {"n": 0}

    async def _fake_refresh(*, refresh_token: str) -> dict[str, Any]:
        calls["n"] += 1
        return {"access_token": "x", "refresh_token": "y", "expires_at_ms": 0}

    monkeypatch.setattr(ap, "refresh_anthropic_token", _fake_refresh)

    prov = AnthropicProvider(data_dir=data_dir)
    await prov._ensure_fresh()
    assert calls["n"] == 0


async def test_async_refresh_credential_returns_true_on_rotation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import corlinman_providers.anthropic_provider as ap

    data_dir = _write_oauth_file(
        tmp_path, expires_at_ms=int(time.time() * 1000) + 3_600_000
    )

    async def _fake_refresh(*, refresh_token: str) -> dict[str, Any]:
        return {
            "access_token": "at-rotated",
            "refresh_token": "rt-rotated",
            "expires_at_ms": int(time.time() * 1000) + 3_600_000,
        }

    monkeypatch.setattr(ap, "refresh_anthropic_token", _fake_refresh)

    prov = AnthropicProvider(data_dir=data_dir)
    recovered = await prov._async_refresh_credential()
    assert recovered is True
    token, _ = prov._credential_resolution()
    assert token == "at-rotated"
