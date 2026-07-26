"""Unprivileged gateway client for the NapCat manager Unix socket."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from corlinman_server.gateway.qq_instances.models import parse_instance_id
from corlinman_server.system.napcat_manager.models import (
    ManagerRequest,
    ManagerResponse,
    NapCatDescriptor,
    NapCatObservedState,
)
from corlinman_server.system.napcat_manager.protocol import NapCatManagerUnavailable

_GENERATION_REQUIRED = frozenset(
    {
        "start",
        "stop",
        "restart",
        "upgrade",
        "remove_runtime",
        "restore",
        "purge_login_state",
        "bind_uin",
    }
)


class NapCatManagerClient:
    def __init__(
        self,
        socket_path: Path,
        *,
        connect_timeout: float = 5.0,
        response_timeout: float = 120.0,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout

    async def is_available(self) -> bool:
        try:
            response = await self.request("inspect", "default")
        except NapCatManagerUnavailable:
            return False
        return response.ok or response.error_code not in {
            "forbidden",
            "manager_unavailable",
        }

    async def request(
        self,
        operation: str,
        instance_id: str,
        *,
        generation: int | None = None,
        expected_uin: int | None = None,
    ) -> ManagerResponse:
        instance_id = str(parse_instance_id(instance_id))
        if operation in _GENERATION_REQUIRED and (
            generation is None or isinstance(generation, bool) or generation <= 0
        ):
            raise ValueError(f"{operation} requires a positive exact generation")
        request = ManagerRequest(
            request_id=uuid.uuid4().hex,
            operation=operation,  # type: ignore[arg-type]
            instance_id=instance_id,
            generation=generation,
            expected_uin=expected_uin,
        )
        payload = {
            "request_id": request.request_id,
            "operation": request.operation,
            "instance_id": request.instance_id,
            "generation": request.generation,
            "expected_uin": request.expected_uin,
        }
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)),
                timeout=self.connect_timeout,
            )
        except (TimeoutError, OSError) as exc:
            raise NapCatManagerUnavailable(str(exc)) from exc
        try:
            writer.write(json.dumps(payload).encode("utf-8") + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(
                reader.readline(), timeout=self.response_timeout
            )
        except (TimeoutError, OSError) as exc:
            raise NapCatManagerUnavailable(str(exc)) from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        if not line:
            raise NapCatManagerUnavailable("manager closed without a response")
        try:
            return _response_from_json(json.loads(line))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise NapCatManagerUnavailable("invalid manager response") from exc


def _response_from_json(payload: dict[str, Any]) -> ManagerResponse:
    observed_raw = payload.get("observed")
    descriptor_raw = payload.get("descriptor")
    observed = None
    if isinstance(observed_raw, dict):
        observed = NapCatObservedState(**observed_raw)
    descriptor = None
    if isinstance(descriptor_raw, dict):
        descriptor = NapCatDescriptor(**descriptor_raw)
    return ManagerResponse(
        ok=bool(payload.get("ok")),
        request_id=str(payload.get("request_id") or ""),
        observed=observed,
        descriptor=descriptor,
        error_code=_optional_str(payload.get("error_code")),
        message=_optional_str(payload.get("message")),
        warning_code=_optional_str(payload.get("warning_code")),
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
