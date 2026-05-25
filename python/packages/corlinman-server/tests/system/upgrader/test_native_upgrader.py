"""Tests for :class:`corlinman_server.system.upgrader.NativeUpgrader` (W1.2).

Coverage:

* ``is_available`` true / false against tmp systemd unit paths
* ``start`` writes request JSON in the expected shape, atomically
* ``start`` raises :class:`UpgradeAlreadyRunning` on a busy store
* The background poller mirrors helper-written status transitions into
  the store (queued → running → succeeded)
* Stall detection — no status file written within ``stall_timeout_s``
  flips the in-store state to ``stalled``
* ``progress`` async iterator yields snapshots and terminates on
  terminal state
"""

from __future__ import annotations

import asyncio
import json
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> UpgradeStateStore:
    return UpgradeStateStore(tmp_path / ".upgrade-state.json")


def _write_status_file(data_dir: Path, payload: dict[str, object]) -> None:
    """Helper-side write: atomic tmp + rename, mirrors bash script."""
    target = data_dir / nu.STATUS_FILE_NAME
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(target)


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_available_true_when_both_units_exist(
    tmp_path: Path, store: UpgradeStateStore
) -> None:
    unit = tmp_path / "corlinman-upgrader.service"
    path_unit = tmp_path / "corlinman-upgrader.path"
    unit.write_text("# stub", encoding="utf-8")
    path_unit.write_text("# stub", encoding="utf-8")

    upgrader = nu.NativeUpgrader(
        store=store,
        data_dir=tmp_path,
        unit_path=unit,
        path_unit_path=path_unit,
    )
    assert await upgrader.is_available() is True


@pytest.mark.asyncio
async def test_is_available_false_when_service_unit_missing(
    tmp_path: Path, store: UpgradeStateStore
) -> None:
    path_unit = tmp_path / "corlinman-upgrader.path"
    path_unit.write_text("# stub", encoding="utf-8")
    upgrader = nu.NativeUpgrader(
        store=store,
        data_dir=tmp_path,
        unit_path=tmp_path / "missing.service",
        path_unit_path=path_unit,
    )
    assert await upgrader.is_available() is False


@pytest.mark.asyncio
async def test_is_available_false_when_path_unit_missing(
    tmp_path: Path, store: UpgradeStateStore
) -> None:
    unit = tmp_path / "corlinman-upgrader.service"
    unit.write_text("# stub", encoding="utf-8")
    upgrader = nu.NativeUpgrader(
        store=store,
        data_dir=tmp_path,
        unit_path=unit,
        path_unit_path=tmp_path / "missing.path",
    )
    assert await upgrader.is_available() is False


# ---------------------------------------------------------------------------
# start() — request file shape + atomicity + single-flight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_writes_request_json_with_expected_shape(
    tmp_path: Path, store: UpgradeStateStore
) -> None:
    upgrader = nu.NativeUpgrader(
        store=store,
        data_dir=tmp_path,
        unit_path=tmp_path / "u.service",
        path_unit_path=tmp_path / "u.path",
        # Long timeouts — we cancel the poller manually at end of test.
        stall_timeout_s=999,
        overall_timeout_s=999,
    )

    try:
        req = await upgrader.start(target_tag="v1.2.1", actor="ops")

        request_path = tmp_path / nu.REQUEST_FILE_NAME
        assert request_path.exists(), "request file not written"
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        assert payload["mode"] == "native"
        assert payload["tag"] == "v1.2.1"
        assert payload["requested_by"] == "ops"
        assert isinstance(payload["requested_at"], int)
        # Helper expects dashed UUID; store carries the hex form. They
        # parse equal as ``uuid.UUID``.
        helper_rid = payload["request_id"]
        assert uuid.UUID(helper_rid).hex == req.request_id

        # Atomicity: no stray .tmp file should linger.
        tmp_file = request_path.with_suffix(request_path.suffix + ".tmp")
        assert not tmp_file.exists(), ".tmp file lingered after atomic rename"

        # The store has the new request registered + a queued snapshot.
        snap = await store.get(req.request_id)
        assert snap is not None
        assert snap.state == "queued"
        assert snap.tag == "v1.2.1"
    finally:
        for task in list(upgrader._background_tasks):
            task.cancel()
        await asyncio.gather(
            *upgrader._background_tasks, return_exceptions=True
        )


@pytest.mark.asyncio
async def test_start_raises_when_another_upgrade_in_flight(
    tmp_path: Path, store: UpgradeStateStore
) -> None:
    # Pre-seed a fake in-flight request via the real store API.
    existing = UpgradeRequest(
        request_id=uuid.uuid4().hex,
        tag="v1.2.0",
        requested_at=int(time.time() * 1000),
        requested_by="someone",
        mode="native",
    )
    await store.begin(existing)

    upgrader = nu.NativeUpgrader(
        store=store,
        data_dir=tmp_path,
        unit_path=tmp_path / "u.service",
        path_unit_path=tmp_path / "u.path",
        stall_timeout_s=999,
        overall_timeout_s=999,
    )
    with pytest.raises(UpgradeAlreadyRunning) as exc:
        await upgrader.start(target_tag="v1.2.1", actor="ops")
    # The exception carries the pre-existing status snapshot.
    assert exc.value.in_flight.request_id == existing.request_id


# ---------------------------------------------------------------------------
# Background poller — status mirroring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poller_mirrors_helper_status_transitions(
    tmp_path: Path, store: UpgradeStateStore
) -> None:
    # Speed up the poller for the test by patching the module-level
    # interval. Otherwise we'd wait full 1 s ticks.
    original = nu._STATUS_POLL_INTERVAL_S
    nu._STATUS_POLL_INTERVAL_S = 0.05
    try:
        upgrader = nu.NativeUpgrader(
            store=store,
            data_dir=tmp_path,
            unit_path=tmp_path / "u.service",
            path_unit_path=tmp_path / "u.path",
            stall_timeout_s=999,
            overall_timeout_s=999,
        )

        req = await upgrader.start(target_tag="v1.2.1", actor="ops")
        # Recover the helper-form request_id from the request file
        # (NativeUpgrader writes the dashed form for jq matching).
        request_payload = json.loads(
            (tmp_path / nu.REQUEST_FILE_NAME).read_text(encoding="utf-8")
        )
        helper_rid = request_payload["request_id"]

        # Helper writes "running"
        _write_status_file(
            tmp_path,
            {
                "request_id": helper_rid,
                "state": "running",
                "started_at": req.requested_at,
                "finished_at": None,
                "log_excerpt": "",
                "error": None,
            },
        )
        await _wait_for_state(store, req.request_id, "running", timeout=2.0)

        # Helper writes "succeeded" — poller should observe + exit.
        _write_status_file(
            tmp_path,
            {
                "request_id": helper_rid,
                "state": "succeeded",
                "started_at": req.requested_at,
                "finished_at": req.requested_at + 1000,
                "log_excerpt": "ok\n",
                "error": None,
            },
        )
        await _wait_for_state(
            store, req.request_id, "succeeded", timeout=2.0
        )

        # Give the background task a tick to wind down.
        for _ in range(40):
            if not upgrader._background_tasks:
                break
            await asyncio.sleep(0.05)
        assert not upgrader._background_tasks, (
            "poller task did not deregister after terminal state"
        )

        # log_excerpt was mirrored too.
        final = await store.get(req.request_id)
        assert final is not None
        assert final.log_excerpt == "ok\n"
    finally:
        nu._STATUS_POLL_INTERVAL_S = original


@pytest.mark.asyncio
async def test_poller_marks_stalled_when_no_status_arrives(
    tmp_path: Path, store: UpgradeStateStore
) -> None:
    original = nu._STATUS_POLL_INTERVAL_S
    nu._STATUS_POLL_INTERVAL_S = 0.02
    try:
        # Use a real-ish clock that returns elapsed monotonic *minus* an
        # offset we can grow. This guarantees the poller observes
        # ``now > first_seen_at`` even though we haven't actually waited
        # the stall window — the test stays deterministic.
        base = time.monotonic()
        fake_offset = [0.0]  # add to (monotonic-base) to fake elapsed time

        def clock() -> float:
            return (time.monotonic() - base) + fake_offset[0]

        upgrader = nu.NativeUpgrader(
            store=store,
            data_dir=tmp_path,
            unit_path=tmp_path / "u.service",
            path_unit_path=tmp_path / "u.path",
            stall_timeout_s=0.05,
            overall_timeout_s=999,
            clock=clock,
        )

        req = await upgrader.start(target_tag="v1.2.1", actor="ops")
        # Yield once so the background task runs, captures
        # first_seen_at, and the first poll tick lands. Then jump the
        # offset past the stall window.
        await asyncio.sleep(0.05)
        fake_offset[0] += 5.0

        await _wait_for_state(store, req.request_id, "stalled", timeout=2.0)
    finally:
        nu._STATUS_POLL_INTERVAL_S = original


# ---------------------------------------------------------------------------
# progress() async iterator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_yields_snapshots_and_terminates(
    tmp_path: Path, store: UpgradeStateStore
) -> None:
    original_status = nu._STATUS_POLL_INTERVAL_S
    original_progress = nu._PROGRESS_POLL_SECONDS
    nu._STATUS_POLL_INTERVAL_S = 0.02
    nu._PROGRESS_POLL_SECONDS = 0.02
    try:
        upgrader = nu.NativeUpgrader(
            store=store,
            data_dir=tmp_path,
            unit_path=tmp_path / "u.service",
            path_unit_path=tmp_path / "u.path",
            stall_timeout_s=999,
            overall_timeout_s=5,
        )

        req = await upgrader.start(target_tag="v1.2.1", actor="ops")
        helper_rid = str(uuid.UUID(req.request_id))

        async def feeder() -> None:
            await asyncio.sleep(0.05)
            _write_status_file(
                tmp_path,
                {
                    "request_id": helper_rid,
                    "state": "running",
                    "started_at": req.requested_at,
                    "finished_at": None,
                    "log_excerpt": "",
                    "error": None,
                },
            )
            await asyncio.sleep(0.1)
            _write_status_file(
                tmp_path,
                {
                    "request_id": helper_rid,
                    "state": "succeeded",
                    "started_at": req.requested_at,
                    "finished_at": req.requested_at + 1000,
                    "log_excerpt": "ok",
                    "error": None,
                },
            )

        collected: list[UpgradeStatus] = []

        async def consume() -> None:
            async for snap in upgrader.progress(req.request_id):
                collected.append(snap)

        await asyncio.gather(consume(), feeder())

        states = [s.state for s in collected]
        # We saw at least the terminal state.
        assert "succeeded" in states, f"missing terminal in {states}"
        # progress() stops on the first terminal.
        assert collected[-1].state == "succeeded"
    finally:
        nu._STATUS_POLL_INTERVAL_S = original_status
        nu._PROGRESS_POLL_SECONDS = original_progress


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


async def _wait_for_state(
    store: UpgradeStateStore,
    request_id: str,
    state: str,
    *,
    timeout: float,
) -> None:
    """Poll the store until a status hits ``state`` or the timeout fires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = await store.get(request_id)
        if snap is not None and snap.state == state:
            return
        await asyncio.sleep(0.02)
    snap = await store.get(request_id)
    actual = snap.state if snap is not None else "<missing>"
    raise AssertionError(
        f"state did not reach {state!r} within {timeout}s (last: {actual!r})"
    )
