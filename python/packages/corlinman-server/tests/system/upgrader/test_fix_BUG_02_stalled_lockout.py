"""Repro for BUG-02: stalled / orphaned upgrade blocks new upgrades forever.

Root cause (per audit):

* ``stalled`` was in BOTH ``is_terminal()`` and ``is_in_flight()``.
* ``current_in_flight()`` filters on ``is_in_flight()`` and every
  ``start()`` raises ``UpgradeAlreadyRunning`` on a non-None result with
  NO reset path.
* ``NativeUpgrader`` flips to ``stalled`` whenever the helper systemd
  unit is missing (the common case on a host that hasn't installed the
  path-watcher yet) → permanent lockout: a retry always 409s.
* A ``queued`` / ``running`` status persisted to disk before a gateway
  restart can never be resumed, yet on cold start it still counts as
  in-flight → another permanent lockout.

Acceptance:

* A terminal status (incl ``stalled`` and orphaned ``running``) is NOT
  in-flight, so a retry is allowed.
* On ``_load_from_disk`` cold start, a non-terminal persisted status is
  reconciled to ``stalled`` so ``current_in_flight()`` returns None.
* Native mode without helper units: POST flips to stalled; a second POST
  is allowed (does not 409 forever).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest
from corlinman_server.system.upgrader import (
    UpgradeAlreadyRunning,
    UpgradeRequest,
    UpgradeStateStore,
    UpgradeStatus,
)
from corlinman_server.system.upgrader import native_upgrader as nu


def _now_ms() -> int:
    return int(time.time() * 1000)


def test_stalled_is_terminal_and_not_in_flight() -> None:
    """A ``stalled`` status is terminal AND must NOT occupy an in-flight
    slot — otherwise ``current_in_flight()`` keeps returning it and every
    retry 409s forever."""
    status = UpgradeStatus(
        request_id="r",
        tag="v1",
        state="stalled",
        phase="stalled",
        started_at=1,
        finished_at=2,
    )
    # R3-005: stalled is terminal so progress SSE loops exit.
    assert status.is_terminal() is True
    # BUG-02: stalled is NOT in-flight so a new upgrade can be started.
    assert status.is_in_flight() is False


@pytest.mark.asyncio
async def test_current_in_flight_none_after_stalled(tmp_path: Path) -> None:
    store = UpgradeStateStore(tmp_path / ".upgrade-state.json")
    req = UpgradeRequest(
        request_id="req-stalled",
        tag="v1.2.0",
        requested_at=_now_ms(),
        requested_by="ops",
        mode="native",
    )
    await store.begin(req)
    await store.update(
        req.request_id, state="stalled", phase="stalled", finished_at=_now_ms()
    )
    # The lockout bug: stalled was treated as in-flight forever.
    assert await store.current_in_flight() is None


@pytest.mark.asyncio
async def test_cold_start_reconciles_running_to_stalled(
    tmp_path: Path,
) -> None:
    """A ``running`` (or ``queued``) status persisted before a restart
    cannot resume — its task is gone. On cold start it must reconcile to
    ``stalled`` so ``current_in_flight()`` returns None."""
    persist = tmp_path / ".upgrade-state.json"
    store1 = UpgradeStateStore(persist)
    req = UpgradeRequest(
        request_id="req-orphan",
        tag="v1.2.0",
        requested_at=_now_ms(),
        requested_by="ops",
        mode="native",
    )
    await store1.begin(req)
    await store1.update(req.request_id, state="running", phase="pulling")

    # Cold start: fresh store reading the same persisted file. The
    # background task is NOT resumed, so a still-"running" record is
    # orphaned and must not lock out new upgrades.
    store2 = UpgradeStateStore(persist)
    recovered = await store2.get(req.request_id)
    assert recovered is not None
    assert recovered.state == "stalled"
    assert await store2.current_in_flight() is None


@pytest.mark.asyncio
async def test_native_second_upgrade_allowed_after_stall(
    tmp_path: Path,
) -> None:
    """Native mode without helper units: first POST flips to stalled; a
    second POST must be ALLOWED (not 409 forever)."""
    original_poll = nu._STATUS_POLL_INTERVAL_S
    nu._STATUS_POLL_INTERVAL_S = 0.02
    try:
        base = time.monotonic()
        fake_offset = [0.0]

        def clock() -> float:
            return (time.monotonic() - base) + fake_offset[0]

        store = UpgradeStateStore(tmp_path / ".upgrade-state.json")
        upgrader = nu.NativeUpgrader(
            store=store,
            data_dir=tmp_path,
            unit_path=tmp_path / "missing.service",
            path_unit_path=tmp_path / "missing.path",
            stall_timeout_s=0.05,
            overall_timeout_s=999,
            clock=clock,
        )

        # First upgrade — no helper writes a status file, so it stalls.
        req1 = await upgrader.start(target_tag="v1.2.1", actor="ops")
        await asyncio.sleep(0.05)
        fake_offset[0] += 5.0

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            snap = await store.get(req1.request_id)
            if snap is not None and snap.state == "stalled":
                break
            await asyncio.sleep(0.02)
        snap1 = await store.get(req1.request_id)
        assert snap1 is not None and snap1.state == "stalled"

        # Second upgrade MUST be allowed — the bug raised
        # UpgradeAlreadyRunning here forever.
        req2 = await upgrader.start(target_tag="v1.2.2", actor="ops")
        assert req2.request_id != req1.request_id
    finally:
        nu._STATUS_POLL_INTERVAL_S = original_poll
        for task in list(upgrader._background_tasks):
            task.cancel()
        await asyncio.gather(
            *upgrader._background_tasks, return_exceptions=True
        )
