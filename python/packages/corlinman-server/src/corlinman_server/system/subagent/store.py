"""Persistent + in-memory tracker for background ``subagent_spawn`` requests.

W1.3 of ``docs/PLAN_MULTI_AGENT.md`` §2 Wave 1/W1.3.

Design mirrors :class:`corlinman_server.system.upgrader.state.UpgradeStateStore`
field-for-field — same atomic JSON persistence (``$DATA_DIR/.subagent-state.json``),
same asyncio.Lock-guarded mutations, same return-a-copy semantics on read.
The only material difference is the row schema: ``UpgradeStatus`` tracks a
single one-click upgrade, this one tracks N parallel background subagent
dispatches per parent session.

Key contract notes
------------------

* ``begin`` returns a deep-copied snapshot — callers may mutate it without
  bleeding back into the in-memory store.
* ``update`` partial-updates and re-flushes; ``KeyError`` on unknown
  request_id matches the upgrade store.
* ``set_killed`` is a convenience over ``update`` that stamps the
  operator (``by``) into ``finish_reason`` for audit alignment.
* ``log_tail`` rolls at 4 kB; same UTF-8-safe right-trim as
  :class:`UpgradeStateStore`.
* ``summary`` is bounded at 4 kB (the synthetic notification truncates to
  3500 chars on the way out — see :mod:`dispatcher` — but we leave the
  store field one buffer larger so a future enlargement doesn't need a
  re-migration).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)


__all__ = [
    "SubagentRequest",
    "SubagentState",
    "SubagentStatus",
    "SubagentTaskStore",
]


# Rolling tail / summary buffer — 4 kB matches the upgrade store's
# ``log_excerpt`` budget so the operator-facing UIs render consistently
# when reading state from disk on a cold start.
_LOG_TAIL_MAX_BYTES: int = 4 * 1024
_SUMMARY_MAX_BYTES: int = 4 * 1024

# Bounded retention of terminal rows. In-flight rows are kept regardless
# (they are naturally capped by the dispatcher's per-tenant quota); only
# completed/failed/timeout/killed rows accrete, so we evict the oldest
# (by ``finished_at``) beyond this window. This keeps ``_statuses`` /
# ``_requests`` — and therefore the on-disk flush — bounded instead of
# growing O(N) over the gateway's lifetime, while leaving a recent
# history deep enough for the operator UI / notification replay.
_TERMINAL_RETENTION_CAP: int = 512


SubagentState = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "timeout",
    "killed",
    "stalled",
]
"""Terminal vs in-flight discriminator for :class:`SubagentStatus`."""


# D3 — ``stalled`` is TERMINAL. An orphaned background row (one persisted as
# queued/running whose driving task died with the process) is resolved to
# ``stalled`` on the next boot; it must then be prune-eligible and must NOT
# count toward the per-tenant in-flight quota — otherwise orphans accrete
# until the 15-slot cap rejects every future background spawn from that
# tenant. Previously ``stalled`` lived in ``_IN_FLIGHT_STATES`` and nothing
# ever set it, so the recovery path was inert and the wedge was permanent.
_TERMINAL_STATES: frozenset[str] = frozenset(
    {"succeeded", "failed", "timeout", "killed", "stalled"}
)
_IN_FLIGHT_STATES: frozenset[str] = frozenset({"queued", "running"})


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SubagentRequest:
    """Immutable record of a background ``subagent_spawn`` request.

    Mirrors :class:`corlinman_server.system.upgrader.state.UpgradeRequest`
    in role: written once at :meth:`SubagentTaskStore.begin`, never
    mutated. The lifecycle data lives on :class:`SubagentStatus`.
    """

    request_id: str
    parent_session_key: str
    parent_agent_id: str
    subagent_type: str
    goal: str
    description: str | None
    requested_at: int  # epoch ms
    requested_by: str | None
    #: Tenant the request belongs to. Used by the dispatcher's per-tenant
    #: quota check (see :class:`AsyncSubagentDispatcher`) so a noisy
    #: tenant cannot starve other tenants at the surface refusal layer.
    #: Defaults to ``"default"`` so callers in single-tenant deployments
    #: and persisted rows from before this field existed keep working.
    tenant_id: str = "default"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SubagentStatus:
    """Mutable status row tracked across the subagent lifecycle.

    Field naming matches the wire shape the admin UI consumes; the store
    serialises this dataclass directly via :func:`dataclasses.asdict`.
    """

    request_id: str
    parent_session_key: str
    subagent_type: str
    description: str | None
    state: SubagentState
    started_at: int | None = None
    finished_at: int | None = None
    child_session_key: str | None = None
    finish_reason: str | None = None
    tool_calls_made: int = 0
    elapsed_ms: int = 0
    error: str | None = None
    summary: str = ""
    log_tail: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    def is_in_flight(self) -> bool:
        return self.state in _IN_FLIGHT_STATES


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SubagentTaskStore:
    """In-memory + atomically-persisted store of background subagent rows.

    The persistence shape is a JSON object::

        {
          "requests": {"<id>": SubagentRequest.to_json(), ...},
          "statuses": {"<id>": SubagentStatus.to_json(), ...}
        }

    flushed atomically to ``persist_path`` after every mutation (tmp +
    :func:`os.replace`). Cold-start hydration is best-effort — a missing
    file is the empty case; a corrupted file logs at WARN and starts
    empty rather than crashing the gateway boot.
    """

    #: Max number of terminal rows kept in memory + on disk. Exposed as a
    #: class attribute so tests (and a future config knob) can reference
    #: it without importing the module-level constant.
    _TERMINAL_RETENTION_CAP: int = _TERMINAL_RETENTION_CAP

    def __init__(self, persist_path: Path) -> None:
        self._persist_path = persist_path
        self._lock = asyncio.Lock()
        self._requests: dict[str, SubagentRequest] = {}
        self._statuses: dict[str, SubagentStatus] = {}
        self._load_from_disk()
        # D3 — a cold-started dispatcher has an EMPTY in-process task map, so
        # any row hydrated as queued/running is orphaned (no live task is
        # driving it). Resolve those to a terminal ``stalled`` state up front
        # so they become prune-eligible and stop consuming per-tenant quota
        # forever. Runs before the prune so freshly-stalled rows are included
        # in the retention bound.
        reconciled = self._reconcile_orphaned_in_flight_locked()
        # A cold-started store may hydrate an unbounded backlog of terminal
        # rows from a pre-retention on-disk file; trim it down on boot.
        self._prune_terminal_locked()
        # Persist the cleaned state so the resolution is durable and the disk
        # file stops re-hydrating orphans / pruned rows on every restart.
        if reconciled:
            logger.info("subagent_state.reconciled_orphans", count=reconciled)
            self._flush_locked()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> None:
        try:
            raw = self._persist_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning(
                "subagent_state.load_failed",
                path=str(self._persist_path),
                error=str(exc),
            )
            return
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            logger.warning(
                "subagent_state.parse_failed",
                path=str(self._persist_path),
                error=str(exc),
            )
            return
        if not isinstance(payload, dict):
            return
        requests = payload.get("requests") or {}
        statuses = payload.get("statuses") or {}
        if isinstance(requests, dict):
            for rid, raw_req in requests.items():
                if not isinstance(raw_req, dict):
                    continue
                try:
                    self._requests[rid] = SubagentRequest(
                        request_id=str(raw_req["request_id"]),
                        parent_session_key=str(raw_req["parent_session_key"]),
                        parent_agent_id=str(raw_req["parent_agent_id"]),
                        subagent_type=str(raw_req["subagent_type"]),
                        goal=str(raw_req["goal"]),
                        description=raw_req.get("description"),
                        requested_at=int(raw_req["requested_at"]),
                        requested_by=raw_req.get("requested_by"),
                        tenant_id=str(raw_req.get("tenant_id") or "default"),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        if isinstance(statuses, dict):
            for rid, raw_status in statuses.items():
                if not isinstance(raw_status, dict):
                    continue
                try:
                    self._statuses[rid] = SubagentStatus(
                        request_id=str(raw_status["request_id"]),
                        parent_session_key=str(
                            raw_status["parent_session_key"]
                        ),
                        subagent_type=str(raw_status["subagent_type"]),
                        description=raw_status.get("description"),
                        state=raw_status["state"],
                        started_at=raw_status.get("started_at"),
                        finished_at=raw_status.get("finished_at"),
                        child_session_key=raw_status.get("child_session_key"),
                        finish_reason=raw_status.get("finish_reason"),
                        tool_calls_made=int(
                            raw_status.get("tool_calls_made") or 0
                        ),
                        elapsed_ms=int(raw_status.get("elapsed_ms") or 0),
                        error=raw_status.get("error"),
                        summary=str(raw_status.get("summary") or ""),
                        log_tail=str(raw_status.get("log_tail") or ""),
                    )
                except (KeyError, TypeError, ValueError):
                    continue

    def _reconcile_orphaned_in_flight_locked(self) -> int:
        """D3 — resolve rows hydrated as queued/running on a cold start.

        A background subagent task lives only in the dispatcher's in-process
        ``_tasks`` map; on a gateway restart that map starts empty, so any row
        persisted as ``queued`` / ``running`` has no live task driving it — it
        is orphaned. Left alone it stays in-flight forever and is counted by
        :meth:`count_in_flight_for_tenant`, permanently consuming the tenant's
        quota until the per-tenant cap rejects every future background spawn.
        Mark each orphan terminal (``stalled``) so it is prune-eligible and
        stops counting.

        Caller MUST hold ``self._lock`` (or be in ``__init__`` before the
        store is published). Returns the number of rows reconciled.
        """
        now = _now_ms()
        reconciled = 0
        for status in self._statuses.values():
            if status.state in ("queued", "running"):
                status.state = "stalled"
                if status.finished_at is None:
                    status.finished_at = now
                if status.finish_reason is None:
                    status.finish_reason = "stalled_on_restart"
                reconciled += 1
        return reconciled

    def _prune_terminal_locked(self) -> None:
        """Evict oldest terminal rows beyond the retention cap.

        Caller MUST hold ``self._lock`` (or be in ``__init__`` before the
        store is published). In-flight rows are never evicted — they are
        bounded by the dispatcher's per-tenant quota and dropping one would
        lose live state. Terminal rows are ordered by ``finished_at`` (ties
        broken by request_id for determinism) and the newest
        ``_TERMINAL_RETENTION_CAP`` are kept. Evicting a status also drops
        its paired request row so the two dicts stay in lockstep.
        """
        terminal_ids = [
            rid
            for rid, status in self._statuses.items()
            if status.is_terminal()
        ]
        if len(terminal_ids) <= self._TERMINAL_RETENTION_CAP:
            return
        # Oldest first; rows missing finished_at sort before timestamped
        # ones (treated as oldest) so they are dropped first.
        terminal_ids.sort(
            key=lambda rid: (
                self._statuses[rid].finished_at or 0,
                rid,
            )
        )
        evict_count = len(terminal_ids) - self._TERMINAL_RETENTION_CAP
        for rid in terminal_ids[:evict_count]:
            self._statuses.pop(rid, None)
            self._requests.pop(rid, None)

    def _flush_locked(self) -> None:
        """Atomically persist current state. Caller MUST hold ``self._lock``."""
        payload = {
            "requests": {
                rid: req.to_json() for rid, req in self._requests.items()
            },
            "statuses": {
                rid: status.to_json() for rid, status in self._statuses.items()
            },
        }
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(
                self._persist_path.suffix + ".tmp"
            )
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._persist_path)
        except OSError as exc:
            logger.warning(
                "subagent_state.flush_failed",
                path=str(self._persist_path),
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def begin(self, req: SubagentRequest) -> SubagentStatus:
        """Record ``req`` + seed its status as ``queued``.

        Returns a snapshot copy — callers may mutate freely.
        """
        status = SubagentStatus(
            request_id=req.request_id,
            parent_session_key=req.parent_session_key,
            subagent_type=req.subagent_type,
            description=req.description,
            state="queued",
        )
        async with self._lock:
            self._requests[req.request_id] = req
            self._statuses[req.request_id] = status
            self._flush_locked()
            return SubagentStatus(**asdict(status))

    async def update(
        self, request_id: str, **fields: Any
    ) -> SubagentStatus:
        """Partial-update a status row. Raises ``KeyError`` on unknown id."""
        async with self._lock:
            current = self._statuses.get(request_id)
            if current is None:
                raise KeyError(request_id)
            for key, value in fields.items():
                if not hasattr(current, key):
                    raise AttributeError(
                        f"SubagentStatus has no field {key!r}"
                    )
                setattr(current, key, value)
            # Snapshot before pruning: the just-updated row has the
            # freshest ``finished_at`` so it is never the one evicted, but
            # we return the copy regardless of whether it stays retained.
            snapshot = SubagentStatus(**asdict(current))
            self._prune_terminal_locked()
            self._flush_locked()
            return snapshot

    async def get(self, request_id: str) -> SubagentStatus | None:
        async with self._lock:
            current = self._statuses.get(request_id)
            if current is None:
                return None
            return SubagentStatus(**asdict(current))

    async def get_request(
        self, request_id: str
    ) -> SubagentRequest | None:
        async with self._lock:
            return self._requests.get(request_id)

    async def current_in_flight(
        self, *, parent_session_key: str | None = None
    ) -> list[SubagentStatus]:
        """Return every status row currently in flight.

        With ``parent_session_key`` provided, scopes to that parent.
        Returns a list (not a single row) — unlike the upgrader's
        one-at-a-time contract, subagents fan out N-at-once under the
        supervisor's per-tenant cap.
        """
        async with self._lock:
            out: list[SubagentStatus] = []
            for status in self._statuses.values():
                if not status.is_in_flight():
                    continue
                if (
                    parent_session_key is not None
                    and status.parent_session_key != parent_session_key
                ):
                    continue
                out.append(SubagentStatus(**asdict(status)))
            return out

    async def list_active(self) -> list[SubagentStatus]:
        """Alias of :meth:`current_in_flight` without scoping.

        Matches the verb the route handler uses for readability.
        """
        return await self.current_in_flight()

    async def list_all(self) -> list[SubagentStatus]:
        """Return every status row (terminal + in-flight) as snapshots."""
        async with self._lock:
            return [
                SubagentStatus(**asdict(status))
                for status in self._statuses.values()
            ]

    async def count_in_flight_for_tenant(self, tenant_id: str) -> int:
        """Return the count of in-flight statuses owned by ``tenant_id``.

        Used by :class:`AsyncSubagentDispatcher` for its per-tenant
        surface refusal so a noisy tenant can't starve other tenants
        (R3-004 — :class:`SubagentStatus` itself doesn't carry the
        tenant; we cross-reference the :class:`SubagentRequest` row
        which does).
        """
        async with self._lock:
            count = 0
            for rid, status in self._statuses.items():
                if not status.is_in_flight():
                    continue
                req = self._requests.get(rid)
                if req is not None and req.tenant_id == tenant_id:
                    count += 1
            return count

    async def append_log(self, request_id: str, chunk: str) -> None:
        """Append ``chunk`` to ``log_tail``, trimming to the last 4 kB.

        No-op for unknown ``request_id`` (background tasks may race with
        cleanup).
        """
        if not chunk:
            return
        async with self._lock:
            current = self._statuses.get(request_id)
            if current is None:
                return
            combined = current.log_tail + chunk
            if len(combined.encode("utf-8")) > _LOG_TAIL_MAX_BYTES:
                encoded = combined.encode("utf-8")[-_LOG_TAIL_MAX_BYTES:]
                combined = encoded.decode("utf-8", errors="ignore")
            current.log_tail = combined
            self._flush_locked()

    async def set_killed(
        self, request_id: str, by: str | None
    ) -> SubagentStatus | None:
        """Flip the row to ``killed`` and stamp the operator into
        ``finish_reason``. No-op (returns ``None``) when the row is
        already terminal or unknown — the caller maps that to the 409
        the kill route surfaces.
        """
        async with self._lock:
            current = self._statuses.get(request_id)
            if current is None:
                return None
            if current.is_terminal():
                return None
            current.state = "killed"
            current.finished_at = _now_ms()
            current.finish_reason = (
                f"killed_by:{by}" if by else "killed"
            )
            snapshot = SubagentStatus(**asdict(current))
            self._prune_terminal_locked()
            self._flush_locked()
            return snapshot

    async def set_summary(self, request_id: str, summary: str) -> None:
        """Persist a bounded ``summary`` blob on the row.

        Truncates at 4 kB. No-op for unknown ids (same race-tolerance
        contract as :meth:`append_log`).
        """
        if summary is None:
            return
        async with self._lock:
            current = self._statuses.get(request_id)
            if current is None:
                return
            if len(summary.encode("utf-8")) > _SUMMARY_MAX_BYTES:
                encoded = summary.encode("utf-8")[:_SUMMARY_MAX_BYTES]
                summary = encoded.decode("utf-8", errors="ignore")
            current.summary = summary
            self._flush_locked()
