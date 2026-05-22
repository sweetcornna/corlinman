"""Integration test: ``run_voice_session`` drives the real OpenAI
Realtime provider end-to-end.

Wires :func:`run_voice_session` against an in-memory :class:`WebSocket`
double and an :class:`OpenAIRealtimeProvider` whose realtime socket is
itself faked. Asserts the route bridges audio + transcript both ways and
that a degraded provider surfaces a clean ``provider_error`` close —
never a crash.

The mock-provider voice tests are exercised separately
(:mod:`test_provider_openai` / :mod:`test_provider_selection`); this file
covers the *route ↔ real provider* seam specifically.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from corlinman_server.gateway.routes_voice.framing import SUBPROTOCOL
from corlinman_server.gateway.routes_voice.mod import (
    CLOSE_CODE_NORMAL,
    CLOSE_CODE_PROVIDER_ERROR,
    VoiceRouterConfig,
    VoiceState,
    run_voice_session,
)
from corlinman_server.gateway.routes_voice.provider_openai import (
    OpenAIRealtimeProvider,
)

from .conftest import FakeRealtimeWebSocket, install_fake_connect

pytestmark = pytest.mark.asyncio

_install_fake_connect = install_fake_connect


# ---------------------------------------------------------------------------
# In-memory WebSocket double
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Minimal Starlette ``WebSocket`` stand-in for :func:`run_voice_session`.

    The client → server direction is fed via :meth:`queue_text` /
    :meth:`queue_bytes`; the server → client direction is captured in
    :attr:`sent_text` / :attr:`sent_bytes`. :meth:`close` records the
    close code + reason.
    """

    def __init__(self, *, subprotocol: str | None = SUBPROTOCOL) -> None:
        self.headers = {}
        if subprotocol is not None:
            self.headers["sec-websocket-protocol"] = subprotocol
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.accepted_subprotocol: str | None = None
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.client_state = _ConnState.CONNECTED

    # ----- client → server feed ----

    def queue_text(self, text: str) -> None:
        self._incoming.put_nowait({"type": "websocket.receive", "text": text})

    def queue_bytes(self, data: bytes) -> None:
        self._incoming.put_nowait({"type": "websocket.receive", "bytes": data})

    def queue_disconnect(self) -> None:
        self._incoming.put_nowait({"type": "websocket.disconnect"})

    # ----- Starlette surface ----

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted_subprotocol = subprotocol

    async def receive(self) -> dict[str, Any]:
        return await self._incoming.get()

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason
        self.client_state = _ConnState.DISCONNECTED

    # ----- helpers ----

    @property
    def sent_control(self) -> list[dict[str, Any]]:
        return [json.loads(t) for t in self.sent_text]


class _ConnState:
    """Tiny stand-in for ``starlette.websockets.WebSocketState`` —
    :func:`run_voice_session` only compares against ``DISCONNECTED``."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


def _voice_state(provider: Any) -> VoiceState:
    cfg = VoiceRouterConfig(
        enabled=True,
        provider_alias="openai",
        budget_minutes_per_tenant_per_day=60,
    )
    return VoiceState(config_loader=lambda: cfg, provider=provider)


# ---------------------------------------------------------------------------
# Happy path — real provider, audio + transcript bridged both ways
# ---------------------------------------------------------------------------


async def test_route_bridges_real_provider_audio_and_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    realtime_ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, realtime_ws)
    provider = OpenAIRealtimeProvider(api_key="sk-route-test")

    ws = FakeWebSocket()
    # start frame → opens the provider session
    ws.queue_text(json.dumps({"type": "start", "session_key": "sk-1"}))
    # client audio frame → bridged up to the realtime socket
    client_pcm = b"\x10\x00\x20\x00"
    ws.queue_bytes(client_pcm)

    tts_pcm = b"\x99\x88\x77\x66"

    async def drive_provider_replies() -> None:
        # Let the route open + push the audio frame, then feed downstream
        # events from OpenAI, then end the realtime stream.
        await asyncio.sleep(0.05)
        realtime_ws.feed(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "hello agent",
            }
        )
        realtime_ws.feed(
            {
                "type": "response.audio.delta",
                "delta": base64.b64encode(tts_pcm).decode("ascii"),
            }
        )
        await asyncio.sleep(0.05)
        realtime_ws.end()

    await asyncio.wait_for(
        asyncio.gather(
            run_voice_session(ws, _voice_state(provider)),
            drive_provider_replies(),
        ),
        timeout=5.0,
    )

    # The client audio frame reached the realtime socket as a base64
    # input_audio_buffer.append.
    appends = [
        f for f in realtime_ws.sent_json
        if f["type"] == "input_audio_buffer.append"
    ]
    assert appends, "client audio never bridged to the realtime socket"
    assert base64.b64decode(appends[0]["audio"]) == client_pcm

    # The route forwarded the OpenAI audio delta back to the client as a
    # binary frame.
    assert tts_pcm in ws.sent_bytes

    # The user transcript surfaced as a transcript_final control frame.
    finals = [
        c for c in ws.sent_control if c.get("type") == "transcript_final"
    ]
    assert any(c.get("text") == "hello agent" for c in finals)

    # A `started` handshake frame went out and the socket closed cleanly.
    assert any(c.get("type") == "started" for c in ws.sent_control)
    assert ws.close_code == CLOSE_CODE_NORMAL


# ---------------------------------------------------------------------------
# Degrade gracefully — no key → typed provider_error close
# ---------------------------------------------------------------------------


async def test_route_degrades_when_provider_has_no_key() -> None:
    """A keyless real provider must not crash the route — it surfaces a
    typed ``error`` control frame + a ``provider_error`` close."""
    provider = OpenAIRealtimeProvider(api_key=None)

    ws = FakeWebSocket()
    ws.queue_text(json.dumps({"type": "start", "session_key": "sk-1"}))

    await asyncio.wait_for(
        run_voice_session(ws, _voice_state(provider)), timeout=5.0
    )

    errors = [c for c in ws.sent_control if c.get("type") == "error"]
    assert errors, "keyless provider should surface a typed error frame"
    assert errors[0]["code"] == "provider_unconfigured"
    assert ws.close_code == CLOSE_CODE_PROVIDER_ERROR


async def test_route_degrades_when_realtime_connect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(url: str, **kwargs: Any) -> Any:
        raise ConnectionError("upstream unreachable")

    monkeypatch.setattr(
        "corlinman_server.gateway.routes_voice.provider_openai._import_ws_connect",
        lambda: boom,
    )
    provider = OpenAIRealtimeProvider(api_key="sk-bad-route")

    ws = FakeWebSocket()
    ws.queue_text(json.dumps({"type": "start", "session_key": "sk-1"}))

    await asyncio.wait_for(
        run_voice_session(ws, _voice_state(provider)), timeout=5.0
    )

    errors = [c for c in ws.sent_control if c.get("type") == "error"]
    assert errors and errors[0]["code"] == "provider_connect_failed"
    assert ws.close_code == CLOSE_CODE_PROVIDER_ERROR
