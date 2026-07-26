"""Narrow lifecycle protocol implemented by Docker and native providers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from corlinman_server.system.napcat_manager.models import (
    NapCatDescriptor,
    NapCatInstanceRecord,
    NapCatObservedState,
)


class NapCatManagerError(RuntimeError):
    code = "manager_error"


class NapCatManagerUnavailable(NapCatManagerError):
    code = "manager_unavailable"


class NapCatInstanceNotFound(NapCatManagerError):
    code = "instance_not_found"


class NapCatResourceNotOwned(NapCatManagerError):
    code = "resource_not_owned"


class NapCatGenerationConflict(NapCatManagerError):
    code = "generation_conflict"


class NapCatInstanceConflict(NapCatManagerError):
    code = "instance_conflict"


class NapCatUnsupportedOperation(NapCatManagerError):
    code = "unsupported_operation"


@runtime_checkable
class NapCatProvider(Protocol):
    kind: str

    async def is_available(self) -> bool: ...

    async def plan_provision(
        self,
        instance_id: str,
        generation: int,
        *,
        webui_port: int | None = None,
        onebot_port: int | None = None,
    ) -> NapCatInstanceRecord: ...

    async def ensure_provisioned(self, record: NapCatInstanceRecord) -> None: ...

    async def plan_adoption(
        self, instance_id: str, generation: int
    ) -> NapCatInstanceRecord: ...

    async def ensure_adopted(self, record: NapCatInstanceRecord) -> None: ...

    async def inspect(self, record: NapCatInstanceRecord) -> NapCatObservedState: ...

    async def start(self, record: NapCatInstanceRecord) -> None: ...

    async def stop(self, record: NapCatInstanceRecord) -> None: ...

    async def restart(self, record: NapCatInstanceRecord) -> None: ...

    async def upgrade(self, record: NapCatInstanceRecord) -> None: ...

    async def remove_runtime(self, record: NapCatInstanceRecord) -> None: ...

    async def restore(self, record: NapCatInstanceRecord) -> None: ...

    async def purge_login_state(self, record: NapCatInstanceRecord) -> None: ...

    async def descriptor(self, record: NapCatInstanceRecord) -> NapCatDescriptor: ...
