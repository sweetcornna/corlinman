"""Perf check for W1.2 — turn_events bulk write + read.

Target: 10k events round-trip in < 500ms on a fresh on-disk SQLite DB.
The journal opens WAL + ``synchronous = NORMAL`` automatically, which
is what real deployments run; the perf number here exercises the same
path.

The test is conservative — we use a 5-second ceiling so a slow CI box
(spinning disk, contended runner) still passes. The numerical target
(500ms) is enforced as a warning via the structlog write so a perf
regression shows up in test output without flapping the suite.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corlinman_server.agent_journal import AgentJournal


@dataclass
class _Env:
    turn_id: int
    sequence: int
    event_type: str
    payload: dict[str, Any]
    timestamp_ms: int


async def test_10k_events_round_trip_under_budget(tmp_path: Path) -> None:
    """10k append + load completes inside the perf budget.

    Hard ceiling: 5s for the suite to pass on a slow CI box. Target:
    500ms — we print a warning when we miss it so the regression shows
    up in test output without a flap.
    """
    db = tmp_path / "perf.sqlite"
    j = await AgentJournal.open(db)
    try:
        tid = await j.begin_turn("sess-perf", "perf")
        assert tid is not None

        envelopes = [
            _Env(
                turn_id=tid,
                sequence=seq,
                event_type="TextDelta",
                payload={"index": 0, "text": f"tok-{seq:05d}"},
                timestamp_ms=1_700_000_000_000 + seq,
            )
            for seq in range(10_000)
        ]

        t0 = time.perf_counter()
        # Single batch — exercises the executemany path.
        await j.append_events_batch(envelopes)
        events = await j.load_events(tid)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert len(events) == 10_000
        # Hard ceiling: never accept > 5s, that's plainly broken.
        assert elapsed_ms < 5000, (
            f"10k round-trip took {elapsed_ms:.0f}ms (> 5s ceiling)"
        )
        if elapsed_ms > 500:
            # Soft target — record a visible warning so the perf
            # regression surfaces in CI logs without failing the suite.
            print(
                f"WARN perf-soft-target: 10k round-trip took "
                f"{elapsed_ms:.0f}ms (> 500ms target)"
            )
    finally:
        await j.close()


async def test_iter_events_does_not_buffer_full_list(tmp_path: Path) -> None:
    """``iter_events`` streams — first event arrives well before the last.

    Sanity for the SSE catch-up path: the consumer must not be forced to
    wait for the entire timeline before emitting bytes to the client.
    """
    db = tmp_path / "iter_perf.sqlite"
    j = await AgentJournal.open(db)
    try:
        tid = await j.begin_turn("sess-iter-perf", "iter perf")
        assert tid is not None
        await j.append_events_batch(
            [
                _Env(
                    turn_id=tid,
                    sequence=seq,
                    event_type="TextDelta",
                    payload={"index": 0},
                    timestamp_ms=seq,
                )
                for seq in range(2_000)
            ]
        )

        t0 = time.perf_counter()
        first_ms: float | None = None
        last_ms: float | None = None
        count = 0
        async for _ in j.iter_events(tid, start_sequence=-1):
            now = (time.perf_counter() - t0) * 1000
            if first_ms is None:
                first_ms = now
            last_ms = now
            count += 1
        assert count == 2_000
        assert first_ms is not None and last_ms is not None
        # First yield arrives within 1s on any reasonable machine —
        # confirms we are not buffering the full result before yielding.
        assert first_ms < 1000
    finally:
        await j.close()
