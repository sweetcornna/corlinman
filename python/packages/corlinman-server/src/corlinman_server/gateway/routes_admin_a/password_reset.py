"""``/admin/password-reset/*`` — host-token self-service password reset.

The recovery model on a single-operator gateway: prove you have root on
the host by reading a one-time token the gateway just wrote to its data
dir. The token file is ``mode=0600``, lives at
``<data_dir>/.password-reset.token``, has a 300-second TTL, and is
deleted on successful use. Once you can read the file, you are
allowed to rewrite ``[admin].password_hash`` — no SSH-side argon2
incantation required.

Flow

1. ``POST /admin/password-reset/request`` (no auth)
   → gateway writes a fresh ``secrets.token_urlsafe(24)`` to
     ``<data_dir>/.password-reset.token``;
   → returns ``{token_path, ttl_seconds, hint}`` so the UI can show the
     operator the exact ``cat`` command.

2. Operator: ``cat <data_dir>/.password-reset.token``

3. ``POST /admin/password-reset/complete`` (no auth) with
   ``{token, new_password}``
   → constant-time compare against the on-disk token;
   → checks mtime is within ttl;
   → rotates ``[admin].password_hash`` via the same atomic write path
     used by ``/admin/password``;
   → deletes the token file;
   → returns ``{status: ok}``.

Both endpoints mount **outside** ``require_admin`` (mirrors the pattern
used by ``/admin/login`` + ``/admin/onboard``) because the whole point
is recovering when you have no valid cookie.

Rate-limit: ``/request`` rejects with 429 if called twice within
60 seconds — prevents an attacker from spamming the file system. The
limit is *global* (single-operator deployment); we don't try to scope
per-IP because tencent CN BGP edges share NAT pools and operator
laptops bounce IPs all day.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hmac
import secrets
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_a.auth import (
    MIN_PASSWORD_LEN,
    _FALLBACK_ADMIN_WRITE_LOCK,
    _lock_async,
    _persist_admin_credentials,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)

#: The recovery file is intentionally dot-prefixed so it doesn't show up
#: in casual ``ls`` listings but is still trivially ``cat``-able by an
#: operator who knows where to look.
TOKEN_FILENAME = ".password-reset.token"

#: TTL after which a previously-issued token is no longer accepted by
#: the complete endpoint. Mirrors hermes' background-review timeout
#: convention (5 minutes is plenty for an SSH+paste roundtrip).
TOKEN_TTL_SECONDS = 300

#: Throttle on ``/request`` — refuse a second mint inside this window
#: so an attacker who reaches the unauthenticated endpoint can't keep
#: rewriting the token file. Global state (single-operator deploy).
REQUEST_THROTTLE_SECONDS = 60

#: Module-level last-issued timestamp (monotonic seconds). Reset on
#: import so the first call after a restart always succeeds.
_LAST_REQUEST_AT: float = 0.0


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class RequestResponse(BaseModel):
    token_path: str
    ttl_seconds: int
    hint: str


class CompleteRequest(BaseModel):
    token: str
    new_password: str


class CompleteResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _token_path(state: AdminState) -> Path | None:
    """Return ``<data_dir>/.password-reset.token`` or ``None`` when the
    bootstrapper didn't supply a data_dir (degraded mode). Routes 503
    in that case so the operator hears about the misconfiguration
    instead of getting a silent failure."""
    if state.data_dir is None:
        return None
    return state.data_dir / TOKEN_FILENAME


async def _write_token(path: Path) -> str:
    """Atomically write a fresh urlsafe token. Returns the plaintext
    so the request handler can decide whether to echo it (we don't —
    only the file path is surfaced)."""
    token = secrets.token_urlsafe(24)

    def _do() -> None:
        parent = path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".new")
        tmp.write_text(token, encoding="utf-8")
        # 0600 — operator-only read; equivalent to ssh-style id_rsa.
        tmp.chmod(0o600)
        tmp.replace(path)

    await asyncio.to_thread(_do)
    return token


async def _read_token(path: Path) -> tuple[str, float] | None:
    """Return ``(stored_token, mtime)`` if the file exists, else ``None``."""

    def _do() -> tuple[str, float] | None:
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8").strip()
            mtime = path.stat().st_mtime
        except OSError:
            return None
        return text, mtime

    return await asyncio.to_thread(_do)


async def _delete_token(path: Path) -> None:
    """Best-effort token unlink (ignored when the file is already
    missing — caller has just consumed it)."""

    def _do() -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            return

    await asyncio.to_thread(_do)


def _service_unavailable(error: str, message: str | None = None) -> HTTPException:
    detail: dict[str, str] = {"error": error}
    if message is not None:
        detail["message"] = message
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter()

    @r.post(
        "/admin/password-reset/request",
        response_model=RequestResponse,
        summary="Mint a one-time password-reset token on disk",
    )
    async def request_reset(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> RequestResponse:
        global _LAST_REQUEST_AT  # noqa: PLW0603 — module-level throttle

        if state.admin_username is None or state.admin_password_hash is None:
            # No admin to reset — direct the caller to the onboard flow
            # instead so they don't get stuck thinking the gateway is
            # broken.
            raise _service_unavailable(
                "admin_not_configured",
                "no admin credentials are configured; use /admin/onboard",
            )

        path = _token_path(state)
        if path is None:
            raise _service_unavailable(
                "data_dir_unset",
                (
                    "gateway booted without a data_dir; token-based "
                    "reset cannot land a file on disk"
                ),
            )

        now = time.monotonic()
        if now - _LAST_REQUEST_AT < REQUEST_THROTTLE_SECONDS:
            wait_s = REQUEST_THROTTLE_SECONDS - int(now - _LAST_REQUEST_AT)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "rate_limited",
                    "message": (
                        f"another reset token was minted recently; retry in "
                        f"{wait_s}s"
                    ),
                    "retry_after_seconds": wait_s,
                },
            )

        await _write_token(path)
        _LAST_REQUEST_AT = now

        return RequestResponse(
            token_path=str(path),
            ttl_seconds=TOKEN_TTL_SECONDS,
            hint=(
                f"SSH to the gateway host and run `cat {path}`, then paste "
                "the token into the form below within "
                f"{TOKEN_TTL_SECONDS // 60} minutes."
            ),
        )

    @r.post(
        "/admin/password-reset/complete",
        response_model=CompleteResponse,
        summary="Consume a password-reset token + rotate the admin password",
    )
    async def complete_reset(
        body: CompleteRequest,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CompleteResponse:
        if state.admin_username is None or state.admin_password_hash is None:
            raise _service_unavailable("admin_not_configured")

        path = _token_path(state)
        if path is None:
            raise _service_unavailable("data_dir_unset")

        # Step 1: read on-disk token (no lock needed — read-only).
        loaded = await _read_token(path)
        if loaded is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "no_reset_token",
                    "message": (
                        "no active reset token; click 'Generate reset token' "
                        "again to issue a fresh one"
                    ),
                },
            )

        stored_token, mtime = loaded
        age = time.time() - mtime
        if age > TOKEN_TTL_SECONDS:
            # Expired — clean up so a stale file doesn't trip a future
            # legitimate request with a wrong-but-recent token.
            await _delete_token(path)
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={
                    "error": "token_expired",
                    "message": (
                        "the reset token expired; click 'Generate reset "
                        "token' again to issue a fresh one"
                    ),
                },
            )

        # Step 2: constant-time compare.
        # Both sides are urlsafe base64 → ascii-only → safe to encode.
        if not hmac.compare_digest(
            stored_token.encode("ascii"), body.token.strip().encode("ascii")
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "invalid_token"},
            )

        # Step 3: validate the new password before consuming the token.
        # If we delete first and then 422, the operator has to re-mint
        # which is annoying. Order: validate → rotate → delete.
        if len(body.new_password) < MIN_PASSWORD_LEN:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "weak_password",
                    "message": (
                        f"password must be at least {MIN_PASSWORD_LEN} "
                        "characters"
                    ),
                },
            )

        # Step 4: persist new credentials atomically.
        lock = state.admin_write_lock or _FALLBACK_ADMIN_WRITE_LOCK
        async with _lock_async(lock):
            # Re-check inside the lock — protects against a concurrent
            # onboard call that just wiped the admin block.
            if state.admin_username is None or state.admin_password_hash is None:
                raise _service_unavailable("admin_not_configured")
            await _persist_admin_credentials(
                state,
                state.admin_username,
                body.new_password,
                must_change_password=False,
            )

        # Step 5: consume the token. Best-effort; if delete fails the
        # token is already invalid (state.admin_password_hash mismatches
        # what the file would compare against on the next call).
        await _delete_token(path)

        return CompleteResponse(status="ok")

    return r


__all__ = [
    "REQUEST_THROTTLE_SECONDS",
    "TOKEN_FILENAME",
    "TOKEN_TTL_SECONDS",
    "CompleteRequest",
    "CompleteResponse",
    "RequestResponse",
    "router",
]
