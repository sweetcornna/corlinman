"""Tests for ``/admin/sessions/{key}/cost`` (W2.3 cost aggregate).

Covers:

* Three completed turns with the W1.2 cost columns populated → totals
  add up; ``cost_status_breakdown`` reflects the per-status counts.
* A pre-W1.2 turn (``cost_status`` IS NULL) with a ``TurnComplete``
  event in ``turn_events`` → fallback path coefficient-scans the usage
  and adds it to the total.
* Empty session → typed zero envelope (turn_count=0, cost=0.0, etc.).
* 503 when no journal is wired.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from corlinman_agent.events import EventEnvelope, TurnComplete
from corlinman_server.agent_journal import AgentJournal
from corlinman_server.gateway.routes_admin_b import sessions_cost
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    require_admin,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def journal(tmp_path: Path) -> AsyncIterator[AgentJournal]:
    j = await AgentJournal.open(tmp_path / "journal.sqlite")
    try:
        yield j
    finally:
        await j.close()


@pytest_asyncio.fixture
async def app(tmp_path: Path, journal: AgentJournal) -> AsyncIterator[FastAPI]:
    state = AdminState(data_dir=tmp_path, journal=journal)
    set_admin_state(state)
    try:
        application = FastAPI()
        application.include_router(sessions_cost.router())
        application.dependency_overrides[require_admin] = lambda: None
        yield application
    finally:
        set_admin_state(None)


async def _seed_completed_turn(
    journal: AgentJournal,
    *,
    session_key: str,
    user_text: str,
    estimated_cost_usd: float | None,
    cost_status: str | None,
    elapsed_ms: int | None = None,
    tool_call_count: int = 0,
) -> int:
    """Helper: open + complete a turn, then populate the W1.2 columns."""
    tid = await journal.begin_turn(session_key, user_text)
    assert tid is not None
    await journal.complete_turn(tid)
    if estimated_cost_usd is not None or cost_status is not None:
        await journal.update_turn_cost(
            tid,
            estimated_cost_usd=estimated_cost_usd,
            cost_status=cost_status,
        )
    # Direct SQL: ``complete_turn`` already computes ``elapsed_ms`` from
    # start/end timestamps, but tests run too fast for a useful value —
    # patch it in to whatever the test wants. ``tool_call_count`` same
    # story (it's recomputed from ``turn_messages``, of which we have
    # none in these tests).
    conn = journal.backend._c  # type: ignore[attr-defined]
    sets: list[str] = []
    params: list[object] = []
    if elapsed_ms is not None:
        sets.append("elapsed_ms = ?")
        params.append(int(elapsed_ms))
    if tool_call_count:
        sets.append("tool_call_count = ?")
        params.append(int(tool_call_count))
    if sets:
        params.append(tid)
        await conn.execute(
            f"UPDATE turns SET {', '.join(sets)} WHERE turn_id = ?",
            params,
        )
        await conn.commit()
    return tid


# ---------------------------------------------------------------------------
# Happy path — three completed turns with cost columns populated
# ---------------------------------------------------------------------------


async def test_cost_aggregate_sums_completed_turns(
    app: FastAPI,
    journal: AgentJournal,
) -> None:
    """Three completed turns aggregate correctly."""
    await _seed_completed_turn(
        journal,
        session_key="sess-A",
        user_text="msg-1",
        estimated_cost_usd=0.02,
        cost_status="estimated",
        elapsed_ms=1000,
        tool_call_count=2,
    )
    await _seed_completed_turn(
        journal,
        session_key="sess-A",
        user_text="msg-2",
        estimated_cost_usd=0.03,
        cost_status="billed",
        elapsed_ms=2000,
        tool_call_count=1,
    )
    await _seed_completed_turn(
        journal,
        session_key="sess-A",
        user_text="msg-3",
        estimated_cost_usd=0.04,
        cost_status="estimated",
        elapsed_ms=3000,
        tool_call_count=4,
    )

    client = TestClient(app)
    resp = client.get("/admin/sessions/sess-A/cost")
    assert resp.status_code == 200
    body = resp.json()

    assert body["session_key"] == "sess-A"
    assert body["turn_count"] == 3
    assert body["total_elapsed_ms"] == 6000
    assert body["total_tool_calls"] == 7
    assert abs(body["total_cost_usd"] - 0.09) < 1e-6
    assert body["cost_status_breakdown"]["estimated"] == 2
    assert body["cost_status_breakdown"]["billed"] == 1
    assert body["cost_status_breakdown"]["unknown"] == 0
    assert body["avg_turn_ms"] == 2000


# ---------------------------------------------------------------------------
# Pre-W1.2 fallback — cost recovered from TurnComplete event payload
# ---------------------------------------------------------------------------


async def test_cost_fallback_reads_turn_complete_payload(
    app: FastAPI,
    journal: AgentJournal,
) -> None:
    """A completed turn whose ``cost_status`` IS NULL is back-filled by
    scanning ``turn_events`` for a ``TurnComplete`` payload."""
    tid = await _seed_completed_turn(
        journal,
        session_key="sess-old",
        user_text="legacy",
        estimated_cost_usd=None,
        cost_status=None,
        elapsed_ms=500,
    )
    # Drop a TurnComplete event with an embedded estimated_cost_usd.
    env = EventEnvelope(
        turn_id=str(tid),
        session_key="sess-old",
        sequence=0,
        timestamp_ms=1_700_000_000_000,
        event=TurnComplete(
            finish_reason="stop",
            usage={"input_tokens": 4000, "output_tokens": 1000},
            elapsed_ms=500,
            estimated_cost_usd=0.05,
            cost_status="estimated",
        ),
    )
    await journal.append_event(env)

    client = TestClient(app)
    resp = client.get("/admin/sessions/sess-old/cost")
    assert resp.status_code == 200
    body = resp.json()

    assert body["turn_count"] == 1
    # The 0.05 came from the TurnComplete event, not the turns table
    # (where cost_status IS NULL and estimated_cost_usd was never set).
    assert abs(body["total_cost_usd"] - 0.05) < 1e-6
    assert body["cost_status_breakdown"]["unknown"] == 1


async def test_cost_fallback_coefficients_usage_without_embedded_cost(
    app: FastAPI,
    journal: AgentJournal,
) -> None:
    """When the TurnComplete payload has only ``usage`` (no
    ``estimated_cost_usd``) the fallback coefficient-scans tokens with
    the deliberately-low Haiku-tier rates."""
    tid = await _seed_completed_turn(
        journal,
        session_key="sess-tokens",
        user_text="legacy-2",
        estimated_cost_usd=None,
        cost_status=None,
    )
    env = EventEnvelope(
        turn_id=str(tid),
        session_key="sess-tokens",
        sequence=0,
        timestamp_ms=1_700_000_000_000,
        event=TurnComplete(
            finish_reason="stop",
            usage={"input_tokens": 1000, "output_tokens": 1000},
            elapsed_ms=100,
        ),
    )
    await journal.append_event(env)

    client = TestClient(app)
    resp = client.get("/admin/sessions/sess-tokens/cost")
    assert resp.status_code == 200
    body = resp.json()

    # 1000 input * $0.00025/1k + 1000 output * $0.00125/1k = $0.0015
    expected = 0.00025 + 0.00125
    assert abs(body["total_cost_usd"] - expected) < 1e-6


# ---------------------------------------------------------------------------
# Empty session
# ---------------------------------------------------------------------------


async def test_cost_aggregate_empty_session_returns_zeros(
    app: FastAPI,
) -> None:
    client = TestClient(app)
    resp = client.get("/admin/sessions/sess-empty/cost")
    assert resp.status_code == 200
    body = resp.json()
    assert body["turn_count"] == 0
    assert body["total_cost_usd"] == 0.0
    assert body["total_elapsed_ms"] == 0
    assert body["total_tool_calls"] == 0
    assert body["last_turn_at_ms"] is None
    assert body["avg_turn_ms"] == 0


# ---------------------------------------------------------------------------
# Disabled
# ---------------------------------------------------------------------------


async def test_503_when_journal_disabled(tmp_path: Path) -> None:
    state = AdminState(data_dir=tmp_path)  # journal=None
    set_admin_state(state)
    try:
        application = FastAPI()
        application.include_router(sessions_cost.router())
        application.dependency_overrides[require_admin] = lambda: None
        client = TestClient(application)
        resp = client.get("/admin/sessions/sess/cost")
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "observability_disabled"
    finally:
        set_admin_state(None)
