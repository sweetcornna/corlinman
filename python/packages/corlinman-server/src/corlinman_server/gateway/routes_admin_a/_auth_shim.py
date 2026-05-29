"""Admin-auth dependency shared by the admin route bundles.

The Rust ``router_with_state`` mounts every admin sub-router behind
``crate::middleware::admin_auth::require_admin``. The Python route
ports use this dependency for the same fail-closed behavior.

Authentication checks a valid ``corlinman_session`` cookie first, then
falls back to HTTP Basic against the configured admin username and
argon2id password hash. Any missing or invalid credential path raises
``401``.

SEC-007: after credentials verify, the shim consults
``state.must_change_password`` (falling back to ``routes_admin_a``'s
canonical state — that's where the seed flag lives — when the per-bundle
state doesn't carry the field). If the flag is set, only the rotation +
introspection paths in :data:`_PW_CHANGE_ALLOWED_PATHS` may proceed;
every other route is short-circuited with ``403 password_change_required``.
This closes the first-boot window during which the seeded ``admin/root``
credentials were accepted by every ``/admin/*`` route — see audit issue
SEC-007 in ``audit/evidence/cleanup/SEC-007/``.
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

# SEC-007: while ``must_change_password`` is set on the admin state, only
# the rotation + introspection paths are allowed to run. Everything else
# is 403'd. The paths below are all mounted **outside** this shim today
# (their handlers do their own per-request credential check), so listing
# them here is belt-and-braces — if a future refactor accidentally moves
# one inside ``require_admin_dependency`` the allowlist keeps recovery
# possible. Exact-match only — no prefix matching — so ``/admin/password``
# can never accidentally allow ``/admin/password-reset/...`` or vice
# versa. Health endpoints (``/healthz``, ``/readyz``) live outside the
# ``/admin/*`` tree and do not flow through this shim, so they don't
# need to be listed here.
_PW_CHANGE_ALLOWED_PATHS: frozenset[str] = frozenset(
    {
        "/admin/me",
        "/admin/login",
        "/admin/logout",
        "/admin/password",
        "/admin/username",
        "/admin/onboard",
        # Onboard finalize lands the initial provider config after
        # admin credentials exist; it has to stay reachable so a
        # forced rotation can complete end-to-end.
        "/admin/onboard/finalize",
        # Host-token recovery flow — mounted outside this shim today
        # but listed defensively so it stays reachable if it ever moves.
        "/admin/password-reset/request",
        "/admin/password-reset/complete",
    }
)


def _unauthorized(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "unauthorized", "reason": reason},
        headers={"WWW-Authenticate": 'Basic realm="corlinman-admin"'},
    )


def _password_change_required() -> HTTPException:
    """The seeded ``admin/root`` credentials are still active. Refuse
    every non-rotation route until the operator picks a real password.
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "password_change_required",
            "message": (
                "rotate the seeded admin/root credentials at "
                "/account/security (or via POST /admin/password) before "
                "using any other admin endpoint"
            ),
        },
        headers={"WWW-Authenticate": 'Basic realm="corlinman-admin"'},
    )


def _must_change_password_active(state: Any) -> bool:
    """True iff the must_change_password gate should fire for this request.

    Reads ``must_change_password`` directly off the passed-in ``state``.
    Both :class:`routes_admin_a.AdminState` and
    :class:`routes_admin_b.AdminState` carry the field (the boot path
    syncs them from the seeded value); the rotation handlers in
    ``auth.py`` + ``onboard.py`` clear both copies when the password
    flips. We deliberately do **not** reach into the admin-A singleton
    as a fallback — that would create cross-bundle test pollution any
    time an admin-A test left state behind in the module-global slot.
    """
    flag = getattr(state, "must_change_password", None)
    return bool(flag) if isinstance(flag, bool) else False


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

    SEC-007: after a successful credential check, if
    ``must_change_password`` is active on the seeded admin state and the
    request path is **not** in :data:`_PW_CHANGE_ALLOWED_PATHS`, we
    short-circuit with 403 ``password_change_required`` so the seeded
    ``admin/root`` credentials cannot drive any other admin surface.
    """
    active_state = state if state is not None else get_admin_state()

    authenticated_principal: Any | None = None

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
                authenticated_principal = session

    if authenticated_principal is None:
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
        if username != admin_username or not _verify_password(
            password, admin_password_hash
        ):
            raise _unauthorized("invalid_credentials")

        request.state.admin_user = username
        request.state.admin_session = None
        authenticated_principal = username

    # SEC-007: credentials verified — now enforce the post-auth
    # must_change_password gate. Exact-path allowlist only (no prefix
    # match). The five-or-so rotation routes mount outside this shim
    # today so they're already exempt; the list below is defensive in
    # case a future refactor moves one inside ``require_admin_dependency``.
    if _must_change_password_active(active_state):
        if request.url.path not in _PW_CHANGE_ALLOWED_PATHS:
            raise _password_change_required()

    return authenticated_principal


def require_admin_dependency(request: Request) -> Any:
    """FastAPI dependency: enforce admin auth for admin-A routes."""
    return authenticate_admin_request(request)


__all__ = [
    "_PW_CHANGE_ALLOWED_PATHS",
    "authenticate_admin_request",
    "require_admin_dependency",
]
