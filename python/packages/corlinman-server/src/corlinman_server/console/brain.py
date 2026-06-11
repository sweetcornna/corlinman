"""Brain abstraction + per-session turn driver for the console.

``Brain`` is the transport seam (embedded UDS servicer vs gateway SSE
attach); :class:`BrainSession` owns everything both share: the rolling
message window, the session key, the active model, and the cancel event
for the in-flight turn.

The window model mirrors claude-code: the *client* accumulates the
conversation (user + assistant turns) and replays it on every request —
the ``/v1/chat/completions`` contract is a stateless window, and the
embedded servicer journals turns server-side for audit/resume on top.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from corlinman_server.console.events import (
    ConsoleEvent,
    TextDelta,
    TurnDone,
    TurnError,
)

if TYPE_CHECKING:
    from corlinman_server.console.compaction import Compactor

__all__ = ["Brain", "BrainSession", "TurnStats", "new_session_key"]


def new_session_key() -> str:
    """Fresh console session key — namespaced so journal rows are
    attributable to the console surface (channels use
    ``<channel>:<account>:<thread>``)."""
    return f"console:{uuid.uuid4().hex[:12]}"


@runtime_checkable
class Brain(Protocol):
    """Transport seam: one turn in, a normalised event stream out."""

    #: Human-readable description shown in /status (e.g.
    #: ``"embedded (unix:///…/console-123.sock)"``).
    descriptor: str

    def run_turn(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        session_key: str,
        cancel: asyncio.Event,
    ) -> AsyncIterator[ConsoleEvent]: ...

    async def aclose(self) -> None: ...


@dataclass
class TurnStats:
    """Aggregates the session's usage for /usage and the status line."""

    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, done: TurnDone) -> None:
        self.turns += 1
        self.prompt_tokens += done.prompt_tokens
        self.completion_tokens += done.completion_tokens
        self.total_tokens += done.total_tokens


@dataclass
class BrainSession:
    """One console conversation: window + session key + model + stats."""

    brain: Brain
    model: str
    session_key: str = field(default_factory=new_session_key)
    window: list[dict[str, str]] = field(default_factory=list)
    stats: TurnStats = field(default_factory=TurnStats)
    system_prompt: str | None = None

    #: Optional compaction policy (see :mod:`…console.compaction`),
    #: attached by the app at construction. Kept *out* of ``send()`` on
    #: purpose: auto-compact must run before the user turn starts so its
    #: utility call never interleaves with the turn's event stream — the
    #: app calls ``compaction.maybe_auto_compact()`` at the top of
    #: ``run_turn`` instead.
    compactor: Compactor | None = None

    #: Set while a turn is streaming; ``cancel_turn()`` fires it.
    _cancel: asyncio.Event | None = field(default=None, repr=False)

    @property
    def busy(self) -> bool:
        return self._cancel is not None

    def cancel_turn(self) -> bool:
        """Interrupt the in-flight turn (Ctrl-C path). Returns whether
        there was one to cancel."""
        if self._cancel is None:
            return False
        self._cancel.set()
        return True

    def reset(self, *, session_key: str | None = None) -> None:
        """``/new`` — drop the window and start a fresh session key."""
        self.window.clear()
        self.session_key = session_key or new_session_key()

    async def send(
        self,
        text: str,
        *,
        model: str | None = None,
    ) -> AsyncIterator[ConsoleEvent]:
        """Run one user turn, re-yielding events while maintaining the
        window: the user message is appended up front, the assistant
        text is accumulated and committed on a clean ``TurnDone``.

        On error/cancel the user message is *kept* (hermes behaviour —
        the user can ``/retry`` or rephrase without retyping) but no
        empty assistant turn is recorded.
        """
        cancel = asyncio.Event()
        self._cancel = cancel
        turn_model = model or self.model

        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(self.window)
        messages.append({"role": "user", "content": text})

        reply_parts: list[str] = []
        try:
            async for ev in self.brain.run_turn(
                model=turn_model,
                messages=messages,
                session_key=self.session_key,
                cancel=cancel,
            ):
                if isinstance(ev, TextDelta):
                    reply_parts.append(ev.text)
                elif isinstance(ev, TurnDone):
                    self.window.append({"role": "user", "content": text})
                    reply = "".join(reply_parts)
                    if reply:
                        self.window.append({"role": "assistant", "content": reply})
                    self.stats.add(ev)
                elif isinstance(ev, TurnError):
                    self.window.append({"role": "user", "content": text})
                yield ev
        finally:
            self._cancel = None
