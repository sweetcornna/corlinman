"""``_make_channels_writer`` — the prod channels-config write-back.

Regression for the gap where ``AdminState.channels_writer`` was never
wired in production (only in a test), so every
``PUT /admin/channels/{channel}/humanlike`` and the keywords PUT 503'd
``channels_writer_missing``. The writer must persist the ``[channels]``
table to ``config.toml`` while leaving other sections intact, and keep
the live ``app.state.config`` in sync.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from corlinman_server.gateway.lifecycle.entrypoint import (
    _make_channels_writer,
    _make_config_swap_fn,
)


def _fake_app(config: dict) -> SimpleNamespace:
    runtime = SimpleNamespace(config=config, config_watcher=None)
    return SimpleNamespace(
        state=SimpleNamespace(
            config=config,
            corlinman_config=config,
            corlinman_state=runtime,
        )
    )


@pytest.mark.asyncio
async def test_writer_persists_channels_and_preserves_other_sections(
    tmp_path: Path,
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[server]\nport = 6005\n\n[channels.qq]\nenabled = true\n",
        encoding="utf-8",
    )
    live = {"server": {"port": 6005}, "channels": {"qq": {"enabled": True}}}
    app = _fake_app(live)
    admin_a_state = SimpleNamespace(config_path=cfg_path, admin_write_lock=asyncio.Lock())

    writer = _make_channels_writer(app, admin_a_state)
    new_channels = {
        "qq": {"enabled": True},
        "telegram": {"humanlike": {"enabled": True, "persona_id": "grantley"}},
    }
    await writer(new_channels)

    on_disk = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    # channels edit persisted
    assert on_disk["channels"]["telegram"]["humanlike"] == {
        "enabled": True,
        "persona_id": "grantley",
    }
    # unrelated section preserved
    assert on_disk["server"]["port"] == 6005
    # live config kept in sync
    assert app.state.config["channels"]["telegram"]["humanlike"]["enabled"] is True


@pytest.mark.asyncio
async def test_writer_updates_tencent_sidecar_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[channels.qq]\nfreeze_risk_topic_blocking = true\n",
        encoding="utf-8",
    )
    sidecar = tmp_path / "py-config.json"
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(sidecar))
    live = {"channels": {"qq": {"freeze_risk_topic_blocking": True}}}
    app = _fake_app(live)
    writer = _make_channels_writer(
        app,
        SimpleNamespace(config_path=cfg_path, admin_write_lock=asyncio.Lock()),
    )

    await writer({"qq": {"freeze_risk_topic_blocking": False}})

    assert app.state.config["channels"]["qq"]["freeze_risk_topic_blocking"] is False
    assert json.loads(sidecar.read_text(encoding="utf-8"))["tencent_safety"]["enabled"] is False


@pytest.mark.asyncio
async def test_writer_preserves_raw_env_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[channels.qq]\naccess_token = { env = "QQ_ACCESS_TOKEN" }\n'
        'group_reply_policy = "mention_or_keyword"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("QQ_ACCESS_TOKEN", "do-not-write-me")
    app = _fake_app(
        {
            "channels": {
                "qq": {
                    "access_token": "do-not-write-me",
                    "group_reply_policy": "mention_or_keyword",
                }
            }
        }
    )
    admin_state = SimpleNamespace(
        config_path=cfg_path,
        admin_write_lock=asyncio.Lock(),
    )

    await _make_channels_writer(app, admin_state)(
        {
            "qq": {
                "access_token": "do-not-write-me",
                "group_reply_policy": "all",
            }
        }
    )

    text = cfg_path.read_text(encoding="utf-8")
    assert "do-not-write-me" not in text
    assert tomllib.loads(text)["channels"]["qq"]["access_token"] == {"env": "QQ_ACCESS_TOKEN"}
    assert app.state.corlinman_state.config["channels"]["qq"]["access_token"] == ("do-not-write-me")


@pytest.mark.asyncio
async def test_writer_serialises_concurrent_updates_under_shared_lock(
    tmp_path: Path,
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[channels.qq]\nenabled = true\n", encoding="utf-8")
    app = _fake_app({"channels": {"qq": {"enabled": True}}})
    lock = asyncio.Lock()
    writer = _make_channels_writer(
        app,
        SimpleNamespace(config_path=cfg_path, admin_write_lock=lock),
    )

    await asyncio.gather(
        writer({"qq": {"enabled": True, "group_reply_policy": "all"}}),
        writer({"qq": {"enabled": True, "group_reply_policy": "mention_or_keyword"}}),
    )

    assert tomllib.loads(cfg_path.read_text(encoding="utf-8"))["channels"]["qq"][
        "group_reply_policy"
    ] in {"all", "mention_or_keyword"}


@pytest.mark.asyncio
async def test_writer_sidecar_failure_is_not_reported_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corlinman_server.gateway.lifecycle import app_factory

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[channels.qq]\n", encoding="utf-8")
    app = _fake_app({"channels": {"qq": {}}})
    writer = _make_channels_writer(
        app,
        SimpleNamespace(config_path=cfg_path, admin_write_lock=asyncio.Lock()),
    )

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("sidecar unavailable")

    monkeypatch.setattr(app_factory, "write_py_config_sync", _fail)
    with pytest.raises(OSError, match="sidecar unavailable"):
        await writer({"qq": {"freeze_risk_topic_blocking": False}})

    assert tomllib.loads(cfg_path.read_text(encoding="utf-8")) == {
        "channels": {"qq": {}}
    }
    assert app.state.config == {"channels": {"qq": {}}}


@pytest.mark.asyncio
async def test_writer_registry_failure_restores_disk_and_live_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / "config.toml"
    original = (
        "[channels.qq]\n"
        'default_instance = "bot-a"\n'
        "[channels.qq.instances.bot-a]\n"
        "enabled = true\n"
        'connection_mode = "managed"\n'
    )
    cfg_path.write_text(original, encoding="utf-8")
    sidecar = tmp_path / "py-config.json"
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(sidecar))
    live = {
        "channels": {
            "qq": {
                "default_instance": "bot-a",
                "instances": {
                    "bot-a": {"enabled": True, "connection_mode": "managed"}
                },
            }
        }
    }
    app = _fake_app(live)

    class _Registry:
        async def reconcile_and_write_sidecar(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("manager unavailable")

    app.state.corlinman_state.qq_runtime_registry = _Registry()
    admin_state = SimpleNamespace(
        config_path=cfg_path,
        admin_write_lock=asyncio.Lock(),
        channels_config=live["channels"],
    )

    with pytest.raises(RuntimeError, match="manager unavailable"):
        await _make_channels_writer(app, admin_state)(
            {
                "qq": {
                    "default_instance": "bot-a",
                    "instances": {
                        "bot-a": {"enabled": False, "connection_mode": "managed"}
                    },
                }
            }
        )

    assert cfg_path.read_text(encoding="utf-8") == original
    assert app.state.config == live
    assert app.state.corlinman_config == live
    assert app.state.corlinman_state.config == live
    assert admin_state.channels_config == live["channels"]


@pytest.mark.asyncio
async def test_writer_persists_nested_monitor_tables(tmp_path: Path) -> None:
    """``[[channels.qq.instances.X.monitors]]`` — a nested list of tables
    must survive the merge + atomic write and read back structurally."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[channels.qq]\nenabled = true\n", encoding="utf-8")
    live = {"channels": {"qq": {"enabled": True}}}
    app = _fake_app(live)
    admin_a_state = SimpleNamespace(
        config_path=cfg_path, admin_write_lock=asyncio.Lock()
    )
    monitor = {
        "id": "daily-brief",
        "enabled": True,
        "source_group": "100200",
        "watch_user_ids": ["11111"],
        "schedule_type": "daily",
        "daily_time": "09:00",
        "timezone": "Asia/Shanghai",
        "window_minutes": 0,
        "target_type": "user",
        "target_id": "22222",
        "style_extra": "",
        "send_when_empty": False,
    }

    await _make_channels_writer(app, admin_a_state)(
        {
            "qq": {
                "enabled": True,
                "instances": {
                    "default": {"ws_url": "ws://x:3001", "monitors": [monitor]}
                },
            }
        }
    )

    on_disk = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    stored = on_disk["channels"]["qq"]["instances"]["default"]["monitors"]
    assert stored == [monitor]


@pytest.mark.asyncio
async def test_writer_raises_without_config_path() -> None:
    app = _fake_app({"channels": {}})
    admin_a_state = SimpleNamespace(config_path=None)
    writer = _make_channels_writer(app, admin_a_state)
    with pytest.raises(RuntimeError):
        await writer({"telegram": {"humanlike": {"enabled": False, "persona_id": None}}})


def test_config_swap_fn_publishes_to_live_snapshot() -> None:
    """Regression: POST /admin/config used to write disk but never update the
    running process because config_swap_fn was only wired when the
    (off-by-default) fs-watcher existed. The unconditionally-wired swap fn
    must publish the new TOML to the live in-memory snapshot."""
    state = SimpleNamespace(config={"models": {"default": "old"}}, config_watcher=None)
    app = _fake_app(state.config)
    swap = _make_config_swap_fn(app, state)

    new_cfg = {"models": {"default": "new"}}
    swap(new_cfg)  # must not raise even though providers reapply is a no-op here

    assert state.config is new_cfg
    assert state.config["models"]["default"] == "new"
    assert app.state.corlinman_config is new_cfg
