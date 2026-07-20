"""W8 — tenant isolation on the ``/admin/sessions*`` routes.

The journal-backed session surfaces (list / delete / delete-all /
patch / replay / cancel) historically ignored the resolved tenant, so
any admin could list, replay, rename, or delete EVERY tenant's sessions
by session_key. These tests pin the route-level fix: the resolved
tenant scopes each journal operation and cross-tenant access reads as
404 / empty, indistinguishable from an unknown key.
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


def _seed_journal(
    data_dir: Path, sessions: dict[str, tuple[str, list[tuple[str, str]]]]
) -> None:
    """Populate ``<data_dir>/agent_journal.sqlite`` with one completed
    turn per session: ``{session_key: (tenant_id, [(role, content)])}``.
    """

    async def _run() -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        journal = await AgentJournal.open(data_dir / "agent_journal.sqlite")
        try:
            for session_key, (tenant_id, msgs) in sessions.items():
                turn_id = await journal.begin_turn(
                    session_key, f"hello from {session_key}", tenant_id=tenant_id
                )
                assert turn_id is not None
                for role, content in msgs:
                    await journal.append_message(turn_id, role, content)
                await journal.complete_turn(turn_id)
        finally:
            await journal.close()

    asyncio.run(_run())


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    _seed_journal(
        tmp_path,
        {
            "sess-acme": ("acme", [("user", "hi"), ("assistant", "hello")]),
            "sess-globex": ("globex", [("user", "yo")]),
            "sess-legacy": ("", [("user", "old row")]),
        },
    )
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
# GET /admin/sessions — tenant-scoped listing
# ---------------------------------------------------------------------------


def test_list_scopes_to_query_tenant(client: TestClient) -> None:
    resp = client.get("/admin/sessions", params={"tenant": "acme"})
    assert resp.status_code == 200
    keys = {s["session_key"] for s in resp.json()["sessions"]}
    assert keys == {"sess-acme"}


def test_list_default_view_owns_legacy_rows(client: TestClient) -> None:
    """No ``?tenant=`` → the default tenant's view: unattributed legacy
    rows are visible, other tenants' sessions are not."""
    resp = client.get("/admin/sessions")
    assert resp.status_code == 200
    keys = {s["session_key"] for s in resp.json()["sessions"]}
    assert "sess-legacy" in keys
    assert "sess-acme" not in keys
    assert "sess-globex" not in keys


# ---------------------------------------------------------------------------
# DELETE /admin/sessions/{key} — cross-tenant delete is a 404 no-op
# ---------------------------------------------------------------------------


def test_delete_cross_tenant_404_and_survives(client: TestClient) -> None:
    resp = client.delete(
        "/admin/sessions/sess-acme", params={"tenant": "globex"}
    )
    assert resp.status_code == 404
    # The session is still there for its owner.
    listed = client.get("/admin/sessions", params={"tenant": "acme"})
    assert {s["session_key"] for s in listed.json()["sessions"]} == {
        "sess-acme"
    }


def test_delete_same_tenant_succeeds(client: TestClient) -> None:
    resp = client.delete(
        "/admin/sessions/sess-acme", params={"tenant": "acme"}
    )
    assert resp.status_code == 204
    listed = client.get("/admin/sessions", params={"tenant": "acme"})
    assert listed.json()["sessions"] == []


# ---------------------------------------------------------------------------
# DELETE /admin/sessions (nuke) — scoped to the resolved tenant
# ---------------------------------------------------------------------------


def test_delete_all_scoped_to_tenant(client: TestClient) -> None:
    resp = client.delete("/admin/sessions", params={"tenant": "acme"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1
    # Other tenants' sessions survive the "nuke".
    globex = client.get("/admin/sessions", params={"tenant": "globex"})
    assert {s["session_key"] for s in globex.json()["sessions"]} == {
        "sess-globex"
    }
    default = client.get("/admin/sessions")
    assert {s["session_key"] for s in default.json()["sessions"]} == {
        "sess-legacy"
    }


# ---------------------------------------------------------------------------
# PATCH /admin/sessions/{key} — cross-tenant meta writes read as 404
# ---------------------------------------------------------------------------


def test_patch_cross_tenant_404(client: TestClient) -> None:
    resp = client.patch(
        "/admin/sessions/sess-acme",
        params={"tenant": "globex"},
        json={"title": "stolen"},
    )
    assert resp.status_code == 404


def test_patch_same_tenant_succeeds(client: TestClient) -> None:
    resp = client.patch(
        "/admin/sessions/sess-acme",
        params={"tenant": "acme"},
        json={"title": "mine"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "mine"


# ---------------------------------------------------------------------------
# POST /admin/sessions/{key}/replay — cross-tenant replay 404s
# ---------------------------------------------------------------------------


def test_replay_cross_tenant_404(client: TestClient) -> None:
    resp = client.post(
        "/admin/sessions/sess-acme/replay",
        params={"tenant": "globex"},
        json={"mode": "transcript"},
    )
    assert resp.status_code == 404


def test_replay_same_tenant_returns_transcript(client: TestClient) -> None:
    resp = client.post(
        "/admin/sessions/sess-acme/replay",
        params={"tenant": "acme"},
        json={"mode": "transcript"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_key"] == "sess-acme"
    roles = [m["role"] for m in body["transcript"]]
    assert "user" in roles


# ---------------------------------------------------------------------------
# POST /admin/sessions/{key}/cancel — cross-tenant probe 404s
# ---------------------------------------------------------------------------


def test_cancel_cross_tenant_404(client: TestClient) -> None:
    resp = client.post(
        "/admin/sessions/sess-acme/cancel", params={"tenant": "globex"}
    )
    assert resp.status_code == 404


def test_cancel_same_tenant_not_running(client: TestClient) -> None:
    resp = client.post(
        "/admin/sessions/sess-acme/cancel", params={"tenant": "acme"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_running"
