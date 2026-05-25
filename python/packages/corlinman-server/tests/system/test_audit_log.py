"""Tests for :class:`corlinman_server.system.SystemAuditLog` (W1.3).

Append-only JSONL writer + reader. The contract:

* Newest-first read order.
* ``before_ts`` paginates correctly.
* ``limit`` clamps the page size.
* Concurrent appends are serialised — no interleaved bytes on disk.
* Write failures (unwritable path) never raise into the caller.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from corlinman_server.system.audit import AuditEntry, SystemAuditLog, utcnow_iso


def _entry(ts: str, event: str, *, request_id: str | None = None) -> AuditEntry:
    return AuditEntry(
        ts=ts,
        event=event,
        request_id=request_id,
        tag=None,
        actor="ops",
        details={"hello": "world"},
    )


@pytest.mark.asyncio
async def test_append_three_returns_newest_first(tmp_path: Path) -> None:
    log = SystemAuditLog(tmp_path / "audit.log")
    await log.append(_entry("2026-05-25T10:00:00.000Z", "a"))
    await log.append(_entry("2026-05-25T10:00:01.000Z", "b"))
    await log.append(_entry("2026-05-25T10:00:02.000Z", "c"))

    entries = await log.tail(limit=10)
    assert [e.event for e in entries] == ["c", "b", "a"]
    # File on disk should be three lines, append-order preserved.
    raw = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert raw.count("\n") == 3
    parsed = [json.loads(line) for line in raw.splitlines()]
    assert [p["event"] for p in parsed] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_before_ts_paginates(tmp_path: Path) -> None:
    log = SystemAuditLog(tmp_path / "audit.log")
    for i in range(5):
        await log.append(
            _entry(f"2026-05-25T10:00:0{i}.000Z", f"event-{i}")
        )

    first_page = await log.tail(limit=2)
    assert [e.event for e in first_page] == ["event-4", "event-3"]

    second_page = await log.tail(limit=2, before_ts=first_page[-1].ts)
    assert [e.event for e in second_page] == ["event-2", "event-1"]

    third_page = await log.tail(limit=2, before_ts=second_page[-1].ts)
    assert [e.event for e in third_page] == ["event-0"]

    fourth_page = await log.tail(limit=2, before_ts=third_page[-1].ts)
    assert fourth_page == []


@pytest.mark.asyncio
async def test_limit_one_returns_newest_only(tmp_path: Path) -> None:
    log = SystemAuditLog(tmp_path / "audit.log")
    await log.append(_entry("2026-05-25T10:00:00.000Z", "a"))
    await log.append(_entry("2026-05-25T10:00:01.000Z", "b"))
    await log.append(_entry("2026-05-25T10:00:02.000Z", "c"))

    entries = await log.tail(limit=1)
    assert len(entries) == 1
    assert entries[0].event == "c"


@pytest.mark.asyncio
async def test_concurrent_appends_are_serialised(tmp_path: Path) -> None:
    """Fire 50 appends in parallel — every line must be intact JSON."""
    log = SystemAuditLog(tmp_path / "audit.log")

    async def _one(i: int) -> None:
        await log.append(
            _entry(f"2026-05-25T10:00:{i:02d}.000Z", f"event-{i}")
        )

    await asyncio.gather(*[_one(i) for i in range(50)])

    raw = (tmp_path / "audit.log").read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert len(lines) == 50
    # Every line must parse as JSON — no interleaved bytes.
    for line in lines:
        json.loads(line)
    # And every event must appear exactly once.
    events = sorted(json.loads(line)["event"] for line in lines)
    assert events == sorted(f"event-{i}" for i in range(50))


@pytest.mark.asyncio
async def test_write_failure_does_not_raise(tmp_path: Path) -> None:
    """Pointing the log at an unwritable path must never raise."""
    # Use a path whose parent we deliberately make read-only — the
    # writer's mkdir + open both fail.
    bad_path = tmp_path / "nope" / "audit.log"
    # Pre-create the parent as a *file* (not a directory) so mkdir
    # fails. The writer must swallow the OSError.
    (tmp_path / "nope").write_text("not a dir", encoding="utf-8")

    log = SystemAuditLog(bad_path)
    # Should not raise.
    await log.append(_entry("2026-05-25T10:00:00.000Z", "boom"))
    # Tail should still return [] cleanly.
    assert await log.tail() == []


@pytest.mark.asyncio
async def test_tail_missing_file_returns_empty(tmp_path: Path) -> None:
    log = SystemAuditLog(tmp_path / "never-created.log")
    assert await log.tail() == []
    assert await log.tail(limit=10, before_ts="2026-01-01T00:00:00.000Z") == []


@pytest.mark.asyncio
async def test_corrupted_line_is_skipped(tmp_path: Path) -> None:
    """A malformed JSON line must not crash the reader."""
    log = SystemAuditLog(tmp_path / "audit.log")
    await log.append(_entry("2026-05-25T10:00:00.000Z", "a"))
    # Manually corrupt the file by appending a non-JSON line.
    with (tmp_path / "audit.log").open("a", encoding="utf-8") as h:
        h.write("not-valid-json\n")
    await log.append(_entry("2026-05-25T10:00:02.000Z", "b"))

    entries = await log.tail(limit=10)
    assert [e.event for e in entries] == ["b", "a"]


def test_utcnow_iso_format() -> None:
    """The timestamp formatter emits ISO-8601 with Z suffix + ms precision."""
    ts = utcnow_iso()
    # YYYY-MM-DDTHH:MM:SS.mmmZ — 24 characters.
    assert len(ts) == 24
    assert ts.endswith("Z")
    assert ts[10] == "T"
    assert ts[4] == "-" and ts[7] == "-"
    assert ts[13] == ":" and ts[16] == ":"
    assert ts[19] == "."
