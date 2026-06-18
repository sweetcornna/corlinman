"""``LiveSubagentRegistry`` — process-global live view of INLINE subagents.

Background subagents (``subagent_spawn`` dispatched async) get a durable
:class:`~corlinman_server.system.subagent.SubagentStatus` row in the
``SubagentTaskStore``, which the ``/admin/subagents`` overview panel reads.
INLINE subagents — the ones a turn spawns via ``subagent_spawn`` /
``subagent_spawn_inline`` and awaits in-process — never touched that store, so
the global panel was empty during exactly the multi-agent runs an operator
wants to watch.

This registry closes that gap WITHOUT touching the agent hot path. Every
subagent lifecycle event already flows through the one funnel
:class:`~corlinman_server.gateway.observability.emitter.JournalBackedEmitter`
``emit()`` — ``SubagentSpawned`` / ``SubagentEvent`` / ``SubagentCompleted``.
The emitter calls :meth:`LiveSubagentRegistry.observe` for those three (and
only those three) envelopes; the registry maintains an in-memory row per child
session key. The ``/admin/subagents`` route then MERGES these rows with the
background store rows so both kinds of agent appear in one live panel.

In-memory only by design: inline subagents are ephemeral and high-frequency;
persisting each to disk would be churn for no benefit (a gateway restart kills
every in-flight inline child anyway). Terminal rows are capped so a long-lived
gateway can't leak memory under heavy fan-out.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Keep at most this many terminal (finished) inline rows so the panel can show
#: recent history without unbounded growth. Active rows are never capped.
_TERMINAL_RETENTION_CAP: int = 200

_TERMINAL_STATES: frozenset[str] = frozenset(
    {"succeeded", "failed", "timeout", "killed"}
)


@dataclass(slots=True)
class LiveSubagentRow:
    """Wire-compatible mirror of ``SubagentStatus`` plus the Codex-Desktop
    extras (``depth`` for supervisor→worker nesting, ``activity`` for the live
    current-action line, ``source`` to distinguish inline vs background)."""

    request_id: str
    parent_session_key: str
    subagent_type: str
    state: str
    description: str | None = None
    started_at: int | None = None
    finished_at: int | None = None
    child_session_key: str | None = None
    finish_reason: str | None = None
    tool_calls_made: int = 0
    elapsed_ms: int = 0
    error: str | None = None
    summary: str = ""
    log_tail: str = ""
    depth: int = 0
    activity: str = ""
    source: str = "inline"

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "parent_session_key": self.parent_session_key,
            "subagent_type": self.subagent_type,
            "description": self.description,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "child_session_key": self.child_session_key,
            "finish_reason": self.finish_reason,
            "tool_calls_made": self.tool_calls_made,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "summary": self.summary,
            "log_tail": self.log_tail,
            "depth": self.depth,
            "activity": self.activity,
            "source": self.source,
        }


def _now_ms(envelope: Any) -> int:
    ts = getattr(envelope, "timestamp_ms", None)
    return int(ts) if isinstance(ts, int) else 0


def _map_finish_reason(reason: str | None, error: str | None) -> str:
    """Map a ``SubagentCompleted.finish_reason`` to a panel state.

    Mirrors the ``SubagentTaskStore`` state vocabulary so inline and
    background rows present identically.
    """
    r = (reason or "").strip().lower()
    if r in ("timeout", "timed_out"):
        return "timeout"
    if r in ("killed", "cancelled", "canceled", "aborted"):
        return "killed"
    if error or r in ("error", "failed", "failure"):
        return "failed"
    return "succeeded"


class LiveSubagentRegistry:
    """In-memory map of ``child_session_key`` → :class:`LiveSubagentRow`.

    Updated synchronously from :meth:`observe` (called on the emitter hot
    path, so every method is non-blocking and swallows its own errors — a
    bookkeeping slip must never deny the agent its event). Read via
    :meth:`list_active` / :meth:`list_all` by the admin route, which copies
    so callers never see the live dict mutate mid-iteration.
    """

    def __init__(self, *, terminal_cap: int = _TERMINAL_RETENTION_CAP) -> None:
        # OrderedDict so terminal-row pruning is insertion-ordered (oldest
        # finished rows drop first).
        self._rows: OrderedDict[str, LiveSubagentRow] = OrderedDict()
        self._terminal_cap = max(1, terminal_cap)

    # ------------------------------------------------------------------
    # Emitter hot-path hook
    # ------------------------------------------------------------------

    def observe(self, envelope: Any) -> None:
        """Update the registry from one ``SubagentSpawned/Event/Completed``
        envelope. Best-effort: never raises (the emitter calls this on the
        hot path)."""
        try:
            event = getattr(envelope, "event", None)
            name = type(event).__name__ if event is not None else ""
            if name == "SubagentSpawned":
                self._on_spawned(envelope, event)
            elif name == "SubagentCompleted":
                self._on_completed(envelope, event)
            elif name == "SubagentEvent":
                self._on_child_event(event)
        except Exception:  # noqa: BLE001 — bookkeeping must not break emit
            logger.debug("live_subagents.observe_failed", exc_info=True)

    def _on_spawned(self, envelope: Any, event: Any) -> None:
        key = str(getattr(event, "child_session_key", "") or "")
        if not key:
            return
        prompt = str(getattr(event, "prompt_preview", "") or "")
        row = LiveSubagentRow(
            request_id=key,
            parent_session_key=str(getattr(event, "parent_session_key", "") or ""),
            subagent_type=str(getattr(event, "child_agent_id", "") or "subagent"),
            state="running",
            description=prompt or None,
            started_at=_now_ms(envelope),
            child_session_key=key,
            depth=int(getattr(event, "depth", 0) or 0),
            source="inline",
        )
        # Re-spawn under the same key (shouldn't happen) replaces the row and
        # moves it to the end so it counts as freshly active.
        self._rows.pop(key, None)
        self._rows[key] = row

    def _on_child_event(self, event: Any) -> None:
        key = str(getattr(event, "child_session_key", "") or "")
        row = self._rows.get(key)
        if row is None or row.state in _TERMINAL_STATES:
            return
        inner = getattr(event, "envelope", None)
        inner_event = getattr(inner, "event", None)
        iname = type(inner_event).__name__ if inner_event is not None else ""
        # Codex-style current-activity line, derived only from coarse,
        # low-churn inner events (tool starts/stops, block/turn boundaries) —
        # never per-token deltas.
        if iname == "ToolStateRunning":
            tool = str(getattr(inner_event, "tool_name", "") or "")
            row.tool_calls_made += 1
            row.activity = f"运行工具 {tool}" if tool else "运行工具"
        elif iname == "ToolStateCompleted":
            row.activity = ""
        elif iname == "BlockStart":
            block = str(getattr(inner_event, "block_type", "") or "")
            if block == "reasoning":
                row.activity = "思考中…"
            elif block == "text":
                row.activity = "撰写回复…"

    def _on_completed(self, envelope: Any, event: Any) -> None:
        key = str(getattr(event, "child_session_key", "") or "")
        row = self._rows.get(key)
        if row is None:
            # Completed without a spawn we saw (e.g. registry started mid-run);
            # synthesize a minimal terminal row so the panel still shows it.
            row = LiveSubagentRow(
                request_id=key or "unknown",
                parent_session_key="",
                subagent_type="subagent",
                state="running",
                child_session_key=key or None,
                source="inline",
            )
            if key:
                self._rows[key] = row
        summary = str(getattr(event, "summary", "") or "")
        row.finish_reason = str(getattr(event, "finish_reason", "") or "") or None
        row.error = None
        row.state = _map_finish_reason(row.finish_reason, row.error)
        row.finished_at = _now_ms(envelope)
        row.tool_calls_made = int(getattr(event, "tool_calls_made", row.tool_calls_made) or 0)
        row.elapsed_ms = int(getattr(event, "elapsed_ms", row.elapsed_ms) or 0)
        row.summary = summary
        row.activity = ""
        self._prune_terminal()

    # ------------------------------------------------------------------
    # Reads (admin route)
    # ------------------------------------------------------------------

    def list_all(self) -> list[LiveSubagentRow]:
        return list(self._rows.values())

    def list_active(self) -> list[LiveSubagentRow]:
        return [r for r in self._rows.values() if r.state not in _TERMINAL_STATES]

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def _prune_terminal(self) -> None:
        terminal_keys = [
            k for k, r in self._rows.items() if r.state in _TERMINAL_STATES
        ]
        excess = len(terminal_keys) - self._terminal_cap
        for k in terminal_keys[:excess] if excess > 0 else []:
            self._rows.pop(k, None)


__all__ = ["LiveSubagentRegistry", "LiveSubagentRow"]
