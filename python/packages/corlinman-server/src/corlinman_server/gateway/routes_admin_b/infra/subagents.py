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


def _sleep_slices(
    poll: float, since_keepalive: float, heartbeat: float
) -> list[tuple[float, bool]]:
    """Split a poll-interval sleep into heartbeat-bounded slices.

    Returns ``(seconds, emit_keepalive_after)`` pairs whose seconds sum to
    ``poll`` exactly, cut wherever the running ``since_keepalive`` clock
    would cross ``heartbeat`` — so a backed-off interval (up to the 10s
    cap) never delays the keepalive past the documented SSE rhythm, while
    the caller's scan cadence still honours the full ``poll``. A slice
    flagged ``True`` resets the caller's keepalive clock. Pure function;
    extracted for testability (Codex #113).
    """
    slices: list[tuple[float, bool]] = []
    remaining = poll
    clock = since_keepalive
    while remaining > 0:
        to_heartbeat = heartbeat - clock
        if to_heartbeat <= 0:
            # Already at/past the rhythm on entry — emit immediately.
            slices.append((0.0, True))
            clock = 0.0
            continue
        if to_heartbeat <= remaining:
            slices.append((to_heartbeat, True))
            remaining -= to_heartbeat
            clock = 0.0
        else:
            slices.append((remaining, False))
            clock += remaining
            remaining = 0.0
    return slices


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

        Polls the store every heartbeat tick (default 10s) and emits a
        ``subagent`` SSE frame for any row whose state changed since the
        last tick. The route is intentionally store-driven (rather than
        emitter-driven) so the overview panel always reflects the durable
        snapshot — operator UIs prefer correctness over sub-second
        latency for this panel.
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

                # Poll faster than the keepalive so the panel feels live
                # (Codex-Desktop-style): a child switching tools surfaces in
                # ~2s instead of waiting a full 10s heartbeat. That liveness
                # only matters while state is moving, though — so the cadence
                # ADAPTS: a fully-unchanged tick backs the interval off ×1.5
                # up to a 10s cap (see ``_next_poll_interval``), and the next
                # change snaps it straight back to 2s. This keeps the idle
                # cost (per-client ``list_all()`` + merge every tick) low
                # without ever sacrificing liveness. The keepalive rides
                # wall-clock elapsed time so idle proxies still see the same
                # ~10s byte rhythm as the other SSE routes.
                _poll = _SUBAGENT_OVERVIEW_POLL_BASE_SECONDS
                since_keepalive = 0.0
                while True:
                    # Sleep the poll interval in heartbeat-bounded slices:
                    # a backed-off interval (up to 10s) must never push the
                    # keepalive past the documented ~10s SSE rhythm, or
                    # idle proxies drop the connection (Codex #113). The
                    # scan cadence still honours the full ``_poll``.
                    for slice_s, emit_keepalive in _sleep_slices(
                        _poll, since_keepalive, _SUBAGENT_SSE_HEARTBEAT_SECONDS
                    ):
                        await asyncio.sleep(slice_s)
                        since_keepalive += slice_s
                        if emit_keepalive:
                            yield b": keepalive\n\n"
                            since_keepalive = 0.0
                    seen_ids: set[str] = set()
                    changed = False
                    for row in await _combined():
                        seen_ids.add(row.request_id)
                        snapshot = (row.state, row.finished_at, row.activity)
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
                    # Reap stale ids (test fixture deletions) — a disappearing
                    # row is also a state change for cadence purposes.
                    stale_ids = [k for k in last if k not in seen_ids]
                    for stale in stale_ids:
                        last.pop(stale, None)
                    if stale_ids:
                        changed = True
                    # Adapt the next interval: snap to base on any change,
                    # else back off toward the idle cap.
                    _poll = _next_poll_interval(changed, _poll)
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


# Suppress unused-import noise from the helpers above. (Time / TenantQuotaExceeded
# are kept around for diagnostics + future audit-log integration in W3.1.)
_ = (time, TenantQuotaExceeded)
