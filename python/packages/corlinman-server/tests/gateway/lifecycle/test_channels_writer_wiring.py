"""``_make_channels_writer`` — the prod channels-config write-back.

Regression for the gap where ``AdminState.channels_writer`` was never
wired in production (only in a test), so every
``PUT /admin/channels/{channel}/humanlike`` and the keywords PUT 503'd
``channels_writer_missing``. The writer must persist the ``[channels]``
table to ``config.toml`` while leaving other sections intact, and keep
the live ``app.state.config`` in sync.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from corlinman_server.gateway.lifecycle.entrypoint import (
    _make_channels_writer,
    _make_config_swap_fn,
)


def _fake_app(config: dict) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(config=config))


@pytest.mark.asyncio
async def test_writer_persists_channels_and_preserves_other_sections(
    tmp_path: Path,
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[server]\nport = 6005\n\n[channels.qq]\nenabled = true\n',
        encoding="utf-8",
    )
    live = {"server": {"port": 6005}, "channels": {"qq": {"enabled": True}}}
    app = _fake_app(live)
    admin_a_state = SimpleNamespace(config_path=cfg_path)

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
