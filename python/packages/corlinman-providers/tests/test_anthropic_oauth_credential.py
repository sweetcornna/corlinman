"""Tests for ``AnthropicProvider`` credential-resolution chain.

Wave W-A1: the provider must consult, in order:

1. ``<data_dir>/.oauth/anthropic.json`` (PKCE-issued OAuth bundle)
2. ``ANTHROPIC_TOKEN`` env var (manual OAuth override)
3. ``spec.api_key`` (the existing TOML-config path)
4. ``ANTHROPIC_API_KEY`` env var (legacy fallback)

Sources (1) and (2) bind to the ``Authorization: Bearer <token>``
header (the SDK ``auth_token=`` kwarg). Sources (3) and (4) bind to the
historic ``x-api-key`` header (the SDK ``api_key=`` kwarg).

These tests assert the chain order and the header-style selection by
mocking ``anthropic.AsyncAnthropic`` and capturing the kwargs it was
constructed with — so we don't need the real Anthropic SDK to run.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, ClassVar

import pytest
from corlinman_providers import AnthropicProvider


class _CaptureAnthropic:
    """Stand-in for ``anthropic.AsyncAnthropic`` that records its kwargs."""

    last_kwargs: ClassVar[dict[str, Any]] = {}

    def __init__(self, **kwargs: Any) -> None:
        _CaptureAnthropic.last_kwargs = kwargs
        # Provide the messages.stream() shape the provider expects.
        self.messages = _StreamHolder()


class _StreamHolder:
    def stream(self, **_: Any) -> _FakeStream:
        return _FakeStream()


class _FakeStream:
    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def __aiter__(self):
        async def _gen():
            if False:
                yield
        return _gen()

    async def get_final_message(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(stop_reason="end_turn")


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic  # type: ignore[import-not-found]

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _CaptureAnthropic)


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every env var the resolution chain reads."""
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


async def _drive_stream(prov: AnthropicProvider) -> None:
    """Run the stream just enough to trigger client construction."""
    async for _chunk in prov.chat_stream(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
    ):
        pass


@pytest.mark.asyncio
async def test_oauth_file_wins_over_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A valid OAuth file shadows ``ANTHROPIC_TOKEN`` AND the legacy key."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_TOKEN", "env-override-bearer")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-legacy-key")
    _install_fake_sdk(monkeypatch)

    # Persist a valid OAuth file via the shared storage module so this
    # test exercises the same load path the production code uses.
    from corlinman_server.gateway.oauth.storage import (
        OAuthCredential,
        save_credential,
    )

    cred = OAuthCredential.new(
        provider="anthropic",
        access_token="oauth-file-access",
        refresh_token="oauth-file-refresh",
        expires_at_ms=int(time.time() * 1000) + 3_600_000,
    )
    save_credential(tmp_path, cred)

    prov = AnthropicProvider(api_key="spec-key", data_dir=tmp_path)
    await _drive_stream(prov)

    # OAuth path → bearer header (auth_token kwarg, no api_key kwarg).
    assert _CaptureAnthropic.last_kwargs.get("auth_token") == "oauth-file-access"
    assert "api_key" not in _CaptureAnthropic.last_kwargs


@pytest.mark.asyncio
async def test_env_token_wins_over_spec_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_TOKEN", "env-bearer-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy-key")
    _install_fake_sdk(monkeypatch)

    # data_dir present but no OAuth file in it.
    prov = AnthropicProvider(api_key="spec-key", data_dir=tmp_path)
    await _drive_stream(prov)

    assert _CaptureAnthropic.last_kwargs.get("auth_token") == "env-bearer-token"
    assert "api_key" not in _CaptureAnthropic.last_kwargs


@pytest.mark.asyncio
async def test_spec_api_key_wins_over_legacy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy-env-key")
    _install_fake_sdk(monkeypatch)

    prov = AnthropicProvider(api_key="spec-key")
    await _drive_stream(prov)

    # spec.api_key → x-api-key header (api_key kwarg).
    assert _CaptureAnthropic.last_kwargs.get("api_key") == "spec-key"
    assert "auth_token" not in _CaptureAnthropic.last_kwargs


@pytest.mark.asyncio
async def test_legacy_env_used_when_nothing_else_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy-env-key")
    _install_fake_sdk(monkeypatch)

    prov = AnthropicProvider()
    await _drive_stream(prov)

    assert _CaptureAnthropic.last_kwargs.get("api_key") == "legacy-env-key"
    assert "auth_token" not in _CaptureAnthropic.last_kwargs


@pytest.mark.asyncio
async def test_no_credential_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_env(monkeypatch)
    _install_fake_sdk(monkeypatch)

    prov = AnthropicProvider()
    with pytest.raises(RuntimeError, match="API key missing"):
        await _drive_stream(prov)


@pytest.mark.asyncio
async def test_oauth_file_without_data_dir_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``data_dir`` is omitted, the OAuth lookup is skipped entirely."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy-env-key")
    _install_fake_sdk(monkeypatch)

    # data_dir=None — even if an OAuth file existed elsewhere, we wouldn't
    # find it. The chain should drop to env.
    prov = AnthropicProvider(data_dir=None)
    await _drive_stream(prov)

    assert _CaptureAnthropic.last_kwargs.get("api_key") == "legacy-env-key"


def test_credential_resolution_returns_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_credential_resolution`` is the synchronous truth source."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_TOKEN", "bearer-xyz")
    prov = AnthropicProvider(api_key="spec-key")
    token, style = prov._credential_resolution()
    assert token == "bearer-xyz"
    assert style == "bearer"

    _clean_env(monkeypatch)
    prov2 = AnthropicProvider(api_key="spec-key")
    token2, style2 = prov2._credential_resolution()
    assert token2 == "spec-key"
    assert style2 == "api_key"
