"""Tests for the journal-backed ``/admin/sessions*`` admin surface.

The legacy implementation read from a never-populated
``sessions.sqlite`` and exposed no delete route. These tests pin the new
behaviour:

* ``GET /admin/sessions`` prefers the per-turn journal when it has data
  (the path the live ``agent_servicer`` writes to).
* ``DELETE /admin/sessions/{key}`` wipes the session's journal turns +
  messages and returns 204; missing key → 404; journal absent → 503.
* ``DELETE /admin/sessions`` (no key) nukes every session and returns
  ``{"deleted": <n>}``.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

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


def _basic_auth_header(username: str = "admin", password: str = "rootroot") -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _seed_journal(data_dir: Path, sessions: dict[str, list[str]]) -> None:
    """Pre-populate ``<data_dir>/agent_journal.sqlite`` with one turn per
    user message, in the order supplied. Each call closes the journal so
    the route's per-request open hits the same file cleanly."""

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


def _journal_session_keys(data_dir: Path) -> list[str]:
    """Read back the journal's session_keys via the same facade the
    routes use — handy for assertions that survive a route round-trip."""

    async def _run() -> list[str]:
        j = await AgentJournal.open(data_dir / "agent_journal.sqlite")
        try:
            summaries = await j.list_session_summaries()
            return [s.session_key for s in summaries]
        finally:
            await j.close()

    return asyncio.run(_run())


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Fresh app + ``AdminState`` per test. ``data_dir`` is the tmp_path
    itself so the journal lives at ``<tmp_path>/agent_journal.sqlite``
    — same convention as the live servicer."""
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
    )
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
        yield c
    set_admin_state(None)


# ---------------------------------------------------------------------------
# GET /admin/sessions — journal-first
# ---------------------------------------------------------------------------


def test_get_admin_sessions_prefers_journal_when_populated(
    client: TestClient, tmp_path: Path
) -> None:
    """When the journal has rows, the listing must come from the
    journal — not the legacy empty ``sessions.sqlite``."""
    _seed_journal(
        tmp_path,
        {
            "sess-alpha": ["hello alpha"],
            "sess-beta": ["hello beta", "second beta msg"],
        },
    )

    resp = client.get("/admin/sessions")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    keys = {s["session_key"] for s in body["sessions"]}
    assert keys == {"sess-alpha", "sess-beta"}

    by_key = {s["session_key"]: s for s in body["sessions"]}
    assert by_key["sess-beta"]["message_count"] == 2
    assert by_key["sess-alpha"]["message_count"] == 1
    # The new optional preview/status fields are populated for journal
    # rows (the legacy fallback leaves them None).
    assert by_key["sess-alpha"]["last_user_text"] == "hello alpha"
    assert by_key["sess-alpha"]["last_status"] == "in_progress"


def test_get_admin_sessions_falls_back_when_journal_empty(
    client: TestClient,
) -> None:
    """No journal file → legacy listing path runs and returns empty."""
    resp = client.get("/admin/sessions")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sessions": []}


# ---------------------------------------------------------------------------
# DELETE /admin/sessions/{session_key}
# ---------------------------------------------------------------------------


def test_delete_admin_session_removes_journal_rows(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_journal(
        tmp_path,
        {
            "sess-doomed": ["wipe me", "and me"],
            "sess-keep": ["leave me alone"],
        },
    )

    resp = client.delete("/admin/sessions/sess-doomed")
    assert resp.status_code == 204, resp.text

    # The doomed session is gone; the bystander survives.
    remaining = _journal_session_keys(tmp_path)
    assert remaining == ["sess-keep"]


def test_delete_admin_session_unknown_key_returns_404(
    client: TestClient, tmp_path: Path
) -> None:
    # Seed at least one row so the journal file exists and the route
    # reaches the "no turns matched" branch, not the 503.
    _seed_journal(tmp_path, {"sess-real": ["hi"]})

    resp = client.delete("/admin/sessions/never-existed")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "not_found"


def test_delete_admin_session_503_when_journal_absent(
    client: TestClient,
) -> None:
    """No journal file → 503 ``journal_unavailable`` (we can't claim a
    success when the underlying store is missing)."""
    resp = client.delete("/admin/sessions/anything")
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error"] == "journal_unavailable"


# ---------------------------------------------------------------------------
# DELETE /admin/sessions  (clear all)
# ---------------------------------------------------------------------------


def test_clear_all_admin_sessions_route(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_journal(
        tmp_path,
        {
            "sess-a": ["one"],
            "sess-b": ["two", "three"],
            "sess-c": ["four"],
        },
    )

    resp = client.delete("/admin/sessions")
    assert resp.status_code == 200, resp.text
    # 4 turns total (1 + 2 + 1) — every begin_turn we seeded.
    assert resp.json() == {"deleted": 4}

    # Subsequent GET sees nothing.
    listing = client.get("/admin/sessions").json()
    assert listing == {"sessions": []}


def test_clear_all_returns_zero_when_journal_empty_but_present(
    client: TestClient, tmp_path: Path
) -> None:
    """Journal file exists (we touched it), no rows → ``{"deleted": 0}``."""

    # Open + close to create the file without inserting any turns.
    async def _touch() -> None:
        j = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
        await j.close()

    asyncio.run(_touch())

    resp = client.delete("/admin/sessions")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 0}


# ---------------------------------------------------------------------------
# sessions_disabled gate continues to apply to the new routes
# ---------------------------------------------------------------------------


def test_delete_session_503_when_sessions_disabled(
    tmp_path: Path,
) -> None:
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
    try:
        with TestClient(
            app, headers={"Authorization": _basic_auth_header()}
        ) as c:
            resp = c.delete("/admin/sessions/anything")
            assert resp.status_code == 503
            assert resp.json()["detail"]["error"] == "sessions_disabled"

            resp = c.delete("/admin/sessions")
            assert resp.status_code == 503
            assert resp.json()["detail"]["error"] == "sessions_disabled"
    finally:
        set_admin_state(None)
