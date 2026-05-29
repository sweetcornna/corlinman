"""Real OpenAI Realtime API voice provider.

Companion to :mod:`.provider`, which defines the
:class:`~corlinman_server.gateway.routes_voice.provider.VoiceProvider` /
:class:`~corlinman_server.gateway.routes_voice.provider.VoiceProviderSession`
Protocols plus the mock used by tests. This module supplies the *real*
adapter — the one wired when an OpenAI API key is configured.

Loosely a Python port of the Rust ``provider_openai.rs`` reference
(``rust/crates/corlinman-gateway/src/routes/voice/provider_openai.rs``),
collapsed onto the asyncio + ``websockets`` stack the rest of the
gateway already depends on.

Shape:

:class:`OpenAIRealtimeProvider` implements
:class:`~corlinman_server.gateway.routes_voice.provider.VoiceProvider`.
Its :meth:`~OpenAIRealtimeProvider.open` dials the OpenAI Realtime
WebSocket (``wss://api.openai.com/v1/realtime?model=...``), authenticates
with the ``Authorization: Bearer <key>`` + ``OpenAI-Beta: realtime=v1``
headers, sends a ``session.update`` to pin PCM-16 audio + the requested
sample rates, and returns an :class:`OpenAIRealtimeSession`.

The session is the single waist point between corlinman's semantic
voice event model (:class:`.provider.VoiceEvent` /
:class:`.provider.ProviderCommand`) and the OpenAI Realtime wire JSON:

* :meth:`OpenAIRealtimeSession.push_audio` base64-encodes each PCM-16
  frame into an ``input_audio_buffer.append`` event.
* :meth:`OpenAIRealtimeSession.push_command` maps an ``interrupt`` onto
  ``response.cancel`` and a ``close`` onto a graceful socket teardown.
* :meth:`OpenAIRealtimeSession.events` drains the upstream socket,
  translating ``response.audio.delta`` → :class:`VoiceEvent` ``AudioOut``,
  the transcript deltas → ``TranscriptPartial`` / ``TranscriptFinal``,
  ``response.function_call_arguments.done`` → ``ToolCall``, and any
  ``error`` frame → a typed :class:`VoiceEvent` ``Error``.

Degrade-gracefully contract: a missing key or a failed realtime connect
never raises out of :meth:`open` as a bare exception that would crash the
session driver — :meth:`open` returns a session that immediately yields a
single typed ``Error`` event followed by an ``End`` with
:data:`~corlinman_server.gateway.routes_voice.provider.ProviderEndReason.START_FAILED`.
The :mod:`.mod` outbound pump already maps that onto the
``provider_error`` close code.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Final

from corlinman_server.gateway.routes_voice.provider import (
    DEFAULT_PROVIDER_CHANNEL_CAPACITY,
    ProviderCommand,
    ProviderEndReason,
    VoiceEvent,
    VoiceSessionStartParams,
)

logger = logging.getLogger("corlinman_server.gateway.routes_voice.provider_openai")

__all__ = [
    "DEFAULT_REALTIME_MODEL",
    "OPENAI_REALTIME_URL",
    "OpenAIRealtimeProvider",
    "OpenAIRealtimeSession",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPENAI_REALTIME_URL: Final[str] = "wss://api.openai.com/v1/realtime"
"""Base WebSocket URL for the OpenAI Realtime API. The model is appended
as a ``?model=`` query parameter at connect time."""

DEFAULT_REALTIME_MODEL: Final[str] = "gpt-4o-realtime-preview"
"""Model used when the caller doesn't pin one explicitly. Matches the
generally-available Realtime preview model id."""

_REALTIME_BETA_HEADER: Final[str] = "realtime=v1"
"""Value for the ``OpenAI-Beta`` header — the Realtime API is still
gated behind this opt-in flag."""

_CONNECT_TIMEOUT_SECONDS: Final[float] = 10.0
"""How long to wait for the realtime WebSocket handshake before giving
up and degrading to a typed ``start_failed`` error."""


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class OpenAIRealtimeSession:
    """One open OpenAI Realtime conversation.

    Implements the
    :class:`~corlinman_server.gateway.routes_voice.provider.VoiceProviderSession`
    Protocol. Constructed in one of two states:

    * **live** — :meth:`connect` succeeded; a background reader task
      drains the upstream socket and a :class:`asyncio.Queue` of
      :class:`VoiceEvent` is bridged out via :meth:`events`.
    * **failed** — :meth:`connect` could not be reached (no key, bad
      connect). The queue is pre-seeded with a typed ``Error`` + ``End``
      so the session driver degrades cleanly rather than crashing.

    Callers never construct this directly — :meth:`OpenAIRealtimeProvider.open`
    is the only factory.
    """

    def __init__(
        self,
        *,
        params: VoiceSessionStartParams,
        model: str,
    ) -> None:
        self._params = params
        self._model = model
        self._ws: Any | None = None
        self._events: asyncio.Queue[VoiceEvent | None] = asyncio.Queue(
            maxsize=DEFAULT_PROVIDER_CHANNEL_CAPACITY * 4
        )
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        # Accumulates the assistant transcript across delta frames so a
        # ``done`` frame can emit one TranscriptFinal even when OpenAI
        # streamed it piecewise.
        self._assistant_transcript: list[str] = []

    # ----- failure path ------------------------------------------------

    @classmethod
    def failed(
        cls,
        *,
        params: VoiceSessionStartParams,
        model: str,
        code: str,
        message: str,
    ) -> OpenAIRealtimeSession:
        """Build a session that never connected. The event queue is
        pre-seeded with a typed ``Error`` then an ``End`` carrying
        :data:`ProviderEndReason.START_FAILED` so the outbound pump
        surfaces a clean ``provider_error`` close instead of a crash."""
        session = cls(params=params, model=model)
        session._closed = True
        session._events.put_nowait(VoiceEvent.error(code=code, message=message))
        session._events.put_nowait(
            VoiceEvent.end(end_reason=ProviderEndReason.START_FAILED)
        )
        session._events.put_nowait(None)
        return session

    # ----- connect -----------------------------------------------------

    async def connect(self, api_key: str) -> None:
        """Dial the OpenAI Realtime WebSocket and start the reader task.

        Raises :class:`OpenAIRealtimeConnectError` on any handshake
        failure; :meth:`OpenAIRealtimeProvider.open` catches that and
        falls back to :meth:`failed` so the route never sees a bare
        exception.
        """
        url = f"{OPENAI_REALTIME_URL}?model={self._model}"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Beta": _REALTIME_BETA_HEADER,
        }
        try:
            connect = _import_ws_connect()
            self._ws = await asyncio.wait_for(
                connect(url, additional_headers=headers),
                timeout=_CONNECT_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise OpenAIRealtimeConnectError(
                "openai realtime connect timed out"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — any dial failure degrades
            raise OpenAIRealtimeConnectError(
                f"openai realtime connect failed: {exc}"
            ) from exc

        # Pin PCM-16 in/out + the requested sample rates so the gateway's
        # framing layer and OpenAI agree on the byte shape.
        await self._send_json(
            {
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "input_audio_transcription": {"model": "whisper-1"},
                    "turn_detection": {"type": "server_vad"},
                },
            }
        )

        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"voice-openai-read-{self._params.session_id}"
        )
        # Synthesise the Ready event the session driver expects so the
        # `started` handshake completes symmetrically with the mock.
        await self._events.put(
            VoiceEvent.ready(provider_session_id=f"openai-{self._params.session_id}")
        )

    # ----- VoiceProviderSession Protocol -------------------------------

    async def push_audio(self, pcm_le_bytes: bytes) -> None:
        """Forward one PCM-16 frame as an ``input_audio_buffer.append``
        event. OpenAI Realtime expects base64-encoded little-endian
        PCM-16 — exactly the framing layer's ``pcm_le_bytes`` shape."""
        if self._closed or self._ws is None:
            return
        payload = base64.b64encode(pcm_le_bytes).decode("ascii")
        await self._send_json(
            {"type": "input_audio_buffer.append", "audio": payload}
        )

    async def push_command(self, command: ProviderCommand) -> None:
        """Translate a gateway :class:`ProviderCommand` onto the realtime
        wire.

        * ``interrupt`` → ``response.cancel`` (stop the in-flight TTS).
        * ``approve_tool`` → no-op upstream; tool approval is mediated by
          the gateway's :class:`VoiceApprovalBridge`, not OpenAI.
        * ``close`` → graceful socket teardown + an ``End`` event.
        """
        if self._closed:
            return
        if command.kind == ProviderCommand.INTERRUPT:
            if self._ws is not None:
                await self._send_json({"type": "response.cancel"})
            return
        if command.kind == ProviderCommand.CLOSE:
            await self._emit(VoiceEvent.end(end_reason=ProviderEndReason.GRACEFUL))
            await self.close()
            return
        # APPROVE_TOOL: the realtime API has no approval concept — the
        # decision is applied gateway-side. Nothing to forward upstream.

    async def events(self) -> AsyncIterator[VoiceEvent]:
        """Drain provider → gateway events until ``End`` (or socket
        close). The reader task feeds the queue; this just demultiplexes
        the sentinel."""
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event
            if event.kind == VoiceEvent.END:
                return

    async def close(self) -> None:
        """Tear down the reader task + WebSocket. Idempotent."""
        if self._closed and self._ws is None and self._reader_task is None:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        # Unstick any awaiter on `events`.
        with contextlib.suppress(asyncio.QueueFull):
            self._events.put_nowait(None)

    # ----- internals ---------------------------------------------------

    async def _send_json(self, obj: dict[str, Any]) -> None:
        """Serialise + send one realtime event. Swallows a closed-socket
        error into a no-op — the reader task will surface the disconnect
        as an ``End`` event so the up-pump doesn't need to react."""
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(obj))
        except Exception as exc:  # noqa: BLE001 — closed socket etc.
            logger.debug("voice.openai: send failed: %s", exc)

    async def _emit(self, event: VoiceEvent) -> None:
        """Enqueue a single provider → gateway event, dropping it if the
        consumer has fallen far enough behind to fill the queue (rather
        than blocking the reader task indefinitely)."""
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "voice.openai: event queue full; dropping %s", event.kind
            )

    async def _read_loop(self) -> None:
        """Background task: drain the upstream socket, translating each
        realtime JSON frame into 0..1 :class:`VoiceEvent`.

        On any socket close / error this enqueues a terminal ``End`` (or
        ``Error`` + ``End``) event so :meth:`events` always terminates.
        """
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    # Realtime frames are JSON text; binary is unexpected.
                    continue
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("voice.openai: non-JSON frame dropped")
                    continue
                if not isinstance(frame, dict):
                    continue
                event = self._translate(frame)
                if event is not None:
                    await self._emit(event)
                    if event.kind == VoiceEvent.END:
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — any socket failure
            logger.debug("voice.openai: read loop errored: %s", exc)
            await self._emit(
                VoiceEvent.error(
                    code="provider_error",
                    message=f"openai realtime stream error: {exc}",
                )
            )
            await self._emit(
                VoiceEvent.end(end_reason=ProviderEndReason.PROVIDER_ERROR)
            )
            return
        # Socket closed cleanly without a `response.done`-driven end.
        await self._emit(VoiceEvent.end(end_reason=ProviderEndReason.GRACEFUL))

    def _translate(self, frame: dict[str, Any]) -> VoiceEvent | None:
        """Map one OpenAI Realtime event onto a corlinman
        :class:`VoiceEvent` (or ``None`` to drop it).

        Only the events the gateway surfaces are translated; the dozens
        of bookkeeping events (``rate_limits.updated``,
        ``input_audio_buffer.committed``, …) are intentionally dropped.
        """
        kind = frame.get("type")
        if not isinstance(kind, str):
            return None

        # ---- audio out ------------------------------------------------
        if kind in ("response.audio.delta", "response.output_audio.delta"):
            delta = frame.get("delta")
            if isinstance(delta, str) and delta:
                try:
                    pcm = base64.b64decode(delta)
                except (ValueError, TypeError):
                    return None
                return VoiceEvent.audio_out(pcm_le_bytes=pcm)
            return None

        # ---- user transcript (input audio) ---------------------------
        if kind == "conversation.item.input_audio_transcription.completed":
            text = frame.get("transcript")
            if isinstance(text, str) and text:
                return VoiceEvent.transcript_final(role="user", text=text)
            return None
        if kind == "conversation.item.input_audio_transcription.delta":
            text = frame.get("delta")
            if isinstance(text, str) and text:
                return VoiceEvent.transcript_partial(role="user", text=text)
            return None

        # ---- assistant transcript (audio transcript) -----------------
        if kind in (
            "response.audio_transcript.delta",
            "response.output_audio_transcript.delta",
        ):
            text = frame.get("delta")
            if isinstance(text, str) and text:
                self._assistant_transcript.append(text)
                return VoiceEvent.transcript_partial(role="assistant", text=text)
            return None
        if kind in (
            "response.audio_transcript.done",
            "response.output_audio_transcript.done",
        ):
            text = frame.get("transcript")
            if not isinstance(text, str) or not text:
                text = "".join(self._assistant_transcript)
            self._assistant_transcript.clear()
            if text:
                return VoiceEvent.transcript_final(role="assistant", text=text)
            return None

        # ---- assistant plain text ------------------------------------
        if kind in ("response.text.done", "response.output_text.done"):
            text = frame.get("text")
            if isinstance(text, str) and text:
                return VoiceEvent.agent_text(text=text)
            return None

        # ---- tool / function call ------------------------------------
        if kind == "response.function_call_arguments.done":
            call_id = frame.get("call_id") or frame.get("item_id") or ""
            tool = frame.get("name") or ""
            raw_args = frame.get("arguments")
            args: Any = raw_args
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    args = {"_raw": raw_args}
            return VoiceEvent.tool_call(
                call_id=str(call_id), tool=str(tool), args=args
            )

        # ---- error ----------------------------------------------------
        if kind == "error":
            err = frame.get("error")
            if isinstance(err, dict):
                code = str(err.get("code") or err.get("type") or "provider_error")
                message = str(err.get("message") or "openai realtime error")
            else:
                code = "provider_error"
                message = "openai realtime error"
            return VoiceEvent.error(code=code, message=message)

        # Everything else (session.created, response.done, rate_limits,
        # buffer commit acks, …) is bookkeeping — drop it.
        return None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OpenAIRealtimeConnectError(Exception):
    """Raised internally when the realtime WebSocket can't be dialled.

    Never escapes :meth:`OpenAIRealtimeProvider.open` — it is caught
    there and converted into a :meth:`OpenAIRealtimeSession.failed`
    session so the route degrades to a typed error rather than a crash.
    """


class OpenAIRealtimeProvider:
    """Real :class:`VoiceProvider` backed by the OpenAI Realtime API.

    Constructed with a resolved OpenAI API key (see
    :func:`~corlinman_server.gateway.routes_voice.mod.resolve_voice_provider`).
    A ``None`` / empty key is tolerated at construction — :meth:`open`
    then yields a degraded :meth:`OpenAIRealtimeSession.failed` session
    so the caller still gets a typed error on the voice stream.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = DEFAULT_REALTIME_MODEL,
    ) -> None:
        self._api_key = (api_key or "").strip() or None
        self._model = model or DEFAULT_REALTIME_MODEL

    @property
    def has_key(self) -> bool:
        """Whether a usable API key is configured."""
        return self._api_key is not None

    async def open(
        self, params: VoiceSessionStartParams
    ) -> OpenAIRealtimeSession:
        """Open a realtime session.

        Never raises: a missing key or a failed connect is converted into
        a degraded :meth:`OpenAIRealtimeSession.failed` session whose
        event stream is a single typed ``Error`` + ``End``.
        """
        if self._api_key is None:
            logger.warning("voice.openai: no API key configured; degrading")
            return OpenAIRealtimeSession.failed(
                params=params,
                model=self._model,
                code="provider_unconfigured",
                message=(
                    "OpenAI Realtime voice provider has no API key; "
                    "set [providers.openai].api_key or OPENAI_API_KEY"
                ),
            )
        session = OpenAIRealtimeSession(params=params, model=self._model)
        try:
            await session.connect(self._api_key)
        except OpenAIRealtimeConnectError as exc:
            logger.warning("voice.openai: connect failed: %s", exc)
            return OpenAIRealtimeSession.failed(
                params=params,
                model=self._model,
                code="provider_connect_failed",
                message=str(exc),
            )
        return session


# ---------------------------------------------------------------------------
# WebSocket client import — isolated so tests can monkeypatch it
# ---------------------------------------------------------------------------


def _import_ws_connect() -> Any:
    """Return the ``websockets`` asyncio client ``connect`` callable.

    Isolated into a tiny indirection so the realtime adapter doesn't hard
    ``import websockets`` at module load (keeps the pure framing/cost
    modules importable in environments without it) and so tests can
    monkeypatch the connect callable without patching the import system.
    """
    from websockets.asyncio.client import connect

    return connect
