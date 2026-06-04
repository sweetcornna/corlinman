"""Tests for ``gateway.channels_runtime`` — Parcel P3 channel bootstrap.

Covers the sibling ``bootstrap(state)`` hook contract:

* enabled channels get a background :class:`asyncio.Task`;
* disabled / missing channels produce no task;
* env-var fallback for the QQ ``ws_url``;
* ``state.chat`` (the ChatBackend) is threaded onto the channel params;
* the returned tasks shut down cleanly when cancelled (the lifespan's
  ``finally`` does ``task.cancel()``).

These exercise ``channels_runtime`` against the *real* ``corlinman-
channels`` package — no inbound transport is opened because the
adapters only dial out once their loop runs, and we cancel the tasks
before that completes (the ``ws_url`` / ``bot_token`` are bogus on
purpose so a stray connect attempt fails fast rather than hanging).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from corlinman_server.gateway import channels_runtime


@dataclass
class _FakeState:
    """Minimal AppState stand-in — only the fields P3 reads."""

    config: Any = None
    chat: Any = None
    channels_cancel: Any = field(default=None)


async def _drain(tasks: list[asyncio.Task[Any]]) -> None:
    """Cancel + await every task the way the gateway lifespan does."""
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — adapter teardown noise
            pass


# ---------------------------------------------------------------------------
# bootstrap — top-level gating
# ---------------------------------------------------------------------------


async def test_bootstrap_no_config_returns_empty() -> None:
    assert channels_runtime.bootstrap(_FakeState(config=None)) == []


async def test_bootstrap_no_channels_section_returns_empty() -> None:
    state = _FakeState(config={"models": {"default": "gpt-x"}})
    assert channels_runtime.bootstrap(state) == []


async def test_bootstrap_all_disabled_returns_empty() -> None:
    state = _FakeState(
        config={
            "channels": {
                "qq": {"enabled": False, "ws_url": "ws://x", "self_ids": [1]},
                "telegram": {"enabled": False, "bot_token": "t"},
            }
        }
    )
    assert channels_runtime.bootstrap(state) == []


# ---------------------------------------------------------------------------
# bootstrap — QQ
# ---------------------------------------------------------------------------


async def test_bootstrap_enabled_qq_spawns_task() -> None:
    state = _FakeState(
        config={
            "models": {"default": "gpt-x"},
            "channels": {
                "qq": {
                    "enabled": True,
                    "ws_url": "ws://127.0.0.1:59999",
                    "self_ids": [123],
                }
            },
        },
        chat=object(),  # any non-None — only presence matters here
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        assert len(tasks) == 1
        assert all(isinstance(t, asyncio.Task) for t in tasks)
        # The shared cancel Event is stashed on the state.
        assert isinstance(state.channels_cancel, asyncio.Event)
    finally:
        await _drain(tasks)


async def test_bootstrap_qq_ws_url_env_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No inline ``ws_url`` → falls back to ``QQ_WS_URL`` env var."""
    monkeypatch.setenv("QQ_WS_URL", "ws://127.0.0.1:59998")
    state = _FakeState(
        config={
            "channels": {
                "qq": {"enabled": True, "self_ids": [1]},
            }
        },
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        # Param build succeeded (ws_url resolved from env) → one task.
        assert len(tasks) == 1
    finally:
        await _drain(tasks)


async def test_bootstrap_qq_missing_ws_url_is_skipped_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ws_url anywhere → ``run_qq_channel`` raises ValueError inside
    the task; bootstrap itself still returns cleanly (the task is
    created, runs, and self-terminates with the logged error)."""
    monkeypatch.delenv("QQ_WS_URL", raising=False)
    state = _FakeState(
        config={"channels": {"qq": {"enabled": True, "self_ids": [1]}}},
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        # A task is still created — the ValueError surfaces *inside* the
        # channel loop, gets logged by ``_run_channel``, and the task
        # ends. The gateway boot is never blocked.
        assert len(tasks) == 1
        # Let the task run to its self-termination.
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2.0)
    finally:
        await _drain(tasks)


# ---------------------------------------------------------------------------
# bootstrap — Telegram
# ---------------------------------------------------------------------------


async def test_bootstrap_enabled_telegram_spawns_task() -> None:
    state = _FakeState(
        config={
            "models": {"default": "gpt-x"},
            "channels": {
                "telegram": {
                    "enabled": True,
                    "bot_token": "fake-token",
                }
            },
        },
        chat=object(),
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        assert len(tasks) == 1
    finally:
        await _drain(tasks)


async def test_bootstrap_both_channels() -> None:
    state = _FakeState(
        config={
            "channels": {
                "qq": {
                    "enabled": True,
                    "ws_url": "ws://127.0.0.1:59997",
                    "self_ids": [1],
                },
                "telegram": {"enabled": True, "bot_token": "t"},
            }
        },
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        assert len(tasks) == 2
    finally:
        await _drain(tasks)


# ---------------------------------------------------------------------------
# bootstrap — Discord / Slack / Feishu
# ---------------------------------------------------------------------------


async def test_bootstrap_enabled_discord_spawns_task() -> None:
    state = _FakeState(
        config={
            "models": {"default": "gpt-x"},
            "channels": {
                "discord": {"enabled": True, "bot_token": "fake-discord-token"},
            },
        },
        chat=object(),
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        assert len(tasks) == 1
    finally:
        await _drain(tasks)


async def test_bootstrap_discord_token_env_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No inline ``bot_token`` → falls back to ``DISCORD_BOT_TOKEN`` env."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-discord-token")
    state = _FakeState(
        config={"channels": {"discord": {"enabled": True}}},
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        assert len(tasks) == 1
    finally:
        await _drain(tasks)


async def test_bootstrap_discord_missing_token_is_skipped_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No token anywhere → ``run_discord_channel`` raises inside the task;
    bootstrap itself still returns cleanly."""
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    state = _FakeState(
        config={"channels": {"discord": {"enabled": True}}},
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        assert len(tasks) == 1
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2.0)
    finally:
        await _drain(tasks)


async def test_bootstrap_enabled_slack_spawns_task() -> None:
    state = _FakeState(
        config={
            "channels": {
                "slack": {
                    "enabled": True,
                    "app_token": "xapp-1",
                    "bot_token": "xoxb-1",
                },
            },
        },
        chat=object(),
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        assert len(tasks) == 1
    finally:
        await _drain(tasks)


async def test_bootstrap_enabled_feishu_spawns_task() -> None:
    state = _FakeState(
        config={
            "channels": {
                "feishu": {
                    "enabled": True,
                    "app_id": "cli_1",
                    "app_secret": "secret-1",
                },
            },
        },
        chat=object(),
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        assert len(tasks) == 1
    finally:
        await _drain(tasks)


async def test_bootstrap_all_five_channels() -> None:
    """qq + telegram + discord + slack + feishu all enabled → 5 tasks."""
    state = _FakeState(
        config={
            "channels": {
                "qq": {
                    "enabled": True,
                    "ws_url": "ws://127.0.0.1:59990",
                    "self_ids": [1],
                },
                "telegram": {"enabled": True, "bot_token": "t"},
                "discord": {"enabled": True, "bot_token": "d"},
                "slack": {
                    "enabled": True,
                    "app_token": "xapp",
                    "bot_token": "xoxb",
                },
                "feishu": {"enabled": True, "app_id": "a", "app_secret": "s"},
            }
        },
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        assert len(tasks) == 5
    finally:
        await _drain(tasks)


async def test_bootstrap_slack_missing_tokens_skipped_not_crash() -> None:
    """A Slack section enabled with no tokens → task is created, the
    ValueError surfaces inside the loop and the task self-terminates.
    The other channels are unaffected (degrade, never crash)."""
    state = _FakeState(
        config={
            "channels": {
                "slack": {"enabled": True},
                "telegram": {"enabled": True, "bot_token": "t"},
            }
        },
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        # Both tasks created — slack self-terminates with a logged error.
        assert len(tasks) == 2
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 0.5
        )
    except TimeoutError:
        pass  # telegram task long-lives; that's fine.
    finally:
        await _drain(tasks)


# ---------------------------------------------------------------------------
# bootstrap — degraded mode (no chat service)
# ---------------------------------------------------------------------------


async def test_bootstrap_no_chat_service_still_starts() -> None:
    """``state.chat`` is None (P2 not wired) → channel still starts so
    ``/admin/channels/qq/status`` can show online; it just won't reply."""
    state = _FakeState(
        config={
            "channels": {
                "qq": {
                    "enabled": True,
                    "ws_url": "ws://127.0.0.1:59996",
                    "self_ids": [1],
                }
            }
        },
        chat=None,
    )
    tasks = channels_runtime.bootstrap(state)
    try:
        assert len(tasks) == 1
    finally:
        await _drain(tasks)


# ---------------------------------------------------------------------------
# build_channel_tasks — chat_service threading
# ---------------------------------------------------------------------------


async def test_build_channel_tasks_threads_chat_service_into_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The QqChannelParams handed to ``run_qq_channel`` carries the
    model + chat_service from the call."""
    captured: dict[str, Any] = {}

    async def _fake_run_qq(params: Any, cancel: asyncio.Event) -> None:
        captured["model"] = params.model
        captured["chat_service"] = params.chat_service
        captured["ws_url"] = params.config.get("ws_url")
        await cancel.wait()

    import corlinman_channels

    monkeypatch.setattr(corlinman_channels, "run_qq_channel", _fake_run_qq)

    sentinel_chat = object()
    cancel = asyncio.Event()
    tasks = channels_runtime.build_channel_tasks(
        {
            "qq": {
                "enabled": True,
                "ws_url": "ws://host:1234",
                "self_ids": [9],
            }
        },
        model="claude-x",
        chat_service=sentinel_chat,
        cancel=cancel,
    )
    try:
        assert len(tasks) == 1
        await asyncio.sleep(0.05)  # let the task body run
        assert captured["model"] == "claude-x"
        assert captured["chat_service"] is sentinel_chat
        assert captured["ws_url"] == "ws://host:1234"
    finally:
        await _drain(tasks)


# ---------------------------------------------------------------------------
# official channels — asset_store threading (Codex #5)
# ---------------------------------------------------------------------------


async def test_official_channels_asset_store_threading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qq_official KEEPS its asset_store (its handler delivers
    ``send_attachment``); wechat_official is built WITHOUT one so
    ``inject_persona_if_enabled`` never appends an emoji block telling
    the model to call a tool wechat_official silently drops. The persona
    store is still wired to both so the system_prompt keeps injecting."""
    sentinel_assets = object()
    sentinel_personas = object()
    captured: dict[str, Any] = {}

    async def _fake_run_qq_official(params: Any, cancel: asyncio.Event) -> None:
        captured["qq_official_asset_store"] = params.asset_store
        captured["qq_official_persona_store"] = params.persona_store
        await cancel.wait()

    async def _fake_run_wechat_official(
        params: Any, cancel: asyncio.Event
    ) -> None:
        captured["wechat_official_asset_store"] = params.asset_store
        captured["wechat_official_persona_store"] = params.persona_store
        await cancel.wait()

    import corlinman_channels

    monkeypatch.setattr(
        corlinman_channels, "run_qq_official_channel", _fake_run_qq_official
    )
    monkeypatch.setattr(
        corlinman_channels,
        "run_wechat_official_channel",
        _fake_run_wechat_official,
    )

    cancel = asyncio.Event()
    tasks = channels_runtime.build_channel_tasks(
        {
            "qq_official": {
                "enabled": True,
                "app_id": "qq-app",
                "app_secret": "qq-secret",
            },
            "wechat_official": {
                "enabled": True,
                "app_id": "wx-app",
                "app_secret": "wx-secret",
                "token": "wx-token",
            },
        },
        model="claude-x",
        chat_service=object(),
        cancel=cancel,
        persona_store=sentinel_personas,
        asset_store=sentinel_assets,
    )
    try:
        assert len(tasks) == 2
        await asyncio.sleep(0.05)  # let the task bodies run
        # qq_official handler delivers send_attachment → keep the assets.
        assert captured["qq_official_asset_store"] is sentinel_assets
        # wechat_official drops send_attachment → must NOT receive assets.
        assert captured["wechat_official_asset_store"] is None
        # …but both keep the persona store so the system_prompt still injects.
        assert captured["qq_official_persona_store"] is sentinel_personas
        assert captured["wechat_official_persona_store"] is sentinel_personas
    finally:
        await _drain(tasks)


# ---------------------------------------------------------------------------
# cancellation contract — lifespan does task.cancel()
# ---------------------------------------------------------------------------


async def test_cancel_sets_shared_event_and_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the lifespan cancels a returned task, ``_run_channel`` sets
    the shared cancel Event so the (fake) channel loop exits cleanly."""
    saw_cancel = asyncio.Event()

    async def _fake_run_qq(params: Any, cancel: asyncio.Event) -> None:
        await cancel.wait()
        saw_cancel.set()

    import corlinman_channels

    monkeypatch.setattr(corlinman_channels, "run_qq_channel", _fake_run_qq)

    state = _FakeState(
        config={
            "channels": {
                "qq": {
                    "enabled": True,
                    "ws_url": "ws://x:1",
                    "self_ids": [1],
                }
            }
        },
    )
    tasks = channels_runtime.bootstrap(state)
    assert len(tasks) == 1
    await asyncio.sleep(0.02)

    # Simulate the lifespan-exit ``finally``: cancel + await.
    for t in tasks:
        t.cancel()
    for t in tasks:
        with pytest.raises(asyncio.CancelledError):
            await t

    # The fake channel loop observed the cancel Event being set.
    assert saw_cancel.is_set()
