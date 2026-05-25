"""``/admin/system*`` — version + update-checker surface (W1.1).

Three endpoints, all admin-gated:

* ``GET  /admin/system/info`` — current :class:`UpdateStatus` (uses cache,
  no network).
* ``POST /admin/system/check-updates`` — forces a fresh poll of the
  GitHub releases API. Rate-limited 1 call / minute per process so a
  refresh-spam UI can't get the gateway IP-banned.
* ``GET  /admin/system/upgrade-commands`` — formatted ``install.sh
  --upgrade --version vX.Y.Z`` strings for the three supported deploy
  modes. Falls back to ``--version main`` when no release has ever been
  observed (greenfield repo / first-ever boot before any tag exists).

Scheduler wiring lives in W2.2; this module only owns the synchronous
HTTP surface. The poll itself happens on
:class:`~corlinman_server.system.UpdateChecker` which is wired onto
:class:`~corlinman_server.gateway.routes_admin_b.state.AdminState` by the
gateway lifecycle.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)
from corlinman_server.system import UpdateChecker, UpdateStatus

__all__ = ["router"]


# 1 call / minute / process. Multi-instance dedup is out of scope for
# MVP (W3 deferred); a per-process counter is plenty given the UI's only
# refresh button is behind admin auth.
_CHECK_UPDATES_MIN_INTERVAL_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Pydantic wire shapes
# ---------------------------------------------------------------------------


class SystemInfoResponse(BaseModel):
    """JSON shape of :class:`UpdateStatus` — pydantic mirror.

    Field names are deliberately snake_case to match the dataclass
    (admin UI consumes the same names).
    """

    current: str
    latest: str | None = None
    available: bool = False
    release_url: str | None = None
    release_notes_md: str | None = None
    published_at: int | None = None
    last_checked_at: int
    prerelease_seen: list[str] = []


class UpgradeCommandsResponse(BaseModel):
    """Three command strings, one per supported deploy mode."""

    native: str
    docker: str
    docker_with_qq: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_to_response(status: UpdateStatus) -> SystemInfoResponse:
    """Coerce the dataclass into the pydantic mirror."""
    return SystemInfoResponse(
        current=status.current,
        latest=status.latest,
        available=status.available,
        release_url=status.release_url,
        release_notes_md=status.release_notes_md,
        published_at=status.published_at,
        last_checked_at=status.last_checked_at,
        prerelease_seen=list(status.prerelease_seen),
    )


def _disabled_503() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "update_checker_disabled",
            "message": "update checker is not wired on this gateway",
        },
    )


def _too_many_requests(retry_after_seconds: float) -> JSONResponse:
    """Standard 429 with both header + body retry-after hint."""
    retry_after = max(1, int(retry_after_seconds))
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limited",
            "message": "Updates can only be checked once per minute.",
            "retry_after": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


def _upgrade_commands(status: UpdateStatus) -> UpgradeCommandsResponse:
    """Format the three install-script invocations.

    When ``status.latest`` is ``None`` (no release ever observed) we
    target ``main`` instead of a tag so the operator can still trigger
    an upgrade against the rolling branch.
    """
    if status.latest:
        version_arg = f"v{status.latest}"
    else:
        version_arg = "main"
    native = f"bash deploy/install.sh --upgrade --version {version_arg}"
    docker = f"bash deploy/install.sh --upgrade --mode docker --version {version_arg}"
    docker_with_qq = (
        f"bash deploy/install.sh --upgrade --mode docker --version {version_arg} --with-qq"
    )
    return UpgradeCommandsResponse(
        native=native, docker=docker, docker_with_qq=docker_with_qq
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Build the admin-system sub-router.

    The 1/min throttle is parked on the AdminState ``extras`` bag so
    tests can reset it between cases (and so other handlers in the
    bundle could read it if they ever needed to).
    """
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "system"])

    @r.get("/admin/system/info", response_model=SystemInfoResponse)
    async def get_system_info() -> SystemInfoResponse | JSONResponse:
        state = get_admin_state()
        checker = _resolve_checker(state)
        if checker is None:
            return _disabled_503()
        status = await checker.poll(force=False)
        return _status_to_response(status)

    @r.post("/admin/system/check-updates", response_model=SystemInfoResponse)
    async def check_updates_now() -> SystemInfoResponse | JSONResponse:
        state = get_admin_state()
        checker = _resolve_checker(state)
        if checker is None:
            return _disabled_503()
        now = time.monotonic()
        last_forced = state.extras.get("system_last_forced_check_ts")
        if isinstance(last_forced, (int, float)):
            elapsed = now - last_forced
            if elapsed < _CHECK_UPDATES_MIN_INTERVAL_SECONDS:
                return _too_many_requests(
                    _CHECK_UPDATES_MIN_INTERVAL_SECONDS - elapsed
                )
        state.extras["system_last_forced_check_ts"] = now
        status = await checker.poll(force=True)
        return _status_to_response(status)

    @r.get(
        "/admin/system/upgrade-commands", response_model=UpgradeCommandsResponse
    )
    async def get_upgrade_commands() -> UpgradeCommandsResponse | JSONResponse:
        state = get_admin_state()
        checker = _resolve_checker(state)
        if checker is None:
            return _disabled_503()
        # Use cached state — operators may hit this endpoint frequently
        # while copying commands; we shouldn't fan out to GitHub here.
        status = await checker.poll(force=False)
        return _upgrade_commands(status)

    return r


def _resolve_checker(state: AdminState) -> UpdateChecker | None:
    """Best-effort extractor that keeps the routes mypy-clean.

    We use a dynamic attribute (``Any``) on AdminState; this helper
    narrows the type for the handlers + lets a future test substitute a
    duck-typed double.
    """
    checker = getattr(state, "update_checker", None)
    if checker is None:
        return None
    if isinstance(checker, UpdateChecker):
        return checker
    # Permit duck-typed doubles (e.g. tests passing a stub with the
    # same ``poll(force=...)`` signature). The handlers only call
    # ``.poll(...)`` so an interface check is enough.
    if hasattr(checker, "poll"):
        return checker  # type: ignore[no-any-return,return-value]
    return None
