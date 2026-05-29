"""Tests for voice provider selection — real OpenAI vs mock.

Exercises :func:`resolve_voice_provider` and
:func:`build_voice_state_from_app` from :mod:`routes_voice.mod`: the
route picks the real :class:`OpenAIRealtimeProvider` whenever an OpenAI
API key is resolvable (from the provider registry, the raw config
``[providers]`` section, or ``OPENAI_API_KEY``), and the
:class:`MockVoiceProvider` otherwise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.routes_voice.mod import (
    VoiceState,
    build_voice_state_from_app,
    resolve_voice_provider,
)
from corlinman_server.gateway.routes_voice.provider import MockVoiceProvider
from corlinman_server.gateway.routes_voice.provider_openai import (
    OpenAIRealtimeProvider,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSpec:
    """Minimal ``ProviderSpec`` stand-in for the registry-scan path."""

    def __init__(self, kind: str, api_key: str | None) -> None:
        self.kind = kind
        self.api_key = api_key


class _FakeRegistry:
    def __init__(self, specs: list[_FakeSpec]) -> None:
        self._specs = specs

    def list_specs(self) -> list[_FakeSpec]:
        return list(self._specs)


class _FakeAppState:
    """Stand-in for the gateway ``AppState`` bundle."""

    def __init__(
        self,
        *,
        provider_registry: Any = None,
        config: Any = None,
        data_dir: Any = None,
    ) -> None:
        self.provider_registry = provider_registry
        self.config = config
        self.data_dir = data_dir


@pytest.fixture(autouse=True)
def _clear_openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts from a clean ``OPENAI_API_KEY`` so the env path
    is only exercised when the test sets it explicitly."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# resolve_voice_provider
# ---------------------------------------------------------------------------


def test_no_key_anywhere_selects_mock() -> None:
    provider = resolve_voice_provider(_FakeAppState())
    assert isinstance(provider, MockVoiceProvider)


def test_registry_openai_spec_selects_real_provider() -> None:
    registry = _FakeRegistry([_FakeSpec("openai", "sk-from-registry")])
    provider = resolve_voice_provider(
        _FakeAppState(provider_registry=registry)
    )
    assert isinstance(provider, OpenAIRealtimeProvider)
    assert provider.has_key is True


def test_registry_non_openai_spec_ignored() -> None:
    registry = _FakeRegistry([_FakeSpec("anthropic", "sk-ant-key")])
    provider = resolve_voice_provider(
        _FakeAppState(provider_registry=registry)
    )
    # An anthropic key does not enable the OpenAI realtime provider.
    assert isinstance(provider, MockVoiceProvider)


def test_registry_openai_spec_without_key_falls_through() -> None:
    registry = _FakeRegistry([_FakeSpec("openai", None)])
    provider = resolve_voice_provider(
        _FakeAppState(provider_registry=registry)
    )
    assert isinstance(provider, MockVoiceProvider)


def test_config_providers_dict_selects_real_provider() -> None:
    config = {
        "providers": {
            "openai": {"kind": "openai", "api_key": "sk-from-config"},
        }
    }
    provider = resolve_voice_provider(_FakeAppState(config=config))
    assert isinstance(provider, OpenAIRealtimeProvider)


def test_config_providers_list_selects_real_provider() -> None:
    config = {
        "providers": [
            {"name": "anthropic", "kind": "anthropic", "api_key": "sk-ant"},
            {"name": "openai", "kind": "openai", "api_key": "sk-list-key"},
        ]
    }
    provider = resolve_voice_provider(_FakeAppState(config=config))
    assert isinstance(provider, OpenAIRealtimeProvider)


def test_env_var_selects_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    provider = resolve_voice_provider(_FakeAppState())
    assert isinstance(provider, OpenAIRealtimeProvider)


def test_registry_key_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    registry = _FakeRegistry([_FakeSpec("openai", "sk-registry")])
    provider = resolve_voice_provider(
        _FakeAppState(provider_registry=registry)
    )
    # Resolution order puts the registry first; either way the real
    # provider is selected — assert it picked the registry key.
    assert isinstance(provider, OpenAIRealtimeProvider)
    assert provider._api_key == "sk-registry"  # noqa: SLF001 — test-only probe


# ---------------------------------------------------------------------------
# build_voice_state_from_app
# ---------------------------------------------------------------------------


async def test_build_voice_state_returns_none_without_voice_section() -> None:
    state = await build_voice_state_from_app(
        _FakeAppState(config={"providers": {}})
    )
    assert state is None


async def _close_state_store(state: VoiceState | None) -> None:
    """Close the wired SqliteVoiceSessionStore (if any) so its aiosqlite
    worker thread doesn't outlive the test event loop and emit a spurious
    "Event loop is closed" warning at teardown."""
    if state is None:
        return
    store = state.session_store
    close = getattr(store, "close", None)
    if callable(close):
        await close()


async def test_build_voice_state_with_mock_provider(tmp_path: Path) -> None:
    config = {
        "voice": {"enabled": True, "provider_alias": "openai"},
        "providers": {},
    }
    data_dir = tmp_path / "voice-data"
    state = await build_voice_state_from_app(
        _FakeAppState(config=config, data_dir=str(data_dir))
    )
    try:
        assert isinstance(state, VoiceState)
        assert isinstance(state.provider, MockVoiceProvider)
        assert state.data_dir == data_dir
        cfg = state.config_loader()
        assert cfg.enabled is True
        assert cfg.provider_alias == "openai"
    finally:
        await _close_state_store(state)


async def test_build_voice_state_with_real_provider(tmp_path: Path) -> None:
    config = {
        "voice": {"enabled": True, "provider_alias": "openai"},
        "providers": {"openai": {"kind": "openai", "api_key": "sk-key"}},
    }
    state = await build_voice_state_from_app(
        _FakeAppState(config=config, data_dir=str(tmp_path))
    )
    try:
        assert isinstance(state, VoiceState)
        assert isinstance(state.provider, OpenAIRealtimeProvider)
    finally:
        await _close_state_store(state)


async def test_build_voice_state_defaults_data_dir(tmp_path: Path) -> None:
    config = {"voice": {"enabled": False}, "providers": {}}
    state = await build_voice_state_from_app(
        _FakeAppState(config=config, data_dir=str(tmp_path))
    )
    try:
        assert state is not None
        assert state.data_dir == tmp_path
    finally:
        await _close_state_store(state)


async def test_build_voice_state_wires_sqlite_session_store(
    tmp_path: Path,
) -> None:
    """REPRODUCE-FIRST: today ``build_voice_state_from_app`` leaves
    ``session_store=None`` so completed voice sessions are never
    persisted. After wiring, the resolved state must carry a real
    :class:`SqliteVoiceSessionStore` (durable) — while the transcript
    bridge stays unset (its merge-semantics design is a follow-up)."""
    from corlinman_server.gateway.routes_voice.persistence import (
        SqliteVoiceSessionStore,
    )

    config = {
        "voice": {"enabled": True, "provider_alias": "openai"},
        "providers": {},
    }
    state = await build_voice_state_from_app(
        _FakeAppState(config=config, data_dir=str(tmp_path))
    )
    try:
        assert isinstance(state, VoiceState)
        assert state.session_store is not None, (
            "build_voice_state_from_app must wire a durable session store so "
            "voice sessions are persisted in production"
        )
        assert isinstance(state.session_store, SqliteVoiceSessionStore)
        # The transcript→chat bridge stays deferred (separate
        # merge-semantics design); the session-store half is independent
        # and lands now.
        assert state.transcript_sink is None
        # The store opened the canonical file under the resolved data dir.
        assert (tmp_path / "voice_sessions.sqlite").exists()
    finally:
        await _close_state_store(state)
    await state.session_store.close()


async def test_build_voice_state_caches_store_across_connects(
    tmp_path: Path,
) -> None:
    """The durable store must be opened ONCE and reused across ``/voice``
    connects — opening a fresh aiosqlite connection per connect would leak
    a thread + fd under connect churn. Two builds on the SAME AppState must
    share one store instance."""
    app = _FakeAppState(
        config={
            "voice": {"enabled": True, "provider_alias": "openai"},
            "providers": {},
        },
        data_dir=str(tmp_path),
    )
    first = await build_voice_state_from_app(app)
    second = await build_voice_state_from_app(app)
    assert first is not None and second is not None
    assert first.session_store is not None
    assert first.session_store is second.session_store, (
        "build_voice_state_from_app must reuse one cached SqliteVoiceSessionStore "
        "across connects, not open a new connection per connect"
    )
    await first.session_store.close()
