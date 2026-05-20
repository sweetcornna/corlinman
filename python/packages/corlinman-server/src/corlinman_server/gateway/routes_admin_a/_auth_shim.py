"""Admin-auth dependency shared by the admin route bundles.

The Rust ``router_with_state`` mounts every admin sub-router behind
``crate::middleware::admin_auth::require_admin``. The Python route
ports use this dependency for the same fail-closed behavior.

Authentication checks a valid ``corlinman_session`` cookie first, then
falls back to HTTP Basic against the configured admin username and
argon2id password hash. Any missing or invalid credential path raises
``401``.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from fastapi import HTTPException, Request, status

from corlinman_server.gateway.routes_admin_a._session_store import (
    SESSION_COOKIE_NAME,
    extract_cookie,
)
from corlinman_server.gateway.routes_admin_a.state import get_admin_state


def _unauthorized(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "unauthorized", "reason": reason},
        headers={"WWW-Authenticate": 'Basic realm="corlinman-admin"'},
    )


def _parse_basic(header_value: str) -> tuple[str, str] | None:
    if not header_value.lower().startswith("basic "):
        return None
    token = header_value[6:].strip()
    try:
        decoded = base64.b64decode(token, validate=True)
    except (ValueError, binascii.Error):
        return None
    try:
        value = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if ":" not in value:
        return None
    username, _, password = value.partition(":")
    return username, password


def _read_session_cookie(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        return token
    raw = request.headers.get("cookie")
    if raw is None:
        return None
    return extract_cookie(raw, SESSION_COOKIE_NAME)


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        from corlinman_server.gateway.routes_admin_a.auth import (
            argon2_verify,
        )
    except ImportError:
        return False
    return argon2_verify(password, password_hash)


def authenticate_admin_request(request: Request, state: Any | None = None) -> Any:
    """Validate an admin request against the supplied state.

    ``state`` is duck-typed so routes_admin_b can reuse the exact same
    cookie + Basic-auth logic without importing routes_admin_a's
    dataclass.
    """
    active_state = state if state is not None else get_admin_state()

    session_store = getattr(active_state, "session_store", None)
    if session_store is not None:
        token = _read_session_cookie(request)
        if token is not None:
            session = session_store.validate(token)
            if session is not None:
                user = getattr(session, "user", None)
                if isinstance(user, str):
                    request.state.admin_user = user
                request.state.admin_session = session
                return session

    admin_username = getattr(active_state, "admin_username", None)
    admin_password_hash = getattr(active_state, "admin_password_hash", None)
    if not admin_username or not admin_password_hash:
        raise _unauthorized("admin_not_configured")

    auth_header = request.headers.get("authorization")
    if auth_header is None:
        raise _unauthorized("missing_authorization")

    parsed = _parse_basic(auth_header)
    if parsed is None:
        raise _unauthorized("malformed_authorization")

    username, password = parsed
    if username != admin_username or not _verify_password(password, admin_password_hash):
        raise _unauthorized("invalid_credentials")

    request.state.admin_user = username
    request.state.admin_session = None
    return username


def require_admin_dependency(request: Request) -> Any:
    """FastAPI dependency: enforce admin auth for admin-A routes."""
    return authenticate_admin_request(request)


__all__ = ["authenticate_admin_request", "require_admin_dependency"]
