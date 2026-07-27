from __future__ import annotations

import asyncio
import json
import os
import socket
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.system.napcat_manager.models import ManagerResponse
from corlinman_server.system.napcat_manager.server import (
    NapCatManagerServer,
    _exclusive_lock,
    _parse_request,
    run_manager,
)


class FakeManager:
    def __init__(self, root: Path | None = None) -> None:
        self.requests: list[Any] = []
        self.recoveries = 0
        inventory_path = (root or Path(".")) / "inventory.json"
        self.inventory = type("Inventory", (), {"path": inventory_path})()

    async def recover_pending(self) -> None:
        self.recoveries += 1

    async def execute(self, request: Any, *, actor: str) -> ManagerResponse:
        self.requests.append((request, actor))
        return ManagerResponse(ok=True, request_id=request.request_id)


def test_parse_request_rejects_arbitrary_fields_and_operations() -> None:
    with pytest.raises(ValueError, match="unknown"):
        _parse_request(
            {
                "request_id": "1",
                "operation": "restart",
                "instance_id": "default",
                "command": "rm -rf /",
            }
        )
    with pytest.raises(ValueError, match="unsupported"):
        _parse_request(
            {"request_id": "1", "operation": "shell", "instance_id": "default"}
        )


@pytest.mark.asyncio
async def test_invalid_request_response_does_not_reflect_payload_detail(
    tmp_path: Path,
) -> None:
    socket_path = Path("/tmp") / f"cm-napcat-bad-{tmp_path.name[-8:]}.sock"
    socket_path.unlink(missing_ok=True)
    server = NapCatManagerServer(FakeManager(), socket_path=socket_path)
    listener = await asyncio.start_unix_server(
        server._handle, path=str(socket_path), limit=16 * 1024 + 1
    )
    private = "http://user:password@localhost/private?token=secret"
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(
            json.dumps(
                {
                    "request_id": "abc",
                    "operation": "inspect",
                    "instance_id": "default",
                    private: private,
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        payload = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
    finally:
        listener.close()
        await listener.wait_closed()
        socket_path.unlink(missing_ok=True)

    assert payload["error_code"] == "invalid_request"
    assert payload["message"] == "manager request is invalid"
    assert private not in json.dumps(payload)


@pytest.mark.asyncio
async def test_unix_server_roundtrip(tmp_path: Path) -> None:
    socket_path = Path("/tmp") / f"cm-napcat-{tmp_path.name[-12:]}.sock"
    socket_path.unlink(missing_ok=True)
    manager = FakeManager()
    server = NapCatManagerServer(manager, socket_path=socket_path)
    listener = await asyncio.start_unix_server(
        server._handle, path=str(socket_path), limit=16 * 1024 + 1
    )
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(
            json.dumps(
                {
                    "request_id": "abc",
                    "operation": "inspect",
                    "instance_id": "default",
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        payload = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
    finally:
        listener.close()
        await listener.wait_closed()
        socket_path.unlink(missing_ok=True)

    assert payload["ok"] is True
    assert payload["request_id"] == "abc"
    assert manager.requests[0][0].instance_id == "default"


@pytest.mark.asyncio
async def test_server_recovers_before_accepting_requests(tmp_path: Path) -> None:
    socket_path = Path("/tmp") / f"cm-napcat-serve-{tmp_path.name[-8:]}.sock"
    socket_path.unlink(missing_ok=True)
    manager = FakeManager(tmp_path)
    server = NapCatManagerServer(manager, socket_path=socket_path)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if socket_path.exists():
                break
            await asyncio.sleep(0.01)
        assert socket_path.exists()
        assert manager.recoveries == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not socket_path.exists()


async def _accept_and_close(
    _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Stand-in for a live manager: prove the socket answers, then hang up.

    The handler MUST close its side. A stub that just returns leaves the
    accepted connection open, and since Python 3.12 ``Server.wait_closed()``
    genuinely waits for in-flight connections — so the teardown below would
    block forever. That is not hypothetical: it wedged the whole ``py-test``
    job on 3.12 (CI) while passing on 3.13 (dev machines).
    """
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_server_refuses_active_socket(tmp_path: Path) -> None:
    socket_path = Path("/tmp") / f"cm-napcat-live-{tmp_path.name[-8:]}.sock"
    socket_path.unlink(missing_ok=True)
    listener = await asyncio.start_unix_server(_accept_and_close, path=socket_path)
    try:
        server = NapCatManagerServer(FakeManager(tmp_path), socket_path=socket_path)
        with pytest.raises(RuntimeError, match="another NapCat manager"):
            await server.serve()
    finally:
        listener.close()
        await listener.wait_closed()
        socket_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_server_replaces_only_stale_socket(tmp_path: Path) -> None:
    socket_path = Path("/tmp") / f"cm-napcat-stale-{tmp_path.name[-8:]}.sock"
    socket_path.unlink(missing_ok=True)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()
    old_inode = socket_path.lstat().st_ino
    server = NapCatManagerServer(FakeManager(tmp_path), socket_path=socket_path)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if socket_path.exists() and socket_path.lstat().st_ino != old_inode:
                break
            await asyncio.sleep(0.01)
        assert socket_path.exists()
        assert socket_path.lstat().st_ino != old_inode
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_server_shutdown_does_not_unlink_replaced_socket(
    tmp_path: Path,
) -> None:
    socket_path = Path("/tmp") / f"cm-napcat-swap-{tmp_path.name[-8:]}.sock"
    socket_path.unlink(missing_ok=True)
    server = NapCatManagerServer(FakeManager(tmp_path), socket_path=socket_path)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if socket_path.exists():
                break
            await asyncio.sleep(0.01)
        socket_path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(str(socket_path))
        replacement_inode = socket_path.lstat().st_ino
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert socket_path.lstat().st_ino == replacement_inode
        replacement.close()
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        socket_path.unlink(missing_ok=True)


def test_server_rejects_concurrent_state_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "manager.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        server = NapCatManagerServer(
            FakeManager(tmp_path),
            socket_path=Path("/tmp") / f"cm-lock-{tmp_path.name[-8:]}.sock",
            state_lock_path=lock_path,
        )
        with pytest.raises(RuntimeError, match="another NapCat manager"):
            asyncio.run(server.serve())
    finally:
        os.close(fd)


def test_run_manager_locks_state_before_factory(tmp_path: Path) -> None:
    socket_path = Path("/tmp") / f"cm-factory-{tmp_path.name[-8:]}.sock"
    socket_path.unlink(missing_ok=True)
    data_dir = tmp_path / "data"

    def factory(received_data_dir: Path, mode: str) -> FakeManager:
        assert received_data_dir == data_dir
        assert mode == "native"
        lock_path = data_dir / ".napcat" / "managed" / "manager.lock"
        with pytest.raises(RuntimeError, match="another NapCat manager"):
            with _exclusive_lock(lock_path):
                pass
        raise RuntimeError("factory observed locked state")

    with pytest.raises(RuntimeError, match="factory observed locked state"):
        run_manager(
            data_dir=data_dir,
            mode="native",
            socket_path=socket_path,
            manager_factory=factory,  # type: ignore[arg-type]
        )
