"""Canonical account-scoped QQ configuration and lifecycle routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status

from corlinman_server.gateway.qq_instances import QqAdminError
from corlinman_server.gateway.qq_instances.admin import RetainedQqInstance
from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a._channels_lib import ChannelConfigBody
from corlinman_server.gateway.routes_admin_a._qq_instances_lib import (
    ConfigOut,
    CreateQqInstanceBody,
    DeletionBody,
    HumanlikeBody,
    HumanlikeOut,
    KeywordsBody,
    KeywordsOut,
    MigrationBody,
    MonitorsBody,
    MonitorsOut,
    MonitorsStatusOut,
    MonitorTriggerOut,
    PatchQqInstanceBody,
    PurgeBody,
    QqInstanceOut,
    QqInstancesOut,
    ReconnectOut,
    RestoreBody,
    apply_config,
    monitor_window_counts,
    parse_monitor_entries,
    qq_admin_service,
    raise_http,
    validate_keywords,
)
from corlinman_server.gateway.routes_admin_a.state import AdminState, get_admin_state
from corlinman_server.gateway.routes_admin_b._napcat_lib import (
    NapcatError,
    _ensure_onebot_websocket_server,
    _napcat_http_error,
    _NapcatClient,
    _onebot_websocket_server_from_instance_config,
    _probe_napcat_diagnostics_for_instance,
    _resolve_napcat_endpoint_for_instance,
)


def _qzone_references(state: AdminState, instance_id: str) -> list[dict[str, Any]]:
    scheduler_state = state.scheduler_admin_state
    if scheduler_state is None:
        raise QqAdminError(
            503,
            "scheduler_admin_unavailable",
            "QZone scheduler control plane is unavailable",
        )
    from corlinman_server.gateway.routes_admin_b.infra._scheduler_lib import (
        list_qzone_instance_references,
    )

    return list_qzone_instance_references(scheduler_state, instance_id)


def _migrate_qzone_references(
    state: AdminState,
    source_instance_id: str,
    target_instance_id: str,
) -> list[dict[str, Any]]:
    scheduler_state = state.scheduler_admin_state
    if scheduler_state is None:
        raise QqAdminError(
            503,
            "scheduler_admin_unavailable",
            "QZone scheduler control plane is unavailable",
        )
    from corlinman_server.gateway.routes_admin_b.infra._scheduler_lib import (
        _runtime_job_to_out,
        migrate_qzone_instance_references,
    )

    return [
        _runtime_job_to_out(row).model_dump()
        for row in migrate_qzone_instance_references(
            scheduler_state,
            source_instance_id,
            target_instance_id,
        )
    ]


def _has_login_attempts(state: AdminState, instance_id: str) -> bool:
    scheduler_state = state.scheduler_admin_state
    store = (
        getattr(scheduler_state, "qq_login_attempts", None)
        if scheduler_state is not None
        else None
    )
    return bool(store is not None and store.has_instance(instance_id))


def _manager(state: AdminState) -> Any:
    if state.napcat_manager is None:
        raise QqAdminError(
            503,
            "manager_unavailable",
            "managed NapCat manager is unavailable",
        )
    return state.napcat_manager


@asynccontextmanager
async def _lifecycle_lock(
    state: AdminState,
    instance_id: str,
) -> AsyncIterator[None]:
    locks = state.qq_lifecycle_locks
    lock = locks.setdefault(instance_id, asyncio.Lock())
    async with lock:
        yield


def _manager_failure(
    response: Any,
    *,
    fallback_code: str,
    fallback_message: str,
) -> QqAdminError:
    public_codes = {
        "forbidden",
        "generation_conflict",
        "instance_conflict",
        "manager_unavailable",
        "resource_not_owned",
        "unsupported_operation",
    }
    code = response.error_code if response.error_code in public_codes else fallback_code
    return QqAdminError(409, code, fallback_message)


def _require_managed_generation(retained: RetainedQqInstance) -> int:
    generation = retained.manager_generation
    if retained.connection_mode != "managed" or generation is None or generation <= 0:
        raise QqAdminError(
            409,
            "retained_generation_missing",
            "retained managed QQ state has no verified manager generation",
        )
    return generation


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/channels/qq/instances",
        response_model=QqInstancesOut,
        summary="List configured and retained QQ instances",
    )
    async def list_instances(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> QqInstancesOut:
        try:
            service = qq_admin_service(state)
            snapshots, revision, warnings = service.list_instances()
            return QqInstancesOut(
                instances=[QqInstanceOut.from_snapshot(row) for row in snapshots],
                retained=sorted(service.retained_store().list()),
                revision=revision,
                warnings=list(warnings),
            )
        except QqAdminError as exc:
            raise_http(exc)

    @r.post(
        "/admin/channels/qq/instances",
        response_model=QqInstanceOut,
        status_code=status.HTTP_201_CREATED,
        summary="Create a managed QQ instance",
    )
    async def create_instance(
        body: CreateQqInstanceBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> QqInstanceOut:
        try:
            snapshot = await qq_admin_service(state).create_instance(
                body.instance_id,
                display_name=body.display_name,
                enabled=body.enabled,
                expected_revision=if_match,
            )
            return QqInstanceOut.from_snapshot(snapshot)
        except QqAdminError as exc:
            raise_http(exc)

    @r.get(
        "/admin/channels/qq/instances/{instance_id}",
        response_model=QqInstanceOut,
    )
    async def get_instance(
        instance_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> QqInstanceOut:
        try:
            return QqInstanceOut.from_snapshot(
                qq_admin_service(state).get_instance(instance_id)
            )
        except QqAdminError as exc:
            raise_http(exc)

    @r.patch(
        "/admin/channels/qq/instances/{instance_id}",
        response_model=QqInstanceOut,
    )
    async def patch_instance(
        instance_id: str,
        body: PatchQqInstanceBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> QqInstanceOut:
        try:
            snapshot = await qq_admin_service(state).patch_instance(
                instance_id,
                display_name=body.display_name,
                enabled=body.enabled,
                expected_revision=if_match,
            )
            return QqInstanceOut.from_snapshot(snapshot)
        except QqAdminError as exc:
            raise_http(exc)

    @r.post(
        "/admin/channels/qq/instances/{instance_id}/set-default",
        response_model=QqInstanceOut,
    )
    async def set_default(
        instance_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> QqInstanceOut:
        try:
            snapshot = await qq_admin_service(state).set_default(
                instance_id,
                expected_revision=if_match,
            )
            return QqInstanceOut.from_snapshot(snapshot)
        except QqAdminError as exc:
            raise_http(exc)

    @r.get(
        "/admin/channels/qq/instances/{instance_id}/status",
        response_model=QqInstanceOut,
    )
    async def instance_status(
        instance_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> QqInstanceOut:
        try:
            return QqInstanceOut.from_snapshot(
                qq_admin_service(state).get_instance(instance_id)
            )
        except QqAdminError as exc:
            raise_http(exc)

    @r.put(
        "/admin/channels/qq/instances/{instance_id}/config",
        response_model=ConfigOut,
    )
    async def put_instance_config(
        instance_id: str,
        body: ChannelConfigBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> ConfigOut:
        try:
            snapshot, wrote = await qq_admin_service(state).update_instance_config(
                instance_id,
                lambda values: apply_config(values, body),
                expected_revision=if_match,
            )
            return ConfigOut(
                wrote=list(wrote),
                instance=QqInstanceOut.from_snapshot(snapshot),
            )
        except QqAdminError as exc:
            raise_http(exc)
        except HTTPException:
            raise

    @r.put(
        "/admin/channels/qq/instances/{instance_id}/keywords",
        response_model=KeywordsOut,
    )
    @r.post(
        "/admin/channels/qq/instances/{instance_id}/keywords",
        response_model=KeywordsOut,
        include_in_schema=False,
    )
    async def put_instance_keywords(
        instance_id: str,
        body: KeywordsBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> KeywordsOut:
        try:
            cleaned = validate_keywords(body.group_keywords)

            def _apply(values: dict[str, Any]) -> dict[str, list[str]]:
                values["group_keywords"] = cleaned
                return cleaned

            snapshot, group_keywords = await qq_admin_service(state).update_instance_config(
                instance_id,
                _apply,
                expected_revision=if_match,
            )
            return KeywordsOut(
                group_keywords=dict(group_keywords),
                revision=snapshot.revision,
            )
        except QqAdminError as exc:
            raise_http(exc)

    @r.get(
        "/admin/channels/qq/instances/{instance_id}/humanlike",
        response_model=HumanlikeOut,
    )
    async def get_instance_humanlike(
        instance_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> HumanlikeOut:
        try:
            snapshot = qq_admin_service(state).get_instance(instance_id)
            block = snapshot.config.get("humanlike")
            block = block if isinstance(block, dict) else {}
            return HumanlikeOut(
                enabled=bool(block.get("enabled", False)),
                persona_id=block.get("persona_id"),
                revision=snapshot.revision,
            )
        except QqAdminError as exc:
            raise_http(exc)

    @r.put(
        "/admin/channels/qq/instances/{instance_id}/humanlike",
        response_model=HumanlikeOut,
    )
    async def put_instance_humanlike(
        instance_id: str,
        body: HumanlikeBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> HumanlikeOut:
        if body.enabled and not body.persona_id:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "persona_id_required",
                    "message": "enabled=true requires persona_id",
                },
            )
        if body.persona_id:
            store = state.persona_store
            if store is None:
                raise HTTPException(
                    status_code=503,
                    detail={"error": "persona_store_missing"},
                )
            if await store.get(body.persona_id) is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "persona_not_found", "id": body.persona_id},
                )
        try:
            def _apply(values: dict[str, Any]) -> dict[str, Any]:
                # No None values on disk — TOML can't serialise null, and
                # a disable-without-persona PUT would kill the config write.
                block: dict[str, Any] = {"enabled": bool(body.enabled)}
                if body.persona_id:
                    block["persona_id"] = body.persona_id
                values["humanlike"] = block
                return block

            snapshot, block = await qq_admin_service(state).update_instance_config(
                instance_id,
                _apply,
                expected_revision=if_match,
            )
            return HumanlikeOut(
                enabled=bool(block["enabled"]),
                persona_id=block.get("persona_id"),
                revision=snapshot.revision,
            )
        except QqAdminError as exc:
            raise_http(exc)

    @r.get(
        "/admin/channels/qq/instances/{instance_id}/monitors",
        response_model=MonitorsOut,
    )
    async def get_instance_monitors(
        instance_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> MonitorsOut:
        try:
            snapshot = qq_admin_service(state).get_instance(instance_id)
            monitors, warnings = parse_monitor_entries(
                snapshot.config.get("monitors")
            )
            return MonitorsOut(
                monitors=monitors,
                warnings=warnings,
                revision=snapshot.revision,
            )
        except QqAdminError as exc:
            raise_http(exc)

    @r.put(
        "/admin/channels/qq/instances/{instance_id}/monitors",
        response_model=MonitorsOut,
    )
    async def put_instance_monitors(
        instance_id: str,
        body: MonitorsBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> MonitorsOut:
        try:
            # exclude_none: TOML has no null — a daily task's
            # interval_minutes=None (or an interval task's daily_time)
            # would make tomli_w blow up the whole config write.
            rows = [
                monitor.model_dump(exclude_none=True) for monitor in body.monitors
            ]

            def _apply(values: dict[str, Any]) -> list[dict[str, Any]]:
                if rows:
                    values["monitors"] = rows
                else:
                    values.pop("monitors", None)
                return rows

            snapshot, _rows = await qq_admin_service(state).update_instance_config(
                instance_id,
                _apply,
                expected_revision=if_match,
            )
            return MonitorsOut(monitors=body.monitors, revision=snapshot.revision)
        except QqAdminError as exc:
            raise_http(exc)

    @r.post(
        "/admin/channels/qq/instances/{instance_id}/monitors/{monitor_id}/trigger",
        response_model=MonitorTriggerOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def trigger_instance_monitor(
        instance_id: str,
        monitor_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> MonitorTriggerOut:
        try:
            snapshot = qq_admin_service(state).get_instance(instance_id)
        except QqAdminError as exc:
            raise_http(exc)
        monitors, _warnings = parse_monitor_entries(snapshot.config.get("monitors"))
        known = {monitor.id for monitor in monitors if monitor.enabled}
        if monitor_id not in known:
            raise HTTPException(
                status_code=404,
                detail={"error": "monitor_not_found", "id": monitor_id},
            )
        from corlinman_channels import qq_monitor_trigger

        if not qq_monitor_trigger(instance_id, monitor_id):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "monitor_loop_not_running",
                    "message": "QQ instance offline or monitor digest inactive",
                },
            )
        return MonitorTriggerOut()

    @r.get(
        "/admin/channels/qq/instances/{instance_id}/monitors/status",
        response_model=MonitorsStatusOut,
    )
    async def get_instance_monitors_status(
        instance_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> MonitorsStatusOut:
        try:
            snapshot = qq_admin_service(state).get_instance(instance_id)
        except QqAdminError as exc:
            raise_http(exc)
        monitors, _warnings = parse_monitor_entries(snapshot.config.get("monitors"))
        from corlinman_channels import qq_monitor_status_snapshot

        return MonitorsStatusOut(
            statuses=qq_monitor_status_snapshot(instance_id),
            counts=await monitor_window_counts(instance_id, monitors),
        )

    @r.post(
        "/admin/channels/qq/instances/{instance_id}/reconnect",
        response_model=ReconnectOut,
    )
    async def reconnect_instance(
        instance_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> ReconnectOut:
        try:
            service = qq_admin_service(state)
            snapshot = service.get_instance(instance_id)
            if not snapshot.enabled:
                raise QqAdminError(409, "channel_disabled", "QQ instance is disabled")
            registry = state.qq_runtime_registry
            if registry is not None and registry.health(instance_id) is not None:
                restarted = await registry.restart(instance_id)
                if not restarted:
                    raise QqAdminError(502, "reconnect_failed", "runtime restart failed")
                return ReconnectOut(status="ok", changed=True)
            endpoint = await _resolve_napcat_endpoint_for_instance(state, instance_id)
            if endpoint.url is None:  # pragma: no cover - resolver fails closed
                raise QqAdminError(
                    503,
                    "napcat_not_configured",
                    "NapCat URL is unavailable",
                )
            async with _NapcatClient(endpoint.url, endpoint.access_token) as client:
                changed = await _ensure_onebot_websocket_server(
                    client,
                    _onebot_websocket_server_from_instance_config(
                        instance_id,
                        snapshot.config,
                        endpoint=endpoint,
                    ),
                )
            return ReconnectOut(status="ok", changed=changed)
        except QqAdminError as exc:
            raise_http(exc)
        except NapcatError as exc:
            error_status, detail = _napcat_http_error(
                exc,
                code="reconnect_failed",
                message="failed to reconnect the QQ instance",
            )
            raise HTTPException(status_code=error_status, detail=detail) from exc

    @r.get(
        "/admin/channels/qq/instances/{instance_id}/napcat/diagnostics",
    )
    @r.get(
        "/admin/channels/qq/instances/{instance_id}/diagnostics",
        include_in_schema=False,
    )
    async def instance_diagnostics(
        instance_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, Any]:
        try:
            qq_admin_service(state).get_instance(instance_id)
            return (
                await _probe_napcat_diagnostics_for_instance(state, instance_id)
            ).model_dump()
        except QqAdminError as exc:
            raise_http(exc)

    @r.get("/admin/channels/qq/instances/{instance_id}/deletion-impact")
    async def deletion_impact(
        instance_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, Any]:
        try:
            snapshot = qq_admin_service(state).get_instance(instance_id)
            references = _qzone_references(state, instance_id)
            attempts = _has_login_attempts(state, instance_id)
            return {
                "instance_id": instance_id,
                "is_default": snapshot.is_default,
                "qzone_references": references,
                "active_login_attempts": attempts,
                "can_delete": not references and not attempts,
                "preserves_login_state": snapshot.connection_mode == "managed",
                "preserves_history": True,
            }
        except QqAdminError as exc:
            raise_http(exc)

    @r.post("/admin/channels/qq/instances/{instance_id}/migrate")
    @r.post(
        "/admin/channels/qq/instances/{instance_id}/qzone-jobs/migrate",
        include_in_schema=False,
    )
    async def migrate_references(
        instance_id: str,
        body: MigrationBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, Any]:
        try:
            qq_admin_service(state).get_instance(instance_id)
            rows = _migrate_qzone_references(
                state,
                instance_id,
                body.target_instance_id,
            )
            return {
                "ok": True,
                "source_instance_id": instance_id,
                "target_instance_id": body.target_instance_id,
                "jobs": rows,
            }
        except QqAdminError as exc:
            raise_http(exc)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "qzone_job_migration_conflict",
                    "message": (
                        "QQ job migration conflicts with existing references"
                    ),
                },
            ) from exc

    @r.delete("/admin/channels/qq/instances/{instance_id}")
    async def delete_instance(
        instance_id: str,
        body: DeletionBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> dict[str, Any]:
        service = qq_admin_service(state)
        try:
            async with _lifecycle_lock(state, instance_id):
                snapshot = service.validate_instance_removal(
                    instance_id,
                    new_default=body.new_default,
                    expected_revision=if_match,
                    require_managed=False,
                )
                references = _qzone_references(state, instance_id)
                if references:
                    raise QqAdminError(
                        409,
                        "qq_instance_referenced",
                        "QZone jobs must be migrated or deleted first",
                        references=references,
                    )
                if _has_login_attempts(state, instance_id):
                    raise QqAdminError(
                        409,
                        "login_attempt_active",
                        "revoke active login attempts before deleting this instance",
                    )
                retained_store = service.retained_store()
                if retained_store.get(instance_id) is not None:
                    raise QqAdminError(
                        409,
                        "retained_instance_exists",
                        "this QQ instance already has retained lifecycle state",
                    )
                manager = None
                generation = None
                if snapshot.connection_mode == "managed":
                    manager = _manager(state)
                    inspected = await manager.request("inspect", instance_id)
                    if not inspected.ok or inspected.observed is None:
                        raise _manager_failure(
                            inspected,
                            fallback_code="manager_inspect_failed",
                            fallback_message="failed to verify the managed NapCat runtime",
                        )
                    observed = inspected.observed
                    if observed.retained:
                        raise QqAdminError(
                            409,
                            "manager_state_conflict",
                            "managed NapCat runtime is already retained",
                        )
                    if not (observed.can_restore and observed.can_purge):
                        raise QqAdminError(
                            409,
                            "manager_migration_required",
                            "this legacy NapCat runtime cannot be safely retained",
                        )
                    generation = observed.generation
                retained = RetainedQqInstance(
                    raw_config=service.raw_instance_config(instance_id),
                    connection_mode=snapshot.connection_mode,
                    manager_generation=generation,
                    phase="deleting",
                )
                retained_store.put(instance_id, retained)
                runtime_retained = False
                try:
                    if manager is not None:
                        response = await manager.request(
                            "remove_runtime",
                            instance_id,
                            generation=generation,
                        )
                        if not response.ok:
                            raise _manager_failure(
                                response,
                                fallback_code="manager_remove_failed",
                                fallback_message="failed to retain the NapCat login state",
                            )
                        runtime_retained = True
                    await service.remove_instance_config(
                        instance_id,
                        new_default=body.new_default,
                        expected_revision=if_match,
                        require_managed=False,
                    )
                    retained_store.put(instance_id, retained.with_phase("retained"))
                except Exception as exc:
                    rollback_errors: list[str] = []
                    if runtime_retained and manager is not None and generation is not None:
                        restored = await manager.request(
                            "restore",
                            instance_id,
                            generation=generation,
                        )
                        if not restored.ok:
                            rollback_errors.append(
                                restored.error_code or "manager_restore_failed"
                            )
                    if not rollback_errors:
                        try:
                            retained_store.delete(instance_id)
                        except Exception:  # noqa: BLE001
                            rollback_errors.append("retained_state_cleanup_failed")
                    if rollback_errors:
                        raise QqAdminError(
                            500,
                            "qq_instance_delete_rollback_incomplete",
                            "QQ instance deletion failed and compensation was incomplete",
                            rollback_errors=rollback_errors,
                        ) from exc
                    raise
                return {
                    "ok": True,
                    "instance_id": instance_id,
                    "retained": snapshot.connection_mode == "managed",
                    "preserved": (
                        ["login_state", "chat", "memory", "audit"]
                        if snapshot.connection_mode == "managed"
                        else ["configuration", "chat", "memory", "audit"]
                    ),
                }
        except QqAdminError as exc:
            raise_http(exc)

    @r.post(
        "/admin/channels/qq/instances/{instance_id}/restore",
        response_model=QqInstanceOut,
    )
    async def restore_instance(
        instance_id: str,
        body: RestoreBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> QqInstanceOut:
        service = qq_admin_service(state)
        try:
            async with _lifecycle_lock(state, instance_id):
                store = service.retained_store()
                retained = store.get(instance_id)
                if retained is None:
                    raise QqAdminError(
                        404,
                        "retained_instance_not_found",
                        f"QQ instance {instance_id!r} has no retained state",
                    )
                if retained.phase != "retained":
                    raise QqAdminError(
                        409,
                        "retained_instance_busy",
                        "retained QQ lifecycle operation is not complete",
                    )
                manager = None
                generation = None
                if retained.connection_mode == "managed":
                    generation = _require_managed_generation(retained)
                    manager = _manager(state)
                store.put(instance_id, retained.with_phase("restoring"))
                runtime_restored = False
                try:
                    if manager is not None and generation is not None:
                        response = await manager.request(
                            "restore",
                            instance_id,
                            generation=generation,
                        )
                        if not response.ok:
                            raise _manager_failure(
                                response,
                                fallback_code="manager_restore_failed",
                                fallback_message="failed to restore the NapCat runtime",
                            )
                        runtime_restored = True
                    snapshot = await service.restore_instance_config(
                        instance_id,
                        retained.raw_config,
                        make_default=body.make_default,
                        expected_revision=if_match,
                    )
                except Exception as exc:
                    rollback_errors: list[str] = []
                    if runtime_restored and manager is not None and generation is not None:
                        rolled_back = await manager.request(
                            "remove_runtime",
                            instance_id,
                            generation=generation,
                        )
                        if not rolled_back.ok:
                            rollback_errors.append(
                                rolled_back.error_code or "manager_remove_failed"
                            )
                    if not rollback_errors:
                        try:
                            store.put(instance_id, retained)
                        except Exception:  # noqa: BLE001
                            rollback_errors.append("retained_state_restore_failed")
                    if rollback_errors:
                        raise QqAdminError(
                            500,
                            "qq_instance_restore_rollback_incomplete",
                            "QQ instance restore failed and compensation was incomplete",
                            rollback_errors=rollback_errors,
                        ) from exc
                    raise
                store.delete(instance_id)
                return QqInstanceOut.from_snapshot(snapshot)
        except QqAdminError as exc:
            raise_http(exc)

    @r.post("/admin/channels/qq/instances/{instance_id}/purge")
    @r.post(
        "/admin/channels/qq/instances/{instance_id}/purge-login-data",
        include_in_schema=False,
    )
    async def purge_instance(
        instance_id: str,
        body: PurgeBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, Any]:
        service = qq_admin_service(state)
        try:
            async with _lifecycle_lock(state, instance_id):
                if body.confirm_instance_id != instance_id:
                    raise QqAdminError(
                        422,
                        "purge_confirmation_mismatch",
                        "confirm_instance_id must exactly match the requested instance",
                    )
                try:
                    service.get_instance(instance_id)
                except QqAdminError as exc:
                    if exc.code != "qq_instance_not_found":
                        raise
                else:
                    raise QqAdminError(
                        409,
                        "instance_still_configured",
                        "delete the QQ instance before purging its login state",
                    )
                store = service.retained_store()
                retained = store.get(instance_id)
                if retained is None:
                    raise QqAdminError(
                        404,
                        "retained_instance_not_found",
                        f"QQ instance {instance_id!r} has no retained state",
                    )
                if retained.phase != "retained":
                    raise QqAdminError(
                        409,
                        "retained_instance_busy",
                        "retained QQ lifecycle operation is not complete",
                    )
                if retained.connection_mode != "managed":
                    raise QqAdminError(
                        409,
                        "external_instance_not_owned",
                        "external QQ login state is not owned by Corlinman",
                    )
                if _has_login_attempts(state, instance_id):
                    raise QqAdminError(
                        409,
                        "login_attempt_active",
                        "revoke active login attempts before purge",
                    )
                generation = _require_managed_generation(retained)
                store.put(instance_id, retained.with_phase("purging"))
                try:
                    response = await _manager(state).request(
                        "purge_login_state",
                        instance_id,
                        generation=generation,
                    )
                    if not response.ok:
                        raise _manager_failure(
                            response,
                            fallback_code="manager_purge_failed",
                            fallback_message="failed to purge retained login state",
                        )
                except Exception:
                    store.put(instance_id, retained)
                    raise
                store.delete(instance_id)
                return {
                    "ok": True,
                    "instance_id": instance_id,
                    "purged": ["login_state", "manager_credentials"],
                    "preserved": ["chat", "memory", "audit"],
                }
        except QqAdminError as exc:
            raise_http(exc)

    return r


__all__ = ["router"]
