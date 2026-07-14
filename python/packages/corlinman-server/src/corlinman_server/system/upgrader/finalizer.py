"""Boot-time finalizer for upgrade records interrupted by the restart.

Every successful upgrade restarts the gateway mid-flight, so the process
that *started* the upgrade never gets to record the outcome — the record
it persisted sits in ``.upgrade-state.json`` as ``queued``/``running``.
Historically ``UpgradeStateStore._load_from_disk`` blanket-flipped those
to the terminal ``stalled`` warning (to protect single-flight, BUG-02),
which meant even a *successful* upgrade ended its audit trail on
``stalled`` and the UI had to infer success from a version change.

This module makes the smarter terminal decision, modeled on sub2api's
"the restarted service is the source of truth" posture:

1. **Version assertion** — the running process resolves its own release
   version; if it equals the record's target tag the upgrade demonstrably
   worked: ``succeeded`` + ``version_verified=True``.
2. **Helper status mirror** — otherwise consult the privileged helper's
   ``$DATA_DIR/.upgrade-status``: a terminal helper verdict is mirrored
   (a helper ``succeeded`` that contradicts the version assertion becomes
   ``failed: version_assertion_failed`` — "healthy but wrong version" is
   a failure, never a silent pass).
3. **Stall fallback** — anything else (helper still mid-flight, status
   file missing/foreign) keeps the legacy ``stalled`` flip, with a hint
   that the helper may still be finishing.

Wiring contract: construct the store with ``defer_boot_reconcile=True``
and call :func:`finalize_boot` immediately after, before the app serves —
the sync store helpers it uses are only safe in that single-threaded
window. Any exception inside falls back to the blanket stall flip so a
finalizer bug can never wedge single-flight.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import structlog

from corlinman_server.system.upgrader.state import (
    UpgradeStateStore,
    UpgradeStatus,
)

logger = structlog.get_logger(__name__)

__all__ = ["finalize_boot", "refresh_late_helper_verdict"]

# Mirrors NativeUpgrader.STATUS_FILE_NAME / the docker helper contract.
_STATUS_FILE_NAME = ".upgrade-status"


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _normalize(version: str) -> str:
    version = version.strip()
    return version[1:] if version[:1] in ("v", "V") else version


def _helper_request_ids(request_id: str) -> set[str]:
    """Both on-disk spellings of a request id.

    The store keeps ``uuid4().hex`` (dashless); the helper files carry
    the dashed form (bash ``UUID_REGEX`` legacy). Match either.
    """
    ids = {request_id}
    try:
        ids.add(str(uuid.UUID(request_id)))
    except ValueError:
        pass
    return ids


def _read_helper_status(data_dir: Path, request_id: str) -> dict | None:
    """Parse ``.upgrade-status`` iff it belongs to ``request_id``."""
    try:
        raw = (data_dir / _STATUS_FILE_NAME).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("request_id") not in _helper_request_ids(request_id):
        return None
    return payload


def _finalize_one(
    store: UpgradeStateStore,
    status: UpgradeStatus,
    *,
    data_dir: Path,
    current: str,
) -> str:
    """Decide + apply the terminal state for one orphaned record.

    Returns the outcome label for logging.
    """
    target = _normalize(status.tag)
    payload = _read_helper_status(data_dir, status.request_id)

    # (0) Helper still mid-flight — park BEFORE the version assertion.
    # The docker helper starts the new container and only then health-
    # checks it, so this (new) gateway's boot always races the helper's
    # verdict; a premature ``succeeded`` here would be terminal and a
    # subsequent helper rollback (broken release, healthcheck timeout)
    # would be silently lost — worse, its ``before_version`` would feed
    # the rollback slot with the version that is actually running.
    # Parked records are settled lazily by
    # :func:`refresh_late_helper_verdict` on the next status read.
    if payload is not None and str(payload.get("state")) in (
        "queued",
        "running",
    ):
        store.finalize_status_sync(
            status.request_id,
            state="stalled",
            phase="stalled",
            error="helper_still_running",
            finished_at=status.finished_at or _now_ms(),
        )
        return "stalled_helper_alive"

    # (1) Version assertion — the strongest signal once no helper is
    # known to be mid-flight (no status file at all, or already terminal
    # — the terminal-mirror branch below then takes precedence over a
    # bare version match only for contradictions).
    if payload is None and current and target and current == target:
        store.finalize_status_sync(
            status.request_id,
            state="succeeded",
            phase="done",
            error=None,
            version_verified=True,
            finished_at=status.finished_at or _now_ms(),
        )
        return "succeeded_version_match"

    # (2) Mirror a terminal helper verdict.
    if payload is not None:
        helper_state = str(payload.get("state") or "")
        if helper_state == "succeeded":
            if current and target and current == target:
                # Helper AND version agree — the strongest possible
                # confirmation.
                store.finalize_status_sync(
                    status.request_id,
                    state="succeeded",
                    phase="done",
                    error=None,
                    version_verified=True,
                    finished_at=_coerce_ms(payload.get("finished_at")),
                )
                return "succeeded_helper_and_version"
            # Helper claims success but the running version disagrees —
            # the swap didn't take (wrong image tag, stale venv, …).
            store.finalize_status_sync(
                status.request_id,
                state="failed",
                phase="version_assertion",
                error="version_assertion_failed",
                version_verified=False,
                finished_at=_coerce_ms(payload.get("finished_at")),
            )
            return "failed_version_assertion"
        if helper_state == "failed":
            fields: dict = {
                "state": "failed",
                "phase": "failed",
                "error": str(payload.get("error") or "helper_reported_failure"),
                "finished_at": _coerce_ms(payload.get("finished_at")),
            }
            rolled_back = payload.get("rolled_back")
            if isinstance(rolled_back, bool):
                fields["rolled_back"] = rolled_back
            log_excerpt = payload.get("log_excerpt")
            if isinstance(log_excerpt, str) and log_excerpt:
                fields["log_excerpt"] = log_excerpt
            store.finalize_status_sync(status.request_id, **fields)
            return "failed_mirrored"

    # (3) Inconclusive — no helper signal at all (or an unrecognized
    # terminal state) and the version doesn't match. Legacy stall flip;
    # live-helper parking happened in branch (0).
    store.finalize_status_sync(
        status.request_id,
        state="stalled",
        phase="stalled",
        error="gateway_restarted_mid_upgrade",
        finished_at=status.finished_at or _now_ms(),
    )
    return "stalled"


def _coerce_ms(raw: object) -> int:
    if isinstance(raw, bool):
        return _now_ms()
    if isinstance(raw, int):
        return raw
    return _now_ms()


async def refresh_late_helper_verdict(
    store: UpgradeStateStore, request_id: str
) -> None:
    """Mirror a helper verdict that arrived AFTER the boot finalizer ran.

    Only touches records the finalizer marked ``stalled`` with
    ``error="helper_still_running"`` (the helper was observed alive at
    boot). If the helper's status file now carries a terminal verdict for
    this request, mirror it — including a late ``succeeded`` that the
    version assertion contradicts (→ ``version_assertion_failed``, same
    rule as the boot pass). Called lazily from the status route on read;
    never raises.
    """
    try:
        status = await store.get(request_id)
        if (
            status is None
            or status.state != "stalled"
            or status.error != "helper_still_running"
        ):
            return
        data_dir = store._persist_path.parent  # noqa: SLF001 — same package
        payload = _read_helper_status(data_dir, request_id)
        if payload is None:
            return
        helper_state = str(payload.get("state") or "")
        if helper_state not in ("succeeded", "failed"):
            return  # still running — keep waiting
        fields: dict = {"finished_at": _coerce_ms(payload.get("finished_at"))}
        if helper_state == "succeeded":
            from corlinman_server.system.app_version import resolve_app_version

            if _normalize(resolve_app_version()) == _normalize(status.tag):
                fields.update(
                    state="succeeded", phase="done", error=None,
                    version_verified=True,
                )
            else:
                fields.update(
                    state="failed", phase="version_assertion",
                    error="version_assertion_failed", version_verified=False,
                )
        else:
            fields.update(
                state="failed", phase="failed",
                error=str(payload.get("error") or "helper_reported_failure"),
            )
            rolled_back = payload.get("rolled_back")
            if isinstance(rolled_back, bool):
                fields["rolled_back"] = rolled_back
            log_excerpt = payload.get("log_excerpt")
            if isinstance(log_excerpt, str) and log_excerpt:
                fields["log_excerpt"] = log_excerpt
        await store.update(request_id, **fields)
        logger.info(
            "upgrade_finalizer.late_verdict_mirrored",
            request_id=request_id,
            state=fields.get("state"),
        )
    except Exception as exc:  # noqa: BLE001 — read-path convenience only
        logger.warning(
            "upgrade_finalizer.late_refresh_failed",
            request_id=request_id,
            error=str(exc),
        )


def finalize_boot(
    store: UpgradeStateStore,
    *,
    data_dir: Path,
    current_version: str | None = None,
) -> None:
    """Reconcile every orphaned record; never raises.

    ``current_version`` is an injection seam for tests; production
    resolves through the shared
    :func:`corlinman_server.system.app_version.resolve_app_version`.
    """
    try:
        pending = store.pending_boot_statuses()
        if not pending:
            return
        if current_version is None:
            from corlinman_server.system.app_version import resolve_app_version

            current_version = resolve_app_version()
        current = _normalize(current_version)
        for status in pending:
            outcome = _finalize_one(
                store, status, data_dir=data_dir, current=current
            )
            logger.info(
                "upgrade_finalizer.reconciled",
                request_id=status.request_id,
                tag=status.tag,
                outcome=outcome,
            )
    except Exception as exc:  # noqa: BLE001 — never wedge single-flight
        logger.warning("upgrade_finalizer.failed", error=str(exc))
    finally:
        # Belt-and-braces: anything still non-terminal (skipped record,
        # exception mid-loop) gets the legacy stall flip so
        # ``current_in_flight`` can never be wedged by a finalizer bug.
        try:
            store.reconcile_orphans_sync()
        except Exception:  # noqa: BLE001 — same rationale
            logger.exception("upgrade_finalizer.orphan_fallback_failed")
