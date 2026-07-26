from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from corlinman_server.system.napcat_manager.inventory import NapCatInventory
from corlinman_server.system.napcat_manager.manager import NapCatManager
from corlinman_server.system.napcat_manager.models import (
    ManagerRequest,
    NapCatDescriptor,
    NapCatInstanceRecord,
    NapCatObservedState,
)


class FakeProvider:
    kind = "native"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.provisions = 0
        self.actions: list[tuple[str, str]] = []
        self.gate = asyncio.Event()
        self.gate.set()

    async def is_available(self) -> bool:
        return True

    async def plan_provision(
        self,
        instance_id: str,
        generation: int,
        *,
        webui_port: int | None = None,
        onebot_port: int | None = None,
    ) -> NapCatInstanceRecord:
        state = self.root / instance_id
        return NapCatInstanceRecord(
            instance_id=instance_id,
            provider="native",
            generation=generation,
            resource_id=f"corlinman-napcat@{instance_id}.service",
            state_root=str(state),
            token_file=str(state / "token"),
            webui_port=webui_port,
            onebot_port=onebot_port,
        )

    async def ensure_provisioned(self, record: NapCatInstanceRecord) -> None:
        self.provisions += 1
        await self.gate.wait()

    async def plan_adoption(
        self, instance_id: str, generation: int
    ) -> NapCatInstanceRecord:
        return await self.plan_provision(
            instance_id,
            generation,
            webui_port=6099,
            onebot_port=3001,
        )

    async def ensure_adopted(self, record: NapCatInstanceRecord) -> None:
        await self.ensure_provisioned(record)

    async def inspect(self, record: NapCatInstanceRecord) -> NapCatObservedState:
        return NapCatObservedState(
            record.instance_id,
            "native",
            record.generation,
            "retained" if record.retained else "running",
            retained=record.retained,
        )

    async def descriptor(self, record: NapCatInstanceRecord) -> NapCatDescriptor:
        return NapCatDescriptor(
            record.instance_id,
            record.generation,
            "ws://x",
            "http://x",
            "secret-one",
            "secret-two",
        )

    async def start(self, record: NapCatInstanceRecord) -> None:
        self.actions.append(("start", record.instance_id))

    async def stop(self, record: NapCatInstanceRecord) -> None:
        self.actions.append(("stop", record.instance_id))

    async def restart(self, record: NapCatInstanceRecord) -> None:
        self.actions.append(("restart", record.instance_id))

    async def upgrade(self, record: NapCatInstanceRecord) -> None:
        self.actions.append(("upgrade", record.instance_id))

    async def remove_runtime(self, record: NapCatInstanceRecord) -> None:
        self.actions.append(("remove", record.instance_id))

    async def restore(self, record: NapCatInstanceRecord) -> None:
        self.actions.append(("restore", record.instance_id))

    async def purge_login_state(self, record: NapCatInstanceRecord) -> None:
        self.actions.append(("purge", record.instance_id))


@pytest.fixture
def manager(tmp_path: Path) -> tuple[NapCatManager, FakeProvider]:
    root = tmp_path / "instances"
    provider = FakeProvider(root)
    inventory = NapCatInventory(tmp_path / "inventory.json", state_root=root)
    return NapCatManager(inventory=inventory, providers={"native": provider}), provider


def test_descriptor_repr_redacts_credentials() -> None:
    descriptor = NapCatDescriptor(
        "bot-a",
        1,
        "ws://x",
        "http://x",
        "onebot-secret",
        "webui-secret",
    )

    rendered = repr(descriptor)

    assert "onebot-secret" not in rendered
    assert "webui-secret" not in rendered


@pytest.mark.asyncio
async def test_manager_provisions_independent_instances(
    manager: tuple[NapCatManager, FakeProvider],
) -> None:
    target, provider = manager

    left, right = await asyncio.gather(
        target.execute(ManagerRequest("1", "provision", "bot-a"), actor="admin"),
        target.execute(ManagerRequest("2", "provision", "bot-b"), actor="admin"),
    )

    assert left.ok and right.ok
    assert provider.provisions == 2
    assert left.descriptor is not None
    assert right.descriptor is not None
    assert left.descriptor.access_token == "secret-one"
    records = target.inventory.all()
    assert records["bot-a"].webui_port != records["bot-b"].webui_port
    assert records["bot-a"].onebot_port != records["bot-b"].onebot_port


@pytest.mark.asyncio
async def test_same_instance_provision_is_single_flight(
    manager: tuple[NapCatManager, FakeProvider],
) -> None:
    target, provider = manager
    first, second = await asyncio.gather(
        target.execute(ManagerRequest("1", "provision", "bot-a"), actor="admin"),
        target.execute(ManagerRequest("2", "provision", "bot-a"), actor="admin"),
    )

    assert provider.provisions == 1
    assert sorted([first.ok, second.ok]) == [False, True]
    failed = first if not first.ok else second
    assert failed.error_code == "instance_conflict"


@pytest.mark.asyncio
async def test_manager_failure_message_does_not_reflect_private_detail(
    manager: tuple[NapCatManager, FakeProvider],
) -> None:
    target, _provider = manager
    private = "http://user:password@localhost/private?token=secret"

    class _FailingProvider(FakeProvider):
        async def ensure_provisioned(
            self, record: NapCatInstanceRecord
        ) -> None:
            del record
            raise RuntimeError(private)

    target.providers["native"] = _FailingProvider(_provider.root)

    response = await target.execute(
        ManagerRequest("1", "provision", "bot-a"), actor="admin"
    )

    assert response.error_code == "manager_error"
    assert response.message == "NapCat manager operation failed"
    assert private not in str(response.to_json())


@pytest.mark.asyncio
async def test_generation_conflict_fails_before_provider_action(
    manager: tuple[NapCatManager, FakeProvider],
) -> None:
    target, provider = manager
    created = await target.execute(
        ManagerRequest("1", "provision", "bot-a"), actor="admin"
    )
    assert created.ok

    response = await target.execute(
        ManagerRequest("2", "restart", "bot-a", generation=999), actor="admin"
    )

    assert response.error_code == "generation_conflict"
    assert provider.actions == []


@pytest.mark.asyncio
async def test_state_change_requires_exact_generation(
    manager: tuple[NapCatManager, FakeProvider],
) -> None:
    target, provider = manager
    await target.execute(ManagerRequest("1", "provision", "bot-a"), actor="admin")

    response = await target.execute(
        ManagerRequest("2", "restart", "bot-a"), actor="admin"
    )

    assert response.error_code == "generation_conflict"
    assert provider.actions == []


@pytest.mark.asyncio
async def test_bind_uin_is_globally_unique(
    manager: tuple[NapCatManager, FakeProvider],
) -> None:
    target, _provider = manager
    await target.execute(ManagerRequest("1", "provision", "bot-a"), actor="admin")
    await target.execute(ManagerRequest("2", "provision", "bot-b"), actor="admin")

    left, right = await asyncio.gather(
        target.execute(
            ManagerRequest("3", "bind_uin", "bot-a", generation=1, expected_uin=10001),
            actor="admin",
        ),
        target.execute(
            ManagerRequest("4", "bind_uin", "bot-b", generation=1, expected_uin=10001),
            actor="admin",
        ),
    )

    assert sorted([left.ok, right.ok]) == [False, True]
    rejected = left if not left.ok else right
    assert rejected.error_code == "instance_conflict"
    bound = [row.bound_uin for row in target.inventory.all().values()]
    assert bound.count(10001) == 1


@pytest.mark.asyncio
async def test_remove_restore_purge_are_staged(
    manager: tuple[NapCatManager, FakeProvider],
) -> None:
    target, provider = manager
    await target.execute(ManagerRequest("1", "provision", "bot-a"), actor="admin")

    removed = await target.execute(
        ManagerRequest("2", "remove_runtime", "bot-a", generation=1), actor="admin"
    )
    assert removed.ok and removed.observed is not None and removed.observed.retained
    restored = await target.execute(
        ManagerRequest("3", "restore", "bot-a", generation=1), actor="admin"
    )
    assert restored.ok and restored.observed is not None and not restored.observed.retained
    refused = await target.execute(
        ManagerRequest("4", "purge_login_state", "bot-a", generation=1), actor="admin"
    )
    assert refused.error_code == "resource_not_owned"
    await target.execute(
        ManagerRequest("5", "remove_runtime", "bot-a", generation=1), actor="admin"
    )
    purged = await target.execute(
        ManagerRequest("6", "purge_login_state", "bot-a", generation=1), actor="admin"
    )
    assert purged.ok
    assert target.inventory.get("bot-a") is None
    assert provider.actions == [
        ("remove", "bot-a"),
        ("restore", "bot-a"),
        ("remove", "bot-a"),
        ("purge", "bot-a"),
    ]


@pytest.mark.asyncio
async def test_retained_instance_cannot_be_reprovisioned(
    manager: tuple[NapCatManager, FakeProvider],
) -> None:
    target, provider = manager
    await target.execute(ManagerRequest("1", "provision", "bot-a"), actor="admin")
    await target.execute(
        ManagerRequest("2", "remove_runtime", "bot-a", generation=1), actor="admin"
    )

    reprovisioned = await target.execute(
        ManagerRequest("3", "provision", "bot-a"), actor="admin"
    )

    assert reprovisioned.error_code == "instance_conflict"
    assert provider.provisions == 1
    record = target.inventory.get("bot-a")
    assert record is not None and record.retained and record.generation == 1


@pytest.mark.asyncio
async def test_generation_high_water_survives_purge(
    manager: tuple[NapCatManager, FakeProvider],
) -> None:
    target, _provider = manager
    first = await target.execute(
        ManagerRequest("1", "provision", "bot-a"), actor="admin"
    )
    assert first.descriptor is not None
    assert first.descriptor.generation == 1
    await target.execute(
        ManagerRequest("2", "remove_runtime", "bot-a", generation=1), actor="admin"
    )
    await target.execute(
        ManagerRequest("3", "purge_login_state", "bot-a", generation=1), actor="admin"
    )

    second = await target.execute(
        ManagerRequest("4", "provision", "bot-a"), actor="admin"
    )

    assert second.descriptor is not None
    assert second.descriptor.generation == 2
    stale = await target.execute(
        ManagerRequest("5", "restart", "bot-a", generation=1), actor="admin"
    )
    assert stale.error_code == "generation_conflict"
