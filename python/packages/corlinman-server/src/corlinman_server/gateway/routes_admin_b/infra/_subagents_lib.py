"""Module-level support for :mod:`...infra.subagents`.

Extracted VERBATIM from ``subagents.py`` (wire-models, constants, and the
``_resolve_*`` / ``_status_to_response`` / ``_error`` helpers) as part of a
behavior-preserving god-file split.

This module MUST NOT import the route module
(``corlinman_server.gateway.routes_admin_b.infra.subagents``) — doing so
would create an import cycle.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, cast

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
)
from corlinman_server.system.subagent import (
    AsyncSubagentDispatcher,
    SubagentStatus,
    SubagentTaskStore,
)

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
    # W2.x — multi-agent panel extras. ``depth`` drives supervisor→worker
    # nesting; ``activity`` is the Codex-style live current-action line;
    # ``source`` distinguishes durable background rows from in-memory inline
    # rows. Defaults keep the background path (``_status_to_response``)
    # byte-compatible — background ``SubagentStatus`` has no such fields.
    depth: int = 0
    activity: str = ""
    source: str = "background"


class SubagentListResponse(BaseModel):
    rows: list[SubagentStatusResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_to_response(status: SubagentStatus) -> SubagentStatusResponse:
    return SubagentStatusResponse(**asdict(status))


def _live_row_to_response(row: Any) -> SubagentStatusResponse:
    """Map a :class:`LiveSubagentRow` (inline) to the wire model.

    Duck-typed (``row.to_dict()``) so the route file doesn't import the
    observability layer directly.
    """
    return SubagentStatusResponse(**row.to_dict())


def _merged_rows(
    store_rows: list[Any],
    registry: Any | None,
    *,
    active_only: bool,
) -> list[SubagentStatusResponse]:
    """Merge durable background-store rows with in-memory inline-registry
    rows into one wire-model list. Background rows win on ``request_id``
    collision (durable store is the source of truth)."""
    out: dict[str, SubagentStatusResponse] = {}
    if registry is not None:
        rows = registry.list_active() if active_only else registry.list_all()
        for row in rows:
            resp = _live_row_to_response(row)
            out[resp.request_id] = resp
    for status in store_rows:
        resp = _status_to_response(status)
        out[resp.request_id] = resp  # background wins on collision
    return list(out.values())


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


def _resolve_live_registry(state: AdminState) -> Any | None:
    """Resolve the in-process :class:`LiveSubagentRegistry` off state.

    Duck-typed (``list_all`` / ``list_active``) so tests can hand in a fake.
    Returns ``None`` on degraded boots (no emitter wired) — callers then fall
    back to the background store alone.
    """
    registry = getattr(state, "live_subagent_registry", None)
    if registry is not None and hasattr(registry, "list_all"):
        return registry
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
