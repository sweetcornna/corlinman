"""Context compaction for the console window — claude-code ``/compact``.

The console window (:attr:`BrainSession.window`) grows without bound;
this module ports claude-code's compaction semantics:

* ``estimate_tokens`` — cheap chars/4 heuristic, no tokenizer import;
* :class:`Compactor` — policy object (threshold / protected tail /
  enabled flag) built from ``[console]`` config keys, with a circuit
  breaker so a broken summarizer can't burn a call on every turn;
* ``compact()`` — one utility-model turn that rewrites the window head
  into a summary briefing while keeping the recent tail verbatim;
* ``maybe_auto_compact()`` — the app-facing seam called at the top of
  ``ConsoleApp.run_turn`` *before* the user message is appended, so
  compaction never interleaves with a streaming turn.

Compaction deliberately runs through ``session.brain.run_turn`` (not
``session.send``): ``send`` maintains the window, and the summarization
turn must never be committed to it.

Config (``config.toml``)::

    [console]
    compact_threshold_tokens = 150000   # auto-compact above this estimate
    compact_keep_recent = 6             # tail messages never summarized
    auto_compact = true
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from corlinman_server.console.events import TextDelta, TurnError

if TYPE_CHECKING:
    from corlinman_server.console.brain import BrainSession

__all__ = [
    "CompactionResult",
    "Compactor",
    "estimate_tokens",
    "maybe_auto_compact",
]

#: claude-code /compact prompt, adapted: the summary is a briefing for
#: an agent resuming the conversation, not prose for a human.
_SUMMARY_PROMPT = (
    "Summarize this conversation compactly, preserving: user goals, "
    "decisions made, key facts/names/paths, unresolved questions. "
    "Write as a briefing for an agent resuming the conversation."
)

_SUMMARY_HEADER = "[Conversation summary — earlier turns were compacted]"
_SUMMARY_ACK = "Understood — continuing from the summary."

#: Consecutive compact failures before auto-compact trips off.
_BREAKER_LIMIT = 3


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    """Estimate the token count of a message window.

    Heuristic: total content characters divided by 4 (the classic
    ~4 chars/token English average). Deliberately cheap and
    tokenizer-free — compaction only needs an order-of-magnitude
    trigger, not an exact count.
    """
    return sum(len(m.get("content", "") or "") for m in messages) // 4


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Outcome of one compaction attempt."""

    ok: bool
    before_tokens: int
    after_tokens: int
    error: str = ""
    summary: str = ""

    @property
    def notice(self) -> str:
        """The one-line notice the renderer prints on success."""
        return f"⛁ context compacted: {self.before_tokens}→{self.after_tokens} est. tokens"


def _as_int(value: Any, default: int) -> int:
    """Lenient config coercion — bools and garbage fall back to default."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Compactor:
    """Per-session compaction policy + circuit-breaker state."""

    DEFAULT_THRESHOLD_TOKENS = 150_000
    DEFAULT_KEEP_RECENT = 6

    def __init__(
        self,
        *,
        threshold_tokens: int = DEFAULT_THRESHOLD_TOKENS,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        enabled: bool = True,
    ) -> None:
        self.threshold_tokens = threshold_tokens
        self.keep_recent = keep_recent
        self.enabled = enabled
        self._consecutive_failures = 0
        self._auto_disabled = False

    @classmethod
    def from_config(cls, config: dict) -> Compactor:
        """Build from a parsed ``config.toml`` dict (``[console]`` block) —
        same shape as :meth:`ModelRouter.from_config`."""
        console_cfg = config.get("console") if isinstance(config, dict) else None
        if not isinstance(console_cfg, dict):
            console_cfg = {}
        return cls(
            threshold_tokens=_as_int(
                console_cfg.get("compact_threshold_tokens"), cls.DEFAULT_THRESHOLD_TOKENS
            ),
            keep_recent=_as_int(console_cfg.get("compact_keep_recent"), cls.DEFAULT_KEEP_RECENT),
            enabled=bool(console_cfg.get("auto_compact", True)),
        )

    @property
    def auto_compact_disabled(self) -> bool:
        """Whether the circuit breaker has tripped (auto path only —
        manual ``/compact`` is always allowed)."""
        return self._auto_disabled

    async def maybe_auto_compact(
        self, session: BrainSession, *, model: str
    ) -> CompactionResult | None:
        """Auto trigger: compact iff enabled, breaker closed, and the
        window estimate exceeds the threshold. ``None`` = no attempt."""
        if not self.enabled or self._auto_disabled:
            return None
        if estimate_tokens(session.window) <= self.threshold_tokens:
            return None
        return await self.compact(session, model=model)

    async def compact(self, session: BrainSession, *, model: str) -> CompactionResult:
        """Summarize the window head, keeping the last ``keep_recent``
        messages verbatim.

        Runs ONE turn directly through ``session.brain.run_turn`` (never
        ``session.send`` — that would commit the summarization exchange
        to the window). On any failure the window is left untouched and
        the failure counts toward the circuit breaker; on success the
        window becomes ``[summary user msg, ack assistant msg] + tail``.
        """
        window = session.window
        before = estimate_tokens(window)
        keep = max(self.keep_recent, 0)
        split = max(len(window) - keep, 0)
        head = list(window[:split])
        tail = list(window[split:])
        if not head:
            # Nothing summarizable — a no-op, not a summarizer failure,
            # so it does not count toward the breaker.
            return CompactionResult(
                ok=False,
                before_tokens=before,
                after_tokens=before,
                error="nothing to compact (window fits in the protected tail)",
            )

        transcript = "\n\n".join(f"[{m.get('role', '?')}]\n{m.get('content', '')}" for m in head)
        prompt = f"{_SUMMARY_PROMPT}\n\n<conversation>\n{transcript}\n</conversation>"

        parts: list[str] = []
        failure: str | None = None
        try:
            async for ev in session.brain.run_turn(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                # A derived key keeps the summarization RPC out of the
                # user's journaled conversation (the servicer begins a
                # journal turn for every non-empty user_text under the
                # session key it is given).
                session_key=f"{session.session_key}:compact",
                cancel=asyncio.Event(),
            ):
                if isinstance(ev, TextDelta):
                    parts.append(ev.text)
                elif isinstance(ev, TurnError):
                    failure = ev.message or ev.reason
        except Exception as exc:  # noqa: BLE001 — a broken summarizer must not kill the REPL
            failure = str(exc) or type(exc).__name__

        summary = "".join(parts).strip()
        if failure is None and not summary:
            failure = "summarizer returned no text"
        if failure is not None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= _BREAKER_LIMIT:
                self._auto_disabled = True
            return CompactionResult(
                ok=False, before_tokens=before, after_tokens=before, error=failure
            )

        # Success: re-arm the breaker (a working summarizer + a shrunk
        # window invalidate the previous failure streak) and rewrite the
        # window in place so existing references stay valid.
        self._consecutive_failures = 0
        self._auto_disabled = False
        window[:] = [
            {"role": "user", "content": f"{_SUMMARY_HEADER}\n{summary}"},
            {"role": "assistant", "content": _SUMMARY_ACK},
            *tail,
        ]
        return CompactionResult(
            ok=True,
            before_tokens=before,
            after_tokens=estimate_tokens(window),
            summary=summary,
        )


async def maybe_auto_compact(session: BrainSession, model: str) -> CompactionResult | None:
    """App-facing seam for ``ConsoleApp.run_turn``: no-op when the
    session has no compactor attached (tests, bare constructions)."""
    compactor = session.compactor
    if compactor is None:
        return None
    return await compactor.maybe_auto_compact(session, model=model)
