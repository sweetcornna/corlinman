"""Tests for ``PATCH /admin/sessions/{key}``.

In-app chat MVP — the operator can attach a display ``title``, flip
``pinned`` (sidebar sort), or ``archived`` (UI filter) on any session
that has at least one journaled turn. The metadata lives in a sibling
``session_meta`` table; legacy sessions with no row default to
``(None, False, False)`` and the LEFT JOIN in
``list_session_summaries`` keeps them rendering.

Coverage matrix:

* (a) title-only patch
* (b) pinned-only patch
* (c) archived-only patch
* (d) all three fields at once
* (e) 404 on unknown session_key
* (f) 422 on empty body
* Pinned sort ordering: a pinned session jumps to the top of the
  listing regardless of ``last_seen_at_ms``.
* Repeated PATCH: the next call only overrides the fields it carries.
* ``sessions_disabled`` gate still applies.
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


def _basic_auth_header(
    username: str = "admin", password: str = "rootroot"
) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _seed_journal(data_dir: Path, sessions: dict[str, list[str]]) -> None:
    """Pre-populate ``<data_dir>/agent_journal.sqlite`` with one turn
    per user message — same helper the sibling tests use."""

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
    with TestClient(
        app, headers={"Authorization": _basic_auth_header()}
    ) as c:
        yield c
    set_admin_state(None)


# ---------------------------------------------------------------------------
# (a) title-only
# ---------------------------------------------------------------------------


def test_patch_title_only_persists_and_returns_summary(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_journal(tmp_path, {"sess-a": ["hello"]})

    resp = client.patch("/admin/sessions/sess-a", json={"title": "My chat"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_key"] == "sess-a"
    assert body["title"] == "My chat"
    assert body["pinned"] is False
    assert body["archived"] is False

    # Subsequent GET reflects the title.
    listing = client.get("/admin/sessions").json()["sessions"]
    by_key = {s["session_key"]: s for s in listing}
    assert by_key["sess-a"]["title"] == "My chat"


# ---------------------------------------------------------------------------
# (b) pinned-only
# ---------------------------------------------------------------------------


def test_patch_pinned_only_flips_flag(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_journal(tmp_path, {"sess-b": ["hi"]})

    resp = client.patch("/admin/sessions/sess-b", json={"pinned": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["pinned"] is True
    assert body["title"] is None
    assert body["archived"] is False

    # Toggling back off works too.
    resp2 = client.patch("/admin/sessions/sess-b", json={"pinned": False})
    assert resp2.status_code == 200
    assert resp2.json()["pinned"] is False


# ---------------------------------------------------------------------------
# (c) archived-only
# ---------------------------------------------------------------------------


def test_patch_archived_only_flips_flag(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_journal(tmp_path, {"sess-c": ["yo"]})

    resp = client.patch("/admin/sessions/sess-c", json={"archived": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["archived"] is True
    assert body["pinned"] is False
    assert body["title"] is None


# ---------------------------------------------------------------------------
# (d) all three at once
# ---------------------------------------------------------------------------


def test_patch_all_fields_at_once(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_journal(tmp_path, {"sess-d": ["all"]})

    resp = client.patch(
        "/admin/sessions/sess-d",
        json={"title": "Triple", "pinned": True, "archived": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Triple"
    assert body["pinned"] is True
    assert body["archived"] is True


# ---------------------------------------------------------------------------
# (e) 404 — unknown session_key
# ---------------------------------------------------------------------------


def test_patch_unknown_session_returns_404(
    client: TestClient, tmp_path: Path
) -> None:
    """The session_meta upsert refuses to land a row for a session_key
    with no journaled turns — the route surfaces this as 404
    ``not_found`` so a typoed key never silently creates ghost rows.
    """
    _seed_journal(tmp_path, {"sess-real": ["real"]})

    resp = client.patch(
        "/admin/sessions/never-existed",
        json={"title": "Ghost"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


def test_patch_when_journal_absent_returns_404(
    client: TestClient,
) -> None:
    """No journal file → no session can possibly exist → 404."""
    resp = client.patch("/admin/sessions/whatever", json={"pinned": True})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# (f) 422 — empty body
# ---------------------------------------------------------------------------


def test_patch_empty_body_returns_422(
    client: TestClient, tmp_path: Path
) -> None:
    """All fields are optional Pydantic-wise, so an empty JSON body
    type-checks. The route adds an explicit ``empty_patch`` check so a
    no-op call fails loudly instead of silently 200-ing."""
    _seed_journal(tmp_path, {"sess-e": ["x"]})

    resp = client.patch("/admin/sessions/sess-e", json={})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "empty_patch"


def test_patch_all_none_body_returns_422(
    client: TestClient, tmp_path: Path
) -> None:
    """Explicit nulls are equivalent to an empty body — both signal no
    change."""
    _seed_journal(tmp_path, {"sess-f": ["x"]})

    resp = client.patch(
        "/admin/sessions/sess-f",
        json={"title": None, "pinned": None, "archived": None},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "empty_patch"


# ---------------------------------------------------------------------------
# Sort: pinned sessions float to the top of the list.
# ---------------------------------------------------------------------------


def test_pinned_session_floats_above_more_recent_unpinned(
    client: TestClient, tmp_path: Path
) -> None:
    """``sess-old`` arrives first, ``sess-new`` second; without pinning
    ``sess-new`` would lead the list. Pinning ``sess-old`` flips the
    order — pinned beats recency."""
    _seed_journal(
        tmp_path,
        {
            "sess-old": ["earlier message"],
            "sess-new": ["later message"],
        },
    )

    # Sanity: by default sess-new (more recent) is on top.
    before = client.get("/admin/sessions").json()["sessions"]
    assert [s["session_key"] for s in before][:2] == ["sess-new", "sess-old"]

    resp = client.patch("/admin/sessions/sess-old", json={"pinned": True})
    assert resp.status_code == 200

    after = client.get("/admin/sessions").json()["sessions"]
    assert [s["session_key"] for s in after][:2] == ["sess-old", "sess-new"]
    # The pinned row carries the flag in the listing too.
    assert after[0]["pinned"] is True
    assert after[1]["pinned"] is False


# ---------------------------------------------------------------------------
# Repeated PATCH — partial updates keep prior values.
# ---------------------------------------------------------------------------


def test_partial_patch_preserves_other_fields(
    client: TestClient, tmp_path: Path
) -> None:
    """Set title + pinned in one call, then PATCH only archived — the
    title + pinned MUST survive (the SQL uses COALESCE for unspecified
    fields)."""
    _seed_journal(tmp_path, {"sess-merge": ["m"]})

    r1 = client.patch(
        "/admin/sessions/sess-merge",
        json={"title": "Keep me", "pinned": True},
    )
    assert r1.status_code == 200
    assert r1.json()["title"] == "Keep me"

    r2 = client.patch(
        "/admin/sessions/sess-merge",
        json={"archived": True},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["title"] == "Keep me"
    assert body["pinned"] is True
    assert body["archived"] is True


# ---------------------------------------------------------------------------
# sessions_disabled gate.
# ---------------------------------------------------------------------------


def test_patch_503_when_sessions_disabled(tmp_path: Path) -> None:
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
            resp = c.patch(
                "/admin/sessions/anything",
                json={"title": "X"},
            )
            assert resp.status_code == 503
            assert resp.json()["detail"]["error"] == "sessions_disabled"
    finally:
        set_admin_state(None)


# ---------------------------------------------------------------------------
# Title is not cleared by a follow-up patch that omits it.
# ---------------------------------------------------------------------------


def test_patch_omitting_title_does_not_clear_existing(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_journal(tmp_path, {"sess-keep-title": ["k"]})
    client.patch(
        "/admin/sessions/sess-keep-title", json={"title": "Permanent"}
    )
    # PATCH only pinned; title must survive.
    resp = client.patch(
        "/admin/sessions/sess-keep-title", json={"pinned": True}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Permanent"
    assert body["pinned"] is True
