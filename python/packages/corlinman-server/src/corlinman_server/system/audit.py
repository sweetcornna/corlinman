"""``corlinman_server.system.audit`` — append-only JSONL system-audit log.

Sibling of :mod:`corlinman_server.system.update_checker`; first consumed
by the one-click upgrade surface (W1.3 of ``docs/PLAN_ONE_CLICK_UPGRADE.md``).

Design contract
---------------

* **Append-only**. Every line is one JSON object terminated by ``\n``.
  Writers never rewrite existing lines; consumers can ``tail -f`` the
  raw file as a stable interface.

* **Best-effort writes**. :meth:`SystemAuditLog.append` *must never*
  raise into the request path — a failed audit-log write should never
  block an upgrade or a credential rotation. Failures log a warning
  and the entry is dropped.

* **Bounded reads**. :meth:`SystemAuditLog.tail` returns at most
  ``limit`` entries newest-first. ``before_ts`` paginates by walking
  backwards in time — callers pass the oldest ``ts`` they've seen to
  fetch the next page.

* **Single-writer serialisation**. An :class:`asyncio.Lock` guards the
  append path so concurrent admin actions (two browser tabs hitting
  ``POST /admin/system/upgrade`` at the same instant) don't interleave
  bytes on disk.

Wire shape
----------

Each line is::

    {
      "ts": "2026-05-25T12:00:00.123Z",
      "event": "system.upgrade.requested",
      "request_id": "abc-123",
      "tag": "v1.2.1",
      "actor": "ops",
      "details": {...}
    }

``ts`` is ISO-8601 UTC with millisecond precision and a trailing ``Z``
so the JS UI can pass it straight to ``new Date()`` without timezone
gymnastics. ``request_id``, ``tag``, ``actor`` are nullable; ``details``
defaults to ``{}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


__all__ = ["AuditEntry", "SystemAuditLog", "utcnow_iso"]


def utcnow_iso() -> str:
    """ISO-8601 UTC timestamp with millisecond precision + ``Z`` suffix.

    Mirrors what the JS UI's ``new Date().toISOString()`` produces so a
    round-trip through the wire keeps lexicographic-sortable ordering.
    """
    now = datetime.now(UTC)
    # ``isoformat`` would give ``+00:00``; the UI canonicalises ``Z``.
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


@dataclass(slots=True, frozen=True)
class AuditEntry:
    """One row in the system-audit log.

    Frozen so callers can stash references without worrying about a
    later mutation racing the writer. ``details`` is a free-form bag —
    consumers should treat unknown keys as forward-compat hints.
    """

    ts: str
    event: str
    request_id: str | None = None
    tag: str | None = None
    actor: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """Plain-dict serialiser for the JSONL line + API responses."""
        return {
            "ts": self.ts,
            "event": self.event,
            "request_id": self.request_id,
            "tag": self.tag,
            "actor": self.actor,
            "details": dict(self.details),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> AuditEntry:
        """Reverse of :meth:`to_json`; tolerates missing optional keys."""
        details = raw.get("details")
        if not isinstance(details, dict):
            details = {}
        return cls(
            ts=str(raw.get("ts") or ""),
            event=str(raw.get("event") or ""),
            request_id=_as_optional_str(raw.get("request_id")),
            tag=_as_optional_str(raw.get("tag")),
            actor=_as_optional_str(raw.get("actor")),
            details=details,
        )


def _as_optional_str(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    return str(raw)


class SystemAuditLog:
    """Append-only JSONL writer + bounded reader.

    One instance per process; the gateway lifecycle constructs it once
    against ``<data_dir>/system-audit.log`` and shares it with every
    admin route that needs to record a state transition.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        # Single asyncio.Lock — every append serialises through it so
        # concurrent admin actions can't interleave bytes mid-line.
        self._write_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """On-disk location (for debug logging + tests)."""
        return self._path

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def append(self, entry: AuditEntry) -> None:
        """Append one entry as a JSONL line.

        Never raises. A write failure (unwritable path, full disk,
        encoding error) logs at WARN and the entry is dropped — the
        caller's upgrade / credential mutation continues without
        cluttered failure paths.
        """
        try:
            line = json.dumps(entry.to_json(), ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as exc:
            logger.warning(
                "system_audit.serialise_failed",
                event=entry.event,
                error=str(exc),
            )
            return

        async with self._write_lock:
            await asyncio.to_thread(self._write_line, line)

    def _write_line(self, line: str) -> None:
        """Synchronous helper run inside :func:`asyncio.to_thread`.

        Creates parent directories on first write so a fresh data dir
        is no longer a precondition. Open-append-close so we don't hold
        a file handle across awaits (the lock serialises us anyway).
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            logger.warning(
                "system_audit.write_failed",
                path=str(self._path),
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    async def tail(
        self,
        limit: int = 50,
        before_ts: str | None = None,
    ) -> list[AuditEntry]:
        """Return up to ``limit`` newest entries, optionally paginated.

        ``before_ts`` is a string ``ts`` value (typically the ``ts`` of
        the oldest entry from the previous page). Entries with
        ``ts < before_ts`` (lexicographic comparison — safe because we
        always emit zero-padded ISO-8601) are returned newest-first.

        Returns ``[]`` on a missing log file or read error — best-effort
        like the writer.
        """
        if limit <= 0:
            return []
        return await asyncio.to_thread(self._read_tail, limit, before_ts)

    def _read_tail(self, limit: int, before_ts: str | None) -> list[AuditEntry]:
        """Synchronous reader, run off the event loop.

        Reads the whole file. The audit log is one line per state
        transition (upgrades are rare); we don't try to be clever about
        reverse-seek I/O until volume justifies it.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning(
                "system_audit.read_failed",
                path=str(self._path),
                error=str(exc),
            )
            return []

        entries: list[AuditEntry] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # Tolerate the odd corrupted line — skip + keep walking.
                continue
            if not isinstance(payload, dict):
                continue
            entry = AuditEntry.from_json(payload)
            entries.append(entry)

        # Newest-first by ``ts``. The writer always stamps a monotonic
        # ISO-8601 string, but a clock jump could put earlier rows
        # after later ones on disk — sorting protects the contract.
        entries.sort(key=lambda e: e.ts, reverse=True)

        if before_ts:
            entries = [e for e in entries if e.ts < before_ts]

        return entries[:limit]


# ---------------------------------------------------------------------------
# Module-level conveniences
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def open_audit_log(path: Path):
    """Async context manager that yields a :class:`SystemAuditLog`.

    Convenience for tests / one-shot scripts that want explicit setup +
    teardown semantics. The class itself doesn't need a teardown today,
    but the context manager future-proofs the interface in case we add
    a rotation flush later.
    """
    log = SystemAuditLog(path)
    try:
        yield log
    finally:
        # Reserved — placeholder for future rotation flush. A bare
        # ``return`` here would swallow any exception raised inside the
        # ``with`` body (B012); use ``pass`` so the exception propagates.
        pass
