"""``/admin/authz/grants*`` — durable-grant admin surface (W3-4).

Covers the list + revoke routes AND the cross-process contract: the
gateway revoking a grant must make a *separate* agent-side GrantStore
instance (already warm, mirror loaded) stop honouring it at its next
permission check — the mtime-based invalidation the GrantStore
docstring documents.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import pytest
from corlinman_agent.authz.grants import GrantStore, arg_digest
from corlinman_agent.authz.model import Subject
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

_SUBJECT = Subject(session_key="acme::s1", tenant_id="acme")


def _basic_auth_header(username: str = "admin", password: str = "rootroot") -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


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
    with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
        yield c
    set_admin_state(None)


def test_list_always_grants(client: TestClient, tmp_path: Path) -> None:
    # Seed as the agent process would: an "always" approval.
    GrantStore(tmp_path).record(_SUBJECT, "run_shell", {"command": "ls"}, "always")

    resp = client.get("/admin/authz/grants")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["tenant"] == "acme"
    assert row["tool"] == "run_shell"
    assert row["arg_digest"] == arg_digest("run_shell", {"command": "ls"})
    assert row["surface"] == "" and row["user_id"] == ""
    assert isinstance(row["created_at"], float)


def test_revoke_is_visible_to_a_warm_agent_store(
    client: TestClient, tmp_path: Path
) -> None:
    """W3-4 AC5: revoking via the admin route takes effect on the agent
    side at the next permission check, without an agent restart."""
    agent_store = GrantStore(tmp_path)
    agent_store.record(_SUBJECT, "run_shell", {"command": "ls"}, "always")
    # Warm the agent-side mirror (simulates mid-session state).
    assert agent_store.is_granted(_SUBJECT, "run_shell", {"command": "ls"})

    digest = arg_digest("run_shell", {"command": "ls"})
    resp = client.request(
        "DELETE",
        "/admin/authz/grants",
        json={
            "tenant": "acme",
            "surface": "",
            "user_id": "",
            "tool": "run_shell",
            "arg_digest": digest,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # The warm agent-side instance re-stats the DB and drops the grant.
    assert not agent_store.is_granted(_SUBJECT, "run_shell", {"command": "ls"})
    assert client.get("/admin/authz/grants").json() == []


def test_revoke_unknown_grant_is_404(client: TestClient) -> None:
    resp = client.request(
        "DELETE",
        "/admin/authz/grants",
        json={
            "tenant": "acme",
            "tool": "run_shell",
            "arg_digest": "deadbeef",
        },
    )
    assert resp.status_code == 404
