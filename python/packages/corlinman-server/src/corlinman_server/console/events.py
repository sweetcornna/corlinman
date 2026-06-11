"""Console event model — one stream shape for every brain backend.

The embedded brain yields :class:`corlinman_server.gateway_api`
``InternalChatEvent`` variants; the attach brain parses OpenAI-shaped SSE
chunks. Both are normalised into the small sum type below so the renderer
(:mod:`corlinman_server.console.render`) and the REPL
(:mod:`corlinman_server.console.app`) never branch on the transport.

Mirrors the claude-code pattern of a single event enum consumed by every
renderer (``StreamEvent`` in ``query.ts``) and opencode's schema-first
``EventMessagePart*`` family.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ConsoleEvent",
    "ReasoningDelta",
    "TextDelta",
    "ToolFinished",
    "ToolStarted",
    "TurnDone",
    "TurnError",
    "args_preview",
    "from_internal_events",
]


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A streamed fragment of the assistant's visible answer."""

    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    """A streamed fragment of extended-thinking output (hidden unless
    the user opted into verbose display)."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolStarted:
    """The brain dispatched a tool call (builtin observation or plugin)."""

    tool: str
    plugin: str = ""
    call_id: str = ""
    args_json: bytes = b""


@dataclass(frozen=True, slots=True)
class ToolFinished:
    """A previously-started tool call completed."""

    tool: str
    plugin: str = ""
    call_id: str = ""
    duration_ms: int = 0
    is_error: bool = False
    error_summary: str = ""


@dataclass(frozen=True, slots=True)
class TurnDone:
    """Terminal event — the turn finished cleanly."""

    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class TurnError:
    """Terminal event — the turn failed (provider error, cancel, …).

    ``reason`` is the lowercase failover discriminant
    (``"billing"`` / ``"rate_limit"`` / ``"unknown"`` / …).
    """

    reason: str = "unknown"
    message: str = ""

    @property
    def is_cancelled(self) -> bool:
        return self.message == "cancelled"


ConsoleEvent = (
    TextDelta
    | ReasoningDelta
    | ToolStarted
    | ToolFinished
    | TurnDone
    | TurnError
)


def args_preview(args_json: bytes | str, limit: int = 80) -> str:
    """One-line human preview of a tool call's argument JSON.

    Picks the most informative scalar (``command`` / ``path`` / ``query`` /
    ``goal`` / first string value) instead of dumping the whole object —
    the hermes-agent "cute tool message" idea, minus the cuteness.
    """
    raw = args_json.decode("utf-8", "replace") if isinstance(args_json, bytes) else args_json
    try:
        obj = json.loads(raw) if raw else {}
    except ValueError:
        obj = {}
    text = ""
    if isinstance(obj, dict):
        for key in ("command", "path", "file_path", "query", "goal", "url", "name"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                text = val.strip()
                break
        else:
            for val in obj.values():
                if isinstance(val, str) and val.strip():
                    text = val.strip()
                    break
    if not text:
        text = raw.strip()
    text = " ".join(text.split())
    return text[: limit - 1] + "…" if len(text) > limit else text


@dataclass
class _ToolBook:
    """Pairs ``ToolStarted`` and ``ToolFinished`` by call_id so renderers
    can show elapsed names even when the servicer's done-observation
    arrives without one (defensive against older agents)."""

    open_calls: dict[str, str] = field(default_factory=dict)


def from_internal_events(
    stream: AsyncIterator[Any],
) -> AsyncIterator[ConsoleEvent]:
    """Normalise a ``ChatService.run(...)`` event stream into
    :data:`ConsoleEvent` items.

    Import of the gateway_api event classes is deferred to call time so
    this module stays importable in stripped-down builds (attach-only
    consoles don't need the gateway plane).
    """

    async def _gen() -> AsyncIterator[ConsoleEvent]:
        from corlinman_server.gateway_api import (  # noqa: PLC0415 — lazy by design
            DoneEvent,
            ErrorEvent,
            TokenDeltaEvent,
            ToolCallEvent,
            ToolResultEvent,
        )

        book = _ToolBook()
        async for ev in stream:
            if isinstance(ev, TokenDeltaEvent):
                if getattr(ev, "is_reasoning", False):
                    yield ReasoningDelta(text=ev.text)
                else:
                    yield TextDelta(text=ev.text)
            elif isinstance(ev, ToolCallEvent):
                book.open_calls[ev.call_id] = ev.tool
                yield ToolStarted(
                    tool=ev.tool,
                    plugin=ev.plugin,
                    call_id=ev.call_id,
                    args_json=bytes(ev.args_json),
                )
            elif isinstance(ev, ToolResultEvent):
                tool = ev.tool or book.open_calls.pop(ev.call_id, "")
                yield ToolFinished(
                    tool=tool,
                    plugin=ev.plugin,
                    call_id=ev.call_id,
                    duration_ms=ev.duration_ms,
                    is_error=ev.is_error,
                    error_summary=ev.error_summary,
                )
            elif isinstance(ev, DoneEvent):
                usage = ev.usage
                yield TurnDone(
                    finish_reason=ev.finish_reason,
                    prompt_tokens=usage.prompt_tokens if usage else 0,
                    completion_tokens=usage.completion_tokens if usage else 0,
                    total_tokens=usage.total_tokens if usage else 0,
                )
                return
            elif isinstance(ev, ErrorEvent):
                yield TurnError(reason=ev.error.reason, message=ev.error.message)
                return
        # Stream ended without a terminal event — synthesise one so the
        # REPL never hangs (same defensive rule as ChatService itself).
        yield TurnDone(finish_reason="stop")

    return _gen()
