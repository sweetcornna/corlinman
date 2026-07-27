"""OpenAI Realtime backend — speech via a WebRTC session.

Realtime models have no one-shot REST speech endpoint. The official API
accepts an SDP offer and session configuration as multipart form data::

    POST {base_url}/v1/realtime/calls
    multipart: sdp=application/sdp, session=application/json
      -> 201 + <answer sdp>

Older Sub2API deployments expose JSON compatibility endpoints at
``POST /v1/live`` or ``POST /backend-api/codex/realtime/calls``. The
negotiator tries the public contract first, then those legacy paths only
when the previous route is absent.

Using a full-duplex conversational model for one-shot synthesis means we
open the session, push a single user item, ask for one audio-only
response, record the inbound track, and tear down — no microphone is ever
attached (the local transceiver is ``recvonly``).

Optional dependency
-------------------
``aiortc`` (plus ``av``) is imported lazily through
:func:`_import_webrtc`, mirroring how ``routes_voice/provider_openai.py``
defers ``websockets``. A deployment that never enables Realtime does not
need the wheel, and one that does gets a precise
``gpt_live_dependency_missing`` envelope instead of an ImportError at
boot.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import structlog

from corlinman_agent.voice.catalog import AudioFormat, BackendDef
from corlinman_agent.voice.errors import SynthesisError

logger = structlog.get_logger(__name__)

__all__ = ["LIVE_PATHS", "probe_live_endpoint", "synthesize_gpt_live"]


#: Official multipart path, followed by legacy JSON gateway spellings.
LIVE_PATHS: tuple[str, ...] = (
    "/v1/realtime/calls",
    "/v1/live",
    "/backend-api/codex/realtime/calls",
)

_OFFICIAL_LIVE_PATH: str = LIVE_PATHS[0]
_ROUTE_MISSING_STATUSES: frozenset[int] = frozenset({404, 405})

#: Data channel the realtime protocol multiplexes its JSON events over.
_EVENT_CHANNEL: str = "oai-events"

#: Fallback ceiling for one synthesis round-trip.
_DEFAULT_TIMEOUT: float = 90.0

#: Prefix that keeps a conversational model from answering the text
#: instead of reading it. Realtime is an assistant, not a TTS engine, so
#: the intent has to be stated explicitly.
_VERBATIM_DIRECTIVE: str = (
    "You are acting as a text-to-speech engine. Read the user's message "
    "aloud verbatim, in its original language. Do not answer it, do not "
    "add greetings, commentary, or any words that are not in the message."
)


def _import_webrtc() -> tuple[Any, Any, Any]:
    """Import aiortc lazily. Test seam + optional-dependency guard."""
    try:
        from aiortc import RTCPeerConnection, RTCSessionDescription
        from aiortc.contrib.media import MediaRecorder
    except Exception as exc:  # noqa: BLE001 — any import failure degrades
        raise SynthesisError(
            "gpt_live_dependency_missing",
            "OpenAI Realtime 需要 aiortc — 请安装 `corlinman-agent[voice]` "
            "或 `uv pip install aiortc`",
        ) from exc
    return RTCPeerConnection, RTCSessionDescription, MediaRecorder


def build_session(
    *,
    model: str,
    voice: str,
    instructions: str | None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the realtime ``session`` object sent with the offer."""
    directive = _VERBATIM_DIRECTIVE
    if instructions and instructions.strip():
        directive = f"{directive}\n\nDelivery: {instructions.strip()}"
    session: dict[str, Any] = {
        "type": "realtime",
        "model": model,
        "output_modalities": ["audio"],
        "audio": {
            "input": {"turn_detection": None},
            "output": {"voice": voice},
        },
        "instructions": directive,
    }
    if extra:
        for key, value in extra.items():
            if value is not None:
                session[str(key)] = value
    return session


def _answer_sdp(response: httpx.Response) -> str:
    """Accept either a raw SDP body or ``{"sdp": ...}``."""
    text = response.text.strip()
    if text.startswith("{"):
        try:
            payload = response.json()
        except ValueError:
            return text
        if isinstance(payload, Mapping):
            for key in ("sdp", "answer", "answer_sdp"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return text
    return text


def _classify_live_error(response: httpx.Response) -> SynthesisError:
    """Turn a gateway failure into a precise, actionable error."""
    body = response.text[:400]
    message = body
    with contextlib.suppress(ValueError):
        payload = response.json()
        if isinstance(payload, Mapping):
            err = payload.get("error")
            if isinstance(err, Mapping):
                message = str(err.get("message") or body)
            elif isinstance(err, str):
                message = err
    lowered = message.lower()
    if "attestation" in lowered:
        return SynthesisError(
            "live_attestation_unavailable",
            "网关拒绝 Live 会话：" + message,
            status_code=response.status_code,
        )
    if response.status_code in _ROUTE_MISSING_STATUSES:
        return SynthesisError(
            "live_endpoint_missing",
            "网关未提供 Realtime WebRTC 会话端点：" + message,
            status_code=response.status_code,
        )
    return SynthesisError(
        "live_http_status",
        f"Live 会话建立失败 ({response.status_code})：{message}",
        status_code=response.status_code,
    )


async def _negotiate(
    *,
    base_url: str,
    api_key: str | None,
    offer_sdp: str,
    session: Mapping[str, Any],
    timeout: float,
    transport: httpx.AsyncBaseTransport | None,
) -> str:
    """POST the offer, return the answer SDP.

    The official endpoint uses multipart form data. Legacy Sub2API paths
    use their historical JSON body. A missing route falls through to the
    next spelling; every other failure is reported as-is.
    """
    headers: dict[str, str] = {"Accept": "application/sdp, application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    root = (base_url or "").rstrip("/")

    client_kwargs: dict[str, Any] = {"timeout": timeout, "headers": headers}
    if transport is not None:
        client_kwargs["transport"] = transport

    last: SynthesisError | None = None
    async with httpx.AsyncClient(**client_kwargs) as client:
        for path in LIVE_PATHS:
            response = await _post_offer(
                client,
                url=f"{root}{path}",
                path=path,
                offer_sdp=offer_sdp,
                session=session,
            )
            if response.status_code < 400:
                answer = _answer_sdp(response)
                if not answer:
                    raise SynthesisError("live_bad_response", "网关返回了空的 answer SDP")
                return answer
            error = _classify_live_error(response)
            last = error
            if response.status_code not in _ROUTE_MISSING_STATUSES:
                raise error
    raise last or SynthesisError("live_endpoint_missing", "没有可用的 Live 端点")


async def _post_offer(
    client: httpx.AsyncClient,
    *,
    url: str,
    path: str,
    offer_sdp: str,
    session: Mapping[str, Any],
) -> httpx.Response:
    """Send one offer using the wire shape required by ``path``."""
    if path == _OFFICIAL_LIVE_PATH:
        files = {
            "sdp": (None, offer_sdp, "application/sdp"),
            "session": (
                None,
                json.dumps(dict(session), ensure_ascii=False),
                "application/json",
            ),
        }
        return await client.post(url, files=files)
    return await client.post(
        url,
        json={"sdp": offer_sdp, "session": dict(session)},
    )


async def probe_live_endpoint(
    *,
    base_url: str,
    api_key: str | None,
    timeout: float = 15.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SynthesisError | None:
    """Check whether the gateway can host a Live session at all.

    Returns the blocking :class:`SynthesisError`, or ``None`` when the
    endpoint looks usable. The probe intentionally sends placeholder SDP.
    The official endpoint answers with 400/422 after authentication because
    that SDP is invalid; this proves the route and credential are usable.
    Legacy Sub2API validates its platform attestation first, so its 503 still
    surfaces as the actionable blocker it is.

    Used by the admin preview to give an operator the actionable failure
    ("this gateway cannot attest") instead of a downstream symptom such
    as a missing local WebRTC dependency.
    """
    headers: dict[str, str] = {"Accept": "application/sdp, application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client_kwargs: dict[str, Any] = {"timeout": timeout, "headers": headers}
    if transport is not None:
        client_kwargs["transport"] = transport
    root = (base_url or "").rstrip("/")
    placeholder = "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n"

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            last: SynthesisError | None = None
            for path in LIVE_PATHS:
                response = await _post_offer(
                    client,
                    url=f"{root}{path}",
                    path=path,
                    offer_sdp=placeholder,
                    session={"type": "realtime"},
                )
                if response.status_code < 400:
                    return None
                if path == _OFFICIAL_LIVE_PATH and response.status_code in (400, 422):
                    return None
                last = _classify_live_error(response)
                if response.status_code not in _ROUTE_MISSING_STATUSES:
                    return last
            return last
    except httpx.HTTPError as exc:
        return SynthesisError("live_unreachable", f"无法连接 Live 端点：{exc}")


async def synthesize_gpt_live(
    backend: BackendDef,
    *,
    base_url: str,
    api_key: str | None,
    text: str,
    voice: str,
    fmt: AudioFormat,
    model: str,
    out_path: Path,
    instructions: str | None = None,
    session_extra: Mapping[str, Any] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Run one Realtime session and write the spoken audio to ``out_path``.

    Returns the number of bytes written. Raises :class:`SynthesisError` on
    every failure path so the caller can emit a uniform envelope.
    """
    rtc_peer_cls, rtc_desc_cls, recorder_cls = _import_webrtc()

    session = build_session(
        model=model, voice=voice, instructions=instructions, extra=session_extra
    )
    pc = rtc_peer_cls()
    # ``MediaRecorder`` picks its muxer from the file extension, which is
    # why the workspace path is created with the right suffix upstream.
    recorder = recorder_cls(str(out_path))
    finished: asyncio.Event = asyncio.Event()
    failure: dict[str, str] = {}
    # Strong refs for fire-and-forget starts kicked off from the track
    # callback — the event loop only holds weak references, so a task
    # without one can be garbage-collected mid-flight.
    pending: set[asyncio.Task[Any]] = set()

    @pc.on("track")
    def _on_track(track: Any) -> None:  # pragma: no cover - callback
        if track.kind != "audio":
            return
        recorder.addTrack(track)
        # ``MediaRecorder.start()`` only spawns pumps for tracks added
        # *before* the call, and skips tracks it already started — so
        # re-starting here is both necessary (if this track arrived after
        # the start below) and safe (if it arrived before).
        task = asyncio.ensure_future(recorder.start())
        pending.add(task)
        task.add_done_callback(pending.discard)

    channel = pc.createDataChannel(_EVENT_CHANNEL)

    def _send(event: Mapping[str, Any]) -> None:
        channel.send(json.dumps(event, ensure_ascii=False))

    @channel.on("open")
    def _on_open() -> None:  # pragma: no cover - callback
        _send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        _send({"type": "response.create", "response": {"output_modalities": ["audio"]}})

    @channel.on("message")
    def _on_message(raw: Any) -> None:  # pragma: no cover - callback
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(event, Mapping):
            return
        kind = str(event.get("type") or "")
        if kind == "error":
            err = event.get("error")
            detail = str(err.get("message")) if isinstance(err, Mapping) else str(err or "unknown")
            failure["message"] = detail
            finished.set()
        elif kind in ("response.done", "response.completed"):
            finished.set()

    try:
        pc.addTransceiver("audio", direction="recvonly")
        offer = await pc.createOffer()
        # aiortc's setLocalDescription blocks until ICE gathering is
        # complete, which is what the non-trickle HTTP handshake needs.
        await pc.setLocalDescription(offer)

        answer_sdp = await _negotiate(
            base_url=base_url,
            api_key=api_key,
            offer_sdp=pc.localDescription.sdp,
            session=session,
            timeout=timeout,
            transport=transport,
        )
        await pc.setRemoteDescription(rtc_desc_cls(sdp=answer_sdp, type="answer"))

        # Unconditional, matching aiortc's own examples. Gating this on
        # "did a track already arrive?" would almost always skip it: the
        # ``track`` event can fire after ``setRemoteDescription`` returns,
        # leaving the recorder never started and every synthesis empty.
        await recorder.start()

        try:
            await asyncio.wait_for(finished.wait(), timeout=timeout)
        except TimeoutError as exc:
            raise SynthesisError(
                "live_timeout",
                f"Realtime 会话在 {timeout:.0f}s 内没有返回完整音频",
            ) from exc

        if failure:
            raise SynthesisError("live_session_error", f"Realtime 会话报错：{failure['message']}")
    finally:
        with contextlib.suppress(Exception):
            await recorder.stop()
        with contextlib.suppress(Exception):
            await pc.close()

    if not out_path.exists():
        raise SynthesisError("live_empty", "Realtime 会话结束但没有产生音频文件")
    size = out_path.stat().st_size
    if size <= 0:
        raise SynthesisError("live_empty", "Realtime 会话返回了空音频")
    logger.info(
        "voice.gpt_live.synthesized",
        model=model,
        voice=voice,
        fmt=fmt.id,
        size_bytes=size,
    )
    return size
