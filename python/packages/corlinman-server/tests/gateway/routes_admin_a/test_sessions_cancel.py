"""Tests for ``POST /admin/sessions/{key}/cancel``.

In-app chat MVP — the admin route surfaces a way to interrupt the
in-progress :class:`~corlinman_agent.reasoning_loop.ReasoningLoop` for a
given session_key. The route consults the process-level registry that
mirrors the servicer's instance-level ``_active_loops`` map, so the
tests stub a fake loop directly into the registry rather than spinning
up the full gRPC servicer.

Coverage matrix:

* ``cancelled``   — a live loop is registered + ``cancel()`` fires.
* ``not_running`` — session_key exists in the journal but has no live
                    loop (the common "user clicked stop after the model
                    already finished" race).
* 404             — session_key has neither journal rows nor a live
                    loop; the operator typoed the key.
* 503 disabled    — ``sessions_disabled`` gate still applies.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_server import agent_servicer
from corlinman_server.agent_journal import AgentJournal
from corlinman_server.gateway.routes_admin_a import (
    AdminState,
    build_router,
    set_admin_state,
)
from corlinman_server.gateway.routes_admin_a._session_store import (
    AdminSessionStore,
)
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _basic_auth_header(
    username: str = "admin", password: str = "rootroot"
) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


class _FakeReasoningLoop:
    """Stand-in for :class:`~corlinman_agent.reasoning_loop.ReasoningLoop`.

    The real loop's ``cancel`` is sync and pokes an internal event +
    schedules a ``Cancelling`` emit; here we just record the call so
    the test can assert the cancel happened. ``_turn_id`` mirrors the
    real attribute the cancel registry reads back for the response.
    """

    def __init__(self, turn_id: int | None = 12345) -> None:
        self._turn_id = turn_id
        self.cancel_called_with: str | None = None

    def cancel(self, reason: str = "user_abort") -> None:
        self.cancel_called_with = reason


def _seed_journal(data_dir: Path, sessions: dict[str, list[str]]) -> None:
    """Pre-populate ``<data_dir>/agent_journal.sqlite`` so the existence
    probe in the 404-vs-not_running branch resolves correctly."""

    async def _run() -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        j = await AgentJournal.open(data_dir / "agent_journal.sqlite")
        try:
            for session_key, msgs in sessions.items():
                for text in msgs:
                    tid = await j.begin_turn(session_key, text)
                    await j.append_message(tid, "user", text)
                    await asyncio.sleep(0.001)
        finally:
            await j.close()

    asyncio.run(_run())


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
    )
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    # Make sure the registry starts empty — a previous test that
    # registered a loop must not bleed into the next one.
    agent_servicer._ACTIVE_LOOPS_BY_SESSION.clear()
    with TestClient(
        app, headers={"Authorization": _basic_auth_header()}
    ) as c:
        yield c
    set_admin_state(None)
    agent_servicer._ACTIVE_LOOPS_BY_SESSION.clear()


# ---------------------------------------------------------------------------
# Happy path: an active loop → cancelled.
# ---------------------------------------------------------------------------


def test_cancel_active_session_invokes_cancel(
    client: TestClient,
) -> None:
    """A registered loop gets ``cancel()`` called and the route returns
    ``status=cancelled`` plus the turn_id so the UI can correlate with
    the live SSE stream."""
    loop = _FakeReasoningLoop(turn_id=98765)
    agent_servicer._ACTIVE_LOOPS_BY_SESSION["sess-live"] = loop

    resp = client.post("/admin/sessions/sess-live/cancel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["turn_id"] == "98765"
    assert loop.cancel_called_with == "admin_abort"


def test_cancel_active_session_without_turn_id(
    client: TestClient,
) -> None:
    """A loop that hasn't pinned a turn_id yet still cancels — we return
    ``turn_id=None`` so the client doesn't try to correlate."""
    loop = _FakeReasoningLoop(turn_id=None)
    agent_servicer._ACTIVE_LOOPS_BY_SESSION["sess-early"] = loop

    resp = client.post("/admin/sessions/sess-early/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"status": "cancelled", "turn_id": None}


# ---------------------------------------------------------------------------
# not_running — session is journaled but no loop is live.
# ---------------------------------------------------------------------------


def test_cancel_session_with_no_active_loop_returns_not_running(
    client: TestClient, tmp_path: Path
) -> None:
    """The session exists in the journal but no in-progress loop is
    registered (e.g. the model already finished). Route returns 200
    not_running rather than 404 because the key is real."""
    _seed_journal(tmp_path, {"sess-idle": ["hello"]})

    resp = client.post("/admin/sessions/sess-idle/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not_running", "turn_id": None}


# ---------------------------------------------------------------------------
# 404 — unknown session_key.
# ---------------------------------------------------------------------------


def test_cancel_unknown_session_returns_404(
    client: TestClient, tmp_path: Path
) -> None:
    """No journal rows + no live loop → the session never existed.
    Surfaces as 404 ``not_found`` so a typoed key fails loudly."""
    # Seed a different session so the journal file exists; the cancel
    # target still has no rows.
    _seed_journal(tmp_path, {"sess-other": ["unrelated"]})

    resp = client.post("/admin/sessions/never-existed/cancel")
    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"]["error"] == "not_found"
    assert body["detail"]["session_key"] == "never-existed"


def test_cancel_unknown_session_when_journal_absent_returns_404(
    client: TestClient,
) -> None:
    """No journal file at all + no loop → 404 still fires (the route
    treats journal-absent as 'session never existed' for the cancel
    path, not 503)."""
    resp = client.post("/admin/sessions/anything/cancel")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


# ---------------------------------------------------------------------------
# sessions_disabled gate.
# ---------------------------------------------------------------------------


def test_cancel_503_when_sessions_disabled(tmp_path: Path) -> None:
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        sessions_disabled=True,
    )
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    agent_servicer._ACTIVE_LOOPS_BY_SESSION.clear()
    try:
        with TestClient(
            app, headers={"Authorization": _basic_auth_header()}
        ) as c:
            resp = c.post("/admin/sessions/anything/cancel")
            assert resp.status_code == 503
            assert resp.json()["detail"]["error"] == "sessions_disabled"
    finally:
        set_admin_state(None)
        agent_servicer._ACTIVE_LOOPS_BY_SESSION.clear()


# ---------------------------------------------------------------------------
# Servicer-level helper — direct unit coverage of cancel_session.
# ---------------------------------------------------------------------------


def test_cancel_session_helper_returns_not_running_for_empty_key() -> None:
    """Empty session_key is never registered — the helper short-circuits
    to ``not_running`` without touching the registry."""
    agent_servicer._ACTIVE_LOOPS_BY_SESSION.clear()
    status, turn_id = agent_servicer.cancel_session("")
    assert status == "not_running"
    assert turn_id is None


def test_register_then_cancel_then_unregister_roundtrip() -> None:
    """Register a loop → cancel finds it → unregister clears it →
    second cancel sees ``not_running``."""
    agent_servicer._ACTIVE_LOOPS_BY_SESSION.clear()
    loop = _FakeReasoningLoop(turn_id=42)
    agent_servicer._register_active_loop("sess-x", loop)
    status, tid = agent_servicer.cancel_session("sess-x")
    assert status == "cancelled"
    assert tid == "42"
    assert loop.cancel_called_with == "admin_abort"

    agent_servicer._unregister_active_loop("sess-x", loop)
    status2, _ = agent_servicer.cancel_session("sess-x")
    assert status2 == "not_running"
