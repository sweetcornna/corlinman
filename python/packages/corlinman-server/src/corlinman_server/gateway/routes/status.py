"""Public **agent status card** routes — the unauthenticated counterpart to
the admin sessions surface.

A status token (see :mod:`corlinman_server.gateway.status_token`) is a signed,
time-limited, read-only capability scoping access to exactly ONE conversation's
status + work trajectory. The agent posts a chat user a clickable link
``{public_url}/status/{token}``; these routes back the page that link opens.

Three URL shapes, all mounted at ROOT with **NO auth** (auth only gates ``/v1``
+ ``/admin`` — the signed token in the path IS the capability):

* ``GET /status/{token}/data`` — JSON snapshot ``{session_key, status, turns,
  events, started_at_ms, updated_at_ms}``. ``events`` are the same
  ``EventEnvelope`` dicts the admin replay surface emits, so the public UI
  folds them through the identical timeline reducer.
* ``GET /status/{token}/events/live`` — SSE feed of new event envelopes
  (#31 live updates). 10s heartbeat; resumable via ``Last-Event-ID`` /
  ``?last_event_id=``.
* bare ``/status/{token}`` is intentionally NOT a route here — the Next static
  export serves the HTML shell there. The distinct ``/data`` + ``/events/live``
  suffixes keep these API routes from colliding with the static page.

The journal is read **lazily** from ``request.app.state.corlinman_journal`` at
request time — it is created in the lifespan *after* routes are mounted, so
capturing it at construction would always yield ``None``.

Privacy (#30): tool-call args/results can carry sensitive content. The public
snapshot redacts them by default (env ``CORLINMAN_STATUS_REDACT``) — tool
*names* + status survive, free-form payload bodies are stripped — so a shared
link can't leak prompts / keys / file contents.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse, StreamingResponse

from corlinman_server.gateway.status_token import (
    resolve_signing_key,
    verify_status_token,
)

__all__ = ["router"]

# SSE heartbeat cadence — keeps proxies from idling the connection out while
# the agent is silent between turns. Matches the admin live stream.
_SSE_HEARTBEAT_SECONDS: float = 10.0
# How often the live feed polls the journal for new events (the journal is the
# source of truth; we tail it rather than hooking the emitter, keeping this
# route decoupled from the observability wiring).
_SSE_POLL_SECONDS: float = 1.0
# Cap on turns / events materialised into a single snapshot so a pathological
# long-lived session can't blow up one response.
_MAX_TURNS: int = 200
_MAX_EVENTS: int = 5000

# Event types whose payloads carry tool args/results — redacted in the public
# snapshot unless redaction is disabled. The event still appears (tool name +
# status), only the free-form payload body is stripped.
_SENSITIVE_EVENT_SUBSTRINGS: tuple[str, ...] = ("tool", "message", "token")
# Payload keys that are safe to keep even under redaction (structural, not
# content) so the timeline still renders a useful card.
_REDACTION_KEEP_KEYS: frozenset[str] = frozenset(
    {
        "tool",
        "tool_name",
        "name",
        "call_id",
        "status",
        "is_error",
        "duration_ms",
        "elapsed_ms",
        "finish_reason",
        "seq",
        "sequence",
        "depth",
        "child_session_key",
        "child_agent_id",
    }
)


def _data_dir() -> Path | None:
    raw = os.environ.get("CORLINMAN_DATA_DIR")
    if raw:
        return Path(raw)
    home = Path.home() / ".corlinman"
    return home if home.exists() else None


def _redaction_enabled() -> bool:
    """Default ON. Disabled only by an explicit falsey env override."""
    raw = os.environ.get("CORLINMAN_STATUS_REDACT")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _redact_event(ev: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive payload content while preserving the event's shape.

    Keeps the structural keys the timeline needs to render a card (tool name,
    status, durations) and drops everything else so a shared public link can't
    leak prompts / tool I/O / file contents.
    """
    etype = str(ev.get("event_type", "")).lower()
    payload = ev.get("payload")
    if not isinstance(payload, dict) or not any(
        s in etype for s in _SENSITIVE_EVENT_SUBSTRINGS
    ):
        return ev
    redacted: dict[str, Any] = {}
    stripped = False
    for k, v in payload.items():
        if k in _REDACTION_KEEP_KEYS:
            redacted[k] = v
        else:
            stripped = True
    if stripped:
        redacted["_redacted"] = True
    return {**ev, "payload": redacted}


def _derive_status(turns: list[dict[str, Any]]) -> str:
    """Coarse run-state for the header pill from the most-recent turn.

    ``turns`` is newest-first (journal contract). Maps the per-turn status
    onto the UI's StatusState union.
    """
    if not turns:
        return "idle"
    latest = str(turns[0].get("status", "")).lower()
    if latest in ("in_progress", "running", "active"):
        return "running"
    if latest in ("error", "errored", "failed"):
        return "errored"
    if latest in ("cancelled", "cancelling"):
        return "cancelling"
    if latest in ("complete", "completed", "done", "ok"):
        return "complete"
    return latest or "idle"


def _summarise_turn(row: dict[str, Any]) -> dict[str, Any]:
    """Project a journal turn row onto the lean public StatusTurn shape."""
    out: dict[str, Any] = {"turn_id": str(row.get("turn_id", ""))}
    for src, dst in (
        ("status", "status"),
        ("elapsed_ms", "elapsed_ms"),
        ("started_at_ms", "started_at_ms"),
        ("ended_at_ms", "ended_at_ms"),
    ):
        if row.get(src) is not None:
            out[dst] = row[src]
    return out


async def _load_snapshot(journal: Any, session_key: str) -> dict[str, Any]:
    """Build the StatusSnapshot dict for ``session_key`` from the journal."""
    redact = _redaction_enabled()
    turns_rows = await journal.list_session_turns(session_key)
    turns_rows = list(turns_rows)[:_MAX_TURNS]

    # Events oldest-first for the timeline: turn rows come newest-first, so
    # reverse, then concat each turn's events in sequence order.
    events: list[dict[str, Any]] = []
    for row in reversed(turns_rows):
        turn_id = row.get("turn_id")
        if turn_id is None:
            continue
        turn_events = await journal.load_events(turn_id)
        for ev in turn_events:
            events.append(_redact_event(ev) if redact else ev)
            if len(events) >= _MAX_EVENTS:
                break
        if len(events) >= _MAX_EVENTS:
            break

    started_at = events[0].get("timestamp_ms") if events else None
    updated_at = events[-1].get("timestamp_ms") if events else None
    return {
        "session_key": session_key,
        "status": _derive_status(turns_rows),
        "turns": [_summarise_turn(r) for r in turns_rows],
        "events": events,
        "started_at_ms": started_at,
        "updated_at_ms": updated_at,
    }


def _empty_snapshot(session_key: str) -> dict[str, Any]:
    return {
        "session_key": session_key,
        "status": "idle",
        "turns": [],
        "events": [],
        "started_at_ms": None,
        "updated_at_ms": None,
    }


def _resolve_session(token: str) -> str | None:
    """Verify ``token`` and return the session_key it authorizes, else None."""
    key = resolve_signing_key(_data_dir())
    return verify_status_token(token, key)


def router() -> APIRouter:
    """Build the public status-card router (mount at ROOT, no auth)."""
    api = APIRouter(tags=["status"])

    @api.get("/status/{token}/data")
    async def status_data(
        request: Request,
        token: str = PathParam(..., min_length=8),
    ) -> JSONResponse:
        session_key = _resolve_session(token)
        if session_key is None:
            return JSONResponse(
                status_code=403,
                content={"error": "invalid_or_expired_token"},
            )
        journal = getattr(request.app.state, "corlinman_journal", None)
        if journal is None:
            # Feature wired but observability journal absent (degraded boot).
            return JSONResponse(status_code=200, content=_empty_snapshot(session_key))
        try:
            snap = await _load_snapshot(journal, session_key)
        except Exception:  # noqa: BLE001 - never 500 a public read
            return JSONResponse(status_code=200, content=_empty_snapshot(session_key))
        return JSONResponse(status_code=200, content=snap)

    @api.get("/status/{token}/events/live")
    async def status_events_live(
        request: Request,
        token: str = PathParam(..., min_length=8),
        last_event_id: str | None = Query(default=None),
    ) -> Any:
        session_key = _resolve_session(token)
        if session_key is None:
            return JSONResponse(
                status_code=403,
                content={"error": "invalid_or_expired_token"},
            )
        journal = getattr(request.app.state, "corlinman_journal", None)
        redact = _redaction_enabled()
        # Honour the SSE reconnect header as well as the query param.
        resume = request.headers.get("last-event-id") or last_event_id

        async def _gen() -> Any:
            # Track the high-water (turn_id:sequence) already sent so each
            # poll only emits genuinely new envelopes. Seed from the resume
            # cursor if the client reconnected.
            sent: set[str] = set()
            if resume:
                sent.add(resume)
            last_beat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                new_frames: list[dict[str, Any]] = []
                if journal is not None:
                    try:
                        turn_ids = await journal.get_session_turn_ids(
                            session_key, limit=_MAX_TURNS
                        )
                        for turn_id in reversed(list(turn_ids)):
                            for ev in await journal.load_events(turn_id):
                                eid = f"{ev.get('turn_id')}:{ev.get('sequence')}"
                                if eid in sent:
                                    continue
                                sent.add(eid)
                                new_frames.append(
                                    _redact_event(ev) if redact else ev
                                )
                    except Exception:  # noqa: BLE001 - tolerate transient reads
                        new_frames = []
                if new_frames:
                    for ev in new_frames:
                        eid = f"{ev.get('turn_id')}:{ev.get('sequence')}"
                        yield f"id: {eid}\ndata: {json.dumps(ev)}\n\n"
                    last_beat = time.monotonic()
                elif time.monotonic() - last_beat >= _SSE_HEARTBEAT_SECONDS:
                    yield ": keep-alive\n\n"
                    last_beat = time.monotonic()
                await asyncio.sleep(_SSE_POLL_SECONDS)

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return api
