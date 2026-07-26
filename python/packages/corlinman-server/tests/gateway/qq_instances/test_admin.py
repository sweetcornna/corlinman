from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_server.gateway.qq_instances.admin import (
    QqAdminError,
    QqInstanceAdminService,
    RetainedQqInstance,
    RetainedQqInstanceStore,
)


class _Writer:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, channels: dict[str, Any]) -> None:
        candidate = deepcopy(channels)
        self.calls.append(candidate)
        self.state.channels_config = candidate


class _MutatingWriter(_Writer):
    async def mutate(self, mutator: Any) -> Any:
        candidate, result = mutator(deepcopy(self.state.channels_config), {})
        await self(candidate)
        return result


@pytest.fixture()
def state(tmp_path: Path) -> Any:
    value = SimpleNamespace(
        data_dir=tmp_path,
        config_path=None,
        channels_config={
            "qq": {
                "enabled": True,
                "display_name": "Legacy bot",
                "connection_mode": "external",
                "ws_url": "ws://legacy.example:3001/ws?token=hidden",
                "access_token": "secret-value",
            }
        },
        qq_runtime_registry=None,
    )
    value.channels_writer = _MutatingWriter(value)
    return value


@pytest.mark.asyncio
async def test_create_materialises_legacy_and_preserves_default(state: Any) -> None:
    service = QqInstanceAdminService(state)

    created = await service.create_instance("bot-b", display_name="Bot B")

    qq = state.channels_config["qq"]
    assert qq["default_instance"] == "default"
    assert qq["instances"]["default"]["display_name"] == "Legacy bot"
    assert qq["instances"]["bot-b"] == {
        "display_name": "Bot B",
        "enabled": False,
        "connection_mode": "managed",
    }
    assert created.instance_id == "bot-b"
    assert created.is_default is False


@pytest.mark.asyncio
async def test_first_instance_becomes_default(tmp_path: Path) -> None:
    state = SimpleNamespace(
        data_dir=tmp_path,
        config_path=None,
        channels_config={"qq": {"instances": {}}},
        qq_runtime_registry=None,
    )
    state.channels_writer = _MutatingWriter(state)

    created = await QqInstanceAdminService(state).create_instance("primary")

    assert created.is_default is True
    assert state.channels_config["qq"]["default_instance"] == "primary"


@pytest.mark.asyncio
async def test_revision_conflict_does_not_write(state: Any) -> None:
    service = QqInstanceAdminService(state)
    before = deepcopy(state.channels_config)

    with pytest.raises(QqAdminError) as excinfo:
        await service.create_instance("bot-b", expected_revision="stale")

    assert excinfo.value.code == "revision_conflict"
    assert state.channels_config == before
    assert state.channels_writer.calls == []


@pytest.mark.asyncio
async def test_patch_is_instance_scoped(state: Any) -> None:
    service = QqInstanceAdminService(state)
    await service.create_instance("bot-b")
    default_before = deepcopy(state.channels_config["qq"]["instances"]["default"])

    await service.patch_instance("bot-b", display_name="Second", enabled=True)

    assert state.channels_config["qq"]["instances"]["default"] == default_before
    assert state.channels_config["qq"]["instances"]["bot-b"]["display_name"] == "Second"
    assert state.channels_config["qq"]["instances"]["bot-b"]["enabled"] is True


def test_snapshot_redacts_secrets_and_endpoint_credentials(state: Any) -> None:
    snapshot = QqInstanceAdminService(state).get_instance()

    assert "access_token" not in snapshot.config
    assert snapshot.config["ws_url"] == "ws://legacy.example:3001/ws"
    assert snapshot.secrets["access_token"] == {
        "is_set": True,
        "source": "literal",
    }


@pytest.mark.asyncio
async def test_deleting_default_requires_explicit_replacement(state: Any) -> None:
    service = QqInstanceAdminService(state)
    await service.create_instance("bot-b")

    with pytest.raises(QqAdminError) as excinfo:
        await service.remove_instance_config("default")

    assert excinfo.value.code == "new_default_required"

    removed = await service.remove_instance_config("default", new_default="bot-b")
    assert removed["resolved_config"]["display_name"] == "Legacy bot"
    assert state.channels_config["qq"]["default_instance"] == "bot-b"


def test_retained_store_is_private_and_round_trips_env_ref(tmp_path: Path) -> None:
    store = RetainedQqInstanceStore(tmp_path)
    raw = {
        "display_name": "Main",
        "access_token": {"env": "QQ_ACCESS_TOKEN"},
    }

    retained = RetainedQqInstance(
        raw_config=raw,
        connection_mode="managed",
        manager_generation=7,
    )
    store.put("default", retained)

    assert store.get("default") == retained
    assert store.path.stat().st_mode & 0o777 == 0o600
    store.delete("default")
    assert store.get("default") is None
    assert not store.path.exists()


def test_retained_store_reads_v1_and_rewrites_v2(tmp_path: Path) -> None:
    store = RetainedQqInstanceStore(tmp_path)
    store.path.write_text(
        '{"version":1,"instances":{"legacy":{"connection_mode":"external",'
        '"access_token":{"env":"QQ_ACCESS_TOKEN"}}}}',
        encoding="utf-8",
    )

    retained = store.get("legacy")

    assert retained is not None
    assert retained.connection_mode == "external"
    assert retained.manager_generation is None
    assert retained.raw_config["access_token"] == {"env": "QQ_ACCESS_TOKEN"}
    store.put("legacy", retained)
    assert '"version": 2' in store.path.read_text(encoding="utf-8")


def test_retained_store_fails_closed_on_invalid_state(tmp_path: Path) -> None:
    store = RetainedQqInstanceStore(tmp_path)
    store.path.write_text("not-json", encoding="utf-8")

    with pytest.raises(QqAdminError) as excinfo:
        store.list()

    assert excinfo.value.code == "retained_state_unavailable"


@pytest.mark.asyncio
async def test_restore_preserves_raw_environment_reference(state: Any) -> None:
    service = QqInstanceAdminService(state)
    await service.create_instance("bot-b")
    raw = {
        "display_name": "Legacy bot",
        "enabled": True,
        "connection_mode": "external",
        "ws_url": "ws://legacy.example:3001",
        "access_token": {"env": "QQ_ACCESS_TOKEN"},
    }

    await service.remove_instance_config("default", new_default="bot-b")
    await service.restore_instance_config("default", raw, make_default=True)

    assert state.channels_config["qq"]["instances"]["default"]["access_token"] == {
        "env": "QQ_ACCESS_TOKEN"
    }
