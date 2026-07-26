"""Serialized and crash-recoverable lifecycle coordinator for NapCat."""

from __future__ import annotations

import asyncio
import socket
from contextlib import suppress
from dataclasses import replace

from corlinman_server.gateway.qq_instances.models import parse_instance_id
from corlinman_server.system.audit import AuditEntry, SystemAuditLog, utcnow_iso
from corlinman_server.system.napcat_manager.inventory import (
    InventoryVersion,
    NapCatInventory,
)
from corlinman_server.system.napcat_manager.models import (
    ManagerRequest,
    ManagerResponse,
    NapCatInstanceRecord,
    NapCatObservedState,
)
from corlinman_server.system.napcat_manager.operation_journal import (
    NapCatOperationIntent,
    NapCatOperationJournal,
)
from corlinman_server.system.napcat_manager.protocol import (
    NapCatGenerationConflict,
    NapCatInstanceConflict,
    NapCatInstanceNotFound,
    NapCatManagerError,
    NapCatManagerUnavailable,
    NapCatProvider,
    NapCatResourceNotOwned,
    NapCatUnsupportedOperation,
)

_DURABLE_OPERATIONS = frozenset(
    {"provision", "adopt", "remove_runtime", "restore", "purge_login_state"}
)
_MUTATING_OPERATIONS = _DURABLE_OPERATIONS | {"bind_uin"}


class NapCatManager:
    """One coordinator with a durable commit point for owned resources."""

    def __init__(
        self,
        *,
        inventory: NapCatInventory,
        providers: dict[str, NapCatProvider],
        journal: NapCatOperationJournal | None = None,
        audit_log: SystemAuditLog | None = None,
        port_start: int = 16099,
        port_end: int = 16999,
    ) -> None:
        self.inventory = inventory
        self.providers = dict(providers)
        self.journal = journal or NapCatOperationJournal(
            inventory.path.parent / "operation.json"
        )
        self.audit_log = audit_log
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._port_start = port_start
        self._port_end = port_end

    async def execute(self, request: ManagerRequest, *, actor: str) -> ManagerResponse:
        instance_id = str(parse_instance_id(request.instance_id))
        request = replace(request, instance_id=instance_id)
        try:
            if request.operation in _MUTATING_OPERATIONS:
                async with self._mutation_lock:
                    await self.recover_pending()
                    lock = await self._instance_lock(instance_id)
                    async with lock:
                        response = await self._execute_locked(request)
            else:
                lock = await self._instance_lock(instance_id)
                async with lock:
                    response = await self._execute_locked(request)
        except NapCatManagerError as exc:
            await self._audit(
                action=request.operation,
                actor=actor,
                instance_id=instance_id,
                outcome="failed",
                detail=exc.code,
            )
            return ManagerResponse(
                ok=False,
                request_id=request.request_id,
                error_code=exc.code,
                message="NapCat manager operation failed",
            )
        except Exception:
            await self._audit(
                action=request.operation,
                actor=actor,
                instance_id=instance_id,
                outcome="failed",
                detail="manager_error",
            )
            return ManagerResponse(
                ok=False,
                request_id=request.request_id,
                error_code="manager_error",
                message="NapCat manager operation failed",
            )
        await self._audit(
            action=request.operation,
            actor=actor,
            instance_id=instance_id,
            outcome="succeeded",
            detail=response.warning_code or "ok",
        )
        return response

    async def recover_pending(self) -> None:
        """Replay one durable transition before accepting another mutation."""
        intent = self.journal.load()
        if intent is None:
            return
        if self.inventory.matches(intent.instance_id, intent.after):
            self._clear_journal_best_effort()
            return
        if not self.inventory.matches(intent.instance_id, intent.before):
            raise NapCatInstanceConflict(
                "pending operation does not match the current inventory"
            )
        provider = self._provider(intent.provider)
        await self._apply_intent(provider, intent)
        self.inventory.commit_version(intent.instance_id, intent.after)
        self._clear_journal_best_effort()

    async def _execute_locked(self, request: ManagerRequest) -> ManagerResponse:
        before = self.inventory.snapshot(request.instance_id)
        record = before.record
        generation_required = request.operation not in {"provision", "adopt", "inspect"}
        if generation_required and request.generation is None:
            raise NapCatGenerationConflict(
                f"{request.operation} requires an exact generation"
            )
        if record is not None and request.generation not in (
            None,
            record.generation,
        ):
            raise NapCatGenerationConflict(
                f"expected generation {record.generation}, got {request.generation}"
            )

        if request.operation in {"provision", "adopt"}:
            if record is not None:
                raise NapCatInstanceConflict("QQ instance already exists")
            provider = await self._available_provider(None)
            generation = before.generation_high_water + 1
            if request.operation == "adopt":
                next_record = await provider.plan_adoption(
                    request.instance_id, generation
                )
            elif provider.kind == "native":
                webui_port, onebot_port = self._allocate_native_ports()
                next_record = await provider.plan_provision(
                    request.instance_id,
                    generation,
                    webui_port=webui_port,
                    onebot_port=onebot_port,
                )
            else:
                next_record = await provider.plan_provision(
                    request.instance_id,
                    generation,
                    webui_port=None,
                    onebot_port=None,
                )
            after = InventoryVersion(
                record=next_record,
                generation_high_water=generation,
            )
            return await self._commit_transition(
                request, provider, before, after
            )

        if record is None:
            raise NapCatInstanceNotFound("QQ instance is not provisioned")
        provider = self._provider(record.provider)
        self.inventory.assert_owned_paths(record)

        if request.operation == "inspect":
            return await self._response_for(request.request_id, provider, record)
        if request.operation == "bind_uin":
            if request.expected_uin is None or request.expected_uin <= 0:
                raise NapCatUnsupportedOperation(
                    "bind_uin requires a positive expected_uin"
                )
            if record.bound_uin not in (None, request.expected_uin):
                raise NapCatInstanceConflict(
                    "QQ instance is already bound to another UIN"
                )
            duplicate = next(
                (
                    candidate.instance_id
                    for candidate in self.inventory.all().values()
                    if candidate.instance_id != request.instance_id
                    and candidate.bound_uin == request.expected_uin
                ),
                None,
            )
            if duplicate is not None:
                raise NapCatInstanceConflict(
                    "QQ UIN is already bound to another managed instance"
                )
            record = replace(record, bound_uin=request.expected_uin)
            self.inventory.put(record)
        elif request.operation == "start":
            await provider.start(record)
        elif request.operation == "stop":
            await provider.stop(record)
        elif request.operation == "restart":
            await provider.restart(record)
        elif request.operation == "upgrade":
            await provider.upgrade(record)
        elif request.operation == "remove_runtime":
            if record.legacy_resource:
                raise NapCatResourceNotOwned(
                    "legacy runtime must be migrated before removal"
                )
            if record.retained:
                raise NapCatInstanceConflict("QQ instance is already retained")
            after_record = replace(record, retained=True)
            return await self._commit_transition(
                request,
                provider,
                before,
                InventoryVersion(
                    record=after_record,
                    generation_high_water=before.generation_high_water,
                ),
            )
        elif request.operation == "restore":
            if not record.retained:
                raise NapCatInstanceConflict("QQ instance is not retained")
            after_record = replace(record, retained=False)
            return await self._commit_transition(
                request,
                provider,
                before,
                InventoryVersion(
                    record=after_record,
                    generation_high_water=before.generation_high_water,
                ),
            )
        elif request.operation == "purge_login_state":
            if record.legacy_resource:
                raise NapCatResourceNotOwned(
                    "legacy runtime must be migrated before purge"
                )
            if not record.retained:
                raise NapCatResourceNotOwned(
                    "runtime must be removed before login data can be purged"
                )
            return await self._commit_transition(
                request,
                provider,
                before,
                InventoryVersion(
                    record=None,
                    generation_high_water=before.generation_high_water,
                ),
            )
        else:
            raise NapCatUnsupportedOperation(request.operation)
        return await self._response_for(request.request_id, provider, record)

    async def _commit_transition(
        self,
        request: ManagerRequest,
        provider: NapCatProvider,
        before: InventoryVersion,
        after: InventoryVersion,
    ) -> ManagerResponse:
        generation = (
            after.record.generation
            if after.record is not None
            else before.record.generation  # type: ignore[union-attr]
        )
        intent = NapCatOperationIntent(
            operation=request.operation,  # type: ignore[arg-type]
            request_id=request.request_id,
            instance_id=request.instance_id,
            provider=provider.kind,
            generation=generation,
            before=before,
            after=after,
        )
        self.journal.write(intent)
        await self._apply_intent(provider, intent)
        self.inventory.commit_version(request.instance_id, after)
        warning = None
        try:
            self.journal.clear()
        except Exception:
            warning = "journal_cleanup_deferred"
        return await self._response_after_commit(
            request.request_id,
            provider,
            after.record,
            warning_code=warning,
        )

    async def _apply_intent(
        self, provider: NapCatProvider, intent: NapCatOperationIntent
    ) -> None:
        before = intent.before.record
        after = intent.after.record
        if intent.operation == "provision":
            assert after is not None
            await provider.ensure_provisioned(after)
        elif intent.operation == "adopt":
            assert after is not None
            await provider.ensure_adopted(after)
        elif intent.operation == "remove_runtime":
            assert before is not None
            await provider.remove_runtime(before)
        elif intent.operation == "restore":
            assert before is not None
            await provider.restore(before)
        elif intent.operation == "purge_login_state":
            assert before is not None
            await provider.purge_login_state(before)
        else:  # pragma: no cover - validated by the journal model
            raise NapCatUnsupportedOperation(intent.operation)

    async def _response_after_commit(
        self,
        request_id: str,
        provider: NapCatProvider,
        record: NapCatInstanceRecord | None,
        *,
        warning_code: str | None = None,
    ) -> ManagerResponse:
        if record is None:
            return ManagerResponse(
                ok=True,
                request_id=request_id,
                warning_code=warning_code,
            )
        observed: NapCatObservedState
        try:
            observed = await provider.inspect(record)
        except Exception:
            observed = NapCatObservedState(
                instance_id=record.instance_id,
                provider=record.provider,
                generation=record.generation,
                state="retained" if record.retained else "error",
                resource_id=record.resource_id,
                retained=record.retained,
                bound_uin=record.bound_uin,
                error_code="post_commit_inspect_failed",
            )
            warning_code = warning_code or "post_commit_inspect_failed"
        if record.legacy_resource:
            observed = replace(observed, can_restore=False, can_purge=False)
        descriptor = None
        if not record.retained:
            try:
                descriptor = await provider.descriptor(record)
            except Exception:
                warning_code = warning_code or "descriptor_unavailable"
        return ManagerResponse(
            ok=True,
            request_id=request_id,
            observed=observed,
            descriptor=descriptor,
            warning_code=warning_code,
        )

    async def _response_for(
        self,
        request_id: str,
        provider: NapCatProvider,
        record: NapCatInstanceRecord,
    ) -> ManagerResponse:
        observed = await provider.inspect(record)
        if record.legacy_resource:
            observed = replace(observed, can_restore=False, can_purge=False)
        descriptor = None
        if not record.retained:
            descriptor = await provider.descriptor(record)
        return ManagerResponse(
            ok=True,
            request_id=request_id,
            observed=observed,
            descriptor=descriptor,
        )

    async def _available_provider(self, preferred: str | None) -> NapCatProvider:
        candidates = (
            [self._provider(preferred)]
            if preferred
            else list(self.providers.values())
        )
        for provider in candidates:
            if await provider.is_available():
                return provider
        raise NapCatManagerUnavailable("no managed NapCat provider is available")

    def _provider(self, kind: str | None) -> NapCatProvider:
        provider = self.providers.get(str(kind))
        if provider is None:
            raise NapCatManagerUnavailable(f"NapCat provider unavailable: {kind}")
        return provider

    async def _instance_lock(self, instance_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(instance_id, asyncio.Lock())

    def _allocate_native_ports(self) -> tuple[int, int]:
        used = {
            port
            for record in self.inventory.all().values()
            for port in (record.webui_port, record.onebot_port)
            if port is not None
        }
        port = self._port_start
        while port + 1 <= self._port_end:
            if port not in used and port + 1 not in used and _ports_free(port, port + 1):
                return port, port + 1
            port += 2
        raise NapCatManagerUnavailable("no native NapCat ports are available")

    def _clear_journal_best_effort(self) -> None:
        with suppress(Exception):
            self.journal.clear()

    async def _audit(
        self,
        *,
        action: str,
        actor: str,
        instance_id: str,
        outcome: str,
        detail: str,
    ) -> None:
        if self.audit_log is None:
            return
        with suppress(Exception):
            await self.audit_log.append(
                AuditEntry(
                    ts=utcnow_iso(),
                    event=f"napcat.{action}",
                    actor=actor,
                    details={
                        "instance_id": instance_id,
                        "outcome": outcome,
                        "detail": detail,
                    },
                )
            )


def _ports_free(*ports: int) -> bool:
    sockets: list[socket.socket] = []
    try:
        for port in ports:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            sockets.append(sock)
    except OSError:
        return False
    finally:
        for sock in sockets:
            sock.close()
    return True
