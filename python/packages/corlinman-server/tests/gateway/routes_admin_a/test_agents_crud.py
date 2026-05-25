"""Tests for the W1.2 ``/admin/agents`` CRUD + reload surface.

Covers:
* GET surfaces ``source`` and ``description`` per row.
* POST creates a new user-overlay card and reloads the registry.
* POST refuses to overwrite existing overlays (400).
* POST refuses to shadow built-ins without ``force=true`` (409).
* POST with ``force=true`` shadows a built-in.
* DELETE refuses to remove a built-in (409).
* DELETE removes a user overlay and reloads the registry.
* POST ``/reload`` re-scans the dir stack.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import pytest
from corlinman_agent.agents import AgentCardRegistry
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


_BUILTIN_YAML = """\
name: mentor
description: built-in mentor card.
system_prompt: |
  You are a built-in helper.
"""


@pytest.fixture()
def workspace(tmp_path: Path) -> Iterator[dict[str, Path]]:
    """Lay out a fake repo + data dir so the registry has both tiers.

    The data dir's ``agents/`` overlay starts empty so each test
    decides whether to seed it.
    """
    builtin_dir = tmp_path / "repo" / "agents"
    builtin_dir.mkdir(parents=True)
    (builtin_dir / "mentor.yaml").write_text(_BUILTIN_YAML, encoding="utf-8")

    data_dir = tmp_path / "data"
    user_overlay = data_dir / "agents"
    user_overlay.mkdir(parents=True)

    yield {
        "builtin_dir": builtin_dir,
        "data_dir": data_dir,
        "user_overlay": user_overlay,
    }


def _build_registry(workspace: dict[str, Path]) -> AgentCardRegistry:
    """Rebuild from the two-tier stack the workspace fixture sets up."""
    return AgentCardRegistry.load_from_dir_stack(
        [
            (workspace["builtin_dir"], "built-in"),
            (workspace["user_overlay"], "user"),
        ]
    )


@pytest.fixture()
def client(workspace: dict[str, Path]) -> Iterator[TestClient]:
    """Authenticated TestClient wired to a fresh registry per test."""
    # Mutable container so the reload helper can swap in a fresh registry
    # without us having to reach back into the AdminState.
    registry_holder: list[AgentCardRegistry] = [_build_registry(workspace)]

    async def _reload() -> AgentCardRegistry:
        registry_holder[0] = _build_registry(workspace)
        return registry_holder[0]

    state = AdminState(
        data_dir=workspace["data_dir"],
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        agent_registry=registry_holder[0],
        agent_registry_reload=_reload,
    )
    # Keep the latest registry reflected onto the state — the reload
    # helper is called by the routes, but the GET path reads
    # ``state.agent_registry`` so we have to mirror the freshest copy
    # back. We monkey-patch ``_reload`` to do that in one step.

    async def _reload_and_publish() -> AgentCardRegistry:
        registry_holder[0] = _build_registry(workspace)
        state.agent_registry = registry_holder[0]
        return registry_holder[0]

    state.agent_registry_reload = _reload_and_publish

    set_admin_state(state)

    app = FastAPI()
    app.include_router(build_router())

    with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
        yield c

    set_admin_state(None)


# ---------------------------------------------------------------------------
# GET /admin/agents — source + description
# ---------------------------------------------------------------------------


def test_list_includes_source_and_description(client: TestClient) -> None:
    """Built-in rows surface as ``source="built-in"`` with their description."""
    resp = client.get("/admin/agents")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    names = {row["name"]: row for row in rows}
    assert "mentor" in names
    assert names["mentor"]["source"] == "built-in"
    assert names["mentor"]["description"] == "built-in mentor card."


# ---------------------------------------------------------------------------
# POST /admin/agents — create
# ---------------------------------------------------------------------------


def test_create_user_overlay_md(
    client: TestClient, workspace: dict[str, Path]
) -> None:
    """A new MD card lands under the user overlay and is registry-visible."""
    body = (
        "---\n"
        "description: a researcher.\n"
        "---\n\n"
        "You are a careful researcher.\n"
    )
    resp = client.post(
        "/admin/agents",
        json={"name": "researcher", "format": "md", "body": body},
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["name"] == "researcher"
    assert payload["source"] == "user"

    # File written to disk.
    written = workspace["user_overlay"] / "researcher.md"
    assert written.is_file()
    assert "careful researcher" in written.read_text(encoding="utf-8")

    # Registry reloaded — the new card shows up via the list endpoint.
    listing = client.get("/admin/agents").json()
    names = {row["name"] for row in listing}
    assert "researcher" in names


def test_create_rejects_existing_overlay(
    client: TestClient, workspace: dict[str, Path]
) -> None:
    """A pre-existing overlay file means create → 400 (delete first)."""
    (workspace["user_overlay"] / "writer.md").write_text(
        "---\ndescription: existing.\n---\n\nbody\n", encoding="utf-8"
    )
    # Rebuild registry so the state sees the seed.
    client.post("/admin/agents/reload")

    resp = client.post(
        "/admin/agents",
        json={
            "name": "writer",
            "format": "md",
            "body": "---\ndescription: new.\n---\n\nbody\n",
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "agent_exists"


def test_create_rejects_invalid_name(client: TestClient) -> None:
    """Names with uppercase / leading digits / slashes → 400 invalid_name."""
    resp = client.post(
        "/admin/agents",
        json={"name": "Bad-Name", "format": "md", "body": "x"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_name"


def test_create_shadows_builtin_requires_force(client: TestClient) -> None:
    """Built-in shadowing without ``force=true`` → 409 shadows_builtin."""
    resp = client.post(
        "/admin/agents",
        json={
            "name": "mentor",  # the built-in name from the fixture.
            "format": "md",
            "body": "---\ndescription: my mentor.\n---\n\nhi\n",
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "shadows_builtin"


def test_create_shadows_builtin_with_force(
    client: TestClient, workspace: dict[str, Path]
) -> None:
    """``force=true`` lets operators deliberately shadow a built-in."""
    resp = client.post(
        "/admin/agents",
        json={
            "name": "mentor",
            "format": "md",
            "body": "---\ndescription: overridden mentor.\n---\n\nNew body.\n",
            "force": True,
        },
    )
    assert resp.status_code == 201, resp.text
    # File landed in the user overlay.
    assert (workspace["user_overlay"] / "mentor.md").is_file()
    # Registry now resolves mentor from the user tier, not built-in.
    listing = client.get("/admin/agents").json()
    mentor = next(r for r in listing if r["name"] == "mentor")
    assert mentor["source"] == "user"
    assert mentor["description"] == "overridden mentor."


# ---------------------------------------------------------------------------
# DELETE /admin/agents/{name}
# ---------------------------------------------------------------------------


def test_delete_user_overlay(
    client: TestClient, workspace: dict[str, Path]
) -> None:
    """DELETE on a user overlay returns 204 + reloads the registry."""
    target = workspace["user_overlay"] / "scratch.md"
    target.write_text(
        "---\ndescription: scratch.\n---\n\nx.\n", encoding="utf-8"
    )
    client.post("/admin/agents/reload")  # publish scratch into registry

    resp = client.delete("/admin/agents/scratch")
    assert resp.status_code == 204, resp.text
    assert not target.exists()

    listing = client.get("/admin/agents").json()
    assert "scratch" not in {r["name"] for r in listing}


def test_delete_builtin_refused(client: TestClient) -> None:
    """Built-ins are immutable from the API — DELETE returns 409."""
    resp = client.delete("/admin/agents/mentor")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "builtin_immutable"


def test_delete_unknown_404(client: TestClient) -> None:
    """No registry entry, no overlay → 404."""
    resp = client.delete("/admin/agents/nope")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /admin/agents/reload
# ---------------------------------------------------------------------------


def test_reload_picks_up_new_files(
    client: TestClient, workspace: dict[str, Path]
) -> None:
    """A file written outside the API surfaces after a reload call."""
    # Sanity: before the write, no ``sneaky`` agent.
    listing = client.get("/admin/agents").json()
    assert "sneaky" not in {r["name"] for r in listing}

    (workspace["user_overlay"] / "sneaky.md").write_text(
        "---\ndescription: sneaky.\n---\n\nbody.\n", encoding="utf-8"
    )

    resp = client.post("/admin/agents/reload")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "ok"
    assert "sneaky" in payload["names"]
    assert payload["count"] == len(payload["names"])
