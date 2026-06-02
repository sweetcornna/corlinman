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
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hmac
import ipaddress
import re
import threading
from pathlib import Path
from typing import Annotated, Any, cast

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from corlinman_server.gateway.lifecycle.admin_seed import (
    _merge_admin_block,
    _render_admin_block,
)
from corlinman_server.gateway.routes_admin_a._session_store import (
    SESSION_COOKIE_NAME,
    AdminSessionStore,
    extract_cookie,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)

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


# ``argon2-cffi`` is the shared hashing implementation already pinned
# in the server package's deps. Constructed once at module import time
# so we don't pay the parameter setup cost per call.
_HASHER = PasswordHasher()

# SEC-011: a precomputed argon2 hash of a throwaway secret. When the
# submitted username doesn't match the configured admin we still run
# :func:`argon2_verify` against THIS hash (never the real one) so the
# total verify cost is identical whether or not the username was
# correct — denying the attacker a timing oracle on the username.
# Computed once at import (the params dominate, the input is irrelevant).
_DUMMY_PASSWORD_HASH = _HASHER.hash("corlinman-timing-equalizer")

# Module-level fallback lock used by the onboard + password routes when
# the AdminState doesn't carry one. Both routes hold it across the
# precondition-check + atomic write so a racing sibling sees the
# winner's state.
_FALLBACK_ADMIN_WRITE_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
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


def hash_password(password: str) -> str:
    """Hash a plaintext password with argon2id. Wrapper around
    :class:`PasswordHasher` so the call-sites all agree on the
    instance + params."""
    return _HASHER.hash(password)


def argon2_verify(password: str, encoded: str) -> bool:
    """Constant-time verify of ``password`` against an argon2 PHC
    string. Returns ``False`` on any mismatch (including malformed
    encodings) — matches the Rust ``argon2_verify`` contract."""
    try:
        return _HASHER.verify(encoded, password)
    except VerifyMismatchError:
        return False
    except Exception:
        # Malformed hash / wrong algorithm — treat as mismatch rather
        # than 500. The Rust side does the same via the typed
        # ``PasswordHash::new`` error returning false.
        return False


def _remote_ip(request: Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Best-effort peer IP extraction from Starlette's request scope."""
    client = request.client
    if client is None or not client.host:
        return None
    try:
        return ipaddress.ip_address(client.host)
    except ValueError:
        return None


def _trusted_forwarded_proto_cidrs(
    state: AdminState,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Return configured trusted proxy CIDRs for ``X-Forwarded-Proto``.

    ``[server].trusted_forwarded_proto_proxies`` is the production knob.
    For compatibility with the simpler ``[server].trust_forwarded_proto =
    true`` shape, boot wiring may set ``trust_forwarded_proto`` without
    explicit CIDRs; in that case only loopback peers are trusted. That keeps
    the flag useful for the documented local reverse-proxy deploy without
    trusting arbitrary clients that can spoof forwarded headers.
    """
    raw_cidrs = tuple(getattr(state, "trusted_forwarded_proto_proxies", ()) or ())
    if not raw_cidrs and bool(getattr(state, "trust_forwarded_proto", False)):
        raw_cidrs = ("127.0.0.0/8", "::1/128")

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in raw_cidrs:
        try:
            networks.append(ipaddress.ip_network(str(raw), strict=False))
        except ValueError:
            # Invalid operator config should fail closed for this request;
            # boot-time validation can grow around the dict-shaped config later.
            continue
    return tuple(networks)


def _request_from_trusted_forwarded_proto_proxy(request: Request, state: AdminState) -> bool:
    """True when the direct peer may supply ``X-Forwarded-Proto``."""
    remote = _remote_ip(request)
    if remote is None:
        return False
    return any(remote in network for network in _trusted_forwarded_proto_cidrs(state))


def _request_is_https(request: Request, state: AdminState) -> bool:
    """True iff the request reached us over TLS.

    Reverse-proxy TLS termination is supported, but ``X-Forwarded-Proto`` is
    only honored when the direct peer is a configured trusted proxy. Direct
    clients can otherwise spoof the header and trick the admin login into
    emitting a ``Secure`` cookie on plain HTTP.
    """
    forwarded = request.headers.get("x-forwarded-proto")
    if forwarded is not None and _request_from_trusted_forwarded_proto_proxy(request, state):
        # The header can carry a comma-separated proxy chain; the
        # client-facing (left-most) hop is what matters.
        first = forwarded.split(",", 1)[0].strip().lower()
        if first == "https":
            return True
    return request.url.scheme == "https"


def _session_cookie_secure(request: Request, state: AdminState) -> bool:
    """Resolve whether the admin session cookie should carry ``Secure``.

    ``[admin].session_cookie_secure`` is an explicit operator override and
    therefore wins over auto-detection from request scheme / trusted proxy
    headers. ``None`` preserves the previous automatic behavior.
    """
    configured = getattr(state, "session_cookie_secure", None)
    if configured is not None:
        return bool(configured)
    return _request_is_https(request, state)


def _set_cookie_header(token: str, max_age_seconds: int, *, secure: bool) -> str:
    """Build the ``Set-Cookie`` header value matching the Rust
    ``set_cookie_header`` — ``HttpOnly``, ``SameSite=Strict``,
    ``Path=/``.

    SEC-009: the ``Secure`` flag is appended **only** when the request
    arrived over https (``secure=True``). The documented deploy
    terminates TLS upstream and forwards plain http on the loopback
    hop, so unconditionally setting ``Secure`` would silently drop the
    cookie there; we add it conditionally so https deploys are hardened
    while http-only local/dev keeps working."""
    header = (
        f"{SESSION_COOKIE_NAME}={token}; "
        f"HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age_seconds}"
    )
    if secure:
        header += "; Secure"
    return header


def _clear_cookie_header(*, secure: bool) -> str:
    """``Set-Cookie`` header value that clears the session cookie."""
    header = f"{SESSION_COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
    if secure:
        header += "; Secure"
    return header


def _iso(dt: _dt.datetime) -> str:
    """RFC-3339 / ISO-8601 UTC string."""
    return dt.astimezone(_dt.UTC).isoformat().replace("+00:00", "Z")


def _ensure_session_store(state: AdminState) -> AdminSessionStore:
    """Return the active session store, creating a default one when the
    bootstrapper didn't pre-build one. We **mutate** the state so
    every route sees the same store — equivalent to the Rust side
    handing one ``Arc`` around."""
    store = state.session_store
    if store is None:
        store = AdminSessionStore(state.session_ttl_seconds)
        state.session_store = store
    if not isinstance(store, AdminSessionStore):
        # Bootstrapper handed us a foreign session-store impl. Trust
        # it — the test harness may swap in a mock. Caller is on the
        # hook for the API shape.
        return cast("AdminSessionStore", store)
    return store


def _read_session_cookie(request: Request) -> str | None:
    """Extract the session cookie from the incoming request."""
    # FastAPI's ``request.cookies`` already parses the header; fall
    # back to the raw header parsing for tests that build a Request
    # directly without going through Starlette's cookie middleware.
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        return token
    raw = request.headers.get("cookie")
    if raw is None:
        return None
    return extract_cookie(raw, SESSION_COOKIE_NAME)


def _service_unavailable(error: str, message: str | None = None) -> HTTPException:
    payload: dict[str, Any] = {"error": error}
    if message is not None:
        payload["message"] = message
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=payload)


def _unauthorized(error: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": error})


async def _atomic_write(path: Path, contents: str) -> None:
    """Async-friendly atomic write: ``<path>.new`` then ``os.replace``.
    The file IO itself is synchronous (the bytes are tiny — admin
    config rather than streaming data), but we offload to a thread
    so the event loop stays free."""

    def _do() -> None:
        parent = path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".new")
        tmp.write_text(contents, encoding="utf-8")
        import os as _os

        _os.replace(tmp, path)

    await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for the session + credential-rotation endpoints.

    **Not** wrapped in the admin-auth dependency — each handler does
    its own credential / cookie check inline."""
    r = APIRouter()

    @r.post(
        "/admin/login",
        response_model=LoginResponse,
        summary="Issue a session cookie",
    )
    async def login(
        body: LoginRequest,
        request: Request,
        response: Response,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> LoginResponse:
        if state.admin_username is None or state.admin_password_hash is None:
            raise _service_unavailable("admin_not_configured")

        # SEC-011: constant-time username compare + ALWAYS run the argon2
        # verify (against the real hash on a username match, the dummy
        # hash otherwise) so the response time can't leak whether the
        # username was correct. Combine the two booleans at the end — no
        # early-out on the username.
        username_ok = hmac.compare_digest(body.username, state.admin_username)
        verify_hash = state.admin_password_hash if username_ok else _DUMMY_PASSWORD_HASH
        password_ok = argon2_verify(body.password, verify_hash)
        if not (username_ok and password_ok):
            raise _unauthorized("invalid_credentials")

        store = _ensure_session_store(state)
        token = store.create(body.username)
        max_age = (
            store.ttl_seconds() if hasattr(store, "ttl_seconds") else state.session_ttl_seconds
        )

        response.headers["set-cookie"] = _set_cookie_header(
            token, max_age, secure=_session_cookie_secure(request, state)
        )
        return LoginResponse(token=token, expires_in=max_age)

    @r.post(
        "/admin/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Clear the session cookie",
    )
    async def logout(
        request: Request,
        response: Response,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> Response:
        token = _read_session_cookie(request)
        if token and state.session_store is not None:
            try:
                state.session_store.invalidate(token)
            except Exception:
                # Best-effort — the cookie clear below still happens.
                pass
        # 204 NO_CONTENT must not have a body; build the response
        # explicitly so FastAPI doesn't append JSON null.
        out = Response(status_code=status.HTTP_204_NO_CONTENT)
        out.headers["set-cookie"] = _clear_cookie_header(
            secure=_session_cookie_secure(request, state)
        )
        return out

    @r.get(
        "/admin/me",
        response_model=MeResponse,
        summary="Inspect the current session",
    )
    async def me(
        request: Request,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> MeResponse:
        if state.session_store is None:
            raise _unauthorized("unauthenticated")
        token = _read_session_cookie(request)
        if token is None:
            raise _unauthorized("unauthenticated")
        session = state.session_store.validate(token)
        if session is None:
            raise _unauthorized("session_expired")
        ttl = (
            state.session_store.ttl()
            if hasattr(state.session_store, "ttl")
            else _dt.timedelta(seconds=state.session_ttl_seconds)
        )
        expires_at = session.last_used + ttl
        return MeResponse(
            user=session.user,
            created_at=_iso(session.created_at),
            expires_at=_iso(expires_at),
            must_change_password=bool(state.must_change_password),
        )

    @r.post("/admin/onboard", summary="First-run admin bootstrap")
    async def onboard(
        body: OnboardRequest,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, str]:
        username = body.username.strip()
        if not username:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_username",
                    "message": "username must be non-empty",
                },
            )
        if len(body.password) < MIN_PASSWORD_LEN:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "weak_password",
                    "message": (f"password must be at least {MIN_PASSWORD_LEN} characters"),
                },
            )

        lock = state.admin_write_lock or _FALLBACK_ADMIN_WRITE_LOCK
        async with _lock_async(lock):
            if state.admin_username is not None or state.admin_password_hash is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "already_onboarded",
                        "message": (
                            "admin credentials are already configured; "
                            "use POST /admin/password to rotate"
                        ),
                    },
                )
            await _persist_admin_credentials(state, username, body.password)
        return {"status": "ok"}

    @r.post("/admin/password", summary="Rotate the admin password")
    async def change_password(
        body: ChangePasswordRequest,
        request: Request,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, str]:
        if state.session_store is None:
            raise _service_unavailable("session_store_missing")
        token = _read_session_cookie(request)
        session = state.session_store.validate(token) if token else None
        if session is None:
            raise _unauthorized("unauthenticated")

        lock = state.admin_write_lock or _FALLBACK_ADMIN_WRITE_LOCK
        async with _lock_async(lock):
            if state.admin_username is None or state.admin_password_hash is None:
                raise _service_unavailable("admin_not_configured")
            if session.user != state.admin_username:
                raise _unauthorized("session_user_mismatch")
            if not argon2_verify(body.old_password, state.admin_password_hash):
                raise _unauthorized("invalid_old_password")
            if len(body.new_password) < MIN_PASSWORD_LEN:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "error": "weak_password",
                        "message": (f"password must be at least {MIN_PASSWORD_LEN} characters"),
                    },
                )
            await _persist_admin_credentials(
                state,
                state.admin_username,
                body.new_password,
                must_change_password=False,
            )
            # A successful rotation clears the first-boot warning flag
            # both in-memory (so ``/admin/me`` reflects it immediately)
            # and on disk (handled inside ``_persist_admin_credentials``
            # which writes the merged ``[admin]`` block).
            state.must_change_password = False
        return {"status": "ok"}

    @r.post("/admin/username", summary="Change the admin username")
    async def change_username(
        body: ChangeUsernameRequest,
        request: Request,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, str]:
        # Mirror ``change_password``: session cookie first, then critical
        # section under the admin write lock so the verify-then-persist
        # transition is atomic against a concurrent rotation.
        if state.session_store is None:
            raise _service_unavailable("session_store_missing")
        token = _read_session_cookie(request)
        session = state.session_store.validate(token) if token else None
        if session is None:
            raise _unauthorized("unauthenticated")

        new_username = body.new_username.strip()
        if not new_username or len(new_username) > USERNAME_MAX_LEN:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_username",
                    "message": (f"username must be 1..{USERNAME_MAX_LEN} characters"),
                },
            )
        if _USERNAME_RE.match(new_username) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_username",
                    "message": (
                        "username must contain only ASCII letters, digits, underscores, and hyphens"
                    ),
                },
            )

        lock = state.admin_write_lock or _FALLBACK_ADMIN_WRITE_LOCK
        async with _lock_async(lock):
            if state.admin_username is None or state.admin_password_hash is None:
                raise _service_unavailable("admin_not_configured")
            if session.user != state.admin_username:
                raise _unauthorized("session_user_mismatch")
            if not argon2_verify(body.old_password, state.admin_password_hash):
                raise _unauthorized("invalid_old_password")

            # Idempotent: same username → no-op but still 200 so the FE
            # can treat the endpoint as "set", not "create".
            if new_username == state.admin_username:
                return {"status": "unchanged", "username": new_username}

            # Re-persist the *existing* hash alongside the new username
            # so we don't force the operator through a password rotation
            # to rename. The session cookie stays valid — the cookie is
            # an opaque token, not a username carrier — but the session
            # row still references the old user. We rename it in place
            # so ``session_user_mismatch`` doesn't fire on the very next
            # request.
            await _persist_admin_credentials(
                state,
                new_username,
                None,
                precomputed_hash=state.admin_password_hash,
                must_change_password=state.must_change_password,
            )
            _rename_active_session(state, session.user, new_username)

        return {"status": "ok", "username": new_username}

    return r


async def _persist_admin_credentials(
    state: AdminState,
    username: str,
    plaintext_password: str | None,
    *,
    precomputed_hash: str | None = None,
    must_change_password: bool = False,
) -> None:
    """Hash, swap in-memory snapshot, and (when ``config_path`` is set)
    flush to disk. Mirrors the Rust ``persist_admin_credentials`` helper.

    One of ``plaintext_password`` or ``precomputed_hash`` must be
    provided. The username-rotation path passes ``precomputed_hash=
    state.admin_password_hash`` so renaming the operator account
    doesn't force a fresh password.

    The on-disk write goes through
    :func:`corlinman_server.gateway.lifecycle.admin_seed._merge_admin_block`
    so other sections in ``config.toml`` are preserved verbatim — only
    the ``[admin]`` block is replaced.

    Raises an :class:`HTTPException` on any unrecoverable failure so the
    handler can surface it directly.
    """
    if precomputed_hash is not None:
        hashed = precomputed_hash
    else:
        if plaintext_password is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": "hash_missing",
                    "message": (
                        "_persist_admin_credentials requires either a "
                        "plaintext password or a precomputed hash"
                    ),
                },
            )
        try:
            hashed = hash_password(plaintext_password)
        except Exception as exc:  # pragma: no cover — argon2 hash rarely fails
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "hash_failed", "message": str(exc)},
            ) from exc

    # Mutate the in-memory snapshot first so subsequent requests see
    # the new credentials even if the disk write fails (matches the
    # Rust ``state.config.store(...)`` + ``rewrite_py_config`` order).
    state.admin_username = username
    state.admin_password_hash = hashed
    state.must_change_password = bool(must_change_password)

    # SEC-007: keep the admin-B mirror of ``must_change_password`` in
    # sync so the shared ``_auth_shim`` gate stops firing the moment a
    # rotation lands. Reach for the live singleton (not the default
    # fallback ``get_admin_state`` returns) so test fixtures that mount
    # only the admin-A router don't accidentally touch an unrelated
    # admin-B :class:`AdminState`. Best-effort — admin-B may not be on
    # the import path in some test trees, in which case the boot-time
    # sync in ``lifecycle/entrypoint.py`` is the only writer and that's
    # fine: the test fixture either installed an admin-B state with
    # ``must_change_password=False`` (in which case there's nothing to
    # do) or it didn't install one at all (in which case the gate has
    # no admin-B routes to govern).
    try:
        from corlinman_server.gateway.routes_admin_b import state as _admin_b_state
    except Exception:  # pragma: no cover — admin_b not on path
        _admin_b_state = None  # type: ignore[assignment]
    if _admin_b_state is not None:
        b_state = getattr(_admin_b_state, "_state", None)
        if b_state is not None and hasattr(b_state, "must_change_password"):
            b_state.must_change_password = bool(must_change_password)

    if state.config_path is None:
        # No on-disk config to update — mirrors the Rust 503 only if the
        # *caller* expects a persisted state, otherwise we just leave
        # the in-memory snapshot updated. The Rust handler 503s when
        # config_path is None; we match that contract.
        raise _service_unavailable(
            "config_path_unset",
            "gateway booted without a config file path",
        )

    try:
        existing = ""
        if state.config_path.exists():
            try:
                existing = state.config_path.read_text(encoding="utf-8")
            except OSError:
                # Missing-perms or transient read failures — fall back
                # to a fresh write rather than a 500. The atomic-write
                # below either succeeds or raises ``OSError`` which we
                # surface below.
                existing = ""
        block = _render_admin_block(
            username=username,
            password_hash=hashed,
            must_change_password=bool(must_change_password),
        )
        merged = _merge_admin_block(existing, block)
        await _atomic_write(state.config_path, merged)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "write_failed", "message": str(exc)},
        ) from exc


def _toml_escape(s: str) -> str:
    """Minimal TOML-string escape for the two fields we serialise."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _rename_active_session(state: AdminState, old_username: str, new_username: str) -> None:
    """Rewrite every active session row that points at ``old_username``
    so subsequent ``session_user_mismatch`` checks pass.

    The session store is intentionally narrow — it doesn't expose a
    rename API on the Rust side — so we reach into its internals via
    duck-typing. Tests that swap in a mock store can pass either a
    dict-backed ``_sessions`` or a ``rename`` method; we tolerate both.
    """
    store = state.session_store
    if store is None:
        return
    rename = getattr(store, "rename", None)
    if callable(rename):
        try:
            rename(old_username, new_username)
            return
        except Exception:
            # Fall through to the duck-typed path so a half-broken
            # mock still works.
            pass
    sessions = getattr(store, "_sessions", None)
    lock = getattr(store, "_lock", None)
    if sessions is None:
        return
    if lock is not None:
        with lock:
            for row in sessions.values():
                if getattr(row, "user", None) == old_username:
                    row.user = new_username
    else:  # pragma: no cover — defensive
        for row in sessions.values():
            if getattr(row, "user", None) == old_username:
                row.user = new_username


# ---------------------------------------------------------------------------
# ``asyncio.Lock`` / ``threading.Lock`` dual-mode async context manager.
# ---------------------------------------------------------------------------


class _LockAsyncCM:
    """Awaitable lock CM that works with either ``asyncio.Lock`` or
    ``threading.Lock``. The Rust side uses ``tokio::sync::Mutex``; the
    Python port accepts either kind so tests that pre-build the lock
    via the state dataclass don't have to know which flavor to pass."""

    def __init__(self, lock: Any) -> None:
        self._lock = lock
        self._kind: str = "noop"

    async def __aenter__(self) -> None:
        lock = self._lock
        if hasattr(lock, "acquire") and asyncio.iscoroutinefunction(lock.acquire):
            await lock.acquire()
            self._kind = "asyncio"
        elif isinstance(lock, threading.Lock):
            await asyncio.to_thread(lock.acquire)
            self._kind = "thread"
        else:
            # Unknown lock shape — best effort: try ``__aenter__``.
            if hasattr(lock, "__aenter__"):
                await lock.__aenter__()
                self._kind = "ctx"
            elif hasattr(lock, "__enter__"):
                await asyncio.to_thread(lock.__enter__)
                self._kind = "sync_ctx"
            else:
                self._kind = "noop"

    async def __aexit__(self, *exc: Any) -> None:
        lock = self._lock
        if self._kind == "asyncio":
            lock.release()
        elif self._kind == "thread":
            lock.release()
        elif self._kind == "ctx":
            await lock.__aexit__(*exc)
        elif self._kind == "sync_ctx":
            await asyncio.to_thread(lock.__exit__, *exc)


def _lock_async(lock: Any) -> _LockAsyncCM:
    return _LockAsyncCM(lock)


__all__ = [
    "DEFAULT_SESSION_TTL_SECS",
    "MIN_PASSWORD_LEN",
    "USERNAME_MAX_LEN",
    "ChangePasswordRequest",
    "ChangeUsernameRequest",
    "LoginRequest",
    "LoginResponse",
    "MeResponse",
    "OnboardRequest",
    "argon2_verify",
    "hash_password",
    "router",
]
