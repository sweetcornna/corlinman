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
from collections.abc import Mapping
from dataclasses import dataclass
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
    if error or r in ("error", "failed", "failure", "rejected", "depth_capped"):
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
        # Per-child set of tool_call_ids already counted, so re-delivery of a
        # ``ToolStateRunning`` frame (fed once per open SSE client poll AND via
        # the emitter observer) increments ``tool_calls_made`` only once.
        self._counted_tool_calls: dict[str, set[str]] = {}
        # Monotonically increasing mutation counter — bumped on every genuine
        # row create / update / remove (see :meth:`revision`). The overview SSE
        # loop reads it as a cheap O(1) change probe instead of re-scanning all
        # rows every tick (Codex #113).
        self._revision: int = 0

    # ------------------------------------------------------------------
    # Cheap change signal (overview-loop probe)
    # ------------------------------------------------------------------

    @property
    def revision(self) -> int:
        """Monotonically increasing counter, bumped on every row mutation.

        The ``/admin/subagents/events/live`` loop probes this (an O(1) int
        read) each base tick to decide whether the expensive ``list_all()`` +
        merge full scan is worth running — a change bumps the revision, so the
        loop detects it within one probe tick without paying scan cost while
        idle. It is *not* a row count and carries no meaning beyond
        "something changed since you last looked"; only equality across two
        reads matters.
        """
        return self._revision

    def _bump(self) -> None:
        """Advance the mutation counter. Called from every row-writing path."""
        self._revision += 1

    # ------------------------------------------------------------------
    # Emitter hot-path hook
    # ------------------------------------------------------------------

    def observe(self, envelope: Any) -> None:
        """Update the registry from one ``SubagentSpawned/Event/Completed``
        dataclass envelope (single-process / in-gateway emit path). Best-effort:
        never raises (the emitter calls this on the hot path)."""
        try:
            event = getattr(envelope, "event", None)
            name = type(event).__name__ if event is not None else ""
            ts = _now_ms(envelope)
            if name == "SubagentSpawned":
                self._apply_spawned(
                    child_key=str(getattr(event, "child_session_key", "") or ""),
                    parent_key=str(getattr(event, "parent_session_key", "") or ""),
                    agent_id=str(getattr(event, "child_agent_id", "") or ""),
                    depth=int(getattr(event, "depth", 0) or 0),
                    prompt=str(getattr(event, "prompt_preview", "") or ""),
                    ts=ts,
                )
            elif name == "SubagentCompleted":
                self._apply_completed(
                    child_key=str(getattr(event, "child_session_key", "") or ""),
                    finish_reason=str(getattr(event, "finish_reason", "") or ""),
                    tool_calls=int(getattr(event, "tool_calls_made", 0) or 0),
                    elapsed_ms=int(getattr(event, "elapsed_ms", 0) or 0),
                    summary=str(getattr(event, "summary", "") or ""),
                    ts=ts,
                )
            elif name == "SubagentEvent":
                inner = getattr(getattr(event, "envelope", None), "event", None)
                self._apply_child_event(
                    child_key=str(getattr(event, "child_session_key", "") or ""),
                    inner_type=type(inner).__name__ if inner is not None else "",
                    tool_name=str(getattr(inner, "tool_name", "") or ""),
                    block_type=str(getattr(inner, "block_type", "") or ""),
                    tool_call_id=str(getattr(inner, "tool_call_id", "") or ""),
                )
        except Exception:  # noqa: BLE001 — bookkeeping must not break emit
            logger.debug("live_subagents.observe_failed", exc_info=True)

    def observe_journal_event(self, ev: Mapping[str, Any]) -> None:
        """Update the registry from one journal-row dict
        (``{event_type, payload, timestamp_ms, ...}``) — the CROSS-PROCESS
        path. In ``grpc_agent`` mode subagents run in the agent process, so
        their lifecycle events never hit the gateway emitter; the gateway's
        session-SSE journal poll feeds them here instead. Best-effort."""
        try:
            name = str(ev.get("event_type") or "")
            if name not in (
                "SubagentSpawned",
                "SubagentCompleted",
                "SubagentEvent",
            ):
                return
            payload = ev.get("payload")
            payload = payload if isinstance(payload, Mapping) else {}
            ts = int(ev.get("timestamp_ms") or 0)
            if name == "SubagentSpawned":
                self._apply_spawned(
                    child_key=str(payload.get("child_session_key") or ""),
                    parent_key=str(payload.get("parent_session_key") or ""),
                    agent_id=str(payload.get("child_agent_id") or ""),
                    depth=int(payload.get("depth") or 0),
                    prompt=str(payload.get("prompt_preview") or ""),
                    ts=ts,
                )
            elif name == "SubagentCompleted":
                self._apply_completed(
                    child_key=str(payload.get("child_session_key") or ""),
                    finish_reason=str(payload.get("finish_reason") or ""),
                    tool_calls=int(payload.get("tool_calls_made") or 0),
                    elapsed_ms=int(payload.get("elapsed_ms") or 0),
                    summary=str(payload.get("summary") or ""),
                    ts=ts,
                )
            elif name == "SubagentEvent":
                inner_env = payload.get("envelope")
                inner_env = inner_env if isinstance(inner_env, Mapping) else {}
                inner_payload = inner_env.get("payload")
                inner_payload = (
                    inner_payload if isinstance(inner_payload, Mapping) else {}
                )
                self._apply_child_event(
                    child_key=str(payload.get("child_session_key") or ""),
                    inner_type=str(inner_env.get("event_type") or ""),
                    tool_name=str(inner_payload.get("tool_name") or ""),
                    block_type=str(inner_payload.get("block_type") or ""),
                    tool_call_id=str(inner_payload.get("tool_call_id") or ""),
                )
        except Exception:  # noqa: BLE001 — bookkeeping must not break SSE
            logger.debug("live_subagents.observe_journal_failed", exc_info=True)

    # ------------------------------------------------------------------
    # Shared row updates (source-agnostic)
    # ------------------------------------------------------------------

    def _apply_spawned(
        self,
        *,
        child_key: str,
        parent_key: str,
        agent_id: str,
        depth: int,
        prompt: str,
        ts: int,
    ) -> None:
        if not child_key:
            return
        # Idempotent: a re-observed spawn (poll re-delivery) must not reset a
        # row that has already advanced to an active state with more info —
        # but only while the row is live. A TERMINAL row under the same key is
        # a previous incarnation (an agent-process restart reuses child keys
        # like ``parent::child::0``), so a fresh spawn replaces it instead of
        # being dropped.
        existing = self._rows.get(child_key)
        if existing is not None:
            if existing.state not in _TERMINAL_STATES:
                return
            self._rows.pop(child_key, None)
            self._counted_tool_calls.pop(child_key, None)
        self._rows[child_key] = LiveSubagentRow(
            request_id=child_key,
            parent_session_key=parent_key,
            subagent_type=agent_id or "subagent",
            state="running",
            description=prompt or None,
            started_at=ts or None,
            child_session_key=child_key,
            depth=depth,
            source="inline",
        )
        self._bump()  # a fresh/replaced row is a change

    def _apply_child_event(
        self,
        *,
        child_key: str,
        inner_type: str,
        tool_name: str,
        block_type: str,
        tool_call_id: str = "",
    ) -> None:
        row = self._rows.get(child_key)
        if row is None or row.state in _TERMINAL_STATES:
            return
        # Snapshot the mutable display fields so a re-delivered frame that
        # changes nothing (same activity, already-counted tool call) does NOT
        # bump the revision — only genuine changes do.
        before = (row.activity, row.tool_calls_made)
        # Codex-style current-activity line, derived only from coarse,
        # low-churn inner events (tool starts/stops, block boundaries) — never
        # per-token deltas.
        if inner_type == "ToolStateRunning":
            # Idempotent per tool call: the same frame is re-delivered by every
            # SSE-client poll (and the emitter), so count each ``tool_call_id``
            # once. A frame without an id (shouldn't happen) still counts, to
            # avoid under-reporting.
            if tool_call_id:
                counted = self._counted_tool_calls.setdefault(child_key, set())
                if tool_call_id not in counted:
                    counted.add(tool_call_id)
                    row.tool_calls_made += 1
            else:
                row.tool_calls_made += 1
            row.activity = f"运行工具 {tool_name}" if tool_name else "运行工具"
        elif inner_type == "ToolStateCompleted":
            row.activity = ""
        elif inner_type == "BlockStart":
            if block_type == "reasoning":
                row.activity = "思考中…"
            elif block_type == "text":
                row.activity = "撰写回复…"
        if (row.activity, row.tool_calls_made) != before:
            self._bump()

    def _apply_completed(
        self,
        *,
        child_key: str,
        finish_reason: str,
        tool_calls: int,
        elapsed_ms: int,
        summary: str,
        ts: int,
    ) -> None:
        if not child_key:
            return
        row = self._rows.get(child_key)
        if row is None:
            # Completed without a spawn we saw (registry started mid-run):
            # synthesize a minimal terminal row so the panel still shows it.
            row = LiveSubagentRow(
                request_id=child_key,
                parent_session_key="",
                subagent_type="subagent",
                state="running",
                child_session_key=child_key,
                source="inline",
            )
            self._rows[child_key] = row
        elif row.state in _TERMINAL_STATES:
            return  # already terminal — ignore poll re-delivery
        row.finish_reason = finish_reason or None
        row.state = _map_finish_reason(row.finish_reason, None)
        row.finished_at = ts or None
        row.tool_calls_made = tool_calls or row.tool_calls_made
        row.elapsed_ms = elapsed_ms or row.elapsed_ms
        row.summary = summary
        row.activity = ""
        self._bump()  # running → terminal transition is a change
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
        if excess <= 0:
            return
        for k in terminal_keys[:excess]:
            self._rows.pop(k, None)
            self._counted_tool_calls.pop(k, None)
        self._bump()  # dropping a row is also a change


__all__ = ["LiveSubagentRegistry", "LiveSubagentRow"]
