"""Tests for :mod:`corlinman_channels.web` — CorlinmanChannel core surface.

Covers W3 of ``docs/PLAN_IN_APP_CHAT.md``:

* :meth:`CorlinmanChannel.ingest` — produces a well-formed
  :class:`InboundEvent` with the expected binding shape.
* :meth:`CorlinmanChannel.subscribe` — drains :meth:`CorlinmanChannel.send` /
  :meth:`CorlinmanChannel.typing` frames in publish order.
* Multi-session isolation — two ``session_key``s don't bleed frames
  into each other.
* Wave 4 stubs (:meth:`edit` / :meth:`delete` / :meth:`react`) raise
  :class:`UnsupportedError`.
* :func:`corlinman_channel_enabled` honours the env flag, and
  :func:`ChannelRegistry.builtin` skips ``web`` when the flag is off.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from corlinman_channels.channel import ChannelRegistry
from corlinman_channels.common import Attachment, AttachmentKind, UnsupportedError
from corlinman_channels.corlinman import (
    CORLINMAN_CHANNEL_ENV_FLAG,
    CorlinmanChannel,
    CorlinmanOutboundFrame,
    corlinman_channel_enabled,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _collect_until(
    stream: AsyncIterator[bytes],
    *,
    max_frames: int,
    timeout: float = 1.0,
) -> list[bytes]:
    """Drain up to ``max_frames`` from the SSE stream with a timeout per pull.

    Used by the SSE-shape tests so a misbehaving iterator can't hang
    pytest indefinitely.
    """
    out: list[bytes] = []
    for _ in range(max_frames):
        try:
            frame = await asyncio.wait_for(stream.__anext__(), timeout=timeout)
        except (StopAsyncIteration, asyncio.TimeoutError):
            break
        out.append(frame)
    return out


def _parse_sse_frame(raw: bytes) -> tuple[str, dict[str, Any] | None]:
    """Parse one SSE frame back into ``(event, data_dict)``.

    Returns ``(event, None)`` for comment / handshake frames so the
    caller can branch.
    """
    text = raw.decode("utf-8")
    if text.startswith(":"):
        return ("comment", None)
    event = ""
    data = ""
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data = line[len("data:"):].strip()
    return (event, json.loads(data) if data else None)


# ---------------------------------------------------------------------------
# Feature flag + registry wiring
# ---------------------------------------------------------------------------


class TestFeatureFlag:
    def test_flag_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(CORLINMAN_CHANNEL_ENV_FLAG, raising=False)
        assert corlinman_channel_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on"])
    def test_flag_truthy_values(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(CORLINMAN_CHANNEL_ENV_FLAG, raw)
        assert corlinman_channel_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "", "no", "off", "garbage"])
    def test_flag_falsy_values(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(CORLINMAN_CHANNEL_ENV_FLAG, raw)
        assert corlinman_channel_enabled() is False

    def test_builtin_registry_omits_corlinman_when_flag_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CORLINMAN_CHANNEL_ENV_FLAG, raising=False)
        ids = [c.id() for c in ChannelRegistry.builtin().iter()]
        assert "corlinman" not in ids
        # Pre-existing channels still register in the legacy order so
        # the prior trait_impl test keeps passing.
        assert ids == ["qq", "telegram"]

    def test_builtin_registry_includes_corlinman_when_flag_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CORLINMAN_CHANNEL_ENV_FLAG, "1")
        ids = [c.id() for c in ChannelRegistry.builtin().iter()]
        assert "corlinman" in ids
        # ``corlinman`` is appended after the legacy two — the legacy log
        # / metric ordering must stay bit-for-bit identical when the
        # flag is off, and append-only when on.
        assert ids.index("corlinman") > ids.index("telegram")


# ---------------------------------------------------------------------------
# Channel Protocol surface
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_identity_and_display(self) -> None:
        ch = CorlinmanChannel()
        assert ch.id() == "corlinman"
        assert ch.display_name() == "Corlinman Chat"

    def test_enabled_reads_env_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ch = CorlinmanChannel()
        monkeypatch.delenv(CORLINMAN_CHANNEL_ENV_FLAG, raising=False)
        assert ch.enabled(object()) is False
        monkeypatch.setenv(CORLINMAN_CHANNEL_ENV_FLAG, "1")
        assert ch.enabled(object()) is True

    @pytest.mark.asyncio
    async def test_run_blocks_until_cancel(self) -> None:
        ch = CorlinmanChannel()
        cancel = asyncio.Event()
        task = asyncio.create_task(ch.run(object(), cancel))
        # The run coroutine should still be in flight a moment later
        # (no transport loop to drive — it just awaits cancel).
        await asyncio.sleep(0.01)
        assert not task.done()
        cancel.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert task.exception() is None

    @pytest.mark.asyncio
    async def test_run_drains_subscribers_on_cancel(self) -> None:
        """Active SSE consumers should observe a ``done`` frame on cancel."""
        ch = CorlinmanChannel()
        cancel = asyncio.Event()

        sub = ch.subscribe("sess-A")
        # Pull the initial `: connected` so the bucket is wired up.
        first = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        assert first.startswith(b":")

        run_task = asyncio.create_task(ch.run(object(), cancel))
        await asyncio.sleep(0.01)  # let run park on cancel.wait()
        cancel.set()

        # The subscriber should now receive a ``done`` frame and exit.
        frames = await _collect_until(sub, max_frames=3, timeout=1.0)
        events = [_parse_sse_frame(f)[0] for f in frames]
        assert "done" in events

        await asyncio.wait_for(run_task, timeout=1.0)


# ---------------------------------------------------------------------------
# ingest()
# ---------------------------------------------------------------------------


class TestIngest:
    @pytest.mark.asyncio
    async def test_basic_event_shape(self) -> None:
        ch = CorlinmanChannel()
        event = await ch.ingest(
            session_key="abc123",
            text="hello world",
            user_id="admin",
        )
        assert event.channel == "corlinman"
        assert event.text == "hello world"
        assert event.message_id is not None and event.message_id.startswith("corlinman-")
        assert event.timestamp > 0
        assert event.mentioned is True  # web inbound is always addressed
        assert event.binding.channel == "corlinman"
        assert event.binding.thread == "abc123"
        assert event.binding.sender == "admin"
        # session_key() is deterministic — re-derive matches.
        assert len(event.binding.session_key()) == 16

    @pytest.mark.asyncio
    async def test_user_id_falls_back_to_anonymous(self) -> None:
        ch = CorlinmanChannel()
        event = await ch.ingest(session_key="s1", text="hi")
        assert event.binding.sender == "anonymous"

    @pytest.mark.asyncio
    async def test_attachments_preserved(self) -> None:
        ch = CorlinmanChannel()
        att = Attachment(kind=AttachmentKind.IMAGE, url="https://x/y.png")
        event = await ch.ingest(
            session_key="s1",
            text="see image",
            attachments=[att],
        )
        assert len(event.attachments) == 1
        assert event.attachments[0].kind == AttachmentKind.IMAGE
        assert event.attachments[0].url == "https://x/y.png"
        assert event.payload is not None
        assert event.payload["attachment_count"] == 1

    @pytest.mark.asyncio
    async def test_rejects_empty_session_key(self) -> None:
        ch = CorlinmanChannel()
        with pytest.raises(ValueError):
            await ch.ingest(session_key="", text="x")

    @pytest.mark.asyncio
    async def test_rejects_non_string_text(self) -> None:
        ch = CorlinmanChannel()
        with pytest.raises(TypeError):
            await ch.ingest(session_key="s1", text=123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# subscribe() + send() + typing()
# ---------------------------------------------------------------------------


class TestSubscribeAndSend:
    @pytest.mark.asyncio
    async def test_send_flows_through_subscribe(self) -> None:
        ch = CorlinmanChannel()
        sub = ch.subscribe("sess-A")
        # Pull the handshake comment.
        first = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        assert first.startswith(b": connected")

        # Producer side pushes one message — give the iterator a chance
        # to wake up on the queue.
        mid = await ch.send("sess-A", "hello browser")
        assert mid.startswith("corlinman-")

        raw = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        event, body = _parse_sse_frame(raw)
        assert event == "message"
        assert body is not None
        assert body["text"] == "hello browser"
        assert body["role"] == "assistant"
        assert body["message_id"] == mid

    @pytest.mark.asyncio
    async def test_typing_frame(self) -> None:
        ch = CorlinmanChannel()
        sub = ch.subscribe("sess-A")
        await asyncio.wait_for(sub.__anext__(), timeout=1.0)  # handshake

        await ch.typing("sess-A", True)
        raw = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        event, body = _parse_sse_frame(raw)
        assert event == "typing"
        assert body is not None
        assert body["typing"] is True

    @pytest.mark.asyncio
    async def test_sessions_do_not_cross_streams(self) -> None:
        """A frame published to session A must NOT reach session B."""
        ch = CorlinmanChannel()
        sub_a = ch.subscribe("sess-A")
        sub_b = ch.subscribe("sess-B")
        # Drain handshakes.
        await asyncio.wait_for(sub_a.__anext__(), timeout=1.0)
        await asyncio.wait_for(sub_b.__anext__(), timeout=1.0)

        await ch.send("sess-A", "for A only")

        raw_a = await asyncio.wait_for(sub_a.__anext__(), timeout=1.0)
        event_a, body_a = _parse_sse_frame(raw_a)
        assert event_a == "message"
        assert body_a is not None
        assert body_a["text"] == "for A only"

        # B must time out — no cross-talk. ``asyncio.wait_for`` cancels
        # the underlying ``__anext__`` task on timeout; depending on the
        # iterator's CancelledError handling that surfaces as either
        # ``TimeoutError`` (the wait_for raise) or ``StopAsyncIteration``
        # (the iterator returning cleanly on cancel). Either confirms
        # the bucket stayed empty.
        with pytest.raises((asyncio.TimeoutError, StopAsyncIteration)):
            await asyncio.wait_for(sub_b.__anext__(), timeout=0.2)

    @pytest.mark.asyncio
    async def test_send_before_subscribe_buffers_frame(self) -> None:
        """Producer racing ahead of the SSE handshake: the frame is held
        in the bounded queue and delivered when the subscriber attaches."""
        ch = CorlinmanChannel()
        await ch.send("sess-A", "early bird")
        sub = ch.subscribe("sess-A")
        # Skip handshake.
        await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        raw = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        event, body = _parse_sse_frame(raw)
        assert event == "message"
        assert body is not None
        assert body["text"] == "early bird"

    @pytest.mark.asyncio
    async def test_active_sessions_tracking(self) -> None:
        ch = CorlinmanChannel()
        assert ch.active_sessions() == []
        await ch.send("alpha", "x")
        await ch.send("beta", "y")
        # Insertion order preserved.
        assert ch.active_sessions() == ["alpha", "beta"]

    @pytest.mark.asyncio
    async def test_subscriber_count(self) -> None:
        ch = CorlinmanChannel()
        assert ch.subscriber_count("nope") == 0
        sub = ch.subscribe("sess-A")
        await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        assert ch.subscriber_count("sess-A") == 1


# ---------------------------------------------------------------------------
# Wave 4 stubs
# ---------------------------------------------------------------------------


class TestWave4Stubs:
    @pytest.mark.asyncio
    async def test_edit_raises_unsupported(self) -> None:
        ch = CorlinmanChannel()
        with pytest.raises(UnsupportedError):
            await ch.edit("s1", "corlinman-abc", "new text")

    @pytest.mark.asyncio
    async def test_delete_raises_unsupported(self) -> None:
        ch = CorlinmanChannel()
        with pytest.raises(UnsupportedError):
            await ch.delete("s1", "corlinman-abc")

    @pytest.mark.asyncio
    async def test_react_raises_unsupported(self) -> None:
        ch = CorlinmanChannel()
        with pytest.raises(UnsupportedError):
            await ch.react("s1", "corlinman-abc", ":+1:")


# ---------------------------------------------------------------------------
# Outbound frame wire shape
# ---------------------------------------------------------------------------


class TestOutboundFrame:
    def test_encode_shape(self) -> None:
        frame = CorlinmanOutboundFrame(event="message", data='{"text":"hi"}')
        wire = frame.encode()
        assert wire == b'event: message\ndata: {"text":"hi"}\n\n'

    def test_done_frame_terminates_subscribe(self) -> None:
        """Confirms the iterator exits after emitting a ``done`` frame."""

        async def _scenario() -> list[bytes]:
            ch = CorlinmanChannel()
            sub = ch.subscribe("sess-A")
            await asyncio.wait_for(sub.__anext__(), timeout=1.0)  # handshake
            # Push directly so we don't depend on send() to set the
            # event name — done is the channel-internal sentinel.
            from corlinman_channels.corlinman import _SessionState  # noqa: PLC0415

            state = ch._outbound["sess-A"]  # type: ignore[attr-defined]
            assert isinstance(state, _SessionState)
            await state.queue.put(CorlinmanOutboundFrame(event="done", data="{}"))
            frames: list[bytes] = []
            async for f in sub:
                frames.append(f)
                if len(f) > 1000:  # belt-and-suspenders bound
                    break
            return frames

        frames = asyncio.run(_scenario())
        # Exactly the one ``done`` frame then iterator terminates.
        assert len(frames) == 1
        event, _ = _parse_sse_frame(frames[0])
        assert event == "done"
