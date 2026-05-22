"""Tests for the Codex OAuth provider.

Covers:
* :mod:`corlinman_providers._codex_oauth` — credential loading + refresh
* :class:`corlinman_providers.codex_provider.CodexProvider` — build + auto-refresh
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from corlinman_providers._codex_oauth import (
    CodexOAuthCredential,
    CodexOAuthRefreshError,
    _decode_jwt_exp,
    load_codex_credential,
)
from corlinman_providers.codex_provider import CodexProvider
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
        assert prov._api_key == "at-ok"


# ---------------------------------------------------------------------------
# CodexProvider.supports
# ---------------------------------------------------------------------------


class TestCodexProviderSupports:
    @pytest.mark.parametrize(
        "model",
        [
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
# CodexProvider.chat_stream — auto-refresh on expired token
# ---------------------------------------------------------------------------


class _FakeOpenAIChunk:
    def __init__(self, text: str) -> None:
        self.choices = [_FakeChoice(text)]


class _FakeChoice:
    def __init__(self, text: str) -> None:
        self.delta = _FakeDelta(text)
        self.finish_reason = None


class _FakeDelta:
    def __init__(self, text: str) -> None:
        self.content = text
        self.tool_calls = None


class _FakeDoneChoice:
    delta = _FakeDelta(None)
    finish_reason = "stop"


@pytest.mark.asyncio
async def test_chat_stream_refreshes_expired_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the token is expired, _ensure_fresh calls refresh_codex_token."""
    expired_ms = int(time.time() * 1000) - 1  # already past skew
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

    # Minimal fake stream so chat_stream can iterate without real network
    async def _fake_stream(**_kwargs: Any):
        yield _FakeOpenAIChunk("hi")
        yield type("C", (), {"choices": [_FakeDoneChoice()]})()

    class _FakeCompletions:
        async def create(self, **_kwargs: Any):
            async def _gen():
                async for chunk in _fake_stream():
                    yield chunk
            return _gen()

    class _FakeMessages:
        completions = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, **_kwargs: Any) -> None:
            self.chat = _FakeMessages()

    with (
        patch("corlinman_providers.codex_provider.refresh_codex_token", mock_refresh),
        patch("corlinman_providers.openai_provider.OpenAIProvider._make_client",
              return_value=_FakeOpenAI()),
    ):
        chunks = []
        async for chunk in prov.chat_stream(
            model="o4-mini",
            messages=[{"role": "user", "content": "ping"}],
        ):
            chunks.append(chunk)

    mock_refresh.assert_awaited_once()
    assert prov._api_key == "new-token"


@pytest.mark.asyncio
async def test_chat_stream_no_refresh_when_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the token is not expired, refresh is not called."""
    future_ms = int(time.time() * 1000) + 3_600_000
    cred = CodexOAuthCredential(
        access_token="good-token",
        refresh_token="rt-xyz",
        expires_at_ms=future_ms,
    )
    prov = CodexProvider(credential=cred)
    mock_refresh = AsyncMock()

    async def _fake_stream(**_kwargs: Any):
        yield type("C", (), {"choices": [_FakeDoneChoice()]})()

    class _FakeCompletions:
        async def create(self, **_kwargs: Any):
            async def _gen():
                async for chunk in _fake_stream():
                    yield chunk
            return _gen()

    class _FakeMessages:
        completions = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, **_kwargs: Any) -> None:
            self.chat = _FakeMessages()

    with (
        patch("corlinman_providers.codex_provider.refresh_codex_token", mock_refresh),
        patch("corlinman_providers.openai_provider.OpenAIProvider._make_client",
              return_value=_FakeOpenAI()),
    ):
        async for _ in prov.chat_stream(
            model="o4-mini",
            messages=[{"role": "user", "content": "ping"}],
        ):
            pass

    mock_refresh.assert_not_awaited()
