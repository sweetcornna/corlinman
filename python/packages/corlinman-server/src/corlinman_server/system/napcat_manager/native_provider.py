"""Native NapCat provider using fixed templated systemd units."""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from corlinman_server.gateway.qq_instances.models import parse_instance_id
from corlinman_server.system.napcat_manager.models import (
    NapCatDescriptor,
    NapCatInstanceRecord,
    NapCatObservedState,
)
from corlinman_server.system.napcat_manager.protocol import (
    NapCatInstanceNotFound,
    NapCatManagerUnavailable,
    NapCatResourceNotOwned,
    NapCatUnsupportedOperation,
)

Runner = Callable[[list[str]], Awaitable[tuple[int, str]]]


class NativeNapCatProvider:
    kind = "native"

    def __init__(
        self,
        *,
        state_root: Path,
        appimage: Path,
        unit_template: Path = Path("/etc/systemd/system/corlinman-napcat@.service"),
        runner: Runner | None = None,
        legacy_state_root: Path | None = None,
        legacy_unit: Path = Path("/etc/systemd/system/corlinman-napcat.service"),
        runtime_uid: int | None = None,
        runtime_gid: int | None = None,
    ) -> None:
        self._state_root = Path(state_root).resolve(strict=False)
        self._appimage = Path(appimage)
        self._unit_template = Path(unit_template)
        self._runner = runner or _run_command
        self._legacy_state_root = Path(
            legacy_state_root or self._state_root.parent
        ).resolve(strict=False)
        self._legacy_unit = Path(legacy_unit)
        self._runtime_uid = runtime_uid
        self._runtime_gid = runtime_gid

    async def is_available(self) -> bool:
        return self._unit_template.exists() and self._appimage.exists()

    async def plan_provision(
        self,
        instance_id: str,
        generation: int,
        *,
        webui_port: int | None = None,
        onebot_port: int | None = None,
    ) -> NapCatInstanceRecord:
        instance_id = str(parse_instance_id(instance_id))
        if not await self.is_available():
            raise NapCatManagerUnavailable("native NapCat template or AppImage missing")
        if webui_port is None or onebot_port is None:
            raise NapCatManagerUnavailable("native instance ports are required")
        root = self._instance_root(instance_id)
        return NapCatInstanceRecord(
            instance_id=instance_id,
            provider="native",
            generation=generation,
            resource_id=f"corlinman-napcat@{instance_id}.service",
            state_root=str(root),
            token_file=str(root / "manager-secrets.env"),
            webui_port=webui_port,
            onebot_port=onebot_port,
        )

    async def ensure_provisioned(self, record: NapCatInstanceRecord) -> None:
        self._assert_record(record)
        if record.webui_port is None or record.onebot_port is None:
            raise NapCatManagerUnavailable("native instance ports are not assigned")
        root = Path(record.state_root)
        root.mkdir(parents=True, exist_ok=True, mode=0o710)
        os.chmod(root, 0o710)
        runtime_root = root / "runtime"
        runtime_root.mkdir(exist_ok=True, mode=0o700)
        (runtime_root / "app").mkdir(exist_ok=True, mode=0o700)
        (runtime_root / "ntqq").mkdir(exist_ok=True, mode=0o700)
        self._grant_runtime_ownership(runtime_root)
        token_file = Path(record.token_file)
        if not token_file.exists():
            _write_token_file(token_file)
        _write_native_env(
            root / "runtime.env", record.webui_port, record.onebot_port
        )
        await self.start(record)

    async def plan_adoption(
        self, instance_id: str, generation: int
    ) -> NapCatInstanceRecord:
        instance_id = str(parse_instance_id(instance_id))
        if instance_id != "default" or not self._legacy_unit.exists():
            raise NapCatInstanceNotFound("no adoptable legacy native NapCat")
        token_file = self._legacy_state_root / "legacy-secrets.env"
        if not token_file.is_file():
            raise NapCatResourceNotOwned("legacy native credentials are unavailable")
        _read_tokens(token_file, require_onebot=False)
        return NapCatInstanceRecord(
            instance_id=instance_id,
            provider="native",
            generation=generation,
            resource_id="corlinman-napcat.service",
            state_root=str(self._legacy_state_root),
            token_file=str(token_file),
            webui_port=6099,
            onebot_port=3001,
            legacy_resource=True,
        )

    async def ensure_adopted(self, record: NapCatInstanceRecord) -> None:
        self._assert_record(record)
        if not record.legacy_resource:
            raise NapCatResourceNotOwned("native adoption target is not legacy")
        if not self._legacy_unit.exists():
            raise NapCatInstanceNotFound("legacy native NapCat is absent")
        _read_tokens(Path(record.token_file), require_onebot=False)

    async def inspect(self, record: NapCatInstanceRecord) -> NapCatObservedState:
        self._assert_record(record)
        code, output = await self._runner(
            ["systemctl", "is-active", record.resource_id]
        )
        if record.retained:
            state = "retained"
        elif code == 0 and output.strip() == "active":
            state = "running"
        elif output.strip() in {"inactive", "failed", "activating", "deactivating"}:
            state = "stopped" if output.strip() == "inactive" else "error"
        else:
            state = "absent"
        return NapCatObservedState(
            instance_id=record.instance_id,
            provider="native",
            generation=record.generation,
            state=state,  # type: ignore[arg-type]
            resource_id=record.resource_id,
            retained=record.retained,
            bound_uin=record.bound_uin,
        )

    async def start(self, record: NapCatInstanceRecord) -> None:
        await self._systemctl(record, "start")

    async def stop(self, record: NapCatInstanceRecord) -> None:
        await self._systemctl(record, "stop")

    async def restart(self, record: NapCatInstanceRecord) -> None:
        await self._systemctl(record, "restart")

    async def upgrade(self, record: NapCatInstanceRecord) -> None:
        self._assert_record(record)
        if record.legacy_resource:
            raise NapCatUnsupportedOperation(
                "legacy default must be migrated before per-instance upgrade"
            )
        await self.restart(record)

    async def remove_runtime(self, record: NapCatInstanceRecord) -> None:
        await self.stop(record)

    async def restore(self, record: NapCatInstanceRecord) -> None:
        await self.start(record)

    async def purge_login_state(self, record: NapCatInstanceRecord) -> None:
        self._assert_record(record)
        if not record.retained:
            raise NapCatResourceNotOwned("runtime must be retained before purge")
        root = Path(record.state_root)
        if root == self._legacy_state_root:
            for name in ("app", "ntqq", "manager-secrets.env"):
                target = root / name
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
        else:
            shutil.rmtree(root, ignore_errors=True)

    async def descriptor(self, record: NapCatInstanceRecord) -> NapCatDescriptor:
        self._assert_record(record)
        webui_token, onebot_token = _read_tokens(
            Path(record.token_file), require_onebot=not record.legacy_resource
        )
        webui_port: int | None
        onebot_port: int | None
        if record.legacy_resource:
            webui_port = record.webui_port or 6099
            onebot_port = record.onebot_port or 3001
        else:
            webui_port = record.webui_port
            onebot_port = record.onebot_port
        if webui_port is None or onebot_port is None:
            raise NapCatManagerUnavailable("native instance ports are not assigned")
        return NapCatDescriptor(
            instance_id=record.instance_id,
            generation=record.generation,
            ws_url=f"ws://127.0.0.1:{onebot_port}",
            http_url=f"http://127.0.0.1:{webui_port}",
            access_token=onebot_token,
            napcat_access_token=webui_token,
            expected_uin=record.bound_uin,
        )

    async def _systemctl(self, record: NapCatInstanceRecord, action: str) -> None:
        self._assert_record(record)
        if action not in {"start", "stop", "restart"}:
            raise NapCatUnsupportedOperation(action)
        code, output = await self._runner(
            ["systemctl", action, record.resource_id]
        )
        if code:
            raise NapCatManagerUnavailable(output.strip() or f"systemctl {action} failed")

    def _instance_root(self, instance_id: str) -> Path:
        return self._state_root / str(parse_instance_id(instance_id))

    def _grant_runtime_ownership(self, runtime_root: Path) -> None:
        if self._runtime_uid is None or self._runtime_gid is None:
            return
        for path in (runtime_root, runtime_root / "app", runtime_root / "ntqq"):
            os.chown(path, self._runtime_uid, self._runtime_gid)
            os.chmod(path, 0o700)

    def _assert_record(self, record: NapCatInstanceRecord) -> None:
        if record.provider != "native":
            raise NapCatResourceNotOwned("record does not belong to native provider")
        parse_instance_id(record.instance_id)
        expected = (
            "corlinman-napcat.service"
            if record.legacy_resource and record.instance_id == "default"
            else f"corlinman-napcat@{record.instance_id}.service"
        )
        if record.resource_id != expected:
            raise NapCatResourceNotOwned("native systemd unit is not manager-owned")
        root = Path(record.state_root).resolve(strict=False)
        if record.legacy_resource:
            if root != self._legacy_state_root:
                raise NapCatResourceNotOwned("legacy state root mismatch")
        else:
            try:
                root.relative_to(self._state_root)
            except ValueError as exc:
                raise NapCatResourceNotOwned("native state root mismatch") from exc


async def _run_command(argv: list[str]) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    return process.returncode or 0, stdout.decode("utf-8", errors="replace")


def _write_token_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        data = (
            f"WEBUI_TOKEN={secrets.token_urlsafe(32)}\n"
            f"ONEBOT_TOKEN={secrets.token_urlsafe(32)}\n"
        )
        os.write(fd, data.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(path.parent)


def _write_native_env(path: Path, webui_port: int, onebot_port: int) -> None:
    data = (
        f"NAPCAT_WEBUI_PREFERRED_PORT={webui_port}\n"
        f"CORLINMAN_ONEBOT_PORT={onebot_port}\n"
    )
    _atomic_write(path, data.encode("ascii"))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_tokens(
    path: Path, *, require_onebot: bool = True
) -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key in {"WEBUI_TOKEN", "NAPCAT_WEBUI_TOKEN"}:
            values.setdefault("WEBUI_TOKEN", value)
        elif key == "ONEBOT_TOKEN":
            values[key] = value
    if not values.get("WEBUI_TOKEN") or (
        require_onebot and not values.get("ONEBOT_TOKEN")
    ):
        raise NapCatManagerUnavailable("NapCat token file is incomplete")
    return values["WEBUI_TOKEN"], values.get("ONEBOT_TOKEN", "")
