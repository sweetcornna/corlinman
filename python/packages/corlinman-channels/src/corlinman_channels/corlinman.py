"""``CorlinmanChannel`` — first-class channel for the admin UI's in-app chat.

Wave 3 of ``docs/PLAN_IN_APP_CHAT.md``. The other channels in this
package (``qq`` / ``telegram`` / ``discord`` / ``slack`` / ``feishu`` /
``qq_official`` / ``wechat_official``) are *transport-driven*: they open
a long-poll / WebSocket / webhook receiver and produce
:class:`InboundEvent` objects as the network feeds them. ``CorlinmanChannel``
is the inverse — it's a **pull** channel where the browser actively
POSTs each user turn to the gateway and SUBSCRIBES (Server-Sent Events)
to receive assistant deltas back.

That polarity flip means :meth:`run` has no transport loop to drive; it
just blocks on the cancel event so :func:`spawn_all` can keep the same
contract. The interesting surface is the public methods the matching
HTTP routes (:mod:`corlinman_server.gateway.routes_admin_b.corlinman_channel`)
call:

* :meth:`ingest` — browser → channel inbound. Returns the normalized
  :class:`InboundEvent` for the gateway router to dispatch.
* :meth:`subscribe` — browser → channel outbound. Yields ``bytes``
  frames already formatted as SSE (``event: <name>\\ndata: <json>\\n\\n``)
  so the route handler can stream them straight to the client.
* :meth:`send` / :meth:`typing` — gateway → channel outbound. Push a
  frame into the per-session outbound queue so it shows up on the next
  subscriber tick.
* :meth:`edit` / :meth:`delete` / :meth:`react` — Wave 4 extension
  points. Signatures are present (so the HTTP layer can wire endpoints
  today) but raise :class:`UnsupportedError` until Wave 4 lands.

## Feature flag

The :func:`ChannelRegistry.builtin` factory does **not** register
``CorlinmanChannel`` by default — Wave 3 ships behind
``CORLINMAN_CHANNEL_ENABLED=1``. Callers wanting always-on (tests,
custom gateway boots) construct + push the channel themselves.

## Multi-session isolation

Each browser tab maps to a unique ``session_key`` (computed gateway-side
from ``ChannelBinding("corlinman", tenant_id, session_key, user_id)``). The
channel keeps one bounded :class:`asyncio.Queue` per active key in
:attr:`_outbound`. Queues are created lazily on the first ``subscribe``
*or* ``send`` (whichever fires first — sends from a fresh turn can
race ahead of the SSE handshake by milliseconds) and garbage-collected
when both the queue is empty AND no subscriber is attached.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from corlinman_channels.common import (
    Attachment,
    ChannelBinding,
    InboundEvent,
    UnsupportedError,
)

__all__ = [
    "CORLINMAN_CHANNEL_ENV_FLAG",
    "DEFAULT_ACCOUNT",
    "CorlinmanChannel",
    "CorlinmanOutboundFrame",
    "corlinman_channel_enabled",
]

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


#: Env var the gateway bootstrapper consults to decide whether to wire
#: :class:`CorlinmanChannel` into the active :class:`ChannelRegistry`. The
#: default is "off" (Wave 3 ships dark — the in-app chat keeps using
#: its direct hermes path until the operator opts in).
CORLINMAN_CHANNEL_ENV_FLAG: str = "CORLINMAN_CHANNEL_ENABLED"

#: Default ``ChannelBinding.account`` for web sessions when the caller
#: doesn't supply a tenant override. Web inbound has no transport-level
#: "bot account" the way Telegram / QQ do; the slug is just a stable
#: namespace so ``session_key()`` derivations stay deterministic.
DEFAULT_ACCOUNT: str = "corlinman"


def corlinman_channel_enabled() -> bool:
    """Return ``True`` when ``CORLINMAN_CHANNEL_ENABLED`` is truthy.

    The check is intentionally lenient — ``"1"``, ``"true"``, ``"yes"``,
    ``"on"`` (case-insensitive) all activate the channel. Anything else
    (including empty / unset) keeps the channel dormant so Wave 3 is a
    no-op for operators who don't opt in.
    """
    import os

    raw = os.environ.get(CORLINMAN_CHANNEL_ENV_FLAG, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Outbound frame model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CorlinmanOutboundFrame:
    """One SSE frame the channel pushes toward a browser.

    Pre-serialised on the producer side so the SSE iterator's only job
    is to peel ``bytes`` off the queue and yield them. ``event`` is the
    SSE event name (``message`` / ``typing`` / ``done``) and ``data``
    is the JSON-serialised payload string (NOT the parsed dict —
    keeping it as a string means the iterator doesn't re-serialise on
    every fan-out).
    """

    event: str
    data: str

    def encode(self) -> bytes:
        """Render the frame in wire SSE shape (``event: …\\ndata: …\\n\\n``)."""
        # SSE protocol: each event optionally has an ``event:`` line followed
        # by one or more ``data:`` lines, terminated by a blank line. We do
        # not split ``data`` on newlines — callers pre-serialise to a JSON
        # string (single line) so the simple form is safe.
        return f"event: {self.event}\ndata: {self.data}\n\n".encode()


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _SessionState:
    """In-process state kept per active web session.

    Two coupled handles: the ``queue`` the producer side writes into and
    the ``subscribers`` counter so we know when it's safe to evict the
    session from :attr:`CorlinmanChannel._outbound`. The queue is bounded so a
    misbehaving consumer (closed tab that never drains) can't OOM the
    gateway — when the buffer fills, the producer drops the oldest
    frame and logs a warning.
    """

    queue: asyncio.Queue[CorlinmanOutboundFrame] = field(
        default_factory=lambda: asyncio.Queue(maxsize=256)
    )
    subscribers: int = 0
    last_seen_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class CorlinmanChannel:
    """In-app web chat as a first-class :class:`Channel` implementation.

    See module docstring for the architecture rationale (pull-mode
    channel, multi-session isolation, feature flag).
    """

    #: Stable channel id used in :class:`ChannelBinding.channel` and as
    #: a metric / log label. Matches the slug Wave 3 specifies for the
    #: HTTP surface (``/api/channels/corlinman/...``).
    CHANNEL_ID: str = "corlinman"

    def __init__(self, account: str = DEFAULT_ACCOUNT) -> None:
        self._account = account
        # Mapping ``session_key -> _SessionState``. Mutated only from
        # the asyncio loop so no lock is needed; we keep the dict
        # operations to single statements anyway.
        self._outbound: dict[str, _SessionState] = {}

    # ------------------------------------------------------------------
    # Channel Protocol surface
    # ------------------------------------------------------------------

    def id(self) -> str:
        return self.CHANNEL_ID

    def display_name(self) -> str:
        return "Corlinman Chat"

    def enabled(self, cfg: Any) -> bool:
        """Activation gate consulted by :func:`spawn_all`.

        Unlike the other channels (which read ``cfg.channels.<slug>``),
        ``CorlinmanChannel`` keys off the environment flag so an operator can
        flip it without editing the TOML config. The check is repeated
        on each boot — there is no caching beyond what
        :func:`os.environ.get` already gives us.
        """
        return corlinman_channel_enabled()

    async def run(self, ctx: Any, cancel: asyncio.Event) -> None:
        """Block on ``cancel`` — :class:`CorlinmanChannel` has no transport loop.

        The HTTP routes (mounted by
        :mod:`corlinman_server.gateway.routes_admin_b.corlinman_channel`)
        drive ingestion / subscription directly against the channel
        instance; this coroutine exists purely so the channel satisfies
        :class:`Channel` and :func:`spawn_all` can keep the uniform
        ``spawn → await cancel → drain`` shape every other adapter has.

        On cancel we close every pending subscriber queue by clearing
        the session map; in-flight :meth:`subscribe` consumers raise
        :class:`StopAsyncIteration` on the next ``await`` and exit
        cleanly.
        """
        # Suppress is defensive — newer Python re-raises the cancellation
        # at the next yield point regardless of who set the event.
        with suppress(asyncio.CancelledError):
            await cancel.wait()
        # Drain — every queue gets a no-op frame so subscribers wake up
        # and observe the close. We then clear the map so the GC can
        # reclaim everything.
        for session_key, state in list(self._outbound.items()):
            # Best-effort: a closed-tab consumer that never drained may
            # leave the queue full; ignore overflow on shutdown.
            with suppress(asyncio.QueueFull):
                state.queue.put_nowait(
                    CorlinmanOutboundFrame(event="done", data="{}")
                )
            _ = session_key  # used only as log context below if we add one
        self._outbound.clear()

    # ------------------------------------------------------------------
    # Inbound — browser → channel
    # ------------------------------------------------------------------

    async def ingest(
        self,
        session_key: str,
        text: str,
        attachments: list[Attachment] | None = None,
        user_id: str | None = None,
    ) -> InboundEvent[dict[str, Any]]:
        """Build an :class:`InboundEvent` from a browser-originated turn.

        Mirrors the per-adapter ``inbound()`` async-iterator pattern but
        operates one-shot: the HTTP route awaits this method, then hands
        the resulting event to the same chat-service dispatch the other
        channels use. Returning the constructed envelope (rather than
        dispatching directly) keeps the channel pure — the route layer
        owns the chat-service wiring and the response shape.

        ``session_key`` is the gateway-derived conversation key (the
        same 16-hex-char digest other channels compute via
        :meth:`ChannelBinding.session_key`). The corlinman channel doesn't
        re-hash; the browser-side computes / stores its own session id
        and the gateway plumbs it through.

        ``user_id`` is the canonical actor id (admin username, tenant
        slug, etc). When absent we fall back to the literal string
        ``"anonymous"`` so the binding's :meth:`session_key` derivation
        still produces a stable hash.
        """
        if not isinstance(session_key, str) or not session_key:
            raise ValueError("session_key must be a non-empty string")
        if not isinstance(text, str):
            raise TypeError("text must be a string")

        actor = user_id or "anonymous"
        binding = ChannelBinding(
            channel=self.CHANNEL_ID,
            account=self._account,
            thread=session_key,
            sender=actor,
        )
        # Generate a transport-side message id so the SSE replies can
        # quote it back when needed (edit/delete in Wave 4). The other
        # adapters use the upstream id (Telegram update_id, OneBot
        # message_id) — we mint our own because the browser has no such
        # token.
        message_id = f"corlinman-{uuid.uuid4().hex[:12]}"
        ts_now = int(time.time())
        attachments_resolved = list(attachments) if attachments else []

        # ``payload`` carries the raw inbound dict so debug surfaces /
        # journal writers can introspect the original turn without
        # re-deriving it from the envelope.
        payload: dict[str, Any] = {
            "session_key": session_key,
            "text": text,
            "user_id": actor,
            "attachment_count": len(attachments_resolved),
            "received_at": ts_now,
        }
        event: InboundEvent[dict[str, Any]] = InboundEvent(
            channel=self.CHANNEL_ID,
            binding=binding,
            text=text,
            message_id=message_id,
            timestamp=ts_now,
            # Web sessions are inherently 1:1 (one browser tab == one
            # session), so every inbound is implicitly "addressed" the
            # same way Telegram private chats are. Downstream gates that
            # check ``mentioned`` for group filtering will pass through.
            mentioned=True,
            attachments=attachments_resolved,
            payload=payload,
        )
        # Touch the session bucket — if a producer was racing ahead of
        # the subscriber, the bucket may already exist; either way we
        # ensure it's there so the first send() can write without a
        # branch. The bookkeeping cost is one dict.get.
        self._touch_session(session_key)
        return event

    # ------------------------------------------------------------------
    # Outbound subscription — channel → browser SSE
    # ------------------------------------------------------------------

    async def subscribe(self, session_key: str) -> AsyncIterator[bytes]:
        """Yield SSE-formatted frames for ``session_key`` until cancelled.

        Designed so a FastAPI handler can write::

            return StreamingResponse(
                channel.subscribe(session_key),
                media_type="text/event-stream",
            )

        and the browser receives one ``event: ...`` block per published
        frame. We do NOT send keep-alives from inside the iterator —
        the HTTP route is welcome to wrap us in a heartbeat helper
        (logs.py does), but the channel itself stays transport-agnostic.

        Multiple subscribers per ``session_key`` are supported but
        broadcast semantics are NOT — each frame is delivered to the
        first consumer that grabs it (queue draining). The browser
        contract is "one tab, one subscriber"; a second subscriber is
        either an upgrade flow (Wave 4 reconnect) or a bug.

        The iterator emits a one-shot ``: connected`` comment line on
        connect so the browser's EventSource fires ``open`` immediately
        — without it the connection looks idle until the first real
        frame, which trips uvicorn's slow-start buffer.
        """
        state = self._touch_session(session_key)
        state.subscribers += 1
        try:
            # Initial handshake — SSE comment lines (start with ``:``)
            # are ignored by EventSource but force a flush of the
            # response headers + first chunk.
            yield b": connected\n\n"
            while True:
                try:
                    frame = await state.queue.get()
                except asyncio.CancelledError:
                    return
                yield frame.encode()
                state.last_seen_at = time.time()
                # ``done`` is the channel-internal sentinel for "this
                # session is going away" (set in :meth:`run` on cancel,
                # or by a future explicit close). Terminate the iterator
                # so the HTTP layer closes the response.
                if frame.event == "done":
                    return
        finally:
            state.subscribers = max(0, state.subscribers - 1)
            # If no subscribers AND no pending frames, evict the bucket
            # so a long-lived gateway doesn't leak per-session memory.
            # We keep the bucket otherwise — the producer may still be
            # mid-turn and the same browser will reconnect.
            if state.subscribers == 0 and state.queue.empty():
                # ``pop`` defaults defensively; another flow may have
                # already replaced the bucket.
                self._outbound.pop(session_key, None)

    # ------------------------------------------------------------------
    # Outbound — gateway → channel queue
    # ------------------------------------------------------------------

    async def send(
        self,
        session_key: str,
        text: str,
        *,
        message_id: str | None = None,
        role: str = "assistant",
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Push one assistant message frame onto the session's queue.

        The frame's wire shape is ``event: message`` with a JSON body
        containing ``{message_id, role, text, ts, ...extra}``. The browser's
        ``MessageList`` consumes this directly.

        Returns the message id (minted if the caller didn't supply one)
        so the dispatch layer can record it in the journal alongside
        the upstream turn id.
        """
        mid = message_id or f"corlinman-{uuid.uuid4().hex[:12]}"
        body: dict[str, Any] = {
            "message_id": mid,
            "role": role,
            "text": text,
            "ts": int(time.time()),
        }
        if extra:
            body.update(extra)
        await self._enqueue(session_key, CorlinmanOutboundFrame(
            event="message",
            data=json.dumps(body, ensure_ascii=False, default=str),
        ))
        return mid

    async def typing(self, session_key: str, state: bool = True) -> None:
        """Emit a typing-indicator frame.

        Mirrors Telegram's ``sendChatAction(action="typing")`` semantics
        — the browser shows the dotted indicator while ``state`` is
        ``True`` and clears it on the next ``typing(False)`` (or after
        the next real ``message`` frame, whichever comes first).
        """
        body = {"typing": bool(state), "ts": int(time.time())}
        await self._enqueue(session_key, CorlinmanOutboundFrame(
            event="typing",
            data=json.dumps(body),
        ))

    # ------------------------------------------------------------------
    # Wave 4 extension points
    # ------------------------------------------------------------------

    async def edit(
        self,
        session_key: str,
        msg_id: str,
        text: str,
    ) -> None:
        """Edit a previously-sent message in place. **Wave 4 stub.**

        The signature is present so the HTTP route can wire its
        ``POST /api/channels/corlinman/edit/{msg_id}`` endpoint today and
        return a typed 503; the actual implementation lands with the
        Wave 4 Telegram-parity work (``docs/PLAN_IN_APP_CHAT.md`` §3).
        """
        raise UnsupportedError(
            f"edit not supported by channel={self.CHANNEL_ID} "
            f"(session={session_key}, msg_id={msg_id}, len={len(text)})"
        )

    async def delete(self, session_key: str, msg_id: str) -> None:
        """Delete a previously-sent message. **Wave 4 stub.** See :meth:`edit`."""
        raise UnsupportedError(
            f"delete not supported by channel={self.CHANNEL_ID} "
            f"(session={session_key}, msg_id={msg_id})"
        )

    async def react(
        self,
        session_key: str,
        msg_id: str,
        emoji: str,
    ) -> None:
        """Attach an emoji reaction to a message. **Wave 4 stub.** See :meth:`edit`."""
        raise UnsupportedError(
            f"react not supported by channel={self.CHANNEL_ID} "
            f"(session={session_key}, msg_id={msg_id}, emoji={emoji})"
        )

    # ------------------------------------------------------------------
    # Inspection helpers (used by tests + admin observability)
    # ------------------------------------------------------------------

    def active_sessions(self) -> list[str]:
        """Snapshot of currently-tracked ``session_key``s.

        Order is insertion order (Python 3.7+ dict guarantee). Useful
        for the upcoming ``channels/web`` admin observability page and
        for assertions in tests.
        """
        return list(self._outbound.keys())

    def subscriber_count(self, session_key: str) -> int:
        """How many active SSE iterators are draining the given session.

        ``0`` for unknown / evicted sessions.
        """
        state = self._outbound.get(session_key)
        return 0 if state is None else state.subscribers

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _touch_session(self, session_key: str) -> _SessionState:
        """Get-or-create the :class:`_SessionState` for ``session_key``."""
        state = self._outbound.get(session_key)
        if state is None:
            state = _SessionState()
            self._outbound[session_key] = state
        else:
            state.last_seen_at = time.time()
        return state

    async def _enqueue(
        self, session_key: str, frame: CorlinmanOutboundFrame
    ) -> None:
        """Bounded-queue push with overflow handling.

        Bounded so a never-draining subscriber can't grow the heap
        without limit. On overflow we drop the OLDEST frame (so a slow
        consumer still sees the latest assistant deltas) and emit a
        warning — preferable to blocking the producer, which would
        cascade backpressure into the chat service.
        """
        state = self._touch_session(session_key)
        try:
            state.queue.put_nowait(frame)
        except asyncio.QueueFull:
            # Drop the oldest, then retry. Logged once per overflow so
            # operators can see the buffer is undersized for the
            # consumer's pace.
            try:
                _ = state.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover — race
                pass
            else:
                _log.warning(
                    "corlinman_channel.queue_overflow session=%s dropped_oldest=1",
                    session_key,
                )
            with suppress(asyncio.QueueFull):
                state.queue.put_nowait(frame)
