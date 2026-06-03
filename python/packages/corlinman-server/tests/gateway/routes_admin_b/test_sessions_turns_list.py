"""Tests for ``GET /admin/sessions/{key}/turns`` (W1.2 UI past-turns).

The session-detail page renders a pill-row of recent turns; this route
feeds it. We exercise:

* Newest-first ordering — three turns return in started_at_ms DESC.
* Empty session → empty list, no error.
* Cursor pagination via ``before_turn_id`` — 5 turns with limit=2 walk
  through correctly without offset drift.
* FastAPI ``Query`` validator clamps ``limit`` to ``[1, 200]``: an
  over-cap request is rejected at the validator layer with 422 (the
  documented FastAPI contract for ``Query(le=...)``).
* Long ``user_text`` truncates at 200 characters with an ellipsis.
* Legacy turns with NULL cost columns surface as ``None`` (not 0.0).
* Admin auth — the route mounts behind ``require_admin``; a request
  without credentials returns 401.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from corlinman_server.agent_journal import AgentJournal
from corlinman_server.gateway.routes_admin_b.infra import sessions_events
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    require_admin,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

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
    """FastAPI app mounting ``sessions_events.router()`` with admin auth
    bypassed via dependency override — the auth path is exercised in a
    dedicated test below with the real guard installed."""
    state = AdminState(data_dir=tmp_path, journal=journal)
    set_admin_state(state)
    try:
        application = FastAPI()
        application.include_router(sessions_events.router())
        application.dependency_overrides[require_admin] = lambda: None
        yield application
    finally:
        set_admin_state(None)


async def _seed_turn(
    journal: AgentJournal,
    *,
    session_key: str,
    user_text: str,
    completed: bool = True,
    estimated_cost_usd: float | None = None,
    cost_status: str | None = None,
) -> int:
    """Open + (optionally) complete a turn; return its turn_id."""
    tid = await journal.begin_turn(session_key, user_text)
    assert tid is not None
    if completed:
        await journal.complete_turn(tid)
    if estimated_cost_usd is not None or cost_status is not None:
        await journal.update_turn_cost(
            tid,
            estimated_cost_usd=estimated_cost_usd,
            cost_status=cost_status,
        )
    return tid


# ---------------------------------------------------------------------------
# Happy path — ordering
# ---------------------------------------------------------------------------


async def test_list_returns_turns_for_session_descending(
    app: FastAPI,
    journal: AgentJournal,
) -> None:
    """Three turns return in started_at_ms DESC (newest first)."""
    tid_a = await _seed_turn(
        journal, session_key="sess-1", user_text="first"
    )
    tid_b = await _seed_turn(
        journal, session_key="sess-1", user_text="second"
    )
    tid_c = await _seed_turn(
        journal, session_key="sess-1", user_text="third"
    )

    client = TestClient(app)
    resp = client.get("/admin/sessions/sess-1/turns")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_key"] == "sess-1"
    assert len(body["turns"]) == 3
    # Newest first — the last seeded turn leads the list.
    ids = [t["turn_id"] for t in body["turns"]]
    assert ids == [str(tid_c), str(tid_b), str(tid_a)]
    # Preview round-trips user_text verbatim when shorter than the cap.
    previews = [t["user_text_preview"] for t in body["turns"]]
    assert previews == ["third", "second", "first"]
    # Status / aggregate columns populate from ``complete_turn``.
    assert all(t["status"] == "completed" for t in body["turns"])
    assert all(t["ended_at_ms"] is not None for t in body["turns"])
    # Short page → no next_cursor (end of listing).
    assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# Empty session
# ---------------------------------------------------------------------------


async def test_list_empty_session_returns_empty(
    app: FastAPI,
) -> None:
    """Unknown session_key → empty turns list, 200 (not 404)."""
    client = TestClient(app)
    resp = client.get("/admin/sessions/sess-empty/turns")
    assert resp.status_code == 200
    body = resp.json()
    assert body["turns"] == []
    assert body["next_cursor"] is None
    assert body["session_key"] == "sess-empty"


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


async def test_list_pagination_with_before_cursor(
    app: FastAPI,
    journal: AgentJournal,
) -> None:
    """5 turns, limit=2 → cursor walks through every page correctly."""
    tids: list[int] = []
    for i in range(5):
        tids.append(
            await _seed_turn(
                journal, session_key="sess-p", user_text=f"msg-{i}"
            )
        )
    # Newest-first iteration order over ``tids``:
    expected_order = list(reversed(tids))

    client = TestClient(app)
    seen: list[str] = []
    cursor: str | None = None
    for _page in range(5):  # plenty of headroom; we expect ~3 pages.
        params: dict[str, str] = {"limit": "2"}
        if cursor is not None:
            params["before_turn_id"] = cursor
        resp = client.get("/admin/sessions/sess-p/turns", params=params)
        assert resp.status_code == 200
        body = resp.json()
        seen.extend(t["turn_id"] for t in body["turns"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    # Every turn visited exactly once, newest-first.
    assert seen == [str(t) for t in expected_order]
    # Final page didn't fill → next_cursor cleared.
    assert cursor is None


# ---------------------------------------------------------------------------
# Limit clamp via Query validator
# ---------------------------------------------------------------------------


async def test_list_limit_clamp(
    app: FastAPI,
    journal: AgentJournal,
) -> None:
    """``limit=1000`` is rejected by the FastAPI ``Query(le=200)``
    validator with 422 — the documented clamp contract."""
    await _seed_turn(journal, session_key="sess-clamp", user_text="x")
    client = TestClient(app)
    resp = client.get("/admin/sessions/sess-clamp/turns?limit=1000")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # FastAPI nests the validation error under ``detail[0]``.
    assert any(
        "less_than_equal" in str(item.get("type", "")) or "200" in str(item)
        for item in detail
    )

    # And confirm the boundary (the max) is accepted.
    resp_ok = client.get(
        f"/admin/sessions/sess-clamp/turns?limit={sessions_events.TURNS_LIST_MAX_LIMIT}"
    )
    assert resp_ok.status_code == 200


# ---------------------------------------------------------------------------
# user_text truncation
# ---------------------------------------------------------------------------


async def test_list_user_text_truncated_at_200(
    app: FastAPI,
    journal: AgentJournal,
) -> None:
    """A 500-char user_text → 200 chars + ellipsis in the preview."""
    long_text = "a" * 500
    await _seed_turn(
        journal, session_key="sess-long", user_text=long_text
    )
    client = TestClient(app)
    resp = client.get("/admin/sessions/sess-long/turns")
    assert resp.status_code == 200
    preview = resp.json()["turns"][0]["user_text_preview"]
    assert preview is not None
    # 200 chars of the original payload + one trailing ellipsis char.
    assert preview.startswith("a" * 200)
    assert preview.endswith("…")
    assert len(preview) == 201


# ---------------------------------------------------------------------------
# Nullable cost fields pass through
# ---------------------------------------------------------------------------


async def test_list_nullable_cost_fields_pass_through(
    app: FastAPI,
    journal: AgentJournal,
) -> None:
    """Legacy turn with NULL ``estimated_cost_usd`` / ``cost_status``
    surfaces as ``None`` on the wire — not coerced to ``0.0`` /
    ``"unknown"``. The UI distinguishes the two cases."""
    await _seed_turn(
        journal,
        session_key="sess-nul",
        user_text="legacy",
        estimated_cost_usd=None,
        cost_status=None,
    )
    client = TestClient(app)
    resp = client.get("/admin/sessions/sess-nul/turns")
    assert resp.status_code == 200
    turn = resp.json()["turns"][0]
    assert turn["estimated_cost_usd"] is None
    assert turn["cost_status"] is None
    # finish_reason is not yet projected onto the turns table — see the
    # backend docstring. We surface ``None`` so the UI renders an "—".
    assert turn["finish_reason"] is None


# ---------------------------------------------------------------------------
# Auth — real require_admin guard installed.
# ---------------------------------------------------------------------------


def test_list_auth_required(tmp_path: Path) -> None:
    """Without admin credentials → 401."""
    state = AdminState(data_dir=tmp_path)
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        application = FastAPI()
        application.include_router(sessions_events.router())
        # No auth header on this client.
        unauthed = TestClient(application)
        resp = unauthed.get("/admin/sessions/sess-x/turns")
        assert resp.status_code == 401

        # And the authenticated client gets through — but trips on the
        # disabled-journal envelope since the AdminState above doesn't
        # wire a journal. 503 is the documented degradation shape,
        # which proves the auth gate passed.
        authed = authenticated_test_client(application)
        resp2 = authed.get("/admin/sessions/sess-x/turns")
        assert resp2.status_code == 503
        assert resp2.json()["detail"]["error"] == "observability_disabled"
    finally:
        set_admin_state(None)
