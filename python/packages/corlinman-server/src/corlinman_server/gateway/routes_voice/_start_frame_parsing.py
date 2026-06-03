"""Mandatory ``start`` control-frame reader + its helper exceptions for
the voice surface.

Extracted verbatim from
:mod:`corlinman_server.gateway.routes_voice.mod` as part of a
behaviour-preserving god-file split. This module MUST NOT import the
source ``mod`` module (no cycle): it only depends on the framing layer.
"""

from __future__ import annotations

import asyncio

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from corlinman_server.gateway.routes_voice.framing import (
    ClientControl,
    ControlParseError,
    parse_client_control,
)


class _StartTimeout(Exception):
    """Raised by :func:`_read_start_frame` when no frame arrives in
    :data:`DEFAULT_START_TIMEOUT_SECONDS`."""


class _StartMalformed(Exception):
    """Raised by :func:`_read_start_frame` when the first frame doesn't
    parse as a control envelope."""


class _StartDisconnect(Exception):
    """Raised by :func:`_read_start_frame` when the client hangs up
    before sending any frame."""


async def _read_start_frame(
    websocket: WebSocket, timeout_seconds: float
) -> tuple[ClientControl, ClientControl | None]:
    """Read the mandatory first control frame.

    Returns ``(start_frame, deferred)`` where ``deferred`` is a non-
    ``start`` control frame that was received first and must be replayed
    to the inbound pump. The Rust analogue allows a non-start first
    frame as a tolerated protocol violation; we forward it instead of
    closing the socket.
    """
    try:
        msg = await asyncio.wait_for(websocket.receive(), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise _StartTimeout("no start frame within timeout") from exc
    except WebSocketDisconnect as exc:
        raise _StartDisconnect("disconnected before start") from exc

    msg_type = msg.get("type")
    if msg_type == "websocket.disconnect":
        raise _StartDisconnect("disconnected before start")
    if msg_type != "websocket.receive":
        raise _StartMalformed(f"unexpected first message type: {msg_type}")

    text = msg.get("text")
    if text is None:
        # The Rust handler treats binary-before-start as a protocol
        # error too. We're stricter than the Rust path here for safety;
        # a future iter can fall back to "buffer + forward".
        raise _StartMalformed("first frame must be a `start` control text frame")

    try:
        control = parse_client_control(text)
    except ControlParseError as exc:
        raise _StartMalformed(str(exc)) from exc

    if control.type == ClientControl.START:
        return control, None
    # Non-start control frame: synthesise a default `start` so the
    # session can proceed, and forward the original frame to the inbound
    # pump as `deferred`.
    return (
        ClientControl(
            type=ClientControl.START,
            session_key=None,
            agent_id=None,
            sample_rate_hz=None,
            format=None,
        ),
        control,
    )
