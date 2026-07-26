from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.system.napcat_manager.client import NapCatManagerClient
from corlinman_server.system.napcat_manager.protocol import NapCatManagerUnavailable


@pytest.fixture
def socket_path() -> Iterator[Path]:
    path = Path(tempfile.gettempdir()) / f"clm-{uuid.uuid4().hex[:10]}.sock"
    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


async def _serve_once(
    path: Path,
    response: dict[str, Any] | bytes,
) -> tuple[asyncio.AbstractServer, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        requests.append(json.loads(await reader.readline()))
        wire = response if isinstance(response, bytes) else json.dumps(response).encode()
        writer.write(wire + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(path))
    return server, requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "start",
        "stop",
        "restart",
        "upgrade",
        "remove_runtime",
        "restore",
        "purge_login_state",
        "bind_uin",
    ],
)
async def test_state_changes_require_positive_exact_generation(
    tmp_path: Path,
    operation: str,
) -> None:
    client = NapCatManagerClient(tmp_path / "missing.sock")

    for generation in (None, 0, -1, True):
        with pytest.raises(ValueError, match="positive exact generation"):
            await client.request(operation, "bot-a", generation=generation)


@pytest.mark.asyncio
async def test_request_sends_generation_and_parses_private_descriptor(
    socket_path: Path,
) -> None:
    response = {
        "ok": True,
        "request_id": "server-request-id",
        "observed": {
            "instance_id": "bot-a",
            "provider": "native",
            "generation": 7,
            "state": "running",
            "resource_id": "corlinman-napcat@bot-a.service",
            "retained": False,
            "bound_uin": 12345,
            "error_code": None,
            "can_restore": True,
            "can_purge": True,
        },
        "descriptor": {
            "instance_id": "bot-a",
            "generation": 7,
            "ws_url": "ws://127.0.0.1:16000",
            "http_url": "http://127.0.0.1:16001",
            "access_token": "onebot-secret",
            "napcat_access_token": "webui-secret",
            "expected_uin": 12345,
        },
        "error_code": None,
        "message": None,
    }
    server, requests = await _serve_once(socket_path, response)
    try:
        result = await NapCatManagerClient(socket_path).request(
            "restart",
            "bot-a",
            generation=7,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert requests[0]["operation"] == "restart"
    assert requests[0]["instance_id"] == "bot-a"
    assert requests[0]["generation"] == 7
    assert result.ok is True
    assert result.observed is not None
    assert result.observed.bound_uin == 12345
    assert result.descriptor is not None
    assert result.descriptor.access_token == "onebot-secret"


@pytest.mark.asyncio
async def test_invalid_manager_response_fails_closed(socket_path: Path) -> None:
    server, _requests = await _serve_once(socket_path, b"not-json")
    try:
        with pytest.raises(NapCatManagerUnavailable, match="invalid manager response"):
            await NapCatManagerClient(socket_path).request("inspect", "default")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_missing_manager_socket_raises_unavailable(tmp_path: Path) -> None:
    client = NapCatManagerClient(tmp_path / "missing.sock", connect_timeout=0.01)

    with pytest.raises(NapCatManagerUnavailable):
        await client.request("inspect", "default")
