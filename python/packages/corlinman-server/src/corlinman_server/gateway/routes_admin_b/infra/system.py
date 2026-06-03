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

The module-level mass (wire shapes, constants, defensive upgrader
exceptions, and helper functions) lives in the sibling ``_system_lib``
module; ``router()`` + its handlers stay here and re-import what they
reference.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from corlinman_server.gateway.routes_admin_b.infra._system_lib import (
    _CHECK_UPDATES_MIN_INTERVAL_SECONDS,
    _TERMINAL_UPGRADE_STATES,
    _UPGRADE_SSE_HEARTBEAT_SECONDS,
    AuditEntryResponse,
    AuditTailResponse,
    SystemInfoResponse,
    UpgradeAlreadyRunning,
    UpgradeCommandsResponse,
    UpgraderUnavailable,
    UpgradeStartBody,
    UpgradeStartResponse,
    UpgradeStatusResponse,
    _disabled_503,
    _is_downgrade,
    _resolve_actor,
    _resolve_audit_log,
    _resolve_checker,
    _resolve_upgrader,
    _safe_audit,
    _status_to_response,
    _too_many_requests,
    _upgrade_commands,
    _upgrade_error,
    _upgrade_status_to_response,
    _validate_tag_against_releases,
)
from corlinman_server.gateway.routes_admin_b.state import (
    get_admin_state,
    require_admin,
)
from corlinman_server.system.audit import AuditEntry, utcnow_iso

if TYPE_CHECKING:  # pragma: no cover — type-only
    from collections.abc import AsyncIterator


__all__ = ["router"]


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
