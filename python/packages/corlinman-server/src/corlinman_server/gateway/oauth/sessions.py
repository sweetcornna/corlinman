"""In-memory OAuth session store for the PKCE flow.

The PKCE flow needs to remember the ``code_verifier`` between the
``/start`` call (which returns the auth URL) and the ``/submit`` call
(which exchanges the authorization code). Persisting the verifier to
disk would defeat its single-use security property, so we keep it in
RAM, keyed by an opaque session id.

Sessions are GC'd after 1 hour. The lock is a threading lock (not
asyncio) so synchronous callers (CLI / tests / FastAPI's threadpool)
can use the helpers without an event loop running.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any

# 1 hour matches the hermes default. The flow itself rarely exceeds 2
# minutes (open URL → authorize → paste code) but operators on slow
# laptops sometimes get interrupted and come back later.
_OAUTH_SESSION_TTL_SECONDS = 60 * 60

_oauth_sessions: dict[str, dict[str, Any]] = {}
_oauth_sessions_lock = threading.Lock()


def create_session(provider: str, *, flow: str = "pkce", **extra: Any) -> tuple[str, dict[str, Any]]:
    """Mint a new session id and store the supplied ``extra`` fields.

    Returns ``(session_id, session_dict)``. The session dict is a *copy*
    of the in-store entry the caller may mutate freely; updates flow
    back via :func:`update_session` so the lock is taken once per write.
    """
    _gc_expired_locked()
    sid = secrets.token_urlsafe(24)
    record: dict[str, Any] = {
        "session_id": sid,
        "provider": provider,
        "flow": flow,
        "created_at_ms": int(time.time() * 1000),
        "expires_at_ms": int(time.time() * 1000) + _OAUTH_SESSION_TTL_SECONDS * 1000,
        "status": "pending",
        **extra,
    }
    with _oauth_sessions_lock:
        _oauth_sessions[sid] = record
    return sid, dict(record)


def get_session(session_id: str) -> dict[str, Any] | None:
    with _oauth_sessions_lock:
        record = _oauth_sessions.get(session_id)
        if record is None:
            return None
        if record["expires_at_ms"] < int(time.time() * 1000):
            _oauth_sessions.pop(session_id, None)
            return None
        return dict(record)


def update_session(session_id: str, **patch: Any) -> dict[str, Any] | None:
    with _oauth_sessions_lock:
        record = _oauth_sessions.get(session_id)
        if record is None:
            return None
        record.update(patch)
        return dict(record)


def drop_session(session_id: str) -> None:
    with _oauth_sessions_lock:
        _oauth_sessions.pop(session_id, None)


def _gc_expired_locked() -> None:
    """Drop expired sessions. Cheap — bounded by TTL window."""
    now_ms = int(time.time() * 1000)
    with _oauth_sessions_lock:
        stale = [sid for sid, rec in _oauth_sessions.items() if rec["expires_at_ms"] < now_ms]
        for sid in stale:
            _oauth_sessions.pop(sid, None)


def session_ttl_seconds() -> int:
    """Expose the TTL constant for the router's ``expires_at_ms`` field."""
    return _OAUTH_SESSION_TTL_SECONDS


# Test-only helper — clears the in-memory store so tests don't leak
# state between runs. Kept under a private name so production callers
# don't accidentally wipe sessions.
def _reset_for_tests() -> None:
    with _oauth_sessions_lock:
        _oauth_sessions.clear()


__all__ = [
    "create_session",
    "drop_session",
    "get_session",
    "session_ttl_seconds",
    "update_session",
]
