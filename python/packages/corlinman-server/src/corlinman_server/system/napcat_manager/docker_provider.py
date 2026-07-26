"""Docker provider for one independently persisted NapCat per QQ instance."""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from corlinman_server.gateway.qq_instances.models import parse_instance_id
from corlinman_server.system.napcat_manager.models import (
    NapCatDescriptor,
    NapCatInstanceRecord,
    NapCatObservedState,
)
from corlinman_server.system.napcat_manager.native_provider import (
    _fsync_directory,
    _read_tokens,
    _write_token_file,
)
from corlinman_server.system.napcat_manager.protocol import (
    NapCatInstanceNotFound,
    NapCatManagerUnavailable,
    NapCatResourceNotOwned,
)

_INSTALLATION_LABEL = "io.corlinman.installation"
_INSTANCE_LABEL = "io.corlinman.qq-instance"
_GENERATION_LABEL = "io.corlinman.generation"
_MANAGED_LABEL = "io.corlinman.managed-napcat"


class DockerNapCatProvider:
    kind = "docker"

    def __init__(
        self,
        *,
        state_root: Path,
        installation_id: str,
        image: str,
        network_name: str,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._state_root = Path(state_root).resolve(strict=False)
        self._installation_id = installation_id
        self._image = image
        self._network_name = network_name
        self._client_factory = client_factory

    async def is_available(self) -> bool:
        try:
            client = await asyncio.to_thread(self._client)
            await asyncio.to_thread(client.ping)
        except Exception:
            return False
        return True

    async def plan_provision(
        self,
        instance_id: str,
        generation: int,
        *,
        webui_port: int | None = None,
        onebot_port: int | None = None,
    ) -> NapCatInstanceRecord:
        del webui_port, onebot_port
        instance_id = str(parse_instance_id(instance_id))
        if not await self.is_available():
            raise NapCatManagerUnavailable("Docker Engine unavailable")
        root = self._state_root / instance_id
        resource_id = f"corlinman-napcat-{instance_id}"
        return NapCatInstanceRecord(
            instance_id=instance_id,
            provider="docker",
            generation=generation,
            resource_id=resource_id,
            state_root=str(root),
            token_file=str(root / "manager-secrets.env"),
            metadata={
                "app_volume": f"{resource_id}-app",
                "qq_volume": f"{resource_id}-qq",
                "image": self._image,
                "network": self._network_name,
            },
        )

    async def ensure_provisioned(self, record: NapCatInstanceRecord) -> None:
        self._assert_record(record)
        if record.legacy_resource:
            raise NapCatResourceNotOwned("legacy resource is not provisionable")
        root = Path(record.state_root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_file = Path(record.token_file)
        if not token_file.exists():
            _write_token_file(token_file)
        webui_token, _ = _read_tokens(token_file)
        labels = self._labels(record.instance_id, record.generation)
        app_volume = record.metadata.get("app_volume")
        qq_volume = record.metadata.get("qq_volume")
        image = record.metadata.get("image")
        network = record.metadata.get("network")
        if not all((app_volume, qq_volume, image, network)):
            raise NapCatResourceNotOwned("Docker inventory metadata is incomplete")

        def _ensure() -> None:
            client = self._client()
            for name in (app_volume, qq_volume):
                try:
                    volume = client.volumes.get(name)
                except Exception as exc:
                    if not _is_not_found(exc):
                        raise
                    volume = client.volumes.create(name=name, labels=labels)
                self._assert_volume(volume, record.instance_id, record.generation)
            try:
                existing = client.containers.get(record.resource_id)
            except Exception as exc:
                if not _is_not_found(exc):
                    raise
            else:
                self._assert_container(
                    existing, record.instance_id, record.generation
                )
                existing.reload()
                if str(existing.status) != "running":
                    existing.start()
                return
            client.containers.run(
                image,
                detach=True,
                name=record.resource_id,
                hostname=record.resource_id,
                environment={
                    "NAPCAT_UID": "1000",
                    "NAPCAT_GID": "1000",
                    "NAPCAT_WEBUI_SECRET_KEY": webui_token,
                    "WEBUI_TOKEN": webui_token,
                },
                labels=labels,
                volumes={
                    app_volume: {"bind": "/app/napcat", "mode": "rw"},
                    qq_volume: {"bind": "/app/.config/QQ", "mode": "rw"},
                },
                network=network,
                restart_policy={"Name": "unless-stopped"},
            )

        try:
            await asyncio.to_thread(_ensure)
        except NapCatResourceNotOwned:
            raise
        except Exception as exc:
            raise NapCatManagerUnavailable(str(exc)) from exc

    async def plan_adoption(
        self, instance_id: str, generation: int
    ) -> NapCatInstanceRecord:
        instance_id = str(parse_instance_id(instance_id))
        if instance_id != "default":
            raise NapCatInstanceNotFound("only the legacy default can be adopted")

        def _inspect() -> None:
            try:
                container = self._client().containers.get("corlinman-napcat")
            except Exception as exc:
                if _is_not_found(exc):
                    raise NapCatInstanceNotFound(
                        "legacy Docker NapCat is absent"
                    ) from exc
                raise
            self._legacy_webui_token(container)

        try:
            await asyncio.to_thread(_inspect)
        except (NapCatInstanceNotFound, NapCatResourceNotOwned):
            raise
        except Exception as exc:
            raise NapCatManagerUnavailable(str(exc)) from exc
        root = self._state_root / instance_id
        return NapCatInstanceRecord(
            instance_id="default",
            provider="docker",
            generation=generation,
            resource_id="corlinman-napcat",
            state_root=str(root),
            token_file=str(root / "manager-secrets.env"),
            legacy_resource=True,
            metadata={"network": self._network_name},
        )

    async def ensure_adopted(self, record: NapCatInstanceRecord) -> None:
        self._assert_record(record)
        if not record.legacy_resource:
            raise NapCatResourceNotOwned("Docker adoption target is not legacy")
        try:
            container = await asyncio.to_thread(
                self._client().containers.get, record.resource_id
            )
        except Exception as exc:
            if _is_not_found(exc):
                raise NapCatInstanceNotFound(
                    "legacy Docker NapCat is absent"
                ) from exc
            raise NapCatManagerUnavailable(str(exc)) from exc
        webui_token = self._legacy_webui_token(container)
        token_file = Path(record.token_file)
        token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not token_file.exists():
            _write_imported_token_file(token_file, webui_token)
        imported, _ = _read_tokens(token_file, require_onebot=False)
        if imported != webui_token:
            raise NapCatResourceNotOwned("legacy credential cache mismatch")

    async def inspect(self, record: NapCatInstanceRecord) -> NapCatObservedState:
        if record.retained:
            state = "retained"
        else:
            try:
                container = await asyncio.to_thread(self._container, record)
                await asyncio.to_thread(container.reload)
                status = str(container.status)
            except NapCatResourceNotOwned:
                raise
            except Exception as exc:
                if _is_not_found(exc):
                    status = "absent"
                else:
                    return self._observed(record, "error", error_code="inspect_failed")
            state = {
                "running": "running",
                "created": "created",
                "exited": "stopped",
                "dead": "error",
                "restarting": "created",
                "paused": "stopped",
                "absent": "absent",
            }.get(status, "error")
        return self._observed(record, state)

    async def start(self, record: NapCatInstanceRecord) -> None:
        await self._container_action(record, "start")

    async def stop(self, record: NapCatInstanceRecord) -> None:
        await self._container_action(record, "stop")

    async def restart(self, record: NapCatInstanceRecord) -> None:
        await self._container_action(record, "restart")

    async def upgrade(self, record: NapCatInstanceRecord) -> None:
        container = await asyncio.to_thread(self._container, record)
        await asyncio.to_thread(container.restart)

    async def remove_runtime(self, record: NapCatInstanceRecord) -> None:
        try:
            container = await asyncio.to_thread(self._container, record)
        except Exception as exc:
            if _is_not_found(exc):
                return
            raise
        await asyncio.to_thread(container.remove, force=True)

    async def restore(self, record: NapCatInstanceRecord) -> None:
        if record.legacy_resource:
            raise NapCatResourceNotOwned("legacy resource cannot be restored after removal")
        await self.ensure_provisioned(record)

    async def purge_login_state(self, record: NapCatInstanceRecord) -> None:
        if not record.retained:
            raise NapCatResourceNotOwned("runtime must be retained before purge")
        if record.legacy_resource:
            raise NapCatResourceNotOwned(
                "legacy bind mounts require operator migration before purge"
            )
        client = self._client()
        for key in ("app_volume", "qq_volume"):
            name = record.metadata.get(key)
            if not name:
                raise NapCatResourceNotOwned("managed volume missing from inventory")
            try:
                volume = await asyncio.to_thread(client.volumes.get, name)
            except Exception as exc:
                if _is_not_found(exc):
                    continue
                raise
            self._assert_volume(
                volume, record.instance_id, record.generation
            )
            await asyncio.to_thread(volume.remove, force=False)
        shutil.rmtree(record.state_root, ignore_errors=True)

    async def descriptor(self, record: NapCatInstanceRecord) -> NapCatDescriptor:
        self._assert_record(record)
        webui_token, onebot_token = _read_tokens(
            Path(record.token_file), require_onebot=not record.legacy_resource
        )
        host = record.resource_id
        return NapCatDescriptor(
            instance_id=record.instance_id,
            generation=record.generation,
            ws_url=f"ws://{host}:3001",
            http_url=f"http://{host}:6099",
            access_token=onebot_token,
            napcat_access_token=webui_token,
            expected_uin=record.bound_uin,
        )

    async def _container_action(
        self, record: NapCatInstanceRecord, action: str
    ) -> None:
        container = await asyncio.to_thread(self._container, record)
        await asyncio.to_thread(getattr(container, action))

    def _container(self, record: NapCatInstanceRecord) -> Any:
        self._assert_record(record)
        container = self._client().containers.get(record.resource_id)
        if record.legacy_resource:
            self._assert_legacy_container(container)
        else:
            self._assert_container(container, record.instance_id, record.generation)
        return container

    def _legacy_webui_token(self, container: Any) -> str:
        self._assert_legacy_container(container)
        env = container.attrs.get("Config", {}).get("Env") or []
        values = dict(item.split("=", 1) for item in env if "=" in item)
        token = values.get("WEBUI_TOKEN") or values.get(
            "NAPCAT_WEBUI_SECRET_KEY"
        )
        if not token:
            raise NapCatResourceNotOwned(
                "legacy Docker credential cannot be imported"
            )
        return str(token)

    @staticmethod
    def _assert_legacy_container(container: Any) -> None:
        labels = dict(container.attrs.get("Config", {}).get("Labels") or {})
        if labels.get("com.docker.compose.service") != "napcat":
            raise NapCatResourceNotOwned("legacy container ownership changed")

    def _assert_record(self, record: NapCatInstanceRecord) -> None:
        if record.provider != "docker":
            raise NapCatResourceNotOwned("record does not belong to Docker provider")
        instance_id = str(parse_instance_id(record.instance_id))
        expected = "corlinman-napcat" if record.legacy_resource else f"corlinman-napcat-{instance_id}"
        if record.resource_id != expected:
            raise NapCatResourceNotOwned("Docker resource id is not manager-owned")
        root = Path(record.state_root).resolve(strict=False)
        try:
            root.relative_to(self._state_root)
        except ValueError as exc:
            raise NapCatResourceNotOwned("Docker manager state root mismatch") from exc

    def _assert_container(
        self, container: Any, instance_id: str, generation: int
    ) -> None:
        labels = dict(container.attrs.get("Config", {}).get("Labels") or {})
        expected = self._labels(instance_id, generation)
        if any(labels.get(key) != value for key, value in expected.items()):
            raise NapCatResourceNotOwned("Docker container labels do not match inventory")

    def _assert_volume(
        self, volume: Any, instance_id: str, generation: int
    ) -> None:
        attrs = getattr(volume, "attrs", {})
        labels = dict(attrs.get("Labels") or {})
        if (
            labels.get(_MANAGED_LABEL) != "true"
            or labels.get(_INSTALLATION_LABEL) != self._installation_id
            or labels.get(_INSTANCE_LABEL) != instance_id
            or labels.get(_GENERATION_LABEL) != str(generation)
        ):
            raise NapCatResourceNotOwned("Docker volume labels do not match inventory")

    def _labels(self, instance_id: str, generation: int) -> dict[str, str]:
        return {
            _MANAGED_LABEL: "true",
            _INSTALLATION_LABEL: self._installation_id,
            _INSTANCE_LABEL: instance_id,
            _GENERATION_LABEL: str(generation),
        }

    def _observed(
        self,
        record: NapCatInstanceRecord,
        state: str,
        *,
        error_code: str | None = None,
    ) -> NapCatObservedState:
        return NapCatObservedState(
            instance_id=record.instance_id,
            provider="docker",
            generation=record.generation,
            state=state,  # type: ignore[arg-type]
            resource_id=record.resource_id,
            retained=record.retained,
            bound_uin=record.bound_uin,
            error_code=error_code,
        )

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        try:
            import docker  # type: ignore[import-not-found]
        except ImportError as exc:
            raise NapCatManagerUnavailable("docker SDK not installed") from exc
        return docker.from_env(timeout=30)  # type: ignore[attr-defined]


def _write_imported_token_file(path: Path, webui_token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(
            fd,
            f"WEBUI_TOKEN={webui_token}\nONEBOT_TOKEN=\n".encode(),
        )
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(path.parent)


def _is_not_found(exc: Exception) -> bool:
    return exc.__class__.__name__ in {"NotFound", "ImageNotFound"} or getattr(
        exc, "status_code", None
    ) == 404


def installation_id(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
    if value:
        return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, value.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return value
