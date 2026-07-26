"""Root-owned Unix-socket server for the narrow NapCat manager protocol."""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import json
import os
import socket
import struct
from collections.abc import Callable
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any, BinaryIO

import structlog

from corlinman_server.system.audit import SystemAuditLog
from corlinman_server.system.napcat_manager.docker_provider import (
    DockerNapCatProvider,
    installation_id,
)
from corlinman_server.system.napcat_manager.inventory import NapCatInventory
from corlinman_server.system.napcat_manager.manager import NapCatManager
from corlinman_server.system.napcat_manager.models import ManagerRequest, ManagerResponse
from corlinman_server.system.napcat_manager.native_provider import NativeNapCatProvider

logger = structlog.get_logger(__name__)
_MAX_REQUEST_BYTES = 16 * 1024


class NapCatManagerServer:
    def __init__(
        self,
        manager: NapCatManager,
        *,
        socket_path: Path,
        allowed_uid: int | None = None,
        socket_gid: int | None = None,
        state_lock_path: Path | None = None,
    ) -> None:
        self.manager = manager
        self.socket_path = Path(socket_path)
        self.allowed_uid = allowed_uid
        self.socket_gid = socket_gid
        self.state_lock_path = Path(
            state_lock_path or manager.inventory.path.parent / "manager.lock"
        )

    async def serve(self, *, state_lock_held: bool = False) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.state_lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        socket_lock_path = self.socket_path.with_name(f"{self.socket_path.name}.lock")
        with ExitStack() as locks:
            if not state_lock_held:
                locks.enter_context(_exclusive_lock(self.state_lock_path))
            locks.enter_context(_exclusive_lock(socket_lock_path))
            await self.manager.recover_pending()
            await _remove_stale_socket(self.socket_path)
            server = await asyncio.start_unix_server(
                self._handle,
                path=str(self.socket_path),
                limit=_MAX_REQUEST_BYTES + 1,
            )
            socket_inode = self.socket_path.lstat().st_ino
            try:
                os.chmod(self.socket_path, 0o660)
                if self.socket_gid is not None:
                    os.chown(self.socket_path, 0, self.socket_gid)
                async with server:
                    await server.serve_forever()
            finally:
                server.close()
                await server.wait_closed()
                _unlink_owned_socket(self.socket_path, socket_inode)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request_id = "unknown"
        try:
            if self.allowed_uid is not None:
                peer_uid = _peer_uid(writer)
                if peer_uid not in {0, self.allowed_uid}:
                    raise PermissionError("manager peer uid is not authorized")
            line = await reader.readline()
            if not line or len(line) > _MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                raise ValueError("invalid manager request framing")
            payload = json.loads(line)
            request = _parse_request(payload)
            request_id = request.request_id
            actor = f"uid:{_peer_uid(writer)}"
            response = await self.manager.execute(request, actor=actor)
        except PermissionError:
            response = ManagerResponse(
                ok=False,
                request_id=request_id,
                error_code="forbidden",
                message="manager request is not authorized",
            )
        except Exception:
            response = ManagerResponse(
                ok=False,
                request_id=request_id,
                error_code="invalid_request",
                message="manager request is invalid",
            )
        writer.write(json.dumps(response.to_json(), ensure_ascii=False).encode("utf-8") + b"\n")
        with suppress(Exception):
            await writer.drain()
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


def _parse_request(payload: Any) -> ManagerRequest:
    if not isinstance(payload, dict):
        raise ValueError("manager request must be an object")
    allowed = {"request_id", "operation", "instance_id", "generation", "expected_uin"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"unknown manager request fields: {sorted(unknown)}")
    operation = str(payload.get("operation") or "")
    operations = {
        "provision",
        "adopt",
        "inspect",
        "start",
        "stop",
        "restart",
        "upgrade",
        "remove_runtime",
        "restore",
        "purge_login_state",
        "bind_uin",
    }
    if operation not in operations:
        raise ValueError("unsupported manager operation")
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id or len(request_id) > 128:
        raise ValueError("invalid request_id")
    return ManagerRequest(
        request_id=request_id,
        operation=operation,  # type: ignore[arg-type]
        instance_id=str(payload.get("instance_id") or ""),
        generation=_optional_int(payload.get("generation")),
        expected_uin=_optional_int(payload.get("expected_uin")),
    )


def _peer_uid(writer: asyncio.StreamWriter) -> int:
    transport_socket = writer.get_extra_info("socket")
    if transport_socket is None or not hasattr(socket, "SO_PEERCRED"):
        return os.getuid()
    raw = transport_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    _pid, uid, _gid = struct.unpack("3i", raw)
    return int(uid)


def stat_is_socket(path: Path) -> bool:
    import stat

    return stat.S_ISSOCK(path.lstat().st_mode)


class _exclusive_lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: BinaryIO | None = None

    def __enter__(self) -> _exclusive_lock:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError("manager lock is unavailable") from exc
        handle = os.fdopen(fd, "r+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeError("another NapCat manager is active") from exc
            raise RuntimeError("manager lock is unavailable") from exc
        self.handle = handle
        return self

    def __exit__(self, *_args: object) -> None:
        assert self.handle is not None
        with suppress(OSError):
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


async def _remove_stale_socket(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if path.is_symlink() or not stat_is_socket(path):
        raise RuntimeError("refusing to replace non-socket manager path")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout=1.0
        )
    except FileNotFoundError:
        return
    except ConnectionRefusedError:
        path.unlink(missing_ok=True)
        return
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return
        if exc.errno == errno.ECONNREFUSED:
            path.unlink(missing_ok=True)
            return
        raise RuntimeError("cannot prove manager socket is stale") from exc
    except TimeoutError as exc:
        raise RuntimeError("cannot prove manager socket is stale") from exc
    else:
        del reader
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
        raise RuntimeError("another NapCat manager is active")


def _unlink_owned_socket(path: Path, inode: int) -> None:
    try:
        if path.is_symlink() or not stat_is_socket(path):
            return
        if path.lstat().st_ino == inode:
            path.unlink()
    except FileNotFoundError:
        pass


def _optional_int(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


def _optional_env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if not value:
        return None
    return int(value)


ManagerFactory = Callable[[Path, str], NapCatManager]


def build_manager(data_dir: Path, mode: str) -> NapCatManager:
    managed_root = data_dir / ".napcat" / "managed"
    inventory = NapCatInventory(
        managed_root / "inventory.json",
        state_root=managed_root / "instances",
        legacy_state_roots=(data_dir / ".napcat",),
    )
    providers: dict[str, Any] = {}
    if mode == "docker":
        providers["docker"] = DockerNapCatProvider(
            state_root=managed_root / "instances",
            installation_id=installation_id(managed_root / "installation-id"),
            image=os.environ.get(
                "CORLINMAN_NAPCAT_IMAGE", "mlikiowa/napcat-docker:v4.18.4"
            ),
            network_name=os.environ.get(
                "CORLINMAN_DOCKER_NETWORK", "compose_corlinman-net"
            ),
        )
    elif mode == "native":
        providers["native"] = NativeNapCatProvider(
            state_root=managed_root / "instances",
            legacy_state_root=data_dir / ".napcat",
            appimage=Path(os.environ.get("CORLINMAN_NAPCAT_APPIMAGE", "")),
            runtime_uid=_optional_env_int("CORLINMAN_NAPCAT_RUNTIME_UID"),
            runtime_gid=_optional_env_int("CORLINMAN_NAPCAT_RUNTIME_GID"),
        )
    else:
        raise ValueError("CORLINMAN_RUNTIME_MODE must be docker or native")
    return NapCatManager(
        inventory=inventory,
        providers=providers,
        audit_log=SystemAuditLog(data_dir / "system-audit.log"),
    )


def run_manager(
    *,
    data_dir: Path,
    mode: str,
    socket_path: Path,
    allowed_uid: int | None = None,
    socket_gid: int | None = None,
    manager_factory: ManagerFactory = build_manager,
) -> None:
    """Build and serve the manager under one lifetime state lock."""
    state_lock_path = data_dir / ".napcat" / "managed" / "manager.lock"
    state_lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _exclusive_lock(state_lock_path):
        manager = manager_factory(data_dir, mode)
        asyncio.run(
            NapCatManagerServer(
                manager,
                socket_path=socket_path,
                allowed_uid=allowed_uid,
                socket_gid=socket_gid,
                state_lock_path=state_lock_path,
            ).serve(state_lock_held=True)
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--mode", choices=("docker", "native"), required=True)
    parser.add_argument("--allowed-uid", type=int)
    parser.add_argument("--socket-gid", type=int)
    args = parser.parse_args()
    run_manager(
        data_dir=Path(args.data_dir),
        mode=args.mode,
        socket_path=Path(args.socket),
        allowed_uid=args.allowed_uid,
        socket_gid=args.socket_gid,
    )
