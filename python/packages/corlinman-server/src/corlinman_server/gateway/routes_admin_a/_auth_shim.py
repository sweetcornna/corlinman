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
introspection paths in :data:`_PW_CHANGE_ALLOWED_PATHS` (and the onboard
wizard subtree in :data:`_PW_CHANGE_ALLOWED_PREFIXES`) may proceed; every
other route is short-circuited with ``403 password_change_required``.
This closes the first-boot window during which the seeded ``admin/root``
credentials were accepted by every ``/admin/*`` route — see audit issue
SEC-007 in ``audit/evidence/cleanup/SEC-007/``.

CMP-01: the first-run onboard wizard runs *through* this shim, so its
``/admin/onboard/finalize-*`` routes are allowlisted (subtree prefix)
during the forced rotation — otherwise a fresh install can't reach
``POST /admin/onboard/finalize-password`` to clear the flag.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from typing import Any

from fastapi import HTTPException, Request, status

from corlinman_server.gateway.routes_admin_a._session_store import (
    SESSION_COOKIE_NAME,
    extract_cookie,
)
from corlinman_server.gateway.routes_admin_a.state import get_admin_state

# SEC-007: while ``must_change_password`` is set on the admin state, only
# the rotation + introspection paths are allowed to run. Everything else
# is 403'd.
#
# CMP-01: the first-run onboard wizard *does* flow through this shim — the
# ``routes_admin_b`` onboard router mounts ``Depends(require_admin)`` — so
# its routes must be on the allowlist or a fresh install can never rotate
# the seeded ``admin/root`` credentials (the rotation itself happens via
# ``POST /admin/onboard/finalize-password``). The wizard fans out across
# ``/admin/onboard/finalize-skip``, ``-account``, ``-password``,
# ``-persona``, and ``-image-provider``, so a single exact path can't
# cover it; we allow the whole ``/admin/onboard/`` subtree via the prefix
# in :data:`_PW_CHANGE_ALLOWED_PREFIXES` below plus the bare
# ``/admin/onboard`` entry route here.
#
# The remaining entries are exact-match (no prefix) so ``/admin/password``
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
        # Host-token recovery flow — mounted outside this shim today
        # but listed defensively so it stays reachable if it ever moves.
        "/admin/password-reset/request",
        "/admin/password-reset/complete",
    }
)

# CMP-01: path prefixes whose entire subtree is reachable during the
# forced first-run rotation. The onboard wizard's ``finalize-*`` steps all
# live under ``/admin/onboard/`` and must stay reachable so a fresh install
# can complete onboarding (and rotate the seeded password) end-to-end. The
# trailing slash is deliberate so the prefix can't broaden to a sibling
# route like ``/admin/onboard-export`` — only true children match.
_PW_CHANGE_ALLOWED_PREFIXES: tuple[str, ...] = ("/admin/onboard/",)


def _pw_change_path_allowed(path: str) -> bool:
    """True iff ``path`` may run while ``must_change_password`` is set.

    Exact match against :data:`_PW_CHANGE_ALLOWED_PATHS`, then a prefix
    match against :data:`_PW_CHANGE_ALLOWED_PREFIXES` (onboard wizard
    subtree). Everything else is 403'd.
    """
    if path in _PW_CHANGE_ALLOWED_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PW_CHANGE_ALLOWED_PREFIXES)


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


def _dummy_password_hash() -> str | None:
    """SEC-011: the throwaway argon2 hash to verify against when the
    Basic-auth username doesn't match, so the verify cost is constant
    regardless of username correctness. Sourced from ``auth.py`` so the
    argon2 params match the real hashes exactly. ``None`` if the module
    isn't importable (then the caller skips the equalizing verify — the
    request is already failing on ``admin_not_configured`` in that case)."""
    try:
        from corlinman_server.gateway.routes_admin_a.auth import (
            _DUMMY_PASSWORD_HASH,
        )
    except ImportError:
        return None
    return _DUMMY_PASSWORD_HASH


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
        # SEC-011: constant-time username compare + ALWAYS run the argon2
        # verify (against the real hash on a match, a dummy hash otherwise)
        # so response time can't leak whether the username was correct.
        # Combine the booleans at the end — no early-out on the username.
        username_ok = hmac.compare_digest(username, admin_username)
        verify_hash = admin_password_hash if username_ok else _dummy_password_hash()
        if verify_hash is None:
            # auth.py not importable → no dummy hash available. Fall back
            # to verifying against the real hash so the path still runs
            # (a non-matching username can't succeed because we AND with
            # ``username_ok`` below).
            verify_hash = admin_password_hash
        password_ok = _verify_password(password, verify_hash)
        if not (username_ok and password_ok):
            raise _unauthorized("invalid_credentials")

        request.state.admin_user = username
        request.state.admin_session = None
        authenticated_principal = username

    # SEC-007 / CMP-01: credentials verified — now enforce the post-auth
    # must_change_password gate. The allowlist is exact-match for the
    # rotation/introspection routes plus a prefix match for the onboard
    # wizard subtree (``/admin/onboard/*``), which mounts *through* this
    # shim and must stay reachable so a fresh install can finish
    # onboarding and rotate the seeded credentials end-to-end.
    if _must_change_password_active(active_state):
        if not _pw_change_path_allowed(request.url.path):
            raise _password_change_required()

    return authenticated_principal


def require_admin_dependency(request: Request) -> Any:
    """FastAPI dependency: enforce admin auth for admin-A routes."""
    return authenticate_admin_request(request)


def admin_session_tenant(request: Request, state: Any | None = None) -> Any | None:
    """Non-raising cookie-only admin check for the ``/v1`` api-key bridge.

    Returns the operator's :class:`~corlinman_server.tenancy.TenantId` when
    the request carries a **valid** ``corlinman_session`` cookie (an
    authenticated admin browser session), else ``None``.

    Unlike :func:`authenticate_admin_request` this never raises, never falls
    back to HTTP Basic, and never sets a ``WWW-Authenticate`` header — it
    answers exactly one question ("is this a logged-in admin?") so
    :class:`~corlinman_server.gateway.middleware.auth.ApiKeyAuthMiddleware`
    can let the in-app chat UI reach ``/v1/chat/completions`` without the
    operator minting an API key. The browser already ships the HttpOnly,
    ``SameSite=Strict`` session cookie on same-origin requests; that
    ``SameSite=Strict`` attribute is the CSRF guard for the bridged POST.

    Fails closed: a missing/invalid cookie (or an unconfigured session
    store) yields ``None`` so the caller falls through to the normal 401.
    The seeded-credential rotation gate (SEC-007) still applies — while
    ``must_change_password`` is set the bridge stays closed, so a fresh
    install behaves the same on ``/v1/chat`` as it does on ``/admin/*``.
    """
    active_state = state if state is not None else get_admin_state()

    session_store = getattr(active_state, "session_store", None)
    if session_store is None:
        return None
    token = _read_session_cookie(request)
    if token is None:
        return None
    session = session_store.validate(token)
    if session is None:
        return None
    if _must_change_password_active(active_state):
        return None

    # Reflect the resolved principal for downstream handlers / observability,
    # mirroring the cookie branch of authenticate_admin_request.
    user = getattr(session, "user", None)
    if isinstance(user, str):
        request.state.admin_user = user
    request.state.admin_session = session

    from corlinman_server.tenancy import default_tenant

    return getattr(active_state, "default_tenant", None) or default_tenant()


__all__ = [
    "_PW_CHANGE_ALLOWED_PATHS",
    "_PW_CHANGE_ALLOWED_PREFIXES",
    "admin_session_tenant",
    "authenticate_admin_request",
    "require_admin_dependency",
]
