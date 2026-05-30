"""Public, token-gated status-card routes.

The ``agent_status_card`` tool mints links shaped as ``/status/{token}``.
This module verifies that token, scopes every read to the embedded
``session_key``, and returns the read-only trajectory summary consumed by the
public status page. It deliberately lives outside the admin route tree.
"""

from __future__ import annotations

import mimetypes
import os
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path as FsPath
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse

from corlinman_server.gateway.status_token import (
    resolve_signing_key,
    verify_status_token,
)

STATUS_TURNS_DEFAULT_LIMIT = 25
STATUS_TURNS_MAX_LIMIT = 100
STATUS_EVENTS_DEFAULT_LIMIT = 5000
STATUS_EVENTS_MAX_LIMIT = 20000

_RUNNING_EVENT_TYPES = {
    "ToolStateRunning",
    "SubagentSpawned",
}
_COMPLETED_EVENT_TYPES = {
    "ToolStateCompleted",
    "SubagentCompleted",
    "TurnComplete",
    "TurnErrored",
}


@dataclass(slots=True)
class StatusState:
    """Minimal runtime handles for public status-card reads."""

    journal: Any | None = None
    data_dir: FsPath | None = None
    app_state: Any | None = None

    def resolve_journal(self) -> Any | None:
        if self.journal is not None:
            return self.journal
        return getattr(self.app_state, "journal", None)

    def resolve_data_dir(self) -> FsPath | None:
        if self.data_dir is not None:
            return self.data_dir
        data_dir = getattr(self.app_state, "data_dir", None)
        return FsPath(data_dir) if data_dir is not None else None


def _disabled_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "observability_disabled",
            "message": "journal is not wired on this gateway",
        },
    )


def _invalid_token_403() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "error": "invalid_status_token",
            "message": "status token is invalid or expired",
        },
    )


def _is_browser_html_request(request: Request, response_format: str | None) -> bool:
    if response_format == "json":
        return False
    accept = request.headers.get("accept", "")
    if "text/html" not in accept:
        return False
    # Fetch/XHR clients can still ask for HTML in tests or browser code.
    # Treat navigation/document-style requests as the public page shell.
    mode = request.headers.get("sec-fetch-mode", "")
    dest = request.headers.get("sec-fetch-dest", "")
    return mode in {"", "navigate"} and dest in {"", "document"}


@lru_cache(maxsize=1)
def _status_shell_path(ui_dir_env: str | None) -> FsPath | None:
    if not ui_dir_env:
        return None
    ui_dir = FsPath(ui_dir_env)
    candidates = [
        ui_dir / "status" / "__token__.html",
        ui_dir / "status" / "[token].html",
        ui_dir / "status.html",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _status_shell_response() -> FileResponse | None:
    shell_path = _status_shell_path(os.environ.get("CORLINMAN_UI_DIR"))
    if shell_path is None:
        return None
    media_type = mimetypes.guess_type(str(shell_path))[0] or "text/html"
    return FileResponse(shell_path, media_type=media_type)


def _resolve_session_key(token: str, state: StatusState) -> str:
    key = resolve_signing_key(state.resolve_data_dir())
    session_key = verify_status_token(token, key)
    if not session_key:
        raise _invalid_token_403()
    return session_key


def _latest_activity(turns: list[dict[str, Any]]) -> int | None:
    latest: int | None = None
    for turn in turns:
        for field in ("ended_at_ms", "started_at_ms"):
            value = turn.get(field)
            if isinstance(value, int):
                latest = value if latest is None else max(latest, value)
                break
    return latest


def _overall_status(turns: list[dict[str, Any]]) -> str:
    if not turns:
        return "not_found"
    statuses = {str(t.get("status") or "") for t in turns}
    if "in_progress" in statuses:
        return "in_progress"
    first = str(turns[0].get("status") or "")
    return first or "unknown"


def _payload_value(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    return None


def _completed_identity(event_type: str, payload: dict[str, Any]) -> str | None:
    if event_type == "ToolStateCompleted":
        value = _payload_value(payload, "tool_call_id", "call_id")
        return str(value) if value else None
    if event_type == "SubagentCompleted":
        value = _payload_value(payload, "child_session_key", "child_agent_id")
        return str(value) if value else None
    if event_type in {"TurnComplete", "TurnErrored"}:
        return "__turn__"
    return None


def _running_step_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(event.get("event_type") or "")
    payload_raw = event.get("payload")
    payload = payload_raw if isinstance(payload_raw, dict) else {}
    turn_id = str(event.get("turn_id") or "")
    if event_type == "ToolStateRunning":
        call_id = _payload_value(payload, "tool_call_id", "call_id")
        name = _payload_value(payload, "tool_name", "name")
        return {
            "kind": "tool",
            "turn_id": turn_id,
            "call_id": str(call_id) if call_id else None,
            "name": str(name) if name else None,
            "event_type": event_type,
        }
    if event_type == "SubagentSpawned":
        child_session_key = _payload_value(payload, "child_session_key")
        child_agent_id = _payload_value(payload, "child_agent_id")
        return {
            "kind": "subagent",
            "turn_id": turn_id,
            "child_session_key": (
                str(child_session_key) if child_session_key else None
            ),
            "child_agent_id": str(child_agent_id) if child_agent_id else None,
            "event_type": event_type,
        }
    return None


def _running_identity(step: dict[str, Any]) -> str | None:
    if step.get("kind") == "tool":
        value = step.get("call_id")
        return str(value) if value else None
    if step.get("kind") == "subagent":
        value = step.get("child_session_key") or step.get("child_agent_id")
        return str(value) if value else None
    return None


async def _current_step(journal: Any, turns: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the newest running tool/subagent without a matching completion."""

    for turn in turns:
        turn_id = turn.get("turn_id")
        if turn_id is None:
            continue
        events: list[dict[str, Any]] = []
        try:
            async for event in journal.iter_events(str(turn_id), start_sequence=-1):
                events.append(event)
        except Exception:  # noqa: BLE001 - status card should degrade.
            continue
        completed: set[str] = set()
        for event in events:
            event_type = str(event.get("event_type") or "")
            if event_type not in _COMPLETED_EVENT_TYPES:
                continue
            payload_raw = event.get("payload")
            payload = payload_raw if isinstance(payload_raw, dict) else {}
            identity = _completed_identity(event_type, payload)
            if identity:
                completed.add(identity)
        for event in reversed(events):
            event_type = str(event.get("event_type") or "")
            if event_type not in _RUNNING_EVENT_TYPES:
                continue
            step = _running_step_from_event(event)
            if step is None:
                continue
            identity = _running_identity(step)
            if identity and identity in completed:
                continue
            if "__turn__" in completed:
                continue
            return step
    return None


async def _status_payload(
    *,
    token: str,
    status_state: StatusState,
    limit: int,
) -> dict[str, Any]:
    session_key = _resolve_session_key(token, status_state)
    journal = status_state.resolve_journal()
    if journal is None:
        raise _disabled_503()

    turns = await journal.list_session_turns(session_key, limit=limit)
    return {
        "session_key": session_key,
        "status": _overall_status(turns),
        "started_at_ms": (
            min(
                t["started_at_ms"]
                for t in turns
                if isinstance(t.get("started_at_ms"), int)
            )
            if any(isinstance(t.get("started_at_ms"), int) for t in turns)
            else None
        ),
        "last_activity_at_ms": _latest_activity(turns),
        "turns": turns,
        "current_step": await _current_step(journal, turns),
    }


async def _session_events(
    *,
    journal: Any,
    session_key: str,
    turn_limit: int,
    event_limit: int,
    after_sequence: int,
) -> list[dict[str, Any]]:
    turn_ids = await journal.get_session_turn_ids(session_key, limit=turn_limit)
    events: list[dict[str, Any]] = []
    for turn_id in reversed(turn_ids):
        async for event in journal.iter_events(
            str(turn_id), start_sequence=after_sequence
        ):
            event_session_key = event.get("session_key")
            if event_session_key is not None and event_session_key != session_key:
                continue
            out = {**event, "session_key": session_key}
            events.append(out)
            if len(events) >= event_limit:
                return events
    events.sort(
        key=lambda ev: (
            int(ev.get("timestamp_ms") or 0),
            str(ev.get("turn_id") or ""),
            int(ev.get("sequence") or 0),
        )
    )
    return events


def router(state: StatusState | None = None) -> APIRouter:
    r = APIRouter(tags=["status"])
    status_state = state or StatusState()

    @r.get("/status/{token}", response_model=None)
    async def get_status_card(
        request: Request,
        token: str = Path(..., description="Signed status-card token."),
        response_format: str | None = Query(
            None,
            alias="format",
            description="Use 'json' to force the status API response.",
        ),
        limit: int = Query(
            STATUS_TURNS_DEFAULT_LIMIT,
            ge=1,
            le=STATUS_TURNS_MAX_LIMIT,
            description="Max recent turns to include.",
        ),
    ) -> Any:
        if _is_browser_html_request(request, response_format):
            shell = _status_shell_response()
            if shell is not None:
                return shell

        return await _status_payload(
            token=token,
            status_state=status_state,
            limit=limit,
        )

    @r.get("/status/{token}/events")
    async def get_status_events(
        token: str = Path(..., description="Signed status-card token."),
        turn_limit: int = Query(
            STATUS_TURNS_MAX_LIMIT,
            ge=1,
            le=STATUS_TURNS_MAX_LIMIT,
            description="Max recent turns to replay.",
        ),
        limit: int = Query(
            STATUS_EVENTS_DEFAULT_LIMIT,
            ge=1,
            le=STATUS_EVENTS_MAX_LIMIT,
            description="Max events to return.",
        ),
        after_sequence: int = Query(
            -1,
            ge=-1,
            description="Return events with sequence > after_sequence.",
        ),
    ) -> dict[str, Any]:
        session_key = _resolve_session_key(token, status_state)
        journal = status_state.resolve_journal()
        if journal is None:
            raise _disabled_503()

        return {
            "session_key": session_key,
            "events": await _session_events(
                journal=journal,
                session_key=session_key,
                turn_limit=turn_limit,
                event_limit=limit,
                after_sequence=after_sequence,
            ),
            "next_cursor": None,
        }

    return r


__all__ = [
    "STATUS_TURNS_DEFAULT_LIMIT",
    "STATUS_TURNS_MAX_LIMIT",
    "StatusState",
    "router",
]
