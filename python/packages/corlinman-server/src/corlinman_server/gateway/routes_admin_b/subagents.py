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

Pattern mirrors :mod:`corlinman_server.gateway.routes_admin_b.system`'s
``/admin/system/upgrade/{id}/events`` route: 10s SSE heartbeat,
terminal-state stream closure, typed JSON error envelopes.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)
from corlinman_server.system.subagent import (
    AsyncSubagentDispatcher,
    SubagentStatus,
    SubagentTaskStore,
    TenantQuotaExceeded,
)

__all__ = ["router"]


# SSE keepalive cadence — matches sessions_events.py / system.py so
# proxies/reverse-proxies idle on the same timer everywhere.
_SUBAGENT_SSE_HEARTBEAT_SECONDS: float = 10.0


# Terminal states that close the per-child SSE stream. Matches the
# store's terminal set; spelled out here so the route file is
# self-contained and a future addition to the state union is an explicit
# code change rather than a silent drift.
_TERMINAL_STATES: frozenset[str] = frozenset(
    {"succeeded", "failed", "timeout", "killed"}
)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class SubagentStatusResponse(BaseModel):
    """Pydantic mirror of :class:`SubagentStatus`."""

    request_id: str
    parent_session_key: str
    subagent_type: str
    description: str | None = None
    state: str
    started_at: int | None = None
    finished_at: int | None = None
    child_session_key: str | None = None
    finish_reason: str | None = None
    tool_calls_made: int = 0
    elapsed_ms: int = 0
    error: str | None = None
    summary: str = ""
    log_tail: str = ""


class SubagentListResponse(BaseModel):
    rows: list[SubagentStatusResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_to_response(status: SubagentStatus) -> SubagentStatusResponse:
    return SubagentStatusResponse(**asdict(status))


def _error(
    status_code: int,
    error: str,
    message: str,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {"error": error, "message": message}
    body.update(extra)
    return JSONResponse(status_code=status_code, content=body)


def _resolve_dispatcher(
    state: AdminState,
) -> AsyncSubagentDispatcher | None:
    """Read the dispatcher handle off AdminState (duck-typed)."""
    dispatcher = getattr(state, "subagent_dispatcher", None)
    if dispatcher is None:
        return None
    # Duck-typed acceptance — tests pass a fake exposing the same
    # ``dispatch_async`` / ``kill`` / ``store`` surface.
    if hasattr(dispatcher, "dispatch_async") or hasattr(dispatcher, "store"):
        # Duck-typed boundary: tests pass a fake exposing the same surface.
        return cast("AsyncSubagentDispatcher", dispatcher)
    return None


def _resolve_store(state: AdminState) -> SubagentTaskStore | None:
    """Resolve the store directly off state, or via the dispatcher."""
    store = getattr(state, "subagent_store", None)
    if store is not None and hasattr(store, "get"):
        # Duck-typed boundary: tests pass a fake exposing ``get``.
        return cast("SubagentTaskStore", store)
    dispatcher = _resolve_dispatcher(state)
    if dispatcher is None:
        return None
    inner = getattr(dispatcher, "store", None)
    if inner is not None and hasattr(inner, "get"):
        return cast("SubagentTaskStore", inner)
    return None


def _resolve_event_emitter(state: AdminState) -> Any | None:
    emitter = getattr(state, "event_emitter", None)
    if emitter is None:
        return None
    if hasattr(emitter, "subscribe"):
        return emitter
    return None


def _resolve_actor(request: Request) -> str:
    """Best-effort extract a username from the auth context.

    Same precedence the upgrade routes use (``request.state.admin_user``
    first, then ``admin_session``). Falls back to ``"admin"``.
    """
    user = getattr(request.state, "admin_user", None)
    if isinstance(user, str) and user:
        return user
    session = getattr(request.state, "admin_session", None)
    if session is not None:
        username = getattr(session, "username", None) or getattr(
            session, "user", None
        )
        if isinstance(username, str) and username:
            return username
    return "admin"


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
        if store is None:
            return _error(
                503,
                "subagent_dispatcher_unavailable",
                "background subagent dispatch is not wired on this gateway",
            )
        rows = (
            await store.list_all()
            if include_terminal
            else await store.list_active()
        )
        return SubagentListResponse(
            rows=[_status_to_response(row) for row in rows]
        )

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
        if store is None:
            return _error(
                503,
                "subagent_dispatcher_unavailable",
                "background subagent dispatch is not wired on this gateway",
            )
        status = await store.get(request_id)
        if status is None:
            return _error(
                404,
                "subagent_request_not_found",
                f"no subagent request with id {request_id!r}",
            )
        return _status_to_response(status)

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
        if store is None:
            return _error(
                503,
                "subagent_dispatcher_unavailable",
                "background subagent dispatch is not wired on this gateway",
            )

        async def _generate() -> AsyncIterator[bytes]:
            last: dict[str, tuple[str, int | None]] = {}
            seq = 0
            try:
                # Emit a snapshot frame on connect so the UI primes
                # without waiting a full heartbeat tick.
                rows = await store.list_all()
                for row in rows:
                    payload = json.dumps(
                        _status_to_response(row).model_dump(),
                        default=str,
                    )
                    yield (
                        f"id: live:{seq}\n"
                        f"event: subagent\n"
                        f"data: {payload}\n\n"
                    ).encode()
                    seq += 1
                    last[row.request_id] = (row.state, row.finished_at)

                while True:
                    await asyncio.sleep(_SUBAGENT_SSE_HEARTBEAT_SECONDS)
                    yield b": keepalive\n\n"
                    rows = await store.list_all()
                    seen_ids: set[str] = set()
                    for row in rows:
                        seen_ids.add(row.request_id)
                        snapshot = (row.state, row.finished_at)
                        prev = last.get(row.request_id)
                        if prev == snapshot:
                            continue
                        last[row.request_id] = snapshot
                        payload = json.dumps(
                            _status_to_response(row).model_dump(),
                            default=str,
                        )
                        yield (
                            f"id: live:{seq}\n"
                            f"event: subagent\n"
                            f"data: {payload}\n\n"
                        ).encode()
                        seq += 1
                    # Reap stale ids (test fixture deletions).
                    for stale in [k for k in last if k not in seen_ids]:
                        last.pop(stale, None)
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
