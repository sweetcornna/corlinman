"""Native-mode :class:`UpgraderProtocol` implementation.

W1.2 of ``docs/PLAN_ONE_CLICK_UPGRADE.md`` §2 Wave 1.

Architecture
------------

Writing a request directly into the gateway process would require root
(``install.sh --upgrade`` calls ``systemctl``) — which is the wrong
posture for a long-running HTTP server. Instead this implementation
hands off to a *privileged helper* via a tiny file-system contract:

#. Gateway writes ``$DATA_DIR/.upgrade-request`` atomically (temp +
   rename). The file is a JSON object with ``request_id``, ``tag``,
   ``requested_at`` (unix ms), ``requested_by`` and ``mode: "native"``.
#. ``corlinman-upgrader.path`` (a systemd ``PathChanged=`` watcher
   provisioned by ``install.sh install_native()``) fires
   ``corlinman-upgrader.service``, which runs as root and is restricted
   to one action: ``install.sh --upgrade --version <tag>`` for a tag
   that exists in the GitHub releases whitelist.
#. The helper writes ``$DATA_DIR/.upgrade-status`` as it transitions
   through ``queued → running → succeeded|failed``.
#. The gateway polls the status file every 1 s and propagates updates
   into :class:`UpgradeStateStore` so HTTP / SSE consumers see live
   progress.

The blast radius is tight:

* Helper rejects any tag that doesn't match ``^v\\d+\\.\\d+\\.\\d+...$``
  (no shell-injectable params).
* Helper re-validates the tag against the live GitHub releases list,
  so a compromised admin session that writes a "good-looking" tag still
  can't run an arbitrary install.sh ref.
* Status file is single-document JSON (we rewrite the whole file on
  every transition) so partial writes can't corrupt the polling reader.

Stall detection
---------------

If 60 s elapse between the request being written and the first
status-file update, we transition the in-memory state to ``stalled`` —
typically meaning the systemd path unit isn't installed (operator
upgraded *to* a version that introduces it but the old one didn't have
the helper yet). The UI surfaces this with the journalctl pointer.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import structlog

from corlinman_server.system.upgrader.protocol import (
    UpgradeAlreadyRunning,
    UpgraderProtocol,
)
from corlinman_server.system.upgrader.state import (
    UpgradeRequest,
    UpgradeStateStore,
    UpgradeStatus,
)

logger = structlog.get_logger(__name__)


__all__ = [
    "DEFAULT_OVERALL_TIMEOUT_S",
    "DEFAULT_STALL_TIMEOUT_S",
    "REQUEST_FILE_NAME",
    "STATUS_FILE_NAME",
    "NativeUpgrader",
]


REQUEST_FILE_NAME = ".upgrade-request"
STATUS_FILE_NAME = ".upgrade-status"

# How long to wait between the request being written and the first
# status update from the helper before we mark the request "stalled" in
# the gateway-side store (most likely cause: helper unit not installed).
DEFAULT_STALL_TIMEOUT_S = 60.0

# Hard cap on the background poll loop. Matches install.sh's
# ``TimeoutStartSec=600`` on the systemd unit.
DEFAULT_OVERALL_TIMEOUT_S = 600.0

# Pollers tick this often.
_STATUS_POLL_INTERVAL_S = 1.0

# Match :data:`DockerUpgrader._PROGRESS_POLL_SECONDS` for the SSE
# yielder — "feels live" without flooding.
_PROGRESS_POLL_SECONDS = 0.5

_SYSTEMD_UNIT_PATH = Path("/etc/systemd/system/corlinman-upgrader.service")
_SYSTEMD_PATH_UNIT = Path("/etc/systemd/system/corlinman-upgrader.path")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically (tmp + rename).

    Uses ``os.fsync`` on the tmp file to make sure the data is on disk
    *before* the rename. The systemd ``PathChanged=`` watcher would
    otherwise have a race where it fires on an empty file because the
    write hadn't flushed yet.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class NativeUpgrader:
    """Implements :class:`UpgraderProtocol` over the systemd path-watcher.

    Parameters
    ----------
    store
        Shared :class:`UpgradeStateStore` (single-flight tracker + state
        cache). Owned by the gateway lifecycle.
    data_dir
        ``$CORLINMAN_DATA_DIR`` — the directory the helper systemd unit
        watches via ``PathChanged=``. Created if missing.
    unit_path / path_unit_path
        Test seams. Production code uses the default
        ``/etc/systemd/system/...`` paths; tests inject a tmp-path so
        :meth:`is_available` can be exercised without touching root.
    stall_timeout_s
        Seconds of no status update from the helper before we flip the
        in-store state to ``stalled``.
    overall_timeout_s
        Hard cap on the background poller (matches install.sh's
        ``TimeoutStartSec=600``).
    clock
        Monotonic-clock injection point for deterministic stall tests.
        Defaults to :func:`time.monotonic`.
    """

    mode = "native"

    def __init__(
        self,
        *,
        store: UpgradeStateStore,
        data_dir: Path,
        unit_path: Path = _SYSTEMD_UNIT_PATH,
        path_unit_path: Path = _SYSTEMD_PATH_UNIT,
        stall_timeout_s: float = DEFAULT_STALL_TIMEOUT_S,
        overall_timeout_s: float = DEFAULT_OVERALL_TIMEOUT_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._data_dir = Path(data_dir)
        self._unit_path = Path(unit_path)
        self._path_unit_path = Path(path_unit_path)
        self._stall_timeout_s = stall_timeout_s
        self._overall_timeout_s = overall_timeout_s
        self._monotonic: Callable[[], float] = clock or time.monotonic

        # Hold strong refs to spawned tasks so they aren't garbage-collected
        # mid-flight (matches DockerUpgrader's pattern).
        self._background_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Protocol surface
    # ------------------------------------------------------------------

    async def is_available(self) -> bool:
        """``True`` iff the helper systemd units are installed.

        Best-effort: we only check that the unit files exist on disk, not
        that they're enabled. Operators occasionally disable units for
        debugging; the helper just won't fire in that case and we'll
        report ``stalled`` instead of misleading them with a 503 at the
        moment of the click.
        """
        return self._unit_path.exists() and self._path_unit_path.exists()

    async def start(self, target_tag: str, actor: str) -> UpgradeRequest:
        """Write the request file + register the in-flight slot.

        Single-flight: reads the store first, raises
        :class:`UpgradeAlreadyRunning` if another upgrade is in flight.
        Returns immediately after writing the request — the privileged
        helper does the actual work; the background poller mirrors its
        status writes into the store.
        """
        in_flight = await self._store.current_in_flight()
        if in_flight is not None:
            raise UpgradeAlreadyRunning(in_flight)

        # Canonicalize to the GitHub release tag form (leading ``v``).
        # The update checker strips the ``v`` for display and the UI
        # POSTs that stripped form back, but the privileged helper
        # validates the tag against the literal release ``tag_name``
        # (``v1.20.0``) and a ``^v…`` regex — an unprefixed tag fails
        # ``tag_invalid`` there. Docker mode must NOT get this prefix:
        # GHCR image tags carry no ``v`` (release-image.yml semver
        # pattern ``{{version}}``).
        stripped = (
            target_tag[1:] if target_tag[:1] in ("v", "V") else target_tag
        )
        canonical_tag = f"v{stripped}"

        request_id = uuid.uuid4().hex
        requested_at = _now_ms()
        req = UpgradeRequest(
            request_id=request_id,
            tag=canonical_tag,
            requested_at=requested_at,
            requested_by=actor,
            mode="native",
        )

        # The helper expects a UUID-with-dashes; the W1.1 convention is
        # ``uuid.uuid4().hex`` (no dashes). Format with dashes for the
        # on-disk payload so the bash UUID_REGEX matches; keep the dash-
        # less form in the store for parity with DockerUpgrader.
        helper_request_id = str(uuid.UUID(request_id))

        payload: dict[str, Any] = {
            "request_id": helper_request_id,
            "tag": canonical_tag,
            "requested_at": requested_at,
            "requested_by": actor,
            "mode": "native",
        }
        request_path = self._data_dir / REQUEST_FILE_NAME
        _atomic_write_json(request_path, payload)

        # Persist the in-flight slot in the store so /admin/system/audit
        # + /status see something immediately.
        await self._store.begin(req)

        # Kick off the background poller. start() returns immediately.
        task = asyncio.create_task(
            self._poll_status_file(req, helper_request_id),
            name=f"native-upgrade-{req.request_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        logger.info(
            "native_upgrader.request_written",
            request_id=req.request_id,
            tag=canonical_tag,
            requested_by=actor,
            path=str(request_path),
        )
        return req

    async def progress(
        self, request_id: str
    ) -> AsyncIterator[UpgradeStatus]:
        """Yield status snapshots until terminal state.

        Mirrors :class:`DockerUpgrader.progress` — polls the store every
        ``_PROGRESS_POLL_SECONDS`` and yields whenever ``state`` /
        ``phase`` / ``log_excerpt`` length changes. Terminates on first
        terminal snapshot.
        """
        initial = await self._store.get(request_id)
        if initial is None:
            return
        last_state: str | None = None
        last_phase: str | None = None
        last_log_len = -1
        while True:
            status = await self._store.get(request_id)
            if status is None:
                return
            changed = (
                status.state != last_state
                or status.phase != last_phase
                or len(status.log_excerpt) != last_log_len
            )
            if changed or last_state is None:
                yield status
                last_state = status.state
                last_phase = status.phase
                last_log_len = len(status.log_excerpt)
            if status.is_terminal():
                return
            await asyncio.sleep(_PROGRESS_POLL_SECONDS)

    # ------------------------------------------------------------------
    # Internal poll loop
    # ------------------------------------------------------------------

    async def _poll_status_file(
        self, req: UpgradeRequest, helper_request_id: str
    ) -> None:
        """Mirror the helper's status-file writes into :attr:`_store`.

        Runs as a fire-and-forget task scheduled by :meth:`start`. Quiet
        exit on terminal state, stall, or overall timeout. Any exception
        is logged and re-flipped into a ``failed`` state on the store so
        the UI doesn't spin forever.
        """
        status_path = self._data_dir / STATUS_FILE_NAME
        first_seen_at = self._monotonic()
        deadline = first_seen_at + self._overall_timeout_s
        last_payload: dict[str, Any] | None = None
        ever_observed = False

        try:
            while True:
                now = self._monotonic()
                if now > deadline:
                    # Overall timeout — upgrader hung. Surface as failed.
                    logger.warning(
                        "native_upgrader.overall_timeout",
                        request_id=req.request_id,
                        timeout_s=self._overall_timeout_s,
                    )
                    await self._safe_update(
                        req.request_id,
                        state="failed",
                        phase="timeout",
                        error="overall_timeout",
                        finished_at=_now_ms(),
                    )
                    return

                payload = self._read_status_file(
                    status_path, helper_request_id
                )
                if payload is not None:
                    ever_observed = True
                    if payload != last_payload:
                        last_payload = payload
                        await self._apply_payload(req, payload)
                        state = str(payload.get("state", ""))
                        if state in {"succeeded", "failed"}:
                            return

                # Stall detection: no status seen at all yet, and we're
                # past the stall_timeout.
                if (
                    not ever_observed
                    and (now - first_seen_at) > self._stall_timeout_s
                ):
                    logger.warning(
                        "native_upgrader.stalled",
                        request_id=req.request_id,
                        stall_timeout_s=self._stall_timeout_s,
                    )
                    await self._safe_update(
                        req.request_id,
                        state="stalled",
                        phase="stalled",
                        error="helper_unit_missing_or_disabled",
                        finished_at=_now_ms(),
                    )
                    return

                await asyncio.sleep(_STATUS_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "native_upgrader.poll_failed",
                request_id=req.request_id,
                error=str(exc),
            )
            await self._safe_update(
                req.request_id,
                state="failed",
                phase="poll_error",
                error=f"poll_exception:{type(exc).__name__}",
                finished_at=_now_ms(),
            )

    async def _apply_payload(
        self, req: UpgradeRequest, payload: dict[str, Any]
    ) -> None:
        """Translate a helper-written status payload to store fields."""
        state_raw = str(payload.get("state", "running"))
        # Map helper states to the store's UpgradeState literal.
        state: str = state_raw if state_raw in {
            "queued", "running", "succeeded", "failed", "stalled"
        } else "running"
        phase = state  # native impl has no sub-phase; mirror state.

        kwargs: dict[str, Any] = {
            "state": state,
            "phase": phase,
        }
        started = payload.get("started_at")
        if isinstance(started, int):
            kwargs["started_at"] = started
        finished = payload.get("finished_at")
        if finished is None and state in {"succeeded", "failed", "stalled"}:
            kwargs["finished_at"] = _now_ms()
        elif isinstance(finished, int):
            kwargs["finished_at"] = finished
        err = payload.get("error")
        if err is None or isinstance(err, str):
            kwargs["error"] = err
        log_excerpt = payload.get("log_excerpt")
        if isinstance(log_excerpt, str):
            # Helper writes the *full* tail (4 KiB cap is enforced
            # there); the store also caps at 4 KiB on append, so we
            # overwrite via `update()` rather than `append_log()` to
            # keep last-write-wins semantics.
            kwargs["log_excerpt"] = log_excerpt
        await self._safe_update(req.request_id, **kwargs)

    async def _safe_update(self, request_id: str, **fields: Any) -> None:
        """``store.update`` that swallows KeyError on cleanup races."""
        try:
            await self._store.update(request_id, **fields)
        except KeyError:
            # Request gone from the store — gateway restarted or test
            # tore the store down. Nothing to do.
            return

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_status_file(
        status_path: Path, expected_request_id: str
    ) -> dict[str, Any] | None:
        """Read + JSON-parse the helper's status file.

        Returns ``None`` if the file doesn't exist, can't be parsed, or
        belongs to a different request_id (the helper may still be
        finishing a previous request when we get here). Never raises.
        """
        try:
            raw = status_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.debug(
                "native_upgrader.status_read_failed",
                path=str(status_path),
                error=str(exc),
            )
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # The helper writes the file atomically (tmp + rename) so we
            # shouldn't catch it mid-write, but a torn read on a weird
            # filesystem is still survivable — just skip this tick.
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("request_id") != expected_request_id:
            return None
        return payload


# Tell the static checker NativeUpgrader satisfies UpgraderProtocol.
# Runtime-checked Protocols don't need explicit registration; this is
# purely for documentation purposes.
_: type[UpgraderProtocol] = NativeUpgrader  # type: ignore[assignment]
