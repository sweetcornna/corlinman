"""Shared in-memory + persistent state tracker for one-click upgrades.

W1.1 of ``docs/PLAN_ONE_CLICK_UPGRADE.md`` §2 Wave 1/W1.1.

Design contract
---------------

* One :class:`UpgradeStateStore` per gateway process; both upgrader impls
  (Docker today, Native in W1.2) share it via :func:`resolve_upgrader`.

* Single-flight is enforced by callers reading
  :meth:`UpgradeStateStore.current_in_flight` before issuing
  :meth:`UpgradeStateStore.begin`. The store itself doesn't refuse
  concurrent ``begin()`` calls — each impl wraps the check in its own
  ``start()`` so the 409 error message can be impl-specific.

* Every mutation is serialised through an :class:`asyncio.Lock` and
  flushed to ``$DATA_DIR/.upgrade-state.json`` via temp+rename so a
  gateway restart mid-upgrade preserves the audit trail. The persisted
  file is the single source of truth on cold start.

* ``log_excerpt`` is bounded at 4 kB (last-N strategy). We want enough
  context for the UI's "tail" panel without ballooning the JSON.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)


__all__ = [
    "UpgradeRequest",
    "UpgradeStatus",
    "UpgradeStateStore",
    "UpgradeState",
]


# Maximum size of the rolling log excerpt persisted per status. 4 kB is
# enough for a few hundred lines of pull progress / compose output while
# keeping the state JSON cheap to read on cold start.
_LOG_EXCERPT_MAX_BYTES = 4 * 1024


UpgradeState = Literal["queued", "running", "succeeded", "failed", "stalled"]
"""Terminal vs in-flight discriminator for :class:`UpgradeStatus`.

``queued`` — request accepted, background task not yet started.
``running`` — pull / recreate / healthcheck in progress.
``stalled`` — operator-visible warning; gateway restarted mid-upgrade.
``succeeded`` / ``failed`` — terminal.
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class UpgradeRequest:
    """Immutable record of a user-initiated upgrade request.

    Written once at :meth:`UpgradeStateStore.begin`; never mutated.
    """

    request_id: str
    tag: str
    requested_at: int
    requested_by: str
    mode: Literal["docker", "native"]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class UpgradeStatus:
    """Mutable status tracked across the upgrade lifecycle."""

    request_id: str
    tag: str
    state: UpgradeState
    phase: str
    started_at: int | None = None
    finished_at: int | None = None
    log_excerpt: str = ""
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def is_terminal(self) -> bool:
        return self.state in ("succeeded", "failed", "stalled")

    def is_in_flight(self) -> bool:
        return self.state in ("queued", "running", "stalled")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class UpgradeStateStore:
    """In-memory tracker of upgrade requests + statuses with JSON persistence.

    On every transition the full state map is flushed atomically to
    ``persist_path`` (``.tmp`` + :func:`os.replace`). Construction loads
    any existing file so a restart mid-upgrade preserves the audit trail
    — though the in-flight task itself does not resume (W1.2 may flag
    stranded jobs as ``state="stalled"``; for W1.1 we just keep the
    record).
    """

    def __init__(self, persist_path: Path) -> None:
        self._persist_path = persist_path
        self._lock = asyncio.Lock()
        self._requests: dict[str, UpgradeRequest] = {}
        self._statuses: dict[str, UpgradeStatus] = {}
        self._load_from_disk()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> None:
        """Best-effort hydrate from the persisted JSON. Never raises."""
        try:
            raw = self._persist_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning(
                "upgrade_state.load_failed",
                path=str(self._persist_path),
                error=str(exc),
            )
            return
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            logger.warning(
                "upgrade_state.parse_failed",
                path=str(self._persist_path),
                error=str(exc),
            )
            return
        if not isinstance(payload, dict):
            return
        requests = payload.get("requests") or {}
        statuses = payload.get("statuses") or {}
        if isinstance(requests, dict):
            for rid, raw_req in requests.items():
                if not isinstance(raw_req, dict):
                    continue
                try:
                    self._requests[rid] = UpgradeRequest(
                        request_id=str(raw_req["request_id"]),
                        tag=str(raw_req["tag"]),
                        requested_at=int(raw_req["requested_at"]),
                        requested_by=str(raw_req["requested_by"]),
                        mode=raw_req["mode"],
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        if isinstance(statuses, dict):
            for rid, raw_status in statuses.items():
                if not isinstance(raw_status, dict):
                    continue
                try:
                    self._statuses[rid] = UpgradeStatus(
                        request_id=str(raw_status["request_id"]),
                        tag=str(raw_status["tag"]),
                        state=raw_status["state"],
                        phase=str(raw_status.get("phase") or ""),
                        started_at=raw_status.get("started_at"),
                        finished_at=raw_status.get("finished_at"),
                        log_excerpt=str(raw_status.get("log_excerpt") or ""),
                        error=raw_status.get("error"),
                    )
                except (KeyError, TypeError, ValueError):
                    continue

    def _flush_locked(self) -> None:
        """Atomically persist current state. Caller MUST hold ``self._lock``."""
        payload = {
            "requests": {
                rid: req.to_json() for rid, req in self._requests.items()
            },
            "statuses": {
                rid: status.to_json() for rid, status in self._statuses.items()
            },
        }
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(
                self._persist_path.suffix + ".tmp"
            )
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._persist_path)
        except OSError as exc:
            logger.warning(
                "upgrade_state.flush_failed",
                path=str(self._persist_path),
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def begin(self, req: UpgradeRequest) -> UpgradeStatus:
        """Record a new request + seed its status as ``queued``.

        Does NOT enforce single-flight — the caller (an Upgrader's
        ``start()``) must check :meth:`current_in_flight` first and raise
        the impl-appropriate error.
        """
        status = UpgradeStatus(
            request_id=req.request_id,
            tag=req.tag,
            state="queued",
            phase="queued",
            started_at=None,
            finished_at=None,
            log_excerpt="",
            error=None,
        )
        async with self._lock:
            self._requests[req.request_id] = req
            self._statuses[req.request_id] = status
            self._flush_locked()
            return UpgradeStatus(**asdict(status))

    async def update(self, request_id: str, **fields: Any) -> UpgradeStatus:
        """Partial-update a status without losing other fields.

        Raises ``KeyError`` when the request isn't known.
        """
        async with self._lock:
            current = self._statuses.get(request_id)
            if current is None:
                raise KeyError(request_id)
            for key, value in fields.items():
                if not hasattr(current, key):
                    raise AttributeError(
                        f"UpgradeStatus has no field {key!r}"
                    )
                setattr(current, key, value)
            self._flush_locked()
            return UpgradeStatus(**asdict(current))

    async def get(self, request_id: str) -> UpgradeStatus | None:
        async with self._lock:
            current = self._statuses.get(request_id)
            if current is None:
                return None
            return UpgradeStatus(**asdict(current))

    async def current_in_flight(self) -> UpgradeStatus | None:
        """Return any status with state in ``{queued, running, stalled}``.

        At most one should ever exist; the caller is responsible for
        enforcing single-flight at the request site.
        """
        async with self._lock:
            for status in self._statuses.values():
                if status.is_in_flight():
                    return UpgradeStatus(**asdict(status))
            return None

    async def append_log(self, request_id: str, chunk: str) -> None:
        """Append ``chunk`` to ``log_excerpt``, trimming to the last 4 kB.

        Silently no-ops on unknown ``request_id`` (a background task may
        race with cleanup; we don't want to crash because of it).
        """
        if not chunk:
            return
        async with self._lock:
            current = self._statuses.get(request_id)
            if current is None:
                return
            combined = current.log_excerpt + chunk
            if len(combined.encode("utf-8")) > _LOG_EXCERPT_MAX_BYTES:
                # UTF-8-safe right-trim: decode after slicing extra bytes.
                encoded = combined.encode("utf-8")[-_LOG_EXCERPT_MAX_BYTES:]
                # ``errors="ignore"`` drops any partial leading codepoint.
                combined = encoded.decode("utf-8", errors="ignore")
            current.log_excerpt = combined
            self._flush_locked()
