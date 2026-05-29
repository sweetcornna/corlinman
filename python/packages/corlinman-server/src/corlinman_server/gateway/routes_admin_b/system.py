"""``/admin/system*`` — version + update-checker + one-click upgrade.

Endpoint inventory, all admin-gated:

* ``GET  /admin/system/info`` — current :class:`UpdateStatus` (W1.1).
* ``POST /admin/system/check-updates`` — force a fresh GitHub poll (W1.1).
* ``GET  /admin/system/upgrade-commands`` — copy-paste fallback (W1.1).
* ``POST /admin/system/upgrade`` — start a one-click upgrade (W1.3).
* ``GET  /admin/system/upgrade/{request_id}/status`` — polled progress.
* ``GET  /admin/system/upgrade/{request_id}/events`` — SSE progress.
* ``GET  /admin/system/audit`` — paginated audit-log tail.

Scheduler wiring lives in W2.2; this module owns the synchronous HTTP
surface for both the legacy update-check trio (W1.1) and the new
one-click upgrade quartet (W1.3 of ``docs/PLAN_ONE_CLICK_UPGRADE.md``).

The actual upgrader (Docker vs native) lives in
``corlinman_server.system.upgrader`` (W1.1/W1.2 of the same plan). This
module imports it defensively — if the upgrader module isn't on the
import path yet (early Wave-1 / partial port) the upgrade routes 503
cleanly with ``upgrader_unavailable`` rather than failing at import
time.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)
from corlinman_server.system import SystemAuditLog, UpdateChecker, UpdateStatus
from corlinman_server.system.audit import AuditEntry, utcnow_iso

# Defensive import of the upgrader module — W1.1/W1.2 may not be landed
# when this module is first imported. We bind a sentinel + the
# UpgradeAlreadyRunning exception type (or a placeholder) so the
# handlers can pattern-match on it without conditional imports inside
# each request path.
try:
    from corlinman_server.system.upgrader import (  # type: ignore[import-not-found]
        UpgradeAlreadyRunning,
        UpgraderUnavailable,
    )
except ImportError:  # pragma: no cover — pre-W1.1/W1.2 boot

    class UpgradeAlreadyRunning(Exception):  # type: ignore[no-redef]
        """Placeholder — upgrader module not yet on the import path.

        The real ``UpgraderProtocol.start`` raises the canonical type
        from ``corlinman_server.system.upgrader``. We re-export a
        same-shape sentinel so the route can pattern-match on the
        symbol it imported at module-load time even if W1.1 hasn't
        landed yet; the duck-typed ``except (UpgradeAlreadyRunning,
        ...)`` catch tolerates both.
        """

        def __init__(self, in_flight: Any = None) -> None:
            super().__init__("upgrade already running")
            self.in_flight = in_flight

    class UpgraderUnavailable(Exception):  # type: ignore[no-redef]
        """Placeholder — see :class:`UpgradeAlreadyRunning`."""


if TYPE_CHECKING:  # pragma: no cover — type-only
    from collections.abc import AsyncIterator


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
# W1.3 — one-click upgrade wire shapes
# ---------------------------------------------------------------------------


class UpgradeStartBody(BaseModel):
    """Request body for ``POST /admin/system/upgrade``.

    ``typed_confirmation`` MUST equal ``tag`` exactly — this is the
    user-friction gate (the UI surfaces a text input with the tag as
    placeholder; we enforce the equality server-side too so a scripted
    client can't bypass the typed-confirmation guard).

    ``allow_downgrade`` defaults to ``False``; setting it ``True``
    overrides the ``Version(target) > Version(current)`` check so an
    operator can roll back to a known-good earlier tag.
    """

    tag: str = Field(..., description="Target release tag, e.g. ``v1.2.1``.")
    typed_confirmation: str = Field(
        ...,
        description=(
            "Must equal ``tag`` exactly — the user-friction gate "
            "preventing accidental upgrades."
        ),
    )
    allow_downgrade: bool = Field(
        False,
        description=(
            "When ``True``, bypass the no-downgrade guard. Use only for "
            "rollback. Default refuses any tag <= current version."
        ),
    )


class UpgradeStartResponse(BaseModel):
    """``202 Accepted`` payload for ``POST /admin/system/upgrade``."""

    request_id: str
    state: str
    mode: str
    tag: str


class UpgradeStatusResponse(BaseModel):
    """Pydantic mirror of :class:`UpgradeStatus` from the upgrader module.

    Matches the W1.1 dataclass field set (epoch-ms timestamps, phase as
    a non-null short label, log_excerpt as a rolling 4 kB tail). The
    OpenAPI doc reflects this contract.
    """

    request_id: str
    tag: str
    state: str
    phase: str | None = None
    started_at: int | None = None
    finished_at: int | None = None
    log_excerpt: str = ""
    error: str | None = None


class AuditEntryResponse(BaseModel):
    """One row in the audit-tail JSON response."""

    ts: str
    event: str
    request_id: str | None = None
    tag: str | None = None
    actor: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AuditTailResponse(BaseModel):
    """Paginated audit-log response.

    ``next_before_ts`` is the ``ts`` of the oldest entry in the page —
    callers walk pages by passing it back as ``?before_ts=...``. It is
    ``None`` when the page was short (end of history).
    """

    entries: list[AuditEntryResponse]
    next_before_ts: str | None = None


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


# ---------------------------------------------------------------------------
# W1.3 — upgrade helpers
# ---------------------------------------------------------------------------

# Defensive semver regex for the fallback whitelist branch (when the
# update_checker is unavailable we still won't accept arbitrary tags —
# they must at least *look* like a semver release).
import re as _re

_SEMVER_RE = _re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# SSE keepalive cadence — matches the 10s sessions_events.py uses so
# proxies/reverse-proxies idle out on the same timer everywhere.
_UPGRADE_SSE_HEARTBEAT_SECONDS: float = 10.0

# Terminal upgrade states — the SSE loop exits the moment one of these
# is emitted so the EventSource client knows the stream is done.
_TERMINAL_UPGRADE_STATES: frozenset[str] = frozenset(
    {"succeeded", "failed", "stalled", "cancelled"}
)


def _strip_v(tag: str) -> str:
    """Strip a leading ``v``/``V`` from a release tag. ``v1.2.1`` -> ``1.2.1``."""
    if len(tag) >= 2 and tag[0] in ("v", "V"):
        return tag[1:]
    return tag


def _upgrade_error(
    status_code: int,
    error: str,
    message: str,
    **extra: Any,
) -> JSONResponse:
    """Typed error envelope matching the rest of routes_admin_b."""
    body: dict[str, Any] = {"error": error, "message": message}
    body.update(extra)
    return JSONResponse(status_code=status_code, content=body)


def _resolve_actor(request: Request) -> str:
    """Best-effort extract a username from the auth context on ``request``.

    The auth-shim populates ``request.state.admin_user`` (string) for
    HTTP-Basic flows and ``request.state.admin_session`` (object with
    ``.user`` / ``.username``) for cookie sessions. Falls back to
    ``"admin"`` when neither is set — audit-log readability hinges on a
    non-empty string here.
    """
    user = getattr(request.state, "admin_user", None)
    if isinstance(user, str) and user:
        return user
    session = getattr(request.state, "admin_session", None)
    if session is not None:
        username = getattr(session, "username", None) or getattr(
            session, "user", None
        )
        if isinstance(username, str) and username:
            return username
    return "admin"


def _upgrade_status_to_response(status: Any, *, fallback_tag: str = "") -> UpgradeStatusResponse:
    """Coerce an :class:`UpgradeStatus`-shaped object to the wire mirror.

    The upgrader module's dataclass field set is locked by the W1.1
    contract; we read each attribute via :func:`getattr` so a future
    additive field doesn't crash this serialiser, and so duck-typed
    test doubles can omit optional fields cleanly.
    """
    return UpgradeStatusResponse(
        request_id=str(getattr(status, "request_id", "")),
        tag=str(getattr(status, "tag", fallback_tag) or fallback_tag),
        state=str(getattr(status, "state", "unknown")),
        phase=_as_optional_str(getattr(status, "phase", None)),
        started_at=_as_optional_int(getattr(status, "started_at", None)),
        finished_at=_as_optional_int(getattr(status, "finished_at", None)),
        log_excerpt=str(getattr(status, "log_excerpt", "") or ""),
        error=_as_optional_str(getattr(status, "error", None)),
    )


def _as_optional_str(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    return str(raw)


def _as_optional_int(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        # bool is a subclass of int — explicitly drop so a stray True
        # doesn't serialise as 1.
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _resolve_upgrader(state: AdminState) -> Any | None:
    """Best-effort extract the upgrader handle from AdminState."""
    upgrader = getattr(state, "upgrader", None)
    if upgrader is None:
        return None
    # Duck-typed acceptance — the route only ever calls ``is_available``,
    # ``start``, ``status`` and ``progress`` so a doubles-providing test
    # double doesn't need to be the canonical concrete class.
    if hasattr(upgrader, "is_available") or hasattr(upgrader, "start"):
        return upgrader
    return None


def _resolve_audit_log(state: AdminState) -> SystemAuditLog | None:
    """Read the audit-log handle off AdminState (loose-typed)."""
    log = getattr(state, "audit_log", None)
    if isinstance(log, SystemAuditLog):
        return log
    if log is not None and hasattr(log, "append") and hasattr(log, "tail"):
        # Duck-typed acceptance for test doubles.
        return log  # type: ignore[no-any-return,return-value]
    return None


async def _validate_tag_against_releases(
    state: AdminState, target_tag: str
) -> bool:
    """Confirm ``target_tag`` shows up in the GitHub releases the checker
    has observed (latest + cached prereleases).

    Falls back to a semver regex check when the update_checker is not
    wired — looser but still refuses arbitrary garbage that a scripted
    client might post.
    """
    checker = _resolve_checker(state)
    target_stripped = _strip_v(target_tag)
    if checker is None:
        # No checker → loose validation against semver.
        return bool(_SEMVER_RE.match(target_tag))
    try:
        status = await checker.poll(force=False)
    except Exception:  # noqa: BLE001 — best-effort
        return bool(_SEMVER_RE.match(target_tag))
    accepted: set[str] = set()
    latest = getattr(status, "latest", None)
    if isinstance(latest, str) and latest:
        accepted.add(_strip_v(latest))
    for pre in getattr(status, "prerelease_seen", []) or []:
        if isinstance(pre, str):
            accepted.add(_strip_v(pre))
    if target_stripped in accepted:
        return True
    # Last-resort permissive check — operators sometimes pin a release
    # the checker hasn't seen yet (just-published, ETag cached). A
    # well-formed semver string is acceptable; the upgrader script
    # re-validates against GitHub before doing anything destructive.
    return bool(_SEMVER_RE.match(target_tag))


def _is_downgrade(current: str, target_tag: str) -> bool:
    """``True`` iff ``Version(target) <= Version(current)`` per PEP 440."""
    try:
        return Version(_strip_v(target_tag)) <= Version(_strip_v(current))
    except InvalidVersion:
        # Malformed — refuse to make a comparison-based call here; the
        # tag-validation step already gated on shape. Treating it as a
        # downgrade here would surface a confusing error; treating it
        # as a non-downgrade would let it slip past. Choose the safer
        # branch: report a downgrade so the caller surfaces a clear
        # 400 to the operator.
        return True


async def _safe_audit(
    audit_log: SystemAuditLog | None, entry: AuditEntry
) -> None:
    """Append an entry if the log is wired; swallow any error.

    The audit log's writer never raises (best-effort by contract); this
    helper layers on a second safety net so a degraded boot still
    serves the rest of the upgrade flow.
    """
    if audit_log is None:
        return
    try:
        await audit_log.append(entry)
    except Exception:  # noqa: BLE001 — best-effort
        return


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

    # ------------------------------------------------------------------
    # W1.3 — one-click upgrade quartet
    # ------------------------------------------------------------------

    @r.post(
        "/admin/system/upgrade",
        response_model=UpgradeStartResponse,
        status_code=202,
    )
    async def start_upgrade(
        body: UpgradeStartBody, request: Request
    ) -> UpgradeStartResponse | JSONResponse:
        """Kick off a one-click upgrade.

        Validation order (matches PLAN_ONE_CLICK_UPGRADE.md §1 W1.3):

        1. ``typed_confirmation`` mismatch → 400
        2. no upgrader wired → 503 ``upgrader_unavailable``
        3. ``upgrader.is_available()`` false → 503 ``upgrader_unavailable``
        4. tag not in observed releases / not semver-shaped → 400
        5. downgrade refusal (unless ``allow_downgrade``) → 400
        6. single-flight (raised by ``upgrader.start``) → 409
        7. happy path → 202 with the queued request id
        """
        state = get_admin_state()

        # (1) typed confirmation
        if body.typed_confirmation != body.tag:
            return _upgrade_error(
                400,
                "typed_confirmation_mismatch",
                "typed_confirmation must equal tag exactly",
            )

        # (2) upgrader wired?
        upgrader = _resolve_upgrader(state)
        if upgrader is None:
            return _upgrade_error(
                503,
                "upgrader_unavailable",
                (
                    "one-click upgrade is not available on this gateway "
                    "(runtime mode unknown or upgrader not installed)"
                ),
            )

        # (3) upgrader self-check
        try:
            available = bool(await upgrader.is_available())
        except Exception as exc:  # noqa: BLE001 — best-effort
            return _upgrade_error(
                503,
                "upgrader_unavailable",
                f"upgrader self-check failed: {exc}",
            )
        if not available:
            return _upgrade_error(
                503,
                "upgrader_unavailable",
                "upgrader self-check returned not-available",
            )

        # (4) tag whitelist
        if not await _validate_tag_against_releases(state, body.tag):
            return _upgrade_error(
                400,
                "tag_not_whitelisted",
                (
                    f"tag {body.tag!r} is not in the observed releases for "
                    "this gateway"
                ),
                tag=body.tag,
            )

        # (5) downgrade refusal
        checker = _resolve_checker(state)
        current = (
            checker.current_version()
            if checker is not None and hasattr(checker, "current_version")
            else "0.0.0"
        )
        if not body.allow_downgrade and _is_downgrade(current, body.tag):
            return _upgrade_error(
                400,
                "downgrade_blocked",
                (
                    f"target {body.tag} is <= current {current}; pass "
                    "allow_downgrade=true to override"
                ),
                current=current,
                target=body.tag,
            )

        # (6) single-flight + (7) happy path
        actor = _resolve_actor(request)
        try:
            upgrade_request = await upgrader.start(body.tag, actor)
        except UpgradeAlreadyRunning as exc:
            # The W1.1 contract puts the in-flight UpgradeStatus on the
            # ``in_flight`` attribute. Some test doubles / future variants
            # may stash just the request_id — we tolerate both shapes.
            in_flight = getattr(exc, "in_flight", None)
            inflight_id: str | None = None
            inflight_tag: str | None = None
            inflight_state: str | None = None
            if in_flight is not None:
                inflight_id = getattr(in_flight, "request_id", None)
                inflight_tag = getattr(in_flight, "tag", None)
                inflight_state = getattr(in_flight, "state", None)
            if inflight_id is None:
                # Plain-string fallback (older / mock shape).
                fallback = getattr(exc, "request_id", None)
                if isinstance(fallback, str):
                    inflight_id = fallback
            return _upgrade_error(
                409,
                "upgrade_already_running",
                "another upgrade is already in flight",
                request_id=inflight_id,
                in_flight_tag=inflight_tag,
                in_flight_state=inflight_state,
            )
        except UpgraderUnavailable as exc:
            return _upgrade_error(
                503,
                "upgrader_unavailable",
                str(exc) or "upgrader is not available",
            )
        except Exception as exc:  # noqa: BLE001 — surface as 500
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "upgrade_start_failed",
                    "message": str(exc),
                },
            ) from exc

        # Audit. The upgrader writes its own "started"/"completed" rows
        # from its progress pump; here we record the request itself so
        # the audit trail captures who clicked + when, even if the
        # upgrade later fails before it emits any progress.
        audit_log = _resolve_audit_log(state)
        await _safe_audit(
            audit_log,
            AuditEntry(
                ts=utcnow_iso(),
                event="system.upgrade.requested",
                request_id=str(
                    getattr(upgrade_request, "request_id", "") or ""
                ),
                tag=str(getattr(upgrade_request, "tag", body.tag) or body.tag),
                actor=actor,
                details={
                    "mode": str(getattr(upgrade_request, "mode", "unknown")),
                    "allow_downgrade": body.allow_downgrade,
                },
            ),
        )

        return UpgradeStartResponse(
            request_id=str(getattr(upgrade_request, "request_id", "")),
            state="queued",
            mode=str(getattr(upgrade_request, "mode", "unknown")),
            tag=str(getattr(upgrade_request, "tag", body.tag) or body.tag),
        )

    @r.get(
        "/admin/system/upgrade/{request_id}/status",
        response_model=UpgradeStatusResponse,
    )
    async def get_upgrade_status(
        request_id: str = Path(..., description="Upgrade request id."),
    ) -> UpgradeStatusResponse | JSONResponse:
        state = get_admin_state()
        upgrader = _resolve_upgrader(state)
        if upgrader is None:
            return _upgrade_error(
                503,
                "upgrader_unavailable",
                "one-click upgrade is not available on this gateway",
            )
        # Read order, all duck-typed against the W1.1 surface:
        #
        # 1. ``upgrader.status(request_id)`` — newer/simpler API; some
        #    test doubles + future impls expose it directly.
        # 2. ``upgrader._store.get(request_id)`` — the canonical
        #    W1.1 store; both DockerUpgrader and NativeUpgrader hold
        #    one as ``self._store`` per the documented contract.
        # 3. First frame of ``progress(request_id)`` — last-resort
        #    fallback; consumes one snapshot then exits the iterator.
        status_fn = getattr(upgrader, "status", None)
        if callable(status_fn):
            try:
                status = await status_fn(request_id)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "upgrade_status_failed",
                        "message": str(exc),
                    },
                ) from exc
            if status is None:
                return _upgrade_error(
                    404,
                    "upgrade_request_not_found",
                    f"no upgrade request with id {request_id!r}",
                )
            return _upgrade_status_to_response(status)

        store = getattr(upgrader, "_store", None) or getattr(
            upgrader, "store", None
        )
        if store is not None and hasattr(store, "get"):
            try:
                status = await store.get(request_id)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "upgrade_status_failed",
                        "message": str(exc),
                    },
                ) from exc
            if status is None:
                return _upgrade_error(
                    404,
                    "upgrade_request_not_found",
                    f"no upgrade request with id {request_id!r}",
                )
            return _upgrade_status_to_response(status)

        # Last-resort fallback: pull the first frame off ``progress``.
        progress_fn = getattr(upgrader, "progress", None)
        if progress_fn is None:
            return _upgrade_error(
                503,
                "upgrader_unavailable",
                "upgrader does not expose a status surface",
            )
        try:
            iterator: AsyncIterator[Any] = progress_fn(request_id)
            async for frame in iterator:
                return _upgrade_status_to_response(frame)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "upgrade_status_failed",
                    "message": str(exc),
                },
            ) from exc
        return _upgrade_error(
            404,
            "upgrade_request_not_found",
            f"no upgrade request with id {request_id!r}",
        )

    @r.get("/admin/system/upgrade/{request_id}/events")
    async def stream_upgrade_events(
        request_id: str = Path(..., description="Upgrade request id."),
    ) -> Any:
        """SSE stream of :class:`UpgradeStatus` frames.

        Emits one ``event: status`` frame per progress tick the upgrader
        publishes, closing the stream the first time it sees a terminal
        ``state`` (``succeeded`` / ``failed`` / ``stalled`` /
        ``cancelled``). A 10s ``: keepalive`` comment frame fires when
        no progress has arrived for the interval — same cadence as the
        sessions-events SSE route.
        """
        state = get_admin_state()
        upgrader = _resolve_upgrader(state)
        if upgrader is None:
            return _upgrade_error(
                503,
                "upgrader_unavailable",
                "one-click upgrade is not available on this gateway",
            )
        progress_fn = getattr(upgrader, "progress", None)
        if progress_fn is None:
            return _upgrade_error(
                503,
                "upgrader_unavailable",
                "upgrader does not expose a progress surface",
            )

        async def _generate() -> AsyncIterator[bytes]:
            sequence = 0
            try:
                iterator: AsyncIterator[Any] = progress_fn(request_id)
                aiter_obj = iterator.__aiter__()
                while True:
                    try:
                        frame = await asyncio.wait_for(
                            aiter_obj.__anext__(),
                            timeout=_UPGRADE_SSE_HEARTBEAT_SECONDS,
                        )
                    except TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    except StopAsyncIteration:
                        break

                    payload = _upgrade_status_to_response(frame).model_dump()
                    data = json.dumps(payload, default=str)
                    sse_frame = (
                        f"id: {request_id}:{sequence}\n"
                        f"event: status\n"
                        f"data: {data}\n\n"
                    )
                    yield sse_frame.encode("utf-8")
                    sequence += 1

                    if payload.get("state") in _TERMINAL_UPGRADE_STATES:
                        # Terminal state — close the stream so the
                        # EventSource client knows the upgrade is done.
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — surface as SSE error
                err_payload = json.dumps(
                    {
                        "request_id": request_id,
                        "state": "failed",
                        "error": str(exc),
                    }
                )
                yield (
                    f"id: {request_id}:{sequence}\n"
                    f"event: status\n"
                    f"data: {err_payload}\n\n"
                ).encode()

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Event-Id-Format": "request_id:sequence",
            },
        )

    @r.get("/admin/system/audit", response_model=AuditTailResponse)
    async def list_audit(
        limit: int = Query(
            50,
            ge=1,
            le=500,
            description="Max entries per page. Server-clamped to [1, 500].",
        ),
        before_ts: str | None = Query(
            None,
            description=(
                "Pagination cursor: return entries with ``ts < before_ts`` "
                "newest-first. Pass the previous page's ``next_before_ts``."
            ),
        ),
    ) -> AuditTailResponse:
        """Paginated audit-log tail.

        Returns an empty page when no log is wired or no entries have
        been recorded — the absence of upgrades is the empty case, not
        a degradation, so we don't 503 here.
        """
        state = get_admin_state()
        audit_log = _resolve_audit_log(state)
        if audit_log is None:
            return AuditTailResponse(entries=[], next_before_ts=None)
        entries = await audit_log.tail(limit=limit, before_ts=before_ts)
        response_entries = [
            AuditEntryResponse(
                ts=e.ts,
                event=e.event,
                request_id=e.request_id,
                tag=e.tag,
                actor=e.actor,
                details=e.details,
            )
            for e in entries
        ]
        next_before_ts: str | None = None
        if response_entries and len(response_entries) >= limit:
            next_before_ts = response_entries[-1].ts
        return AuditTailResponse(
            entries=response_entries, next_before_ts=next_before_ts
        )

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
