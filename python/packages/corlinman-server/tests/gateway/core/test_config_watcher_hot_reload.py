"""Hot-reload coverage for :class:`ConfigWatcher` — Parcel P11.

Exercises the ``on_reload`` post-reload hook the gateway lifespan wires:

* a detected change re-loads + swaps the live snapshot and fires
  ``on_reload`` once with ``(report, old_cfg, new_cfg)``;
* a malformed reload keeps the previous good snapshot and does **not**
  fire ``on_reload``;
* a no-op reload (identical file) fires nothing;
* an ``on_reload`` callback that raises is caught — the watcher loop
  survives and the snapshot stays swapped.

The fs-observer + SIGHUP paths are not under test here (they need a real
event loop + signals); :meth:`ConfigWatcher.trigger_reload` is the
deterministic seam every reload source funnels through, so driving it
directly covers the reload pipeline end to end.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from corlinman_server.gateway.core.config import load_from_path
from corlinman_server.gateway.core.config_watcher import ConfigWatcher
from corlinman_server.gateway.lifecycle.config_loading import _start_config_watcher

_GOOD_TOML = """\
[server]
bind = "127.0.0.1:6005"

[providers.openai]
kind = "openai"
api_key = "sk-original"
"""

_EDITED_TOML = """\
[server]
bind = "127.0.0.1:6005"

[providers.openai]
kind = "openai"
api_key = "sk-rotated"
"""

_MALFORMED_TOML = "[providers.openai\nkind = broken"


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_reload_swaps_snapshot_and_fires_on_reload(tmp_path: Path) -> None:
    cfg_path = tmp_path / "corlinman.toml"
    _write(cfg_path, _GOOD_TOML)
    initial = load_from_path(cfg_path)

    calls: list[tuple[list[str], str | None]] = []

    def _on_reload(report, old_cfg, new_cfg) -> None:
        new_key = new_cfg["providers"]["openai"]["api_key"]
        calls.append((list(report.changed_sections), new_key))

    watcher = ConfigWatcher(cfg_path, initial, parser=load_from_path, on_reload=_on_reload)

    # Edit the file, then drive a reload.
    _write(cfg_path, _EDITED_TOML)
    report = await watcher.trigger_reload()

    assert report.errors == []
    assert "providers" in report.changed_sections
    # The live snapshot reflects the edit.
    assert watcher.current()["providers"]["openai"]["api_key"] == "sk-rotated"
    # on_reload fired exactly once with the new snapshot.
    assert len(calls) == 1
    assert "providers" in calls[0][0]
    assert calls[0][1] == "sk-rotated"


@pytest.mark.asyncio
async def test_malformed_reload_keeps_previous_snapshot(tmp_path: Path) -> None:
    cfg_path = tmp_path / "corlinman.toml"
    _write(cfg_path, _GOOD_TOML)
    initial = load_from_path(cfg_path)

    calls: list[object] = []
    watcher = ConfigWatcher(
        cfg_path,
        initial,
        parser=load_from_path,
        on_reload=lambda *a: calls.append(a),
    )

    # Corrupt the file, then reload.
    _write(cfg_path, _MALFORMED_TOML)
    report = await watcher.trigger_reload()

    assert report.errors  # parse failure recorded
    assert report.changed_sections == []
    # Previous good snapshot retained — the gateway stays up.
    assert watcher.current()["providers"]["openai"]["api_key"] == "sk-original"
    # No re-apply hook fired for a rejected reload.
    assert calls == []


@pytest.mark.asyncio
async def test_noop_reload_fires_nothing(tmp_path: Path) -> None:
    cfg_path = tmp_path / "corlinman.toml"
    _write(cfg_path, _GOOD_TOML)
    initial = load_from_path(cfg_path)

    calls: list[object] = []
    watcher = ConfigWatcher(
        cfg_path,
        initial,
        parser=load_from_path,
        on_reload=lambda *a: calls.append(a),
    )

    # No file change — reload is a no-op.
    report = await watcher.trigger_reload()
    assert report.is_noop()
    assert calls == []


@pytest.mark.asyncio
async def test_on_reload_exception_is_caught(tmp_path: Path) -> None:
    """A buggy re-apply hook never crashes the watcher; the snapshot
    (swapped before the hook fires) still reflects the edit."""
    cfg_path = tmp_path / "corlinman.toml"
    _write(cfg_path, _GOOD_TOML)
    initial = load_from_path(cfg_path)

    def _boom(report, old_cfg, new_cfg) -> None:
        raise RuntimeError("re-apply blew up")

    watcher = ConfigWatcher(cfg_path, initial, parser=load_from_path, on_reload=_boom)

    _write(cfg_path, _EDITED_TOML)
    report = await watcher.trigger_reload()  # must not raise

    assert "providers" in report.changed_sections
    assert watcher.current()["providers"]["openai"]["api_key"] == "sk-rotated"


@pytest.mark.asyncio
async def test_async_on_reload_is_awaited(tmp_path: Path) -> None:
    cfg_path = tmp_path / "corlinman.toml"
    _write(cfg_path, _GOOD_TOML)
    initial = load_from_path(cfg_path)

    awaited: list[str] = []

    async def _on_reload(report, old_cfg, new_cfg) -> None:
        awaited.append(new_cfg["providers"]["openai"]["api_key"])

    watcher = ConfigWatcher(cfg_path, initial, parser=load_from_path, on_reload=_on_reload)
    _write(cfg_path, _EDITED_TOML)
    await watcher.trigger_reload()
    assert awaited == ["sk-rotated"]


@pytest.mark.asyncio
async def test_gateway_channel_reload_awaits_qq_reconcile_and_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / "corlinman.toml"
    sidecar = tmp_path / "py-config.json"
    monkeypatch.setenv("CORLINMAN_CONFIG_HOT_RELOAD", "1")
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(sidecar))
    _write(
        cfg_path,
        "[channels.qq]\n"
        'default_instance = "bot-a"\n'
        "[channels.qq.instances.bot-a]\n"
        "enabled = true\n"
        'connection_mode = "managed"\n',
    )
    initial = load_from_path(cfg_path)
    calls: list[tuple[dict, dict, Path]] = []

    class _Registry:
        async def reconcile_and_write_sidecar(self, channels, *, config, path) -> None:
            calls.append((channels, config, Path(path)))

    state = SimpleNamespace(config=initial, qq_runtime_registry=_Registry())
    app = SimpleNamespace(state=SimpleNamespace(corlinman_config=initial))
    task = _start_config_watcher(app, state, cfg_path)
    assert task is not None
    try:
        watcher = state.config_watcher
        _write(
            cfg_path,
            "[channels.qq]\n"
            'default_instance = "bot-a"\n'
            "[channels.qq.instances.bot-a]\n"
            "enabled = false\n"
            'connection_mode = "managed"\n',
        )

        report = await watcher.trigger_reload()

        assert report.errors == []
        assert calls == [(state.config["channels"], state.config, sidecar)]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_gateway_channel_reload_restores_snapshot_on_reconcile_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / "corlinman.toml"
    monkeypatch.setenv("CORLINMAN_CONFIG_HOT_RELOAD", "1")
    _write(
        cfg_path,
        "[channels.qq]\n"
        'default_instance = "bot-a"\n'
        "[channels.qq.instances.bot-a]\n"
        "enabled = true\n"
        'connection_mode = "managed"\n',
    )
    initial = load_from_path(cfg_path)

    class _Registry:
        async def reconcile_and_write_sidecar(self, channels, *, config, path) -> None:
            raise RuntimeError("manager unavailable")

    state = SimpleNamespace(config=initial, qq_runtime_registry=_Registry())
    app = SimpleNamespace(state=SimpleNamespace(corlinman_config=initial))
    task = _start_config_watcher(app, state, cfg_path)
    assert task is not None
    try:
        watcher = state.config_watcher
        _write(
            cfg_path,
            "[channels.qq]\n"
            'default_instance = "bot-a"\n'
            "[channels.qq.instances.bot-a]\n"
            "enabled = false\n"
            'connection_mode = "managed"\n',
        )

        report = await watcher.trigger_reload()

        assert report.errors == ["QQ runtime reconcile failed: manager unavailable"]
        assert state.config == initial
        assert app.state.corlinman_config == initial
        assert watcher.current() == initial
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
