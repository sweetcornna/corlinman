"""Public, token-gated status-card routes.

The ``agent_status_card`` tool mints links shaped as ``/status/{token}``.
This module verifies that token, scopes every read to the embedded
``session_key``, and returns the read-only trajectory summary consumed by the
public status page. It deliberately lives outside the admin route tree.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path as FsPath
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query

from corlinman_server.gateway.status_token import (
    resolve_signing_key,
    verify_status_token,
)

STATUS_TURNS_DEFAULT_LIMIT = 25
STATUS_TURNS_MAX_LIMIT = 100

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


def router(state: StatusState | None = None) -> APIRouter:
    r = APIRouter(tags=["status"])
    status_state = state or StatusState()

    @r.get("/status/{token}")
    async def get_status_card(
        token: str = Path(..., description="Signed status-card token."),
        limit: int = Query(
            STATUS_TURNS_DEFAULT_LIMIT,
            ge=1,
            le=STATUS_TURNS_MAX_LIMIT,
            description="Max recent turns to include.",
        ),
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

    return r


__all__ = [
    "STATUS_TURNS_DEFAULT_LIMIT",
    "STATUS_TURNS_MAX_LIMIT",
    "StatusState",
    "router",
]
