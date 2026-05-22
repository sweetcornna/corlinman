"""Tests for the real OpenAI Realtime voice provider.

Drives :class:`OpenAIRealtimeProvider` /
:class:`OpenAIRealtimeSession` against a fake WebSocket double so the
test never touches the network. Covers:

* the connect URL + ``Authorization`` / ``OpenAI-Beta`` headers,
* the up-bridge — PCM-16 audio → base64 ``input_audio_buffer.append``,
* the down-bridge — realtime JSON events → :class:`VoiceEvent`,
* command translation (``interrupt`` → ``response.cancel``, ``close``),
* the degrade-gracefully paths — missing key + failed connect both
  surface a typed ``Error`` + ``End`` rather than crashing.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from corlinman_server.gateway.routes_voice.provider import (
    ProviderCommand,
    ProviderEndReason,
    VoiceEvent,
    VoiceSessionStartParams,
)
from corlinman_server.gateway.routes_voice.provider_openai import (
    DEFAULT_REALTIME_MODEL,
    OPENAI_REALTIME_URL,
    OpenAIRealtimeProvider,
    OpenAIRealtimeSession,
)

from .conftest import FakeRealtimeWebSocket, install_fake_connect

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_fake_connect(
    monkeypatch: pytest.MonkeyPatch, ws: FakeRealtimeWebSocket
) -> dict[str, Any]:
    return install_fake_connect(monkeypatch, ws)


def _params() -> VoiceSessionStartParams:
    return VoiceSessionStartParams(
        session_id="sess-1",
        provider_alias="openai",
        sample_rate_hz_in=16_000,
        sample_rate_hz_out=24_000,
        agent_id="agent-7",
    )


async def _drain(session: OpenAIRealtimeSession) -> list[VoiceEvent]:
    """Collect every event the session yields, with a safety timeout."""
    events: list[VoiceEvent] = []

    async def _collect() -> None:
        async for ev in session.events():
            events.append(ev)

    await asyncio.wait_for(_collect(), timeout=5.0)
    return events


# ---------------------------------------------------------------------------
# Connect URL + auth
# ---------------------------------------------------------------------------


async def test_connect_uses_realtime_url_and_auth_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    captured = _install_fake_connect(monkeypatch, ws)

    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    assert captured["url"] == f"{OPENAI_REALTIME_URL}?model={DEFAULT_REALTIME_MODEL}"
    headers = captured["kwargs"]["additional_headers"]
    assert headers["Authorization"] == "Bearer sk-test-key"
    assert headers["OpenAI-Beta"] == "realtime=v1"

    # The first outbound frame pins the PCM-16 audio format.
    session_update = ws.sent_json[0]
    assert session_update["type"] == "session.update"
    assert session_update["session"]["input_audio_format"] == "pcm16"
    assert session_update["session"]["output_audio_format"] == "pcm16"

    ws.end()
    await session.close()


async def test_connect_honours_explicit_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    captured = _install_fake_connect(monkeypatch, ws)

    provider = OpenAIRealtimeProvider(
        api_key="sk-test-key", model="gpt-4o-realtime-2025"
    )
    session = await provider.open(_params())

    assert captured["url"].endswith("?model=gpt-4o-realtime-2025")
    ws.end()
    await session.close()


async def test_ready_event_emitted_after_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())
    ws.end()

    events = await _drain(session)
    assert events[0].kind == VoiceEvent.READY
    assert events[0].provider_session_id == "openai-sess-1"
    await session.close()


# ---------------------------------------------------------------------------
# Up-bridge — audio in
# ---------------------------------------------------------------------------


async def test_push_audio_appends_base64_input_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    pcm = b"\x01\x00\x02\x00\x03\x00"
    await session.push_audio(pcm)

    append = ws.sent_json[-1]
    assert append["type"] == "input_audio_buffer.append"
    assert base64.b64decode(append["audio"]) == pcm

    ws.end()
    await session.close()


# ---------------------------------------------------------------------------
# Command translation
# ---------------------------------------------------------------------------


async def test_interrupt_command_maps_to_response_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    await session.push_command(ProviderCommand.interrupt())
    assert ws.sent_json[-1] == {"type": "response.cancel"}

    ws.end()
    await session.close()


async def test_close_command_emits_graceful_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    await session.push_command(ProviderCommand.close())
    events = await _drain(session)
    assert events[-1].kind == VoiceEvent.END
    assert events[-1].end_reason == ProviderEndReason.GRACEFUL


async def test_approve_tool_command_is_noop_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    before = len(ws.sent)
    await session.push_command(
        ProviderCommand.approve_tool("call-1", approve=True)
    )
    # Tool approval is gateway-side — nothing extra hits the realtime wire.
    assert len(ws.sent) == before

    ws.end()
    await session.close()


# ---------------------------------------------------------------------------
# Down-bridge — realtime events → VoiceEvent
# ---------------------------------------------------------------------------


async def test_audio_delta_translates_to_audio_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    pcm = b"\xaa\xbb\xcc\xdd"
    ws.feed(
        {
            "type": "response.audio.delta",
            "delta": base64.b64encode(pcm).decode("ascii"),
        }
    )
    ws.end()

    events = await _drain(session)
    audio = [e for e in events if e.kind == VoiceEvent.AUDIO_OUT]
    assert len(audio) == 1
    assert audio[0].pcm_le_bytes == pcm


async def test_input_transcript_completed_translates_to_user_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    ws.feed(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "hello there",
        }
    )
    ws.end()

    events = await _drain(session)
    finals = [e for e in events if e.kind == VoiceEvent.TRANSCRIPT_FINAL]
    assert len(finals) == 1
    assert finals[0].role == "user"
    assert finals[0].text == "hello there"


async def test_assistant_transcript_delta_and_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    ws.feed({"type": "response.audio_transcript.delta", "delta": "hi "})
    ws.feed({"type": "response.audio_transcript.delta", "delta": "world"})
    ws.feed(
        {"type": "response.audio_transcript.done", "transcript": "hi world"}
    )
    ws.end()

    events = await _drain(session)
    partials = [e for e in events if e.kind == VoiceEvent.TRANSCRIPT_PARTIAL]
    finals = [e for e in events if e.kind == VoiceEvent.TRANSCRIPT_FINAL]
    assert [p.text for p in partials] == ["hi ", "world"]
    assert all(p.role == "assistant" for p in partials)
    assert len(finals) == 1
    assert finals[0].role == "assistant"
    assert finals[0].text == "hi world"


async def test_function_call_done_translates_to_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    ws.feed(
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call-42",
            "name": "get_weather",
            "arguments": json.dumps({"city": "Paris"}),
        }
    )
    ws.end()

    events = await _drain(session)
    calls = [e for e in events if e.kind == VoiceEvent.TOOL_CALL]
    assert len(calls) == 1
    assert calls[0].call_id == "call-42"
    assert calls[0].tool == "get_weather"
    assert calls[0].args == {"city": "Paris"}


async def test_error_frame_translates_to_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    ws.feed(
        {
            "type": "error",
            "error": {"code": "rate_limit", "message": "slow down"},
        }
    )
    ws.end()

    events = await _drain(session)
    errors = [e for e in events if e.kind == VoiceEvent.ERROR]
    assert len(errors) == 1
    assert errors[0].code == "rate_limit"
    assert errors[0].message == "slow down"


async def test_bookkeeping_frames_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    ws.feed({"type": "session.created", "session": {}})
    ws.feed({"type": "rate_limits.updated", "rate_limits": []})
    ws.feed({"type": "response.done", "response": {}})
    ws.end()

    events = await _drain(session)
    # Only the synthesised Ready + the clean End survive.
    kinds = [e.kind for e in events]
    assert VoiceEvent.READY in kinds
    assert VoiceEvent.END in kinds
    assert VoiceEvent.AUDIO_OUT not in kinds


async def test_clean_socket_close_yields_graceful_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())
    ws.end()

    events = await _drain(session)
    assert events[-1].kind == VoiceEvent.END
    assert events[-1].end_reason == ProviderEndReason.GRACEFUL


# ---------------------------------------------------------------------------
# Degrade gracefully — missing key + failed connect
# ---------------------------------------------------------------------------


async def test_missing_key_degrades_to_typed_error() -> None:
    provider = OpenAIRealtimeProvider(api_key=None)
    assert provider.has_key is False

    session = await provider.open(_params())
    events = await _drain(session)

    assert events[0].kind == VoiceEvent.ERROR
    assert events[0].code == "provider_unconfigured"
    assert events[-1].kind == VoiceEvent.END
    assert events[-1].end_reason == ProviderEndReason.START_FAILED


async def test_failed_connect_degrades_to_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(url: str, **kwargs: Any) -> Any:
        raise ConnectionError("dns lookup failed")

    monkeypatch.setattr(
        "corlinman_server.gateway.routes_voice.provider_openai._import_ws_connect",
        lambda: boom,
    )

    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())
    events = await _drain(session)

    assert events[0].kind == VoiceEvent.ERROR
    assert events[0].code == "provider_connect_failed"
    assert "dns lookup failed" in events[0].message
    assert events[-1].kind == VoiceEvent.END
    assert events[-1].end_reason == ProviderEndReason.START_FAILED


async def test_read_loop_error_surfaces_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-stream socket failure surfaces as ``Error`` + a
    ``provider_error`` ``End`` — never an unhandled crash."""

    class ExplodingWebSocket(FakeRealtimeWebSocket):
        async def __anext__(self) -> str:
            raise RuntimeError("socket reset by peer")

    ws = ExplodingWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    events = await _drain(session)
    errors = [e for e in events if e.kind == VoiceEvent.ERROR]
    assert errors and errors[0].code == "provider_error"
    assert events[-1].kind == VoiceEvent.END
    assert events[-1].end_reason == ProviderEndReason.PROVIDER_ERROR


async def test_push_after_close_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    await session.close()
    # Pushing audio / commands on a closed session must not raise.
    await session.push_audio(b"\x00\x00")
    await session.push_command(ProviderCommand.interrupt())


async def test_close_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeRealtimeWebSocket()
    _install_fake_connect(monkeypatch, ws)
    provider = OpenAIRealtimeProvider(api_key="sk-test-key")
    session = await provider.open(_params())

    await session.close()
    await session.close()  # second close must be a clean no-op
    assert ws.closed is True
