"""``/admin/subagents*`` — live activity surface for background subagents.

W1.3 of ``docs/PLAN_MULTI_AGENT.md`` §2 Wave 1/W1.3.

Five endpoints, all admin-gated:

* ``GET  /admin/subagents`` — list active (or all) subagent rows.
* ``GET  /admin/subagents/{id}/status`` — single-row poll.
* ``GET  /admin/subagents/{id}/events`` — per-child SSE event stream
  scoped to the spawned child's session key. Re-uses the gateway's
  shared :class:`JournalBackedEmitter` — the supervisor's
  :class:`BubbleEmitter` already bubbles child events into the parent's
  stream tagged with the child's session key, and we subscribe to that
  child key here.
* ``GET  /admin/subagents/events/live`` — global SSE feed of
  :class:`SubagentSpawned` / :class:`SubagentCompleted` events for the
  /admin/subagents overview panel.
* ``POST /admin/subagents/{id}/kill`` — operator kill switch.

Pattern mirrors :mod:`corlinman_server.gateway.routes_admin_b.infra.system`'s
``/admin/system/upgrade/{id}/events`` route: 10s SSE heartbeat,
terminal-state stream closure, typed JSON error envelopes.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from corlinman_server.gateway.routes_admin_b.infra._subagents_lib import (
    _SUBAGENT_SSE_HEARTBEAT_SECONDS,
    SubagentListResponse,
    SubagentStatusResponse,
    _error,
    _live_row_to_response,
    _merged_rows,
    _resolve_actor,
    _resolve_dispatcher,
    _resolve_event_emitter,
    _resolve_live_registry,
    _resolve_store,
    _status_to_response,
)
from corlinman_server.gateway.routes_admin_b.state import (
    get_admin_state,
    require_admin,
)
from corlinman_server.system.subagent import (
    TenantQuotaExceeded,
)

__all__ = ["router"]


# ---------------------------------------------------------------------------
# Adaptive overview-poll cadence
# ---------------------------------------------------------------------------

#: The ``/admin/subagents/events/live`` overview poll starts fast for
#: liveness, then backs off while nothing is moving. Base = the snappy
#: cadence a *changing* panel wants; max = the slow idle cadence (same as
#: the SSE heartbeat, so a fully-idle stream settles into the heartbeat
#: rhythm); factor = the per-idle-tick multiplier.
_SUBAGENT_OVERVIEW_POLL_BASE_SECONDS = 2.0
_SUBAGENT_OVERVIEW_POLL_MAX_SECONDS = 10.0
_SUBAGENT_OVERVIEW_POLL_BACKOFF = 1.5


def _next_poll_interval(changed: bool, current: float) -> float:
    """Next overview-poll interval given whether this tick saw a change.

    Liveness only matters while the panel is *moving*: a child switching
    tools should surface in ~2s. But an idle panel that re-runs
    ``store.list_all()`` + merge every 2s for every connected client is
    pure overhead. So each fully-unchanged tick multiplies the interval by
    :data:`_SUBAGENT_OVERVIEW_POLL_BACKOFF` up to the
    :data:`_SUBAGENT_OVERVIEW_POLL_MAX_SECONDS` ceiling, and the very next
    change resets straight to :data:`_SUBAGENT_OVERVIEW_POLL_BASE_SECONDS`
    — so we back off the idle cost without ever trading away liveness when
    something actually happens. Pure function; extracted for testability.
    """
    if changed:
        return _SUBAGENT_OVERVIEW_POLL_BASE_SECONDS
    return min(
        current * _SUBAGENT_OVERVIEW_POLL_BACKOFF,
        _SUBAGENT_OVERVIEW_POLL_MAX_SECONDS,
    )


def _overview_change_signature(
    store: Any, registry: Any
) -> tuple[int | None, int | None]:
    """Cheap O(1) change probe for the overview loop (Codex #113).

    Returns ``(registry_revision, store_file_mtime_ns)`` — a tuple the loop
    compares against the signature captured at its last full scan to decide
    whether anything changed WITHOUT paying for ``list_all()`` + a merge:

    * ``registry.revision`` — a plain int the in-memory
      :class:`LiveSubagentRegistry` bumps on every row mutation.
    * the backing store file's ``st_mtime_ns`` — the store flushes to its
      ``_persist_path`` after every mutation, so the mtime moves whenever a
      background (durable) row changes.

    Every component defensively degrades to ``None`` (registry absent or
    missing the attribute, store unwired, no persist path, stat error)
    rather than raising — the probe rides the SSE hot loop and must never
    break it. A ``None`` component simply means "can't cheaply tell for this
    source"; the loop's ``next_full_scan`` deadline still bounds staleness.
    """
    revision: int | None
    try:
        revision = int(registry.revision) if registry is not None else None
    except Exception:  # noqa: BLE001 — probe must never raise
        revision = None

    mtime: int | None = None
    # ``SubagentTaskStore`` persists to ``_persist_path`` (a ``Path``); look it
    # up defensively so a store shape without one degrades to ``None`` instead
    # of exploding the stream.
    path = getattr(store, "_persist_path", None) if store is not None else None
    if path is not None:
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            # Missing file (nothing flushed yet) or any stat failure — treat as
            # "unknown" rather than a spurious change.
            mtime = None
    return (revision, mtime)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(
        dependencies=[Depends(require_admin)], tags=["admin", "subagents"]
    )

    # ------------------------------------------------------------------
    # GET /admin/subagents
    # ------------------------------------------------------------------

    @r.get("/admin/subagents", response_model=SubagentListResponse)
    async def list_subagents(
        include_terminal: bool = Query(
            False,
            description=(
                "When True, include terminal rows (succeeded/failed/"
                "timeout/killed) in addition to in-flight rows."
            ),
        ),
    ) -> SubagentListResponse | JSONResponse:
        state = get_admin_state()
        store = _resolve_store(state)
        registry = _resolve_live_registry(state)
        if store is None and registry is None:
            return _error(
                503,
                "subagent_dispatcher_unavailable",
                "background subagent dispatch is not wired on this gateway",
            )
        store_rows: list[Any] = []
        if store is not None:
            store_rows = (
                await store.list_all()
                if include_terminal
                else await store.list_active()
            )
        rows = _merged_rows(
            store_rows, registry, active_only=not include_terminal
        )
        return SubagentListResponse(rows=rows)

    # ------------------------------------------------------------------
    # GET /admin/subagents/{id}/status
    # ------------------------------------------------------------------

    @r.get(
        "/admin/subagents/{request_id}/status",
        response_model=SubagentStatusResponse,
    )
    async def get_subagent_status(
        request_id: str = Path(..., description="Background subagent id."),
    ) -> SubagentStatusResponse | JSONResponse:
        state = get_admin_state()
        store = _resolve_store(state)
        registry = _resolve_live_registry(state)
        if store is None and registry is None:
            return _error(
                503,
                "subagent_dispatcher_unavailable",
                "background subagent dispatch is not wired on this gateway",
            )
        if store is not None:
            status = await store.get(request_id)
            if status is not None:
                return _status_to_response(status)
        # Store miss (or store unwired) — fall back to the in-memory inline
        # registry so registry-only rows the LIST route already merges are
        # pollable by id too. Same projection as ``_merged_rows``.
        if registry is not None:
            for row in registry.list_all():
                resp = _live_row_to_response(row)
                if resp.request_id == request_id:
                    return resp
        return _error(
            404,
            "subagent_request_not_found",
            f"no subagent request with id {request_id!r}",
        )

    # ------------------------------------------------------------------
    # POST /admin/subagents/{id}/kill
    # ------------------------------------------------------------------

    @r.post(
        "/admin/subagents/{request_id}/kill",
        response_model=SubagentStatusResponse,
    )
    async def kill_subagent(
        request: Request,
        request_id: str = Path(..., description="Background subagent id."),
    ) -> SubagentStatusResponse | JSONResponse:
        state = get_admin_state()
        dispatcher = _resolve_dispatcher(state)
        if dispatcher is None:
            return _error(
                503,
                "subagent_dispatcher_unavailable",
                "background subagent dispatch is not wired on this gateway",
            )
        # Pre-check existence so we can split unknown-id (404) from
        # already-terminal (409). The dispatcher returns None for both
        # cases otherwise.
        store = _resolve_store(state)
        if store is not None:
            existing = await store.get(request_id)
            if existing is None:
                return _error(
                    404,
                    "subagent_request_not_found",
                    f"no subagent request with id {request_id!r}",
                )
            if existing.is_terminal():
                return _error(
                    409,
                    "subagent_already_terminal",
                    (
                        f"subagent {request_id!r} is already terminal "
                        f"(state={existing.state})"
                    ),
                    state=existing.state,
                )

        actor = _resolve_actor(request)
        try:
            killed = await dispatcher.kill(request_id, by=actor)
        except Exception as exc:  # noqa: BLE001 — surface as 500
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "subagent_kill_failed",
                    "message": str(exc),
                },
            ) from exc
        if killed is None:
            # Race: row went terminal between the pre-check + the kill.
            return _error(
                409,
                "subagent_already_terminal",
                f"subagent {request_id!r} reached terminal mid-kill",
            )
        return _status_to_response(killed)

    # ------------------------------------------------------------------
    # GET /admin/subagents/{id}/events
    # ------------------------------------------------------------------

    @r.get("/admin/subagents/{request_id}/events")
    async def stream_subagent_events(
        request_id: str = Path(..., description="Background subagent id."),
    ) -> Any:
        """SSE stream scoped to one background subagent.

        Uses the shared :class:`JournalBackedEmitter` and subscribes to
        the *child* session key. The supervisor's :class:`BubbleEmitter`
        wraps every event the child emits into a :class:`SubagentEvent`
        envelope on the parent's stream, but it also routes them into
        the journal under the child's own session_key — the subscribe
        here grabs the child-tagged copy directly so the operator sees
        the live child stream without parsing the parent's nested events.

        Closes on terminal-state observation (best-effort heartbeat poll
        of the store every 10s; this is in addition to the regular
        envelope-driven SSE frames).
        """
        state = get_admin_state()
        store = _resolve_store(state)
        emitter = _resolve_event_emitter(state)
        if store is None:
            return _error(
                503,
                "subagent_dispatcher_unavailable",
                "background subagent dispatch is not wired on this gateway",
            )

        initial = await store.get(request_id)
        if initial is None:
            return _error(
                404,
                "subagent_request_not_found",
                f"no subagent request with id {request_id!r}",
            )

        async def _generate() -> AsyncIterator[bytes]:
            # Always emit the initial status row first so the client
            # immediately knows the request_id / state / parent_session.
            seq = 0
            initial_payload = json.dumps(
                _status_to_response(initial).model_dump(),
                default=str,
            )
            yield (
                f"id: {request_id}:{seq}\n"
                f"event: status\n"
                f"data: {initial_payload}\n\n"
            ).encode()
            seq += 1

            if initial.is_terminal():
                # Nothing to subscribe to — the child is already done.
                return

            child_key = initial.child_session_key
            queue: Any | None = None
            unsubscribe: Any | None = None
            if emitter is not None and child_key:
                try:
                    queue, unsubscribe = await emitter.subscribe(child_key)
                except Exception:  # noqa: BLE001 — best-effort
                    queue, unsubscribe = None, None

            try:
                while True:
                    if queue is not None:
                        try:
                            envelope = await asyncio.wait_for(
                                queue.get(),
                                timeout=_SUBAGENT_SSE_HEARTBEAT_SECONDS,
                            )
                        except TimeoutError:
                            # Heartbeat + terminal-state poll.
                            yield b": keepalive\n\n"
                            current = await store.get(request_id)
                            if current is not None and current.is_terminal():
                                payload = json.dumps(
                                    _status_to_response(current).model_dump(),
                                    default=str,
                                )
                                yield (
                                    f"id: {request_id}:{seq}\n"
                                    f"event: status\n"
                                    f"data: {payload}\n\n"
                                ).encode()
                                break
                            continue
                        # Forward the child envelope as ``event: event``.
                        to_json = getattr(envelope, "to_json", None)
                        payload_obj: Any
                        if callable(to_json):
                            payload_obj = to_json()
                        else:
                            payload_obj = envelope
                        try:
                            payload = json.dumps(
                                payload_obj, default=str
                            )
                        except (TypeError, ValueError):
                            payload = json.dumps(str(payload_obj))
                        yield (
                            f"id: {request_id}:{seq}\n"
                            f"event: event\n"
                            f"data: {payload}\n\n"
                        ).encode()
                        seq += 1
                    else:
                        # Emitter / child_key not wired — fall back to
                        # poll-only mode: emit a status frame every
                        # heartbeat tick and close on terminal.
                        await asyncio.sleep(_SUBAGENT_SSE_HEARTBEAT_SECONDS)
                        current = await store.get(request_id)
                        if current is None:
                            break
                        payload = json.dumps(
                            _status_to_response(current).model_dump(),
                            default=str,
                        )
                        yield (
                            f"id: {request_id}:{seq}\n"
                            f"event: status\n"
                            f"data: {payload}\n\n"
                        ).encode()
                        seq += 1
                        if current.is_terminal():
                            break
            except asyncio.CancelledError:
                raise
            finally:
                if unsubscribe is not None:
                    try:
                        await unsubscribe()
                    except Exception:  # noqa: BLE001 — best-effort
                        pass

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Event-Id-Format": "request_id:sequence",
            },
        )

    # ------------------------------------------------------------------
    # GET /admin/subagents/events/live
    # ------------------------------------------------------------------

    @r.get("/admin/subagents/events/live")
    async def stream_subagents_overview() -> Any:
        """Global feed of subagent state transitions.

        Emits a ``subagent`` SSE frame for any row whose state changed. The
        route is intentionally store-driven (rather than emitter-driven) so
        the overview panel always reflects the durable snapshot — operator
        UIs prefer correctness over sub-second latency for this panel.

        The loop runs on three independent monotonic clocks (see the inline
        comment on the loop for the full rationale + cost model): a
        real-elapsed keepalive rhythm, a cheap per-tick change *probe*, and a
        backed-off *full scan*. Idle cost is O(1) per probe tick; the
        expensive ``list_all()`` + merge runs only at the backed-off cadence
        or the instant the probe sees a change.
        """
        state = get_admin_state()
        store = _resolve_store(state)
        registry = _resolve_live_registry(state)
        if store is None and registry is None:
            return _error(
                503,
                "subagent_dispatcher_unavailable",
                "background subagent dispatch is not wired on this gateway",
            )

        async def _combined() -> list[SubagentStatusResponse]:
            store_rows: list[Any] = []
            if store is not None:
                store_rows = await store.list_all()
            # ``active_only=False`` — the overview shows terminal rows too
            # (it diffs state transitions, incl. running→succeeded).
            return _merged_rows(store_rows, registry, active_only=False)

        async def _generate() -> AsyncIterator[bytes]:
            # The activity line is part of the diff key so a child switching
            # tools (running→running but new activity) still pushes a frame.
            last: dict[str, tuple[str, int | None, str]] = {}
            seq = 0
            try:
                # Emit a snapshot frame on connect so the UI primes
                # without waiting a full heartbeat tick.
                for row in await _combined():
                    payload = json.dumps(row.model_dump(), default=str)
                    yield (
                        f"id: live:{seq}\n"
                        f"event: subagent\n"
                        f"data: {payload}\n\n"
                    ).encode()
                    seq += 1
                    last[row.request_id] = (
                        row.state,
                        row.finished_at,
                        row.activity,
                    )

                # ----------------------------------------------------------
                # Three monotonic clocks (Codex #113). Everything is measured
                # from ``time.monotonic()`` — REAL elapsed time — so time spent
                # in ``_combined()`` / serialization / a blocked yield is
                # counted, never merely the sleep we *asked* for.
                #
                #  1. HEARTBEAT (real-elapsed output silence). ``last_byte``
                #     moves after EVERY yield — data frames AND keepalives — and
                #     a keepalive fires the instant ``HEARTBEAT`` seconds of true
                #     wall time elapse with no byte. So the first idle keepalive
                #     lands on the ~10s rhythm idle proxies expect regardless of
                #     scan/backoff cost (fixes finding (a)).
                #  2. PROBE (cheap change signal at the base cadence). Each tick
                #     reads ``_overview_change_signature`` — an int + one stat,
                #     no ``list_all()``. When it differs from the signature
                #     captured at the last full scan, a change happened: scan
                #     immediately. So a change after an idle backoff surfaces
                #     within one base tick (~2s) rather than waiting out the
                #     whole backed-off interval up to 10s (fixes finding (b)).
                #  3. FULL SCAN (expensive ``_combined()`` + merge). Runs when
                #     the backed-off ``next_full_scan`` deadline arrives OR the
                #     probe flagged a change; the existing ``_next_poll_interval``
                #     backoff (base→cap on idle, snap-to-base on change) still
                #     drives that deadline.
                #
                # Cost model: while idle the per-tick cost is O(1) — one
                # ``stat`` + one int read every base interval (~2s); the full
                # scan runs only at the backed-off cadence or the moment a real
                # change is detected, never every tick per connected client.
                # ----------------------------------------------------------
                now = time.monotonic()
                last_byte = now
                _poll = _SUBAGENT_OVERVIEW_POLL_BASE_SECONDS
                next_full_scan = now + _poll
                last_scan_sig = _overview_change_signature(store, registry)
                while True:
                    now = time.monotonic()
                    # (1) keepalive on real-elapsed output silence.
                    if now - last_byte >= _SUBAGENT_SSE_HEARTBEAT_SECONDS:
                        yield b": keepalive\n\n"
                        last_byte = now
                    # (2)+(3) full scan on the backed-off deadline OR the moment
                    # the cheap probe diverges from the last scan's signature.
                    sig = _overview_change_signature(store, registry)
                    if now >= next_full_scan or sig != last_scan_sig:
                        seen_ids: set[str] = set()
                        changed = False
                        for row in await _combined():
                            seen_ids.add(row.request_id)
                            snapshot = (
                                row.state,
                                row.finished_at,
                                row.activity,
                            )
                            prev = last.get(row.request_id)
                            if prev == snapshot:
                                continue
                            changed = True
                            last[row.request_id] = snapshot
                            payload = json.dumps(row.model_dump(), default=str)
                            yield (
                                f"id: live:{seq}\n"
                                f"event: subagent\n"
                                f"data: {payload}\n\n"
                            ).encode()
                            seq += 1
                            last_byte = time.monotonic()
                        # Reap stale ids (test fixture deletions) — a
                        # disappearing row is also a state change for cadence.
                        stale_ids = [k for k in last if k not in seen_ids]
                        for stale in stale_ids:
                            last.pop(stale, None)
                        if stale_ids:
                            changed = True
                        # Adapt the next interval: snap to base on any change,
                        # else back off toward the idle cap (unchanged logic).
                        _poll = _next_poll_interval(changed, _poll)
                        # Anchor the signature to the value we scanned against
                        # (captured BEFORE the scan) so any mutation that raced
                        # the scan re-triggers next tick — never miss a change.
                        last_scan_sig = sig
                        next_full_scan = time.monotonic() + _poll
                    # Sleep until whichever clock ticks next — the base interval
                    # bound IS the probe cadence — floored so we never
                    # busy-spin when a deadline is already due.
                    now = time.monotonic()
                    sleep_for = min(
                        next_full_scan - now,
                        _SUBAGENT_SSE_HEARTBEAT_SECONDS - (now - last_byte),
                        _SUBAGENT_OVERVIEW_POLL_BASE_SECONDS,
                    )
                    await asyncio.sleep(max(sleep_for, 0.05))
            except asyncio.CancelledError:
                raise

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Event-Id-Format": "live:sequence",
            },
        )

    return r


# Suppress unused-import noise. (``TenantQuotaExceeded`` is kept around for
# future audit-log integration in W3.1; ``time`` is now used by the overview
# loop's monotonic clocks.)
_ = (TenantQuotaExceeded,)
