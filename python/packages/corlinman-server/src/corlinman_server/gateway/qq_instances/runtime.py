"""Desired-vs-running reconciler for account-keyed QQ channel runtimes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import structlog

from corlinman_server.gateway.qq_instances import QqFleetConfig, normalize_qq_fleet
from corlinman_server.system.napcat_manager.client import NapCatManagerClient

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class QqRuntimeHandle:
    instance_id: str
    fingerprint: str
    cancel: asyncio.Event
    task: asyncio.Task[None]
    health: dict[str, Any]
    transport: dict[str, Any]
    runtime_config: dict[str, Any]
    expected_uin: int | None
    runtime_token: object
    managed: bool
    default_instance: bool
    generation: int | None = None
    identity_task: asyncio.Task[None] | None = None
    live_config: dict[str, Any] | None = None
    """THE config dict the running channel reads (``params.config``).
    Behavior-only reconciles mutate it in place instead of restarting
    the transport — see :data:`_QQ_HOT_APPLY_KEYS`."""


@dataclass(frozen=True, slots=True)
class _RuntimeSpec:
    config: dict[str, Any]
    expected_uin: int | None
    managed: bool
    fingerprint: str
    default_instance: bool

    @classmethod
    def from_handle(cls, handle: QqRuntimeHandle) -> _RuntimeSpec:
        return cls(
            config=dict(handle.runtime_config),
            expected_uin=handle.expected_uin,
            managed=handle.managed,
            fingerprint=handle.fingerprint,
            default_instance=handle.default_instance,
        )


class QqIdentityRegistry:
    """Bind one UIN per instance and reject the same UIN across instances."""

    def __init__(self) -> None:
        self._by_instance: dict[str, int] = {}
        self._by_uin: dict[int, str] = {}

    def verify(self, instance_id: str, observed_uin: int, expected_uin: int | None) -> bool:
        if expected_uin is not None and observed_uin != expected_uin:
            return False
        existing_instance = self._by_uin.get(observed_uin)
        if existing_instance not in (None, instance_id):
            return False
        existing_uin = self._by_instance.get(instance_id)
        if existing_uin not in (None, observed_uin):
            return False
        self._by_instance[instance_id] = observed_uin
        self._by_uin[observed_uin] = instance_id
        return True

    def release(self, instance_id: str) -> None:
        uin = self._by_instance.pop(instance_id, None)
        if uin is not None and self._by_uin.get(uin) == instance_id:
            self._by_uin.pop(uin, None)


class QqRuntimeRegistry:
    """Own per-instance cancellation, health, identity, and channel task."""

    def __init__(
        self,
        *,
        model: str,
        chat_service: Any,
        manager: NapCatManagerClient | None = None,
        persona_store: Any = None,
        asset_store: Any = None,
        run_qq: Any = None,
    ) -> None:
        self.model = model
        self.chat_service = chat_service
        self.manager = manager
        self.persona_store = persona_store
        self.asset_store = asset_store
        self._run_qq = run_qq
        self._handles: dict[str, QqRuntimeHandle] = {}
        self._identity = QqIdentityRegistry()
        self._lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._sidecar_config: Any = None
        self._sidecar_path: Any = None

    def handles(self) -> dict[str, QqRuntimeHandle]:
        return dict(self._handles)

    def health(self, instance_id: str) -> dict[str, Any] | None:
        handle = self._handles.get(instance_id)
        return dict(handle.health) if handle is not None else None

    def sidecar_transports(self) -> dict[str, dict[str, Any]]:
        """Snapshot identity-bound action transports for the agent sidecar."""
        return {
            instance_id: dict(handle.transport)
            for instance_id, handle in self._handles.items()
            if handle.transport.get("expected_uin") not in (None, "")
        }

    async def reconcile(self, channels: Mapping[str, Any] | QqFleetConfig) -> None:
        fleet = channels if isinstance(channels, QqFleetConfig) else normalize_qq_fleet(channels)
        async with self._lock:
            desired = {
                str(instance_id): config
                for instance_id, config in fleet.instances.items()
                if config.enabled
            }
            previous = {
                instance_id: _RuntimeSpec.from_handle(handle)
                for instance_id, handle in self._handles.items()
            }
            try:
                for instance_id, config in desired.items():
                    is_default = fleet.default_instance == config.instance_id
                    fingerprint = _config_fingerprint(config.values)
                    current = self._handles.get(instance_id)
                    if (
                        current is not None
                        and current.fingerprint == fingerprint
                        and current.default_instance == is_default
                    ):
                        continue
                    if (
                        current is not None
                        and current.default_instance == is_default
                        and current.live_config is not None
                        and _transport_fingerprint(current.runtime_config)
                        == _transport_fingerprint(config.values)
                    ):
                        # Behavior-only diff (keywords / whitelist /
                        # proactive / monitors / …) — hot-apply into the
                        # RUNNING channel instead of dropping the WS.
                        # Every admin save used to restart the instance;
                        # a restart is only for transport-level changes.
                        live = current.live_config
                        old_values = current.runtime_config
                        changed_keys = sorted(
                            str(k)
                            for k in (set(old_values) | set(config.values))
                            if old_values.get(k) != config.values.get(k)
                        )
                        # Keys injected at start time (managed-descriptor
                        # transports, env-backfilled ws_url) are absent
                        # from config.values — carry them over.
                        preserved = {
                            k: live[k]
                            for k in (
                                "ws_url",
                                "napcat_url",
                                "access_token",
                                "napcat_access_token",
                            )
                            if k in live and k not in config.values
                        }
                        live.clear()
                        live.update(dict(config.values))
                        live.update(preserved)
                        current.runtime_config = dict(config.values)
                        current.fingerprint = fingerprint
                        logger.info(
                            "gateway.qq_instance.hot_applied",
                            instance_id=instance_id,
                            changed_keys=changed_keys,
                        )
                        continue
                    if current is not None:
                        await self._stop_locked(instance_id, publish_sidecar=False)
                    await self._start_locked(
                        instance_id,
                        dict(config.values),
                        expected_uin=config.expected_uin,
                        managed=config.connection_mode == "managed",
                        fingerprint=fingerprint,
                        default_instance=is_default,
                    )
                for instance_id in sorted(set(self._handles) - set(desired)):
                    await self._stop_locked(instance_id, publish_sidecar=False)
            except Exception as reconcile_error:
                rollback_errors: list[Exception] = []
                for instance_id, handle in list(self._handles.items()):
                    old = previous.get(instance_id)
                    if old is not None and old.fingerprint == handle.fingerprint:
                        continue
                    try:
                        await self._stop_locked(instance_id, publish_sidecar=False)
                    except Exception as exc:  # noqa: BLE001
                        rollback_errors.append(exc)
                for instance_id, old in previous.items():
                    current = self._handles.get(instance_id)
                    if current is not None and current.fingerprint == old.fingerprint:
                        continue
                    try:
                        await self._start_locked(
                            instance_id,
                            dict(old.config),
                            expected_uin=old.expected_uin,
                            managed=old.managed,
                            fingerprint=old.fingerprint,
                            default_instance=old.default_instance,
                        )
                    except Exception as exc:  # noqa: BLE001
                        rollback_errors.append(exc)
                try:
                    await self._write_live_sidecar()
                except Exception as exc:  # noqa: BLE001
                    rollback_errors.append(exc)
                if rollback_errors:
                    raise RuntimeError(
                        "QQ runtime reconcile failed and rollback was incomplete"
                    ) from reconcile_error
                raise

    async def reconcile_and_write_sidecar(
        self,
        channels: Mapping[str, Any] | QqFleetConfig,
        *,
        config: Any,
        path: Any,
    ) -> None:
        """Publish a writable sidecar before applying runtime changes."""
        import os
        import tempfile
        from pathlib import Path

        from corlinman_server.gateway.lifecycle.py_config import write_py_config_sync

        # Preflight on a disposable sibling so directory permissions, encoding,
        # and atomic rename are exercised without replacing the live sidecar.
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, probe_name = tempfile.mkstemp(
            prefix=target.name + ".probe.",
            dir=str(target.parent),
        )
        os.close(fd)
        probe = Path(probe_name)
        probe.unlink(missing_ok=True)
        try:
            write_py_config_sync(config, probe, qq_transport_overlay={})
        finally:
            probe.unlink(missing_ok=True)
        previous_config = self._sidecar_config
        previous_path = self._sidecar_path
        self._sidecar_config = config
        self._sidecar_path = path
        try:
            await self.reconcile(channels)
            await self.write_sidecar(config=config, path=path)
        except Exception:
            self._sidecar_config = previous_config
            self._sidecar_path = previous_path
            if previous_config is not None and previous_path is not None:
                write_py_config_sync(
                    previous_config,
                    previous_path,
                    qq_transport_overlay=self.sidecar_transports(),
                )
            raise

    async def write_sidecar(self, *, config: Any, path: Any) -> None:
        from corlinman_server.gateway.lifecycle.py_config import write_py_config_sync

        self._sidecar_config = config
        self._sidecar_path = path
        write_py_config_sync(
            config,
            path,
            qq_transport_overlay=self.sidecar_transports(),
        )

    async def _write_live_sidecar(self) -> None:
        config = self._sidecar_config
        path = self._sidecar_path
        if config is None or path is None:
            return
        await self.write_sidecar(config=config, path=path)

    async def stop_all(self) -> None:
        async with self._lock:
            for instance_id in list(self._handles):
                await self._stop_locked(instance_id)

    async def restart(self, instance_id: str) -> bool:
        async with self._lock:
            handle = self._handles.get(instance_id)
            if handle is None:
                return False
            spec = _RuntimeSpec.from_handle(handle)
            if self.manager is not None and handle.generation is not None:
                response = await self.manager.request(
                    "restart", instance_id, generation=handle.generation
                )
                if not response.ok:
                    return False
            await self._stop_locked(instance_id, publish_sidecar=False)
            try:
                await self._start_locked(
                    instance_id,
                    dict(spec.config),
                    expected_uin=spec.expected_uin,
                    managed=spec.managed,
                    fingerprint=spec.fingerprint,
                    default_instance=spec.default_instance,
                )
            except Exception:
                await self._write_live_sidecar()
                raise
            await self._write_live_sidecar()
            return True

    async def _start_locked(
        self,
        instance_id: str,
        config: dict[str, Any],
        *,
        expected_uin: int | None,
        managed: bool,
        fingerprint: str,
        default_instance: bool,
        verified_uin: Any = None,
    ) -> None:
        runtime_config = dict(config)
        generation = None
        if managed:
            if self.manager is None:
                logger.warning("gateway.qq_instance.manager_missing", instance_id=instance_id)
                raise RuntimeError(f"managed QQ instance {instance_id!r} requires NapCat manager")
            response = await self.manager.request("inspect", instance_id)
            if not response.ok and response.error_code == "instance_not_found":
                operation = "adopt" if default_instance else "provision"
                response = await self.manager.request(operation, instance_id)
                if (
                    operation == "adopt"
                    and not response.ok
                    and response.error_code == "instance_not_found"
                ):
                    response = await self.manager.request("provision", instance_id)
            if not response.ok or response.descriptor is None:
                logger.warning(
                    "gateway.qq_instance.provision_failed",
                    instance_id=instance_id,
                    error=response.error_code,
                )
                raise RuntimeError(
                    f"managed QQ instance {instance_id!r} could not be started: "
                    f"{response.error_code or 'descriptor_missing'}"
                )
            descriptor = response.descriptor
            generation = descriptor.generation
            config.update(
                ws_url=descriptor.ws_url,
                napcat_url=descriptor.http_url,
                access_token=descriptor.access_token,
                napcat_access_token=descriptor.napcat_access_token,
            )
            if expected_uin is None:
                expected_uin = descriptor.expected_uin

        transport = {
            "ws_url": str(config.get("ws_url") or ""),
            "access_token": str(config.get("access_token") or "") or None,
            # The configured expected UIN constrains verification but does not
            # prove the currently connected runtime's identity.
            "expected_uin": verified_uin,
        }

        from corlinman_channels.service import new_qq_health

        health = new_qq_health()
        if default_instance:
            from corlinman_channels.service import QQ_HEALTH

            health = QQ_HEALTH
            health.clear()
            health.update(new_qq_health())
        cancel = asyncio.Event()
        runtime_token = object()
        guard, ready = self._identity_guard(
            instance_id,
            expected_uin,
            generation,
            runtime_token,
            health,
            initially_verified=verified_uin not in (None, ""),
        )
        if verified_uin not in (None, ""):
            self._identity.verify(instance_id, int(verified_uin), expected_uin)
        task = asyncio.create_task(
            self._run_instance(instance_id, config, cancel, health, guard, ready),
            name=f"channel-qq-{instance_id}",
        )
        self._handles[instance_id] = QqRuntimeHandle(
            instance_id=instance_id,
            fingerprint=fingerprint,
            cancel=cancel,
            task=task,
            health=health,
            transport=transport,
            runtime_config=runtime_config,
            expected_uin=expected_uin,
            runtime_token=runtime_token,
            managed=managed,
            default_instance=default_instance,
            generation=generation,
            live_config=config,
        )

    async def _run_instance(
        self,
        instance_id: str,
        config: dict[str, Any],
        cancel: asyncio.Event,
        health: dict[str, Any],
        identity_guard: Any,
        identity_ready: Any,
    ) -> None:
        from corlinman_server.gateway.channels_runtime import (
            _build_qq_params,
            _run_channel,
        )

        if self._run_qq is None:
            from corlinman_channels import run_qq_channel
        else:
            run_qq_channel = self._run_qq
        params = _build_qq_params(
            config,
            self.model,
            self.chat_service,
            instance_id=instance_id,
            health=health,
            identity_guard=identity_guard,
            identity_ready=identity_ready,
            persona_store=self.persona_store,
            asset_store=self.asset_store,
        )
        # Hot-apply contract: behavior-only reconciles mutate `config`
        # (the handle's ``live_config``) in place — the channel must read
        # the SAME object. ``_build_qq_params`` returns a copy (legacy
        # callers depend on that); fold its backfills (env ws_url) into
        # the shared dict and alias it back.
        if isinstance(params.config, dict):
            for key, value in params.config.items():
                config.setdefault(key, value)
            params.config = config
        await _run_channel(
            f"qq:{instance_id}",
            lambda event, p=params: run_qq_channel(p, event),
            cancel,
        )

    def _identity_guard(
        self,
        instance_id: str,
        expected_uin: int | None,
        generation: int | None,
        runtime_token: object,
        health: dict[str, Any],
        *,
        initially_verified: bool = False,
    ) -> tuple[Any, Any]:
        verified = initially_verified
        binding = False

        async def _bind(observed_uin: int) -> None:
            nonlocal binding, verified
            try:
                if generation is None:
                    verified = await self._publish_external_identity(
                        instance_id,
                        runtime_token,
                        observed_uin,
                    )
                else:
                    verified = await self._bind_and_publish(
                        instance_id,
                        generation,
                        runtime_token,
                        observed_uin,
                    )
            finally:
                binding = False

        def _verify(observed_uin: int) -> bool | None:
            nonlocal binding, verified
            valid = self._identity.verify(instance_id, observed_uin, expected_uin)
            if not valid:
                health.update(
                    online=False,
                    account_online=False,
                    account_last_error=(
                        "identity_mismatch"
                        if expected_uin not in (None, observed_uin)
                        else "duplicate_uin"
                    ),
                )
                verified = False
                return False
            if verified:
                return True
            if not binding:
                binding = True
                task = asyncio.create_task(_bind(observed_uin))
                handle = self._handles.get(instance_id)
                if handle is not None and handle.runtime_token is runtime_token:
                    handle.identity_task = task
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            # Binding in flight (first observation, or a retry after a
            # failed publish): pending is NOT rejected. Returning False
            # here made the channel brand a perfectly healthy login
            # "identity_rejected / kicked offline" for the window between
            # the first observed event and the async bind completing —
            # inbound stays gated via identity_ready either way.
            return None

        return _verify, lambda: verified

    async def _publish_external_identity(
        self,
        instance_id: str,
        runtime_token: object,
        observed_uin: int,
    ) -> bool:
        async with self._lock:
            handle = self._handles.get(instance_id)
            if (
                handle is None
                or handle.runtime_token is not runtime_token
                or handle.expected_uin not in (None, observed_uin)
            ):
                return False
            handle.transport["expected_uin"] = str(observed_uin)
            try:
                await self._write_live_sidecar()
            except Exception as exc:  # noqa: BLE001
                handle.transport["expected_uin"] = None
                handle.health.update(
                    account_online=False,
                    account_last_error="identity_publish_failed",
                )
                logger.warning(
                    "gateway.qq_instance.identity_publish_failed",
                    instance_id=instance_id,
                    error_type=type(exc).__name__,
                )
                return False
            return True

    async def _bind_and_publish(
        self,
        instance_id: str,
        generation: int,
        runtime_token: object,
        observed_uin: int,
    ) -> bool:
        if self.manager is None:
            return False
        response = await self.manager.request(
            "bind_uin",
            instance_id,
            generation=generation,
            expected_uin=observed_uin,
        )
        async with self._lock:
            handle = self._handles.get(instance_id)
            if (
                not response.ok
                or handle is None
                or handle.generation != generation
                or handle.runtime_token is not runtime_token
                or handle.expected_uin not in (None, observed_uin)
            ):
                if handle is not None:
                    handle.health.update(
                        account_online=False,
                        account_last_error="identity_bind_failed",
                    )
                return False
            handle.transport["expected_uin"] = str(observed_uin)
            try:
                await self._write_live_sidecar()
            except Exception as exc:  # noqa: BLE001
                handle.transport["expected_uin"] = None
                handle.health.update(
                    account_online=False,
                    account_last_error="identity_publish_failed",
                )
                logger.warning(
                    "gateway.qq_instance.identity_publish_failed",
                    instance_id=instance_id,
                    error_type=type(exc).__name__,
                )
                return False
            if handle.health.get("account_last_error") in {
                "identity_bind_failed",
                "identity_publish_failed",
            }:
                handle.health["account_last_error"] = None
            return True

    async def _stop_locked(
        self,
        instance_id: str,
        *,
        publish_sidecar: bool = True,
    ) -> None:
        handle = self._handles.pop(instance_id, None)
        if handle is None:
            return
        handle.cancel.set()
        if handle.identity_task is not None:
            handle.identity_task.cancel()
            try:
                await handle.identity_task
            except (asyncio.CancelledError, Exception):
                pass
        handle.task.cancel()
        try:
            await handle.task
        except (asyncio.CancelledError, Exception):
            pass
        self._identity.release(instance_id)
        if publish_sidecar:
            await self._write_live_sidecar()


def _config_fingerprint(values: Mapping[str, Any]) -> str:
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


#: Instance-config keys the running channel reads LIVE off ``params.config``
#: (dispatch-loop router gates, proactive loop, monitor loop, humanlike /
#: tencent resolvers). A reconcile where ONLY these differ hot-applies by
#: mutating the running channel's config dict in place — no transport
#: restart, no WS drop. Anything NOT listed (unknown/new keys included)
#: keeps restart semantics: a snapshot-read key added later fails SAFE
#: (restart applies it) instead of silently not applying.
_QQ_HOT_APPLY_KEYS = frozenset(
    {
        "display_name",
        "group_keywords",
        "group_whitelist",
        "group_replies_enabled",
        "group_reply_policy",
        "group_reply_cooldown_secs",
        "group_rate_limit_window_minutes",
        "group_rate_limit_max_messages",
        "freeze_risk_topic_blocking",
        "humanlike",
        "monitors",
        "monitor_retention_hours",
    }
)


def _is_hot_apply_key(key: str) -> bool:
    return key in _QQ_HOT_APPLY_KEYS or key.startswith("proactive_")


def _transport_fingerprint(values: Mapping[str, Any]) -> str:
    """Fingerprint of the restart-requiring subset of an instance config."""
    return _config_fingerprint(
        {k: v for k, v in values.items() if not _is_hot_apply_key(str(k))}
    )
