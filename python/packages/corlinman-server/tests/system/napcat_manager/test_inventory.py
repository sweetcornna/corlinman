from __future__ import annotations

import json
from pathlib import Path

import pytest
from corlinman_server.system.napcat_manager.inventory import (
    InventoryVersion,
    NapCatInventory,
)
from corlinman_server.system.napcat_manager.models import NapCatInstanceRecord


def _record(root: Path, instance_id: str = "bot-a") -> NapCatInstanceRecord:
    state = root / instance_id
    return NapCatInstanceRecord(
        instance_id=instance_id,
        provider="native",
        generation=1,
        resource_id=f"corlinman-napcat@{instance_id}.service",
        state_root=str(state),
        token_file=str(state / "manager-secrets.env"),
    )


def test_inventory_roundtrips_without_secret_contents(tmp_path: Path) -> None:
    root = tmp_path / "instances"
    inventory_path = tmp_path / "inventory.json"
    inventory = NapCatInventory(inventory_path, state_root=root)
    record = _record(root)

    inventory.put(record)

    loaded = NapCatInventory(inventory_path, state_root=root).get("bot-a")
    assert loaded == record
    text = inventory_path.read_text(encoding="utf-8")
    assert "access_token" not in text
    assert "WEBUI_TOKEN" not in text
    assert "ONEBOT_TOKEN" not in text
    payload = json.loads(text)
    assert payload["schema"] == 2
    assert payload["generations"] == {"bot-a": 1}
    assert inventory_path.stat().st_mode & 0o777 == 0o600


def test_inventory_rejects_unrecognized_metadata(tmp_path: Path) -> None:
    root = tmp_path / "instances"
    inventory = NapCatInventory(tmp_path / "inventory.json", state_root=root)
    record = _record(root)
    record.metadata = {"access_token": "must-not-persist"}

    with pytest.raises(ValueError, match="unsupported NapCat inventory metadata"):
        inventory.put(record)

    assert not inventory.path.exists()


def test_inventory_rejects_unrecognized_metadata_on_load(tmp_path: Path) -> None:
    root = tmp_path / "instances"
    path = tmp_path / "inventory.json"
    record = _record(root)
    payload = record.to_json()
    payload["metadata"] = {"WEBUI_TOKEN": "must-not-load"}
    path.write_text(
        json.dumps(
            {
                "schema": 2,
                "generations": {"bot-a": 1},
                "instances": {"bot-a": payload},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inventory"):
        NapCatInventory(path, state_root=root)


def test_inventory_rejects_paths_outside_managed_root(tmp_path: Path) -> None:
    inventory = NapCatInventory(
        tmp_path / "inventory.json", state_root=tmp_path / "instances"
    )
    record = _record(tmp_path / "elsewhere")

    with pytest.raises(ValueError, match="outside"):
        inventory.put(record)


def test_inventory_fails_closed_on_corrupt_payload(tmp_path: Path) -> None:
    path = tmp_path / "inventory.json"
    path.write_text('{"schema":2,"instances":{"bot-a":null}}', encoding="utf-8")

    with pytest.raises(ValueError, match="inventory"):
        NapCatInventory(path, state_root=tmp_path / "instances")


def test_failed_flush_does_not_mutate_in_memory_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "instances"
    inventory = NapCatInventory(tmp_path / "inventory.json", state_root=root)
    original = _record(root)
    inventory.put(original)

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr("os.replace", fail_replace)
    replacement = _record(root)
    replacement.generation = 2

    with pytest.raises(OSError, match="disk unavailable"):
        inventory.put(replacement)

    assert inventory.get("bot-a") == original
    assert inventory.next_generation("bot-a") == 2


def test_get_and_all_return_detached_records(tmp_path: Path) -> None:
    root = tmp_path / "instances"
    inventory = NapCatInventory(tmp_path / "inventory.json", state_root=root)
    inventory.put(_record(root))

    selected = inventory.get("bot-a")
    assert selected is not None
    selected.retained = True
    selected.metadata["unsafe"] = "mutation"
    inventory.all()["bot-a"].bound_uin = 10001

    persisted = inventory.get("bot-a")
    assert persisted is not None
    assert persisted.retained is False
    assert persisted.bound_uin is None
    assert persisted.metadata == {}


def test_inventory_snapshot_commit_preserves_generation_high_water(
    tmp_path: Path,
) -> None:
    root = tmp_path / "instances"
    inventory = NapCatInventory(tmp_path / "inventory.json", state_root=root)
    record = _record(root)
    inventory.commit_version(
        "bot-a", InventoryVersion(record=record, generation_high_water=1)
    )

    assert inventory.matches(
        "bot-a", InventoryVersion(record=record, generation_high_water=1)
    )
    inventory.commit_version(
        "bot-a", InventoryVersion(record=None, generation_high_water=1)
    )

    assert inventory.snapshot("bot-a") == InventoryVersion(
        record=None, generation_high_water=1
    )
    assert inventory.next_generation("bot-a") == 2


def test_inventory_allows_only_exact_configured_legacy_root(tmp_path: Path) -> None:
    managed = tmp_path / "managed" / "instances"
    legacy = tmp_path / "legacy"
    inventory = NapCatInventory(
        tmp_path / "inventory.json",
        state_root=managed,
        legacy_state_roots=(legacy,),
    )
    record = NapCatInstanceRecord(
        instance_id="default",
        provider="native",
        generation=1,
        resource_id="corlinman-napcat.service",
        state_root=str(legacy),
        token_file=str(legacy / "legacy-secrets.env"),
        legacy_resource=True,
    )

    inventory.put(record)
    assert inventory.get("default") == record

    record.state_root = str(tmp_path)
    record.token_file = str(tmp_path / "legacy-secrets.env")
    with pytest.raises(ValueError, match="explicitly owned"):
        inventory.put(record)


def test_inventory_rejects_symlink_state_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    root = tmp_path / "instances"
    root.mkdir()
    (root / "bot-a").symlink_to(real, target_is_directory=True)
    inventory = NapCatInventory(tmp_path / "inventory.json", state_root=root)

    with pytest.raises(ValueError, match="symlink"):
        inventory.put(_record(root))
