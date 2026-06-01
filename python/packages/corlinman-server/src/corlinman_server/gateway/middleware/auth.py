"""API-key (Bearer) auth middleware for ``/v1/*``.

Python port of ``rust/crates/corlinman-gateway/src/middleware/auth.rs``.
The Rust file is a TODO stub; this implementation tracks the
ultimately-intended contract documented across the gateway:

* Read ``Authorization: Bearer <token>`` (or, as a curl-friendly fallback,
  ``X-API-Key: <token>`` — same precedence as the rest of the codebase).
* Verify the cleartext against
  :meth:`corlinman_server.tenancy.AdminDb.verify_api_key` (sha256 lookup
  against ``tenant_api_keys`` with ``revoked_at_ms IS NULL``).
* On a hit, stash the matching :class:`~corlinman_server.tenancy.ApiKeyRow`
  on ``request.state.api_key`` and the resolved :class:`TenantId` on
  ``request.state.tenant`` so downstream handlers (and the
  ``tenant_scope`` middleware) can read it without a second DB hit.
* On a miss / missing header, short-circuit with HTTP 401 in the same
  envelope shape the Rust admin_auth path uses (``{"error":
  "unauthorized", "reason": "..."}``).

The middleware is **path-scoped**: only requests whose path starts with
one of the configured prefixes (default ``["/v1/"]``) are gated. Public
routes (``/healthz``, ``/metrics``, ``/admin/*`` — admin_auth gates that
prefix) pass through untouched. The path filter avoids accidentally
breaking the unauthenticated bootstrap surface while still failing
closed on the protected one.

Also exposes :func:`require_api_key` — a FastAPI ``Depends`` factory
sibling for handlers that prefer per-route gating over the
middleware-wide path filter. The two paths share
:func:`_verify_token_against_admin_db` so behaviour stays consistent
whichever entry point a route picks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import Depends, HTTPException, Request, status
from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from corlinman_server.tenancy import AdminDb, ApiKeyRow, TenantId

logger = structlog.get_logger(__name__)


#: Path prefixes the middleware gates by default. Mirrors the Rust
#: gateway's ``/v1/*`` mount point. Public-by-design endpoints (metrics,
#: health, the admin surface, the OpenAPI docs) are deliberately absent.
DEFAULT_PROTECTED_PREFIXES: tuple[str, ...] = ("/v1/",)


#: SEC-09: per-prefix required-scope map. After a token verifies, the
#: matched key's scope set must contain the scope the route's prefix
#: demands or the request is rejected 403 ``insufficient_scope``.
#:
#: Scoped to the CHAT endpoints (``/v1/chat`` covers ``/v1/chat/completions``
#: and friends) — the scope every existing prod chat key already holds (mint
#: defaults / docs use ``scope = "chat"``), so this gate is invisible to
#: current chat keys while blocking a key minted for a *narrower* scope
#: (e.g. ``"embeddings"``) from reaching chat. It deliberately does NOT gate
#: the rest of ``/v1/`` (models, voice, memory, plugin callbacks, canvas):
#: those are still api-key-authenticated but not chat-scope-gated, so a
#: plugin webhook or a models listing is not 403'd for lacking ``"chat"``.
#: Longest-prefix match wins so a future ``/v1/embeddings/`` entry can
#: override a broader rule. Super-scopes (see :data:`SUPERSCOPES`) bypass.
DEFAULT_REQUIRED_SCOPES: tuple[tuple[str, str], ...] = (("/v1/chat", "chat"),)


@dataclass
class ApiKeyAuthState:
    """Cloneable bundle of state the API-key middleware reads on every
    request. ``admin_db`` is the single source of truth for active keys;
    ``protected_prefixes`` controls which paths require a token.

    Both fields are mutable so an operator can rotate the AdminDb
    handle (rare) or extend the protected prefix list (more common, e.g.
    adding ``/mcp/`` once the MCP surface goes private) without
    re-installing the middleware.
    """

    admin_db: AdminDb | None = None
    protected_prefixes: tuple[str, ...] = DEFAULT_PROTECTED_PREFIXES
    #: SEC-09: ``(prefix, required_scope)`` pairs enforced after a token
    #: verifies. Defaults to ``/v1/`` → ``"chat"``. Mutable so an operator
    #: can tighten / extend the map without re-installing the middleware.
    required_scopes: tuple[tuple[str, str], ...] = DEFAULT_REQUIRED_SCOPES


# ---------------------------------------------------------------------------
# Helpers — shared by the middleware and the Depends factory.
# ---------------------------------------------------------------------------


def extract_bearer_token(request: HTTPConnection) -> str | None:
    """Pull the bearer token out of ``Authorization`` / ``X-API-Key``.

    Order: ``Authorization: Bearer <token>`` first (mirrors the Rust
    precedence + the rest of the Python codebase), then ``X-API-Key``
    as a curl / SDK fallback. Returns ``None`` if neither header carries
    a usable token.

    Accepts any :class:`~starlette.requests.HTTPConnection` (both
    :class:`Request` and :class:`WebSocket`) since it only reads headers.
    """

    auth = request.headers.get("authorization")
    if auth is not None:
        # Case-insensitive prefix match — RFC 7235 says the scheme is
        # case-insensitive, and clients (curl, fetch) routinely send
        # ``bearer ...`` lowercased.
        if auth[:7].lower() == "bearer ":
            token = auth[7:].strip()
            if token:
                return token

    api_key = request.headers.get("x-api-key")
    if api_key is not None:
        token = api_key.strip()
        if token:
            return token

    return None


def _unauthorized(reason: str) -> JSONResponse:
    """401 response in the shape the rest of the gateway uses."""

    return JSONResponse(
        {"error": "unauthorized", "reason": reason},
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": 'Bearer realm="corlinman"'},
    )


def _forbidden(reason: str, *, required: str | None = None) -> JSONResponse:
    """403 response for an authenticated-but-under-scoped key (SEC-09).

    Same envelope shape as :func:`_unauthorized` so clients parse one
    error format; ``required`` names the scope the route demanded so an
    operator can see why a key was rejected.
    """

    body: dict[str, Any] = {"error": "forbidden", "reason": reason}
    if required is not None:
        body["required_scope"] = required
    return JSONResponse(body, status_code=status.HTTP_403_FORBIDDEN)


def parse_scopes(scope: str) -> frozenset[str]:
    """Split a stored ``scope`` string (``"a,b"`` / ``"a b"``) into a set.

    SEC-09: keys store ``scope`` as a free-form comma- (or whitespace-)
    separated string. Splitting here gives the enforcement path a stable
    membership test. Empty / blank yields an empty set (which fails any
    non-empty required-scope check — fail closed)."""

    parts = [p.strip() for p in scope.replace(",", " ").split()]
    return frozenset(p for p in parts if p)


#: Super-scopes that satisfy ANY per-route required scope. A key granted a
#: super-scope (an operator / admin / all-access key) reaches every gated
#: route; only a *narrower* scope (e.g. ``"embeddings"``) is held back from
#: ``"chat"`` (SEC-09). Without this, a broad ``scope="full"`` key would be
#: wrongly 403'd from /v1/chat.
SUPERSCOPES: frozenset[str] = frozenset({"*", "full", "admin"})


def scope_satisfies(granted: frozenset[str], needed: str) -> bool:
    """Whether a key's granted scope set satisfies a required scope.

    True when the exact scope is granted OR the key holds a super-scope
    (``*`` / ``full`` / ``admin``). An empty grant satisfies nothing (fail
    closed)."""

    return needed in granted or bool(granted & SUPERSCOPES)


def required_scope_for_path(
    path: str, required_scopes: tuple[tuple[str, str], ...]
) -> str | None:
    """Return the scope a ``path`` requires, or ``None`` if none.

    Longest matching prefix wins so a narrow ``/v1/embeddings/`` rule can
    override a broad ``/v1/`` rule regardless of declaration order."""

    best: tuple[int, str] | None = None
    for prefix, scope in required_scopes:
        if path.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), scope)
    return best[1] if best is not None else None


async def _verify_token_against_admin_db(
    admin_db: AdminDb, token: str
) -> ApiKeyRow | None:
    """Thin wrapper so the middleware + Depends path share one code path.

    Returns the matched :class:`ApiKeyRow` on success or ``None`` on miss
    / revoked / unknown. DB errors propagate — boot wiring should catch
    them at install time, not on every request.
    """

    return await admin_db.verify_api_key(token)


def _resolve_state(request: Request) -> ApiKeyAuthState | None:
    """Pull the auth state off ``app.state``. Returns ``None`` if the
    middleware was installed without an explicit state and boot never
    populated ``app.state.api_key_auth`` either — in that case the
    middleware fails closed (401)."""

    state = getattr(request.app.state, "api_key_auth", None)
    if isinstance(state, ApiKeyAuthState):
        return state
    return None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """Gate ``/v1/*`` (configurable) behind a tenant API key.

    Construction takes an explicit :class:`ApiKeyAuthState`. The state
    is also published on ``app.state.api_key_auth`` so the
    :func:`require_api_key` :class:`Depends` factory (and routes that
    want to peek without re-validating) can pick it up.
    """

    def __init__(
        self,
        app: ASGIApp,
        state: ApiKeyAuthState | None = None,
    ) -> None:
        super().__init__(app)
        self._state = state or ApiKeyAuthState()

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # Re-resolve so boot can rebind the state after install.
        state = _resolve_state(request) or self._state

        if not _path_is_protected(request.url.path, state.protected_prefixes):
            return await call_next(request)

        if state.admin_db is None:
            # Fail closed: protected route but nothing to verify against.
            logger.warning(
                "api_key_auth.no_admin_db",
                path=request.url.path,
            )
            return _unauthorized("admin_db_not_configured")

        token = extract_bearer_token(request)
        if token is None:
            return _unauthorized("missing_authorization")

        try:
            row = await _verify_token_against_admin_db(state.admin_db, token)
        except Exception as exc:  # noqa: BLE001 — surface as 401, log details
            logger.warning(
                "api_key_auth.verify_failed",
                path=request.url.path,
                error=str(exc),
            )
            return _unauthorized("verify_failed")

        if row is None:
            return _unauthorized("invalid_token")

        # SEC-09: enforce the per-prefix required scope. ``/v1/*`` demands
        # ``"chat"`` by default — a key minted for a narrower scope is
        # authenticated but not authorized for this surface.
        needed = required_scope_for_path(
            request.url.path, state.required_scopes
        )
        if needed is not None and not scope_satisfies(parse_scopes(row.scope), needed):
            logger.info(
                "api_key_auth.insufficient_scope",
                path=request.url.path,
                required=needed,
                key_id=row.key_id,
            )
            return _forbidden("insufficient_scope", required=needed)

        # Stash on request.state so handlers + tenant_scope can read.
        request.state.api_key = row
        request.state.tenant = row.tenant_id

        return await call_next(request)


def _path_is_protected(path: str, prefixes: tuple[str, ...]) -> bool:
    """Whether ``path`` falls under one of the gated prefixes."""

    return any(path.startswith(p) for p in prefixes)


def install_api_key_middleware(
    app: Any,
    *,
    admin_db: AdminDb | None = None,
    protected_prefixes: tuple[str, ...] = DEFAULT_PROTECTED_PREFIXES,
    required_scopes: tuple[tuple[str, str], ...] = DEFAULT_REQUIRED_SCOPES,
) -> ApiKeyAuthState:
    """Attach :class:`ApiKeyAuthMiddleware` to ``app``.

    Returns the :class:`ApiKeyAuthState` instance so the caller can
    rebind ``admin_db`` later (e.g. after lazy tenancy init in boot).
    The same instance is also published on ``app.state.api_key_auth``.

    SEC-09: ``required_scopes`` defaults to ``/v1/`` → ``"chat"``; pass
    an extended map to demand additional per-prefix scopes.
    """

    state = ApiKeyAuthState(
        admin_db=admin_db,
        protected_prefixes=protected_prefixes,
        required_scopes=required_scopes,
    )
    app.state.api_key_auth = state
    app.add_middleware(ApiKeyAuthMiddleware, state=state)
    return state


# ---------------------------------------------------------------------------
# FastAPI ``Depends`` factory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthenticatedApiKey:
    """The successful result of :func:`require_api_key`.

    Carries the matched :class:`ApiKeyRow` plus its resolved
    :class:`TenantId` so handlers don't have to import the tenancy
    module to reach into the row.
    """

    api_key: ApiKeyRow
    tenant: TenantId = field(init=False)

    def __post_init__(self) -> None:
        # ApiKeyRow already holds the TenantId; expose it on the wrapper
        # so handlers can write ``auth.tenant`` rather than
        # ``auth.api_key.tenant_id``.
        object.__setattr__(self, "tenant", self.api_key.tenant_id)


def require_api_key(required_scope: str | None = None) -> Any:
    """Return a FastAPI dependency that validates the request's bearer
    token and resolves it to an :class:`AuthenticatedApiKey`.

    Usage::

        @router.get("/v1/something")
        async def handler(auth: AuthenticatedApiKey = require_api_key()):
            ...

    Raises :class:`HTTPException` 401 with the same envelope shape the
    middleware uses. Handlers that already sit behind
    :class:`ApiKeyAuthMiddleware` can skip this — ``request.state.api_key``
    is already populated.

    SEC-09: ``required_scope`` (explicit, optional) plus the per-prefix
    :data:`DEFAULT_REQUIRED_SCOPES` map (resolved from the request path)
    are both enforced after a successful verify; a key whose scope set
    lacks the required scope raises **403 ``insufficient_scope``**. The
    explicit argument wins when both resolve so a route can demand a
    tighter scope than its prefix implies. ``/v1/*`` defaults to
    ``"chat"`` (held by existing prod keys).
    """

    def _enforce_scope(request: Request, row: ApiKeyRow) -> None:
        state = _resolve_state(request)
        prefix_scope = required_scope_for_path(
            request.url.path,
            state.required_scopes if state is not None else DEFAULT_REQUIRED_SCOPES,
        )
        needed = required_scope or prefix_scope
        if needed is not None and not scope_satisfies(parse_scopes(row.scope), needed):
            logger.info(
                "api_key_auth.depends.insufficient_scope",
                path=request.url.path,
                required=needed,
                key_id=row.key_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden",
                    "reason": "insufficient_scope",
                    "required_scope": needed,
                },
            )

    async def dependency(request: Request) -> AuthenticatedApiKey:
        # Reuse a row stashed by the middleware if present — avoids a
        # second DB round-trip for routes that have both gates wired.
        existing = getattr(request.state, "api_key", None)
        if isinstance(existing, ApiKeyRow):
            _enforce_scope(request, existing)
            return AuthenticatedApiKey(api_key=existing)

        state = _resolve_state(request)
        if state is None or state.admin_db is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "unauthorized",
                    "reason": "admin_db_not_configured",
                },
                headers={"WWW-Authenticate": 'Bearer realm="corlinman"'},
            )

        token = extract_bearer_token(request)
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthorized", "reason": "missing_authorization"},
                headers={"WWW-Authenticate": 'Bearer realm="corlinman"'},
            )

        try:
            row = await _verify_token_against_admin_db(state.admin_db, token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("api_key_auth.depends.verify_failed", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthorized", "reason": "verify_failed"},
                headers={"WWW-Authenticate": 'Bearer realm="corlinman"'},
            ) from exc

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthorized", "reason": "invalid_token"},
                headers={"WWW-Authenticate": 'Bearer realm="corlinman"'},
            )

        _enforce_scope(request, row)
        request.state.api_key = row
        request.state.tenant = row.tenant_id
        return AuthenticatedApiKey(api_key=row)

    return Depends(dependency)


__all__ = [
    "DEFAULT_PROTECTED_PREFIXES",
    "DEFAULT_REQUIRED_SCOPES",
    "ApiKeyAuthMiddleware",
    "ApiKeyAuthState",
    "AuthenticatedApiKey",
    "extract_bearer_token",
    "install_api_key_middleware",
    "parse_scopes",
    "require_api_key",
    "required_scope_for_path",
]
