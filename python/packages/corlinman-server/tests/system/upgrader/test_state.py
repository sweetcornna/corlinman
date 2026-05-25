"""Tests for :class:`corlinman_server.system.upgrader.UpgradeStateStore`.

Covers the W1.1 contract: round-trip get, partial updates, in-flight
discovery, log rolling at 4 kB, persistence across instances.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from corlinman_server.system.upgrader.state import (
    UpgradeRequest,
    UpgradeStateStore,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_req(
    request_id: str = "req-1",
    tag: str = "v1.2.0",
    mode: str = "docker",
) -> UpgradeRequest:
    return UpgradeRequest(
        request_id=request_id,
        tag=tag,
        requested_at=_now_ms(),
        requested_by="alice",
        mode=mode,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_begin_then_get_roundtrips(tmp_path: Path) -> None:
    store = UpgradeStateStore(tmp_path / ".upgrade-state.json")
    req = _make_req()
    seeded = await store.begin(req)

    fetched = await store.get(req.request_id)

    assert fetched is not None
    assert fetched.request_id == req.request_id
    assert fetched.tag == "v1.2.0"
    assert fetched.state == "queued"
    assert fetched.phase == "queued"
    assert fetched.log_excerpt == ""
    # Returned snapshot is a copy — mutating it MUST NOT bleed back.
    seeded.state = "succeeded"  # type: ignore[misc]
    fetched_again = await store.get(req.request_id)
    assert fetched_again is not None
    assert fetched_again.state == "queued"


@pytest.mark.asyncio
async def test_update_partial_preserves_other_fields(tmp_path: Path) -> None:
    store = UpgradeStateStore(tmp_path / ".upgrade-state.json")
    req = _make_req()
    await store.begin(req)

    await store.update(req.request_id, state="running", phase="pulling")
    after = await store.get(req.request_id)
    assert after is not None
    assert after.state == "running"
    assert after.phase == "pulling"
    # tag / request_id were not touched
    assert after.tag == "v1.2.0"

    # Now flip to terminal, preserving started_at
    await store.update(req.request_id, started_at=12345)
    await store.update(
        req.request_id, state="succeeded", phase="done", finished_at=67890
    )
    terminal = await store.get(req.request_id)
    assert terminal is not None
    assert terminal.state == "succeeded"
    assert terminal.phase == "done"
    assert terminal.started_at == 12345
    assert terminal.finished_at == 67890


@pytest.mark.asyncio
async def test_current_in_flight_returns_queued_or_running(
    tmp_path: Path,
) -> None:
    store = UpgradeStateStore(tmp_path / ".upgrade-state.json")
    req = _make_req(request_id="req-A")
    await store.begin(req)

    in_flight = await store.current_in_flight()
    assert in_flight is not None
    assert in_flight.request_id == "req-A"

    await store.update("req-A", state="running")
    in_flight2 = await store.current_in_flight()
    assert in_flight2 is not None
    assert in_flight2.state == "running"


@pytest.mark.asyncio
async def test_current_in_flight_returns_none_after_terminal(
    tmp_path: Path,
) -> None:
    store = UpgradeStateStore(tmp_path / ".upgrade-state.json")
    req = _make_req(request_id="req-B")
    await store.begin(req)
    await store.update("req-B", state="succeeded", phase="done")

    assert await store.current_in_flight() is None

    # Failed counts as terminal too.
    req2 = _make_req(request_id="req-C", tag="v1.3.0")
    await store.begin(req2)
    await store.update("req-C", state="failed", phase="image_pull_failed")
    assert await store.current_in_flight() is None


@pytest.mark.asyncio
async def test_append_log_rolls_at_4kb(tmp_path: Path) -> None:
    store = UpgradeStateStore(tmp_path / ".upgrade-state.json")
    req = _make_req()
    await store.begin(req)

    # Push 8 kB of input through; expect ~4 kB retained.
    chunk = "x" * 1024
    for _ in range(8):
        await store.append_log(req.request_id, chunk)

    status = await store.get(req.request_id)
    assert status is not None
    assert len(status.log_excerpt.encode("utf-8")) <= 4 * 1024
    # And the tail is preserved (last bytes should be 'x' since all input
    # was the same character).
    assert status.log_excerpt.endswith("x")


@pytest.mark.asyncio
async def test_persistence_round_trip_across_instances(tmp_path: Path) -> None:
    """Write a state, instantiate a fresh store at the same path,
    confirm we recover the exact same status. This is the audit-trail
    guarantee."""
    persist = tmp_path / ".upgrade-state.json"
    store1 = UpgradeStateStore(persist)
    req = _make_req(request_id="req-PERSIST", tag="v9.9.9")
    await store1.begin(req)
    await store1.update(
        "req-PERSIST",
        state="running",
        phase="pulling",
        started_at=4242,
    )
    await store1.append_log("req-PERSIST", "hello world\n")

    # Brand-new store reading the same file.
    store2 = UpgradeStateStore(persist)
    recovered = await store2.get("req-PERSIST")
    assert recovered is not None
    assert recovered.tag == "v9.9.9"
    assert recovered.state == "running"
    assert recovered.phase == "pulling"
    assert recovered.started_at == 4242
    assert recovered.log_excerpt == "hello world\n"


@pytest.mark.asyncio
async def test_update_unknown_request_raises(tmp_path: Path) -> None:
    store = UpgradeStateStore(tmp_path / ".upgrade-state.json")
    with pytest.raises(KeyError):
        await store.update("does-not-exist", state="running")


@pytest.mark.asyncio
async def test_get_unknown_request_returns_none(tmp_path: Path) -> None:
    store = UpgradeStateStore(tmp_path / ".upgrade-state.json")
    assert await store.get("nope") is None


@pytest.mark.asyncio
async def test_append_log_unknown_request_noop(tmp_path: Path) -> None:
    """append_log silently swallows unknown ids — background tasks
    race with cleanup and we don't want to crash."""
    store = UpgradeStateStore(tmp_path / ".upgrade-state.json")
    # Must not raise.
    await store.append_log("ghost", "data")
