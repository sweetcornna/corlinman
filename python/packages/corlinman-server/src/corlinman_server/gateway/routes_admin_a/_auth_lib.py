"""``/admin/login``, ``/admin/logout``, ``/admin/me``,
``/admin/onboard``, ``/admin/password`` — session lifecycle + admin
credential rotation.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/auth.rs``.

These routes mount **outside** the ``require_admin`` middleware on the
Rust side — each handler does its own credential check (argon2 verify
or cookie validate) so the chicken-and-egg "you need a cookie to set
your first cookie" problem doesn't apply. The Python port preserves
that pattern; the router built by :func:`router` does **not** depend
on :func:`require_admin_dependency`.

Mechanical extract-and-reimport sibling of
:mod:`corlinman_server.gateway.routes_admin_a.auth`: holds the wire
shapes, brute-force-guard constants, the stateless
:class:`AdminLoginFailureStore` limiter, and the pure helpers. The
security core (hashers, cookie/session plumbing, persistence, and the
router) stays in ``auth.py``, which re-imports everything moved here so
its public surface is unchanged. This module must never import the
source module (no cycle).
"""

from __future__ import annotations

import datetime as _dt
import math
import re
import threading
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel

# Minimum length operators must use when picking the admin password.
MIN_PASSWORD_LEN = 8

# Username constraints. Mirrors the slug regex hermes uses for profiles —
# ASCII alphanumerics + ``_`` + ``-`` only, capped so the UI can render
# the value without truncation gymnastics.
USERNAME_MAX_LEN = 64
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Default idle TTL for admin sessions (24 hours). Mirrors
# ``DEFAULT_SESSION_TTL_SECS`` on the Rust side.
DEFAULT_SESSION_TTL_SECS = 86_400


# Login brute-force guard. The fixed window is deliberately short enough
# to avoid operator lockouts after typo bursts while still slowing online
# guessing attacks against a single client-IP/username pair.
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 60


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class AdminLoginFailureStore:
    """Small in-memory fixed-window limiter for ``/admin/login`` failures.

    Keys include both client IP and submitted username so a typo burst for
    one operator identity does not lock every admin username behind the same
    proxy, while repeated guesses for the same pair are throttled.
    """

    def __init__(
        self,
        *,
        limit: int = LOGIN_FAILURE_LIMIT,
        window_seconds: int = LOGIN_FAILURE_WINDOW_SECONDS,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._now = now
        self._lock = threading.Lock()
        self._failures: dict[tuple[str, str], tuple[int, float]] = {}

    def _clock(self) -> float:
        if self._now is not None:
            return self._now()
        return _dt.datetime.now(_dt.UTC).timestamp()

    def retry_after(self, *, client_ip: str, username: str) -> int | None:
        """Return remaining lockout seconds when the key is throttled."""
        key = (client_ip, username)
        now = self._clock()
        with self._lock:
            item = self._failures.get(key)
            if item is None:
                return None
            count, reset_at = item
            if now >= reset_at:
                self._failures.pop(key, None)
                return None
            if count >= self.limit:
                return max(1, math.ceil(reset_at - now))
            return None

    def record_failure(self, *, client_ip: str, username: str) -> int | None:
        """Increment the failure count and return a Retry-After if locked."""
        key = (client_ip, username)
        now = self._clock()
        with self._lock:
            count, reset_at = self._failures.get(key, (0, now + self.window_seconds))
            if now >= reset_at:
                count = 0
                reset_at = now + self.window_seconds
            count += 1
            self._failures[key] = (count, reset_at)
            if count >= self.limit:
                return max(1, math.ceil(reset_at - now))
            return None

    def clear(self, *, client_ip: str, username: str) -> None:
        """Forget failure history for a successfully authenticated pair."""
        with self._lock:
            self._failures.pop((client_ip, username), None)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    expires_in: int


class MeResponse(BaseModel):
    user: str
    created_at: str
    expires_at: str
    # ``True`` while the in-memory credentials are still the first-boot
    # default (``admin``/``root``). The UI watches this flag and force-
    # redirects to ``/account/security`` after login so the operator
    # picks a real password before doing anything else. The
    # ``/admin/password`` endpoint flips it (and persists the flip to
    # the on-disk ``[admin]`` block) once a fresh password lands.
    must_change_password: bool = False


class OnboardRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class ChangeUsernameRequest(BaseModel):
    """Wire shape for ``POST /admin/username``.

    Mirrors the rotate-password pattern: the operator authenticates with
    their *current* password (in addition to the session cookie) before
    we accept the rename. We never read or rewrite ``new_password`` here
    — the existing argon2 hash is re-persisted verbatim alongside the
    new username so a single endpoint covers the "I picked a bad
    username during onboarding" recovery path without forcing a fresh
    password rotation.
    """

    old_password: str
    new_username: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """Best-effort client IP extraction for login throttling keys."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


def _too_many_login_attempts(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"error": "too_many_login_attempts"},
        headers={"Retry-After": str(retry_after)},
    )


def _iso(dt: _dt.datetime) -> str:
    """RFC-3339 / ISO-8601 UTC string."""
    return dt.astimezone(_dt.UTC).isoformat().replace("+00:00", "Z")


def _service_unavailable(error: str, message: str | None = None) -> HTTPException:
    payload: dict[str, Any] = {"error": error}
    if message is not None:
        payload["message"] = message
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=payload)


def _unauthorized(error: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": error})
