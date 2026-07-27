"""End-to-end proof of the OpenAI Realtime WebRTC path, minus the vendor.

Everything except the provider itself is exercised here: a real
:mod:`aiortc` peer plays the role of the Realtime endpoint, answers our
SDP offer, opens the ``oai-events`` data channel, and streams actual
audio back. The client under test is the shipped
:func:`synthesize_gpt_live` — same offer, same session JSON, same
recorder wiring — so a pass means the only remaining variable in
production is whether the gateway will hold up its end.

This is the test that would have caught the recorder-start ordering bug:
gating ``recorder.start()`` on "did a track arrive yet?" leaves the
output file empty, which the assertions below reject.

Skipped when the optional ``voice`` extra (``aiortc``) or ffmpeg is
absent, so it never blocks a lean install.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Any

import httpx
import pytest

aiortc = pytest.importorskip("aiortc", reason="needs the `voice` extra (aiortc)")

from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: E402
from aiortc.contrib.media import MediaPlayer  # noqa: E402
from corlinman_agent.voice.catalog import AUDIO_FORMATS, get_backend  # noqa: E402
from corlinman_agent.voice.gpt_live import synthesize_gpt_live  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="needs ffmpeg to synthesise a source clip"
)

#: How long the fake Live endpoint speaks before declaring the turn done.
_SPEAK_SECONDS = 2


@pytest.fixture()
def source_wav(tmp_path: Path) -> Path:
    """A real 2s tone the fake endpoint will 'speak'."""
    out = tmp_path / "source.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={_SPEAK_SECONDS}",
            "-ar",
            "48000",
            "-ac",
            "1",
            str(out),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return out


class _FakeLiveEndpoint:
    """An aiortc peer that behaves like ``POST /v1/realtime/calls``.

    Answers the offer, streams ``source_wav`` on an audio track, and
    drives the realtime event protocol over ``oai-events``: it waits for
    ``response.create``, lets audio flow, then emits ``response.done``.
    """

    def __init__(self, source: Path) -> None:
        self.source = source
        self.pc: RTCPeerConnection | None = None
        self.seen_events: list[dict[str, Any]] = []
        self.session: dict[str, Any] | None = None
        self._player: MediaPlayer | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        content_type = request.headers["content-type"]
        envelope = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + request.content
        )
        message = BytesParser(policy=default).parsebytes(envelope)
        parts = {
            str(part.get_param("name", header="content-disposition")): part.get_payload(decode=True)
            for part in message.iter_parts()
        }
        self.session = json.loads(parts["session"])
        offer_sdp = parts["sdp"].decode()

        pc = RTCPeerConnection()
        self.pc = pc

        # Stream the tone back on the audio transceiver the client opened.
        self._player = MediaPlayer(str(self.source))
        pc.addTrack(self._player.audio)

        @pc.on("datachannel")
        def _on_channel(channel: Any) -> None:
            @channel.on("message")
            def _on_message(raw: Any) -> None:
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    return
                self.seen_events.append(event)
                if event.get("type") == "response.create":
                    task = asyncio.ensure_future(_finish(channel))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)

        async def _finish(channel: Any) -> None:
            # Let real audio flow before ending the turn, the way a
            # provider would.
            await asyncio.sleep(_SPEAK_SECONDS * 0.6)
            channel.send(json.dumps({"type": "response.done"}))

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        return httpx.Response(201, text=pc.localDescription.sdp)

    async def aclose(self) -> None:
        if self._player is not None and self._player.audio is not None:
            self._player.audio.stop()
        if self.pc is not None:
            await self.pc.close()


async def test_gpt_live_records_real_audio_end_to_end(source_wav: Path, tmp_path: Path) -> None:
    """The shipped client negotiates, records, and writes playable audio."""
    endpoint = _FakeLiveEndpoint(source_wav)
    out_path = tmp_path / "spoken.wav"
    try:
        size = await synthesize_gpt_live(
            get_backend("gpt_live"),
            base_url="https://gateway.test",
            api_key="sk-test",
            text="你好，这是一次端到端验证。",
            voice="marin",
            fmt=AUDIO_FORMATS["wav"],
            model="gpt-realtime-2.1",
            out_path=out_path,
            timeout=30,
            transport=httpx.MockTransport(endpoint.handle),
        )
    finally:
        await endpoint.aclose()

    # A file that exists but holds only a container header would mean the
    # recorder never consumed the track — exactly the ordering bug.
    assert out_path.is_file()
    assert size > 4096, f"recorded only {size} bytes — recorder likely never started"

    probe = subprocess.run(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(out_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(probe.stdout.strip())
    assert duration > 0.3, f"recorded {duration}s of audio"


async def test_client_sends_the_documented_realtime_protocol(
    source_wav: Path, tmp_path: Path
) -> None:
    """The session block and event sequence match the wire contract."""
    endpoint = _FakeLiveEndpoint(source_wav)
    try:
        await synthesize_gpt_live(
            get_backend("gpt_live"),
            base_url="https://gateway.test",
            api_key="sk-test",
            text="read this aloud",
            voice="cedar",
            fmt=AUDIO_FORMATS["wav"],
            model="gpt-realtime-2.1-mini",
            out_path=tmp_path / "out.wav",
            instructions="speak slowly",
            timeout=30,
            transport=httpx.MockTransport(endpoint.handle),
        )
    finally:
        await endpoint.aclose()

    session = endpoint.session
    assert session is not None
    assert session["model"] == "gpt-realtime-2.1-mini"
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["output"]["voice"] == "cedar"
    # No microphone is attached, so server VAD must be off or the model
    # would wait for speech that never comes.
    assert session["audio"]["input"]["turn_detection"] is None
    assert "speak slowly" in session["instructions"]

    kinds = [e.get("type") for e in endpoint.seen_events]
    assert kinds == ["conversation.item.create", "response.create"]
    item = endpoint.seen_events[0]["item"]
    assert item["role"] == "user"
    assert item["content"][0]["text"] == "read this aloud"
