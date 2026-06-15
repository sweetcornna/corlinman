"""Tests for the Codex OAuth provider.

Covers:
* :mod:`corlinman_providers._codex_oauth` — credential loading + refresh
* :class:`corlinman_providers.codex_provider.CodexProvider` — build + auto-refresh
* :func:`corlinman_providers.codex_provider._messages_to_responses_input` — conversion
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from corlinman_providers._codex_oauth import (
    CodexOAuthCredential,
    CodexOAuthRefreshError,
    _decode_jwt_exp,
    codex_cloudflare_headers,
    load_codex_credential,
)
from corlinman_providers.codex_provider import CodexProvider, _messages_to_responses_input
from corlinman_providers.specs import ProviderKind, ProviderSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_auth_json(path: Path, *, access_token: str = "tok-access",
                     refresh_token: str | None = "tok-refresh") -> None:
    tokens: dict[str, Any] = {"access_token": access_token}
    if refresh_token:
        tokens["refresh_token"] = refresh_token
    (path / "auth.json").write_text(
        json.dumps({"tokens": tokens, "OPENAI_API_KEY": None, "last_refresh": "2026-01-01"}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# _decode_jwt_exp
# ---------------------------------------------------------------------------


class TestDecodeJwtExp:
    def test_returns_none_for_non_jwt(self) -> None:
        assert _decode_jwt_exp("not-a-jwt") is None

    def test_returns_exp_in_ms(self) -> None:
        import base64
        # Build a minimal JWT payload with exp = 2000000000 (far future)
        payload = json.dumps({"exp": 2_000_000_000}).encode()
        b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
        fake_jwt = f"header.{b64}.sig"
        result = _decode_jwt_exp(fake_jwt)
        assert result == 2_000_000_000_000  # ms

    def test_returns_none_when_no_exp(self) -> None:
        import base64
        payload = json.dumps({"sub": "user"}).encode()
        b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
        assert _decode_jwt_exp(f"hdr.{b64}.sig") is None


# ---------------------------------------------------------------------------
# load_codex_credential
# ---------------------------------------------------------------------------


class TestLoadCodexCredential:
    def test_returns_none_when_file_absent(self, tmp_path: Path) -> None:
        assert load_codex_credential(tmp_path / "nope.json") is None

    def test_loads_credential(self, tmp_path: Path) -> None:
        _write_auth_json(tmp_path, access_token="at-123", refresh_token="rt-456")
        cred = load_codex_credential(tmp_path / "auth.json")
        assert cred is not None
        assert cred.access_token == "at-123"
        assert cred.refresh_token == "rt-456"

    def test_returns_none_for_missing_tokens_key(self, tmp_path: Path) -> None:
        (tmp_path / "auth.json").write_text('{"OPENAI_API_KEY": null}', encoding="utf-8")
        assert load_codex_credential(tmp_path / "auth.json") is None

    def test_returns_none_for_malformed_json(self, tmp_path: Path) -> None:
        (tmp_path / "auth.json").write_text("not json", encoding="utf-8")
        assert load_codex_credential(tmp_path / "auth.json") is None

    def test_no_refresh_token_is_ok(self, tmp_path: Path) -> None:
        _write_auth_json(tmp_path, access_token="at", refresh_token=None)
        cred = load_codex_credential(tmp_path / "auth.json")
        assert cred is not None
        assert cred.refresh_token is None


# ---------------------------------------------------------------------------
# CodexOAuthCredential.is_expired
# ---------------------------------------------------------------------------


class TestCodexOAuthCredentialIsExpired:
    def test_not_expired_when_no_exp(self) -> None:
        c = CodexOAuthCredential(access_token="t", refresh_token=None, expires_at_ms=None)
        assert not c.is_expired()

    def test_expired_when_past_skew(self) -> None:
        past_ms = int(time.time() * 1000) - 1  # already past skew threshold
        c = CodexOAuthCredential(access_token="t", refresh_token=None, expires_at_ms=past_ms)
        assert c.is_expired()

    def test_not_expired_when_far_future(self) -> None:
        future_ms = int(time.time() * 1000) + 3_600_000  # 1 hour
        c = CodexOAuthCredential(access_token="t", refresh_token=None, expires_at_ms=future_ms)
        assert not c.is_expired()


# ---------------------------------------------------------------------------
# codex_cloudflare_headers
# ---------------------------------------------------------------------------


class TestCodexCloudflareHeaders:
    def test_basic_headers_always_present(self) -> None:
        headers = codex_cloudflare_headers("plain-not-a-jwt")
        assert headers["User-Agent"] == "codex_cli_rs/0.0.0"
        assert headers["originator"] == "codex_cli_rs"
        assert "ChatGPT-Account-ID" not in headers  # no valid JWT claims

    def test_account_id_extracted_from_jwt(self) -> None:
        import base64
        claims = {
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-abc123"}
        }
        payload = base64.urlsafe_b64encode(
            json.dumps(claims).encode()
        ).rstrip(b"=").decode()
        fake_jwt = f"header.{payload}.sig"
        headers = codex_cloudflare_headers(fake_jwt)
        assert headers["ChatGPT-Account-ID"] == "acct-abc123"


# ---------------------------------------------------------------------------
# CodexProvider.build
# ---------------------------------------------------------------------------


class TestCodexProviderBuild:
    def test_build_raises_when_no_auth_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEX_HOME", "/nonexistent/path/that/does/not/exist")
        spec = ProviderSpec(name="codex", kind=ProviderKind.CODEX)
        with pytest.raises(RuntimeError, match="codex login"):
            CodexProvider.build(spec)

    def test_build_succeeds_with_auth_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_auth_json(tmp_path, access_token="at-ok")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        spec = ProviderSpec(name="codex", kind=ProviderKind.CODEX)
        prov = CodexProvider.build(spec)
        assert prov._credential.access_token == "at-ok"

    def test_build_uses_data_dir_auth_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "data"
        codex_dir = data_dir / ".codex"
        codex_dir.mkdir(parents=True)
        _write_auth_json(codex_dir, access_token="at-data-dir")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-codex-home"))
        spec = ProviderSpec(name="codex", kind=ProviderKind.CODEX)

        prov = CodexProvider.build(spec, data_dir=data_dir)

        assert prov._credential.access_token == "at-data-dir"

    def test_provider_not_openai_subclass(self) -> None:
        """CodexProvider must NOT extend OpenAIProvider — it uses a different API."""
        from corlinman_providers.openai_provider import OpenAIProvider
        assert not issubclass(CodexProvider, OpenAIProvider)


# ---------------------------------------------------------------------------
# CodexProvider.supports
# ---------------------------------------------------------------------------


class TestCodexProviderSupports:
    @pytest.mark.parametrize(
        "model",
        [
            "gpt-5.5",
            "gpt-4o",
            "o1-mini",
            "o3-pro",
            "o4-mini",
            "codex-mini",
            "chatgpt-4o-latest",
            "chatgpt-4o",
        ],
    )
    def test_supported_models(self, model: str) -> None:
        assert CodexProvider.supports(model)

    @pytest.mark.parametrize("model", ["claude-3-5-sonnet", "gemini-pro", "deepseek-chat"])
    def test_unsupported_models(self, model: str) -> None:
        assert not CodexProvider.supports(model)


# ---------------------------------------------------------------------------
# _messages_to_responses_input
# ---------------------------------------------------------------------------


class TestMessagesToResponsesInput:
    def test_user_message_dict(self) -> None:
        result = _messages_to_responses_input([{"role": "user", "content": "hello"}])
        assert result == [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]

    def test_assistant_message_dict(self) -> None:
        result = _messages_to_responses_input([{"role": "assistant", "content": "hi there"}])
        assert result == [{"role": "assistant", "content": [{"type": "output_text", "text": "hi there"}]}]

    def test_system_messages_skipped(self) -> None:
        """System messages are handled as instructions — not passed to input."""
        result = _messages_to_responses_input([{"role": "system", "content": "be helpful"}])
        assert result == []

    def test_mixed_conversation(self) -> None:
        msgs = [
            {"role": "user", "content": "ping"},
            {"role": "assistant", "content": "pong"},
            {"role": "user", "content": "again"},
        ]
        result = _messages_to_responses_input(msgs)
        assert len(result) == 3
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "user"

    def test_object_message_with_attributes(self) -> None:
        from types import SimpleNamespace
        msg = SimpleNamespace(role="user", content="hi")
        result = _messages_to_responses_input([msg])
        assert result == [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]

    def test_none_content_becomes_empty_string(self) -> None:
        result = _messages_to_responses_input([{"role": "user", "content": None}])
        assert result[0]["content"][0]["text"] == ""

    def test_assistant_tool_calls_become_function_call_items(self) -> None:
        """An assistant message with tool_calls → one function_call item per call."""
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "calculator", "arguments": '{"expr": "2+2"}'},
                }
            ],
        }
        result = _messages_to_responses_input([msg])
        assert result == [
            {
                "type": "function_call",
                "call_id": "call_abc",
                "name": "calculator",
                "arguments": '{"expr": "2+2"}',
            }
        ]

    def test_tool_message_becomes_function_call_output(self) -> None:
        """A role=tool message → function_call_output keyed by tool_call_id."""
        msg = {"role": "tool", "tool_call_id": "call_abc", "content": "4"}
        result = _messages_to_responses_input([msg])
        assert result == [
            {"type": "function_call_output", "call_id": "call_abc", "output": "4"}
        ]

    def test_tool_round_trip_conversion(self) -> None:
        """A full tool round (user → assistant tool_calls → tool result) converts."""
        msgs = [
            {"role": "user", "content": "what is 2+2"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": '{"expr":"2+2"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "4"},
        ]
        result = _messages_to_responses_input(msgs)
        assert result[0]["role"] == "user"
        assert result[1]["type"] == "function_call"
        assert result[1]["call_id"] == "call_1"
        assert result[2]["type"] == "function_call_output"
        assert result[2]["call_id"] == "call_1"


# ---------------------------------------------------------------------------
# CodexProvider.chat_stream — tool-call streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_stream_emits_tool_call_chunks() -> None:
    """Responses API function-call events become tool_call_* chunks."""
    from types import SimpleNamespace

    future_ms = int(time.time() * 1000) + 3_600_000
    cred = CodexOAuthCredential(
        access_token="good-token", refresh_token=None, expires_at_ms=future_ms
    )
    prov = CodexProvider(credential=cred)

    fn_item = SimpleNamespace(
        type="function_call", id="fc_1", call_id="call_1", name="calculator",
        arguments="",
    )
    events = [
        SimpleNamespace(type="response.output_item.added", item=fn_item),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            item_id="fc_1", delta='{"expr":',
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            item_id="fc_1", delta='"2+2"}',
        ),
        SimpleNamespace(type="response.output_item.done", item=fn_item),
    ]

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for e in events:
                yield e

    class _FakeResponses:
        def stream(self, **_kwargs):
            return _FakeStream()

    class _FakeClient:
        responses = _FakeResponses()

    with patch.object(prov, "_make_client", return_value=_FakeClient()):
        chunks = []
        async for chunk in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "what is 2+2"}],
            tools=[{"function": {"name": "calculator", "parameters": {}}}],
        ):
            chunks.append(chunk)

    starts = [c for c in chunks if c.kind == "tool_call_start"]
    deltas = [c for c in chunks if c.kind == "tool_call_delta"]
    ends = [c for c in chunks if c.kind == "tool_call_end"]
    assert len(starts) == 1
    assert starts[0].tool_call_id == "call_1"
    assert starts[0].tool_name == "calculator"
    assert "".join(d.arguments_delta for d in deltas) == '{"expr":"2+2"}'
    assert all(d.tool_call_id == "call_1" for d in deltas)
    assert len(ends) == 1
    assert ends[0].tool_call_id == "call_1"
    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_chat_stream_tool_call_oneshot_args() -> None:
    """When the backend ships args only on output_item.done, replay as a delta."""
    from types import SimpleNamespace

    future_ms = int(time.time() * 1000) + 3_600_000
    cred = CodexOAuthCredential(
        access_token="good-token", refresh_token=None, expires_at_ms=future_ms
    )
    prov = CodexProvider(credential=cred)

    added_item = SimpleNamespace(
        type="function_call", id="fc_9", call_id="call_9", name="web_search",
        arguments="",
    )
    done_item = SimpleNamespace(
        type="function_call", id="fc_9", call_id="call_9", name="web_search",
        arguments='{"query":"weather"}',
    )
    events = [
        SimpleNamespace(type="response.output_item.added", item=added_item),
        SimpleNamespace(type="response.output_item.done", item=done_item),
    ]

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for e in events:
                yield e

    class _FakeResponses:
        def stream(self, **_kwargs):
            return _FakeStream()

    class _FakeClient:
        responses = _FakeResponses()

    with patch.object(prov, "_make_client", return_value=_FakeClient()):
        chunks = []
        async for chunk in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "weather?"}],
            tools=[{"function": {"name": "web_search", "parameters": {}}}],
        ):
            chunks.append(chunk)

    deltas = [c for c in chunks if c.kind == "tool_call_delta"]
    assert "".join(d.arguments_delta for d in deltas) == '{"query":"weather"}'
    assert chunks[-1].finish_reason == "tool_calls"


# ---------------------------------------------------------------------------
# CodexProvider.chat_stream — auto-refresh on expired token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_stream_refreshes_expired_token() -> None:
    """When the token is expired, _ensure_fresh calls refresh_codex_token."""
    expired_ms = int(time.time() * 1000) - 1
    cred = CodexOAuthCredential(
        access_token="old-token",
        refresh_token="rt-xyz",
        expires_at_ms=expired_ms,
    )
    prov = CodexProvider(credential=cred)

    new_cred = CodexOAuthCredential(
        access_token="new-token",
        refresh_token="rt-xyz",
        expires_at_ms=int(time.time() * 1000) + 3_600_000,
    )
    mock_refresh = AsyncMock(return_value=new_cred)

    # Fake stream context manager that yields nothing then exits cleanly.
    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _FakeResponses:
        def stream(self, **_kwargs):
            return _FakeStream()

    class _FakeClient:
        responses = _FakeResponses()

    with (
        patch("corlinman_providers.codex_provider.refresh_codex_token", mock_refresh),
        patch.object(prov, "_make_client", return_value=_FakeClient()),
    ):
        chunks = []
        async for chunk in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "ping"}],
        ):
            chunks.append(chunk)

    mock_refresh.assert_awaited_once()
    assert prov._credential.access_token == "new-token"
    # Should end with a done chunk.
    assert chunks[-1].kind == "done"


@pytest.mark.asyncio
async def test_chat_stream_no_refresh_when_fresh() -> None:
    """When the token is not expired, refresh is not called."""
    future_ms = int(time.time() * 1000) + 3_600_000
    cred = CodexOAuthCredential(
        access_token="good-token",
        refresh_token="rt-xyz",
        expires_at_ms=future_ms,
    )
    prov = CodexProvider(credential=cred)
    mock_refresh = AsyncMock()

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _FakeResponses:
        def stream(self, **_kwargs):
            return _FakeStream()

    class _FakeClient:
        responses = _FakeResponses()

    with (
        patch("corlinman_providers.codex_provider.refresh_codex_token", mock_refresh),
        patch.object(prov, "_make_client", return_value=_FakeClient()),
    ):
        async for _ in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "ping"}],
        ):
            pass

    mock_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_stream_emits_token_deltas() -> None:
    """Text deltas from output_text.delta events become token chunks."""
    future_ms = int(time.time() * 1000) + 3_600_000
    cred = CodexOAuthCredential(
        access_token="good-token",
        refresh_token=None,
        expires_at_ms=future_ms,
    )
    prov = CodexProvider(credential=cred)

    from types import SimpleNamespace

    events = [
        SimpleNamespace(type="response.output_text.delta", delta="Hello"),
        SimpleNamespace(type="response.output_text.delta", delta=" world"),
    ]

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return iter(events).__aiter__()

    async def _fake_aiter():
        for e in events:
            yield e

    class _FakeStreamIter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for e in events:
                yield e

    class _FakeResponses:
        def stream(self, **_kwargs):
            return _FakeStreamIter()

    class _FakeClient:
        responses = _FakeResponses()

    with patch.object(prov, "_make_client", return_value=_FakeClient()):
        chunks = []
        async for chunk in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
        ):
            chunks.append(chunk)

    token_chunks = [c for c in chunks if c.kind == "token"]
    assert len(token_chunks) == 2
    assert token_chunks[0].text == "Hello"
    assert token_chunks[1].text == " world"
    done_chunks = [c for c in chunks if c.kind == "done"]
    assert done_chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_chat_stream_uses_extra_reasoning_effort() -> None:
    """Per-turn reasoning_effort should override Codex's balanced default."""
    future_ms = int(time.time() * 1000) + 3_600_000
    cred = CodexOAuthCredential(
        access_token="good-token",
        refresh_token=None,
        expires_at_ms=future_ms,
    )
    prov = CodexProvider(credential=cred)

    from types import SimpleNamespace

    captured: dict[str, Any] = {}
    events = [SimpleNamespace(type="response.output_text.delta", delta="ok")]

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for e in events:
                yield e

    class _FakeResponses:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return _FakeStream()

    class _FakeClient:
        responses = _FakeResponses()

    with patch.object(prov, "_make_client", return_value=_FakeClient()):
        chunks = []
        async for chunk in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
            extra={"reasoning_effort": "high"},
        ):
            chunks.append(chunk)

    assert captured["reasoning"]["effort"] == "high"
    assert chunks[-1].kind == "done"


@pytest.mark.asyncio
async def test_chat_stream_handles_stream_error() -> None:
    """Exceptions during streaming result in a done/error chunk, not a crash."""
    future_ms = int(time.time() * 1000) + 3_600_000
    cred = CodexOAuthCredential(
        access_token="good-token",
        refresh_token=None,
        expires_at_ms=future_ms,
    )
    prov = CodexProvider(credential=cred)

    class _ErrorStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            raise RuntimeError("network error")
            yield  # noqa: unreachable — makes this a generator

    class _FakeResponses:
        def stream(self, **_kwargs):
            return _ErrorStream()

    class _FakeClient:
        responses = _FakeResponses()

    with patch.object(prov, "_make_client", return_value=_FakeClient()):
        chunks = []
        async for chunk in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
        ):
            chunks.append(chunk)

    assert any(c.kind == "done" and c.finish_reason == "error" for c in chunks)


# ---------------------------------------------------------------------------
# CodexProvider.chat_stream — retry / backoff (T1.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_stream_retries_on_429_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 with ``Retry-After: 0.1`` is retried; the second attempt streams."""
    from types import SimpleNamespace

    future_ms = int(time.time() * 1000) + 3_600_000
    cred = CodexOAuthCredential(
        access_token="good-token", refresh_token=None, expires_at_ms=future_ms
    )
    prov = CodexProvider(credential=cred)

    class _Fake429(Exception):
        def __init__(self) -> None:
            super().__init__("HTTP 429 rate limited")
            self.status_code = 429
            self.response = SimpleNamespace(
                status_code=429, headers={"Retry-After": "0.1"}
            )

    events = [
        SimpleNamespace(type="response.output_text.delta", delta="hello"),
    ]

    class _GoodStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for e in events:
                yield e

    class _Raise429Stream:
        """Raises 429 right on ``__aenter__`` to force a retry of the open phase."""

        async def __aenter__(self):
            raise _Fake429()

        async def __aexit__(self, *_):
            return False

    call_count = {"n": 0}

    class _FakeResponses:
        def stream(self, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _Raise429Stream()
            return _GoodStream()

    class _FakeClient:
        responses = _FakeResponses()

    # Stub asyncio.sleep so the test is instant and we can assert the
    # delay equals Retry-After exactly (no jitter for header-driven retries).
    slept: list[float] = []

    async def _fake_sleep(d: float) -> None:
        slept.append(d)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)

    with patch.object(prov, "_make_client", return_value=_FakeClient()):
        chunks = []
        async for chunk in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "ping"}],
        ):
            chunks.append(chunk)

    assert call_count["n"] == 2  # one retry happened
    assert slept == [0.1]  # Retry-After honored verbatim

    token_chunks = [c for c in chunks if c.kind == "token"]
    assert len(token_chunks) == 1
    assert token_chunks[0].text == "hello"
    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_chat_stream_does_not_retry_on_insufficient_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``insufficient_quota`` is a billing error — terminal even under a 429."""
    from types import SimpleNamespace

    future_ms = int(time.time() * 1000) + 3_600_000
    cred = CodexOAuthCredential(
        access_token="good-token", refresh_token=None, expires_at_ms=future_ms
    )
    prov = CodexProvider(credential=cred)

    class _QuotaError(Exception):
        def __init__(self) -> None:
            super().__init__("Error 429: insufficient_quota — billing problem")
            self.status_code = 429
            self.response = SimpleNamespace(status_code=429, headers={})

    class _RaiseQuotaStream:
        async def __aenter__(self):
            raise _QuotaError()

        async def __aexit__(self, *_):
            return False

    call_count = {"n": 0}

    class _FakeResponses:
        def stream(self, **_kwargs):
            call_count["n"] += 1
            return _RaiseQuotaStream()

    class _FakeClient:
        responses = _FakeResponses()

    slept: list[float] = []

    async def _fake_sleep(d: float) -> None:
        slept.append(d)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)

    with patch.object(prov, "_make_client", return_value=_FakeClient()):
        chunks = []
        async for chunk in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "ping"}],
        ):
            chunks.append(chunk)

    assert call_count["n"] == 1  # no retry — billing problem is terminal
    assert slept == []  # never slept
    # The provider surfaces the open failure as a done/error chunk so
    # the reasoning loop can decide what to do with the turn.
    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "error"


# ---------------------------------------------------------------------------
# CodexProvider.chat_stream — usage / cost tracking (T1.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_stream_emits_usage_on_done() -> None:
    """A ``response.completed`` event's usage rides the terminal done chunk.

    The Responses API streams a ``response.completed`` event at the
    very end carrying ``event.response.usage`` with vendor token
    accounting. T1.4 captures the documented integer fields
    (``input_tokens``, ``output_tokens``, plus optional
    ``cached_input_tokens`` / ``cached_output_tokens`` /
    ``reasoning_tokens``) and attaches them as a plain ``dict`` on the
    terminal ``done`` :class:`ProviderChunk`, where the reasoning loop
    forwards them onto the outer :class:`DoneEvent`.
    """
    from types import SimpleNamespace

    future_ms = int(time.time() * 1000) + 3_600_000
    cred = CodexOAuthCredential(
        access_token="good-token", refresh_token=None, expires_at_ms=future_ms
    )
    prov = CodexProvider(credential=cred)

    usage_obj = SimpleNamespace(
        input_tokens=10,
        output_tokens=20,
        cached_input_tokens=3,
        # output_tokens_details etc. omitted — the extractor ignores
        # everything outside the documented integer keys.
    )
    response_obj = SimpleNamespace(usage=usage_obj, status="completed")
    events = [
        SimpleNamespace(type="response.output_text.delta", delta="hi"),
        SimpleNamespace(type="response.completed", response=response_obj),
    ]

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for e in events:
                yield e

    class _FakeResponses:
        def stream(self, **_kwargs):
            return _FakeStream()

    class _FakeClient:
        responses = _FakeResponses()

    with patch.object(prov, "_make_client", return_value=_FakeClient()):
        chunks = []
        async for chunk in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
        ):
            chunks.append(chunk)

    done = chunks[-1]
    assert done.kind == "done"
    assert done.finish_reason == "stop"
    assert done.usage == {
        "input_tokens": 10,
        "output_tokens": 20,
        "cached_input_tokens": 3,
    }


@pytest.mark.asyncio
async def test_chat_stream_done_usage_none_when_upstream_omits() -> None:
    """Without a ``response.completed`` event, the done chunk's usage is None.

    Older Codex backends and the legacy / pre-Responses test fakes
    don't emit ``response.completed``. The provider must keep the
    ``usage`` attribute at its default ``None`` in that case so the
    cost meter cleanly skips the turn instead of recording zeros.
    """
    from types import SimpleNamespace

    future_ms = int(time.time() * 1000) + 3_600_000
    cred = CodexOAuthCredential(
        access_token="good-token", refresh_token=None, expires_at_ms=future_ms
    )
    prov = CodexProvider(credential=cred)

    events = [
        SimpleNamespace(type="response.output_text.delta", delta="ok"),
    ]

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for e in events:
                yield e

    class _FakeResponses:
        def stream(self, **_kwargs):
            return _FakeStream()

    class _FakeClient:
        responses = _FakeResponses()

    with patch.object(prov, "_make_client", return_value=_FakeClient()):
        chunks = []
        async for chunk in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
        ):
            chunks.append(chunk)

    done = chunks[-1]
    assert done.kind == "done"
    assert done.usage is None


# ---------------------------------------------------------------------------
# Reactive token recovery — 401 token_invalidated → refresh → retry once
# ---------------------------------------------------------------------------


def _fake_invalidated_401() -> Exception:
    """Build an exception that looks like a Codex token_invalidated 401."""

    class _AuthError(Exception):
        pass

    err = _AuthError(
        "Error code: 401 - {'error': {'code': 'token_invalidated', 'message': "
        "'Your authentication token has been invalidated.'}}"
    )
    err.status_code = 401  # type: ignore[attr-defined]
    resp = MagicMock()
    resp.status_code = 401
    resp.json.return_value = {
        "error": {
            "code": "token_invalidated",
            "message": "Your authentication token has been invalidated.",
        }
    }
    err.response = resp  # type: ignore[attr-defined]
    return err


def test_is_token_invalidated_detects_real_shape() -> None:
    from corlinman_providers.codex_provider import _is_token_invalidated

    assert _is_token_invalidated(_fake_invalidated_401()) is True


def test_is_token_invalidated_skips_refresh_token_invalidated() -> None:
    """Refresh-token invalidation is NOT recoverable locally — must return False."""

    from corlinman_providers.codex_provider import _is_token_invalidated

    class _AuthError(Exception):
        pass

    err = _AuthError(
        "Error code: 401 - {'error': {'code': 'refresh_token_invalidated'}}"
    )
    err.status_code = 401  # type: ignore[attr-defined]
    resp = MagicMock()
    resp.status_code = 401
    resp.json.return_value = {"error": {"code": "refresh_token_invalidated"}}
    err.response = resp  # type: ignore[attr-defined]
    assert _is_token_invalidated(err) is False


def test_is_token_invalidated_ignores_other_errors() -> None:
    from corlinman_providers.codex_provider import _is_token_invalidated

    assert _is_token_invalidated(RuntimeError("network down")) is False


@pytest.mark.asyncio
async def test_chat_stream_recovers_on_token_invalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 token_invalidated triggers refresh + persist + one retry."""
    from types import SimpleNamespace

    # Point auth.json at a tmp file so persist_codex_credential is sandboxed.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_auth_json(tmp_path, access_token="stale-token", refresh_token="rt-good")

    cred = CodexOAuthCredential(
        access_token="stale-token",
        refresh_token="rt-good",
        expires_at_ms=int(time.time() * 1000) + 3_600_000,
    )
    prov = CodexProvider(credential=cred)

    fresh_cred = CodexOAuthCredential(
        access_token="brand-new",
        refresh_token="rt-good",
        expires_at_ms=int(time.time() * 1000) + 3_600_000,
    )
    refresh_called = {"count": 0}

    async def _fake_refresh(*, refresh_token: str) -> CodexOAuthCredential:
        refresh_called["count"] += 1
        assert refresh_token == "rt-good"
        return fresh_cred

    # Provider-side _make_client fakes: first call returns a client that
    # raises 401-invalidated; second call (after recovery) returns a
    # client that streams a real token.
    attempt = {"n": 0}

    class _FakeStream:
        async def __aenter__(self):
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise _fake_invalidated_401()
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield SimpleNamespace(type="response.output_text.delta", delta="hi")

    class _FakeResponses:
        def stream(self, **_kw):
            return _FakeStream()

    class _FakeClient:
        responses = _FakeResponses()

    with (
        patch("corlinman_providers.codex_provider.refresh_codex_token", _fake_refresh),
        patch.object(prov, "_make_client", return_value=_FakeClient()),
    ):
        chunks = []
        async for chunk in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "ping"}],
        ):
            chunks.append(chunk)

    # Refresh was attempted once; the retry produced the actual token.
    assert refresh_called["count"] == 1
    assert prov._credential.access_token == "brand-new"
    # Persisted to the sandbox auth.json.
    persisted = json.loads((tmp_path / "auth.json").read_text())
    assert persisted["tokens"]["access_token"] == "brand-new"
    # And the stream's token reached the caller.
    token_chunks = [c for c in chunks if c.kind == "token"]
    assert token_chunks and token_chunks[0].text == "hi"


@pytest.mark.asyncio
async def test_chat_stream_gives_up_when_refresh_token_also_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If refresh also fails, the original 401 surfaces as a clean done/error."""

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_auth_json(tmp_path, access_token="dead", refresh_token="rt-also-dead")

    cred = CodexOAuthCredential(
        access_token="dead",
        refresh_token="rt-also-dead",
        expires_at_ms=int(time.time() * 1000) + 3_600_000,
    )
    prov = CodexProvider(credential=cred)

    async def _fake_refresh(*, refresh_token: str) -> CodexOAuthCredential:
        raise CodexOAuthRefreshError("refresh_token_invalidated")

    class _FakeStream:
        async def __aenter__(self):
            raise _fake_invalidated_401()

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _FakeResponses:
        def stream(self, **_kw):
            return _FakeStream()

    class _FakeClient:
        responses = _FakeResponses()

    with (
        patch("corlinman_providers.codex_provider.refresh_codex_token", _fake_refresh),
        patch.object(prov, "_make_client", return_value=_FakeClient()),
    ):
        chunks = []
        async for chunk in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "ping"}],
        ):
            chunks.append(chunk)

    # Clean done/error envelope — no exception leaks; access token unchanged.
    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "error"
    assert prov._credential.access_token == "dead"


@pytest.mark.asyncio
async def test_chat_stream_threads_prompt_cache_key_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T3.5: with the env switch on, extra['prompt_cache_key'] flows
    into the Responses API call as ``prompt_cache_key``."""
    monkeypatch.setenv("CORLINMAN_CODEX_PROMPT_CACHE", "1")

    cred = CodexOAuthCredential(
        access_token="ok",
        refresh_token=None,
        expires_at_ms=int(time.time() * 1000) + 3_600_000,
    )
    prov = CodexProvider(credential=cred)

    captured: dict[str, Any] = {}

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            return
            yield  # pragma: no cover — make this a generator

    class _FakeResponses:
        def stream(self, **kw):
            captured.update(kw)
            return _FakeStream()

    class _FakeClient:
        responses = _FakeResponses()

    with patch.object(prov, "_make_client", return_value=_FakeClient()):
        async for _ in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
            extra={"prompt_cache_key": "sess-cached"},
        ):
            pass

    assert captured.get("prompt_cache_key") == "sess-cached"


@pytest.mark.asyncio
async def test_chat_stream_skips_prompt_cache_key_by_default() -> None:
    """Without CORLINMAN_CODEX_PROMPT_CACHE, the key is NOT sent."""
    cred = CodexOAuthCredential(
        access_token="ok",
        refresh_token=None,
        expires_at_ms=int(time.time() * 1000) + 3_600_000,
    )
    prov = CodexProvider(credential=cred)

    captured: dict[str, Any] = {}

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            return
            yield  # pragma: no cover

    class _FakeResponses:
        def stream(self, **kw):
            captured.update(kw)
            return _FakeStream()

    class _FakeClient:
        responses = _FakeResponses()

    with patch.object(prov, "_make_client", return_value=_FakeClient()):
        async for _ in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
            extra={"prompt_cache_key": "sess-cached"},
        ):
            pass

    assert "prompt_cache_key" not in captured


def test_persist_codex_credential_atomic_write(tmp_path: Path) -> None:
    """persist_codex_credential writes the new tokens and preserves siblings."""
    from corlinman_providers._codex_oauth import persist_codex_credential

    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {"access_token": "old", "refresh_token": "rt-old"},
                "OPENAI_API_KEY": None,
                "last_refresh": "2026-01-01",
            }
        ),
        encoding="utf-8",
    )

    new_cred = CodexOAuthCredential(
        access_token="new-access",
        refresh_token="new-refresh",
        expires_at_ms=None,
    )
    assert persist_codex_credential(new_cred, path=auth_path) is True

    data = json.loads(auth_path.read_text())
    assert data["tokens"]["access_token"] == "new-access"
    assert data["tokens"]["refresh_token"] == "new-refresh"
    # Sibling fields preserved.
    assert "OPENAI_API_KEY" in data
    # Timestamp refreshed.
    assert data["last_refresh"] != "2026-01-01"
