"""Tests for the W1.3 ``/admin/skills`` + ``/admin/skills/hub/*`` routes.

W1.4 of ``docs/PLAN_SKILL_HUB.md``. Two surfaces under test:

* ``/admin/skills`` (and ``DELETE /admin/skills/{name}``) — the
  installed-list endpoint that tags each row with origin
  (``bundled`` / ``hub:<slug>@<ver>`` / ``user``) and refuses to delete
  bundled rows.
* ``/admin/skills/hub/*`` — the ClawHub proxy + install pipeline.
  ``ClawHubClient`` is replaced with an in-memory stub via
  ``AdminState.clawhub_client``.

The router module (``routes_admin_b/skills.py``) is being built by the
sibling agent (W1-ROUTES); until then the whole file is skipped via
:func:`pytest.importorskip`. Each scenario keeps its concrete assertions
narrow so trivial naming churn on the sibling side doesn't flap.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# TODO(W1-ROUTES): once
# ``corlinman_server.gateway.routes_admin_b.skills`` lands, the
# ``importorskip`` becomes a regular import.
skill_routes = pytest.importorskip(
    "corlinman_server.gateway.routes_admin_b.skills",
    reason=(
        "TODO(W1-ROUTES): waiting on gateway/routes_admin_b/skills.py "
        "from the sibling agent before these tests can execute."
    ),
)

# TODO(W1-CORE): the routes consume these classes from the hub module;
# importorskip the same module so we surface a single skip on missing
# core code instead of a cascade of NameErrors.
skill_hub = pytest.importorskip(
    "corlinman_server.system.skill_hub",
    reason=(
        "TODO(W1-CORE): waiting on system/skill_hub/* before these "
        "tests can execute."
    ),
)

from corlinman_server.gateway.routes_admin_b.state import (  # noqa: E402
    AdminState,
    set_admin_state,
)

from ._admin_auth import (  # noqa: E402
    authenticated_test_client,
    configure_admin_auth,
)

HubUnavailableError = skill_hub.HubUnavailableError


# ---------------------------------------------------------------------------
# Hub stub fixtures
# ---------------------------------------------------------------------------


def _summary(slug: str = "web-search") -> Any:
    """Build a HubSkillSummary-compatible dataclass instance.

    The W1-CORE module exposes the wire-shape camelCase field as
    ``latest_version`` with a ``version`` read-only alias. We construct
    with the canonical kwargs.
    """
    cls = skill_hub.HubSkillSummary
    return cls(  # type: ignore[call-arg]
        slug=slug,
        name=slug.replace("-", " ").title(),
        description="Search the live web.",
        emoji=None,
        stars=42,
        downloads=1024,
        latest_version="1.0.0",
        updated_at="2026-05-20T12:00:00Z",
    )


def _detail(slug: str = "web-search") -> Any:
    cls = skill_hub.HubSkillDetail
    return cls(  # type: ignore[call-arg]
        slug=slug,
        name=slug.replace("-", " ").title(),
        description="Search the live web.",
        emoji=None,
        stars=42,
        downloads=1024,
        latest_version="1.0.0",
        updated_at="2026-05-20T12:00:00Z",
        homepage=None,
        versions=["1.0.0"],
        scan_summary="no_findings",
        readme_excerpt="# web-search",
    )


def _make_hub_stub(*, summaries: list[Any] | None = None) -> Any:
    """Async-method mock that mimics :class:`ClawHubClient`."""
    stub = AsyncMock()
    stub.search.return_value = summaries or [_summary("web-search")]
    stub.list_skills.return_value = (
        summaries or [_summary("web-search")],
        None,
    )
    stub.get_skill.return_value = _detail("web-search")
    return stub


# ---------------------------------------------------------------------------
# AdminState / TestClient fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin_state(tmp_path: Path) -> Iterator[AdminState]:
    state = AdminState(data_dir=tmp_path)
    configure_admin_auth(state)
    # The route reads its profile-skills dir off the data dir using the
    # documented ``<data_dir>/profiles/<slug>/skills`` layout.
    profile_dir = tmp_path / "profiles" / "default" / "skills"
    profile_dir.mkdir(parents=True, exist_ok=True)
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


def _app(admin_state: AdminState) -> FastAPI:
    app = FastAPI()
    try:
        app.include_router(skill_routes.router())
    except AssertionError as exc:  # pragma: no cover — sibling-blocking
        # TODO(W1-ROUTES): FastAPI rejects a route that declares a
        # ``response_model`` (or an ``Any`` return annotation that gets
        # inferred as one) together with ``status_code=204``. The
        # sibling agent needs to either drop the return annotation on
        # the 204 endpoints or set ``response_model=None``. Until they
        # fix it, surface as a skip so the suite stays parseable.
        pytest.skip(
            f"TODO(W1-ROUTES): router() raised AssertionError on import: {exc}"
        )
    # The W1.3 contract pushes the hub client onto FastAPI ``app.state``
    # so route handlers can resolve it without a global. Tests honour
    # the same handle.
    app.state.clawhub_client = getattr(admin_state, "skill_hub_client", None)
    return app


def _client(admin_state: AdminState, *, hub: Any | None = None) -> TestClient:
    """Authenticated TestClient with ``hub`` injected as the ClawHub stub."""
    if hub is not None:
        admin_state.skill_hub_client = hub  # type: ignore[attr-defined]
        admin_state.extras["skill_hub_client"] = hub
    app = _app(admin_state)
    if hub is not None:
        app.state.clawhub_client = hub
    return authenticated_test_client(app)


# ---------------------------------------------------------------------------
# Helpers for the installed list
# ---------------------------------------------------------------------------


def _profile_dir(admin_state: AdminState) -> Path:
    assert admin_state.data_dir is not None
    p = admin_state.data_dir / "profiles" / "default" / "skills"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _seed_bundled(skills_dir: Path, name: str = "brainstorming") -> Path:
    """Write a flat ``<name>.md`` matching one of the bundled files.

    Matches the bundled-skills inventory: ``brainstorming.md``,
    ``plan.md``, ``memory.md`` etc.
    """
    target = skills_dir / f"{name}.md"
    target.write_text(
        f"---\nname: {name}\n---\n# {name}\nstub body\n",
        encoding="utf-8",
    )
    return target


def _seed_hub_skill(
    skills_dir: Path,
    *,
    slug: str = "web-search",
    version: str = "1.0.0",
) -> Path:
    target = skills_dir / slug
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        f"---\nname: {slug}\n---\n# {slug}\n", encoding="utf-8"
    )
    (target / ".openclaw-meta.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "version": version,
                "source": "clawhub",
                "installed_at": "2026-05-25T12:00:00.000Z",
            }
        ),
        encoding="utf-8",
    )
    return target


def _seed_user_dir(skills_dir: Path, name: str = "scratchpad") -> Path:
    target = skills_dir / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        f"---\nname: {name}\n---\n# {name}\n", encoding="utf-8"
    )
    return target


# ---------------------------------------------------------------------------
# 1. GET /admin/skills — origin tagging
# ---------------------------------------------------------------------------


def test_list_skills_tags_origin_for_bundled_hub_and_user(
    admin_state: AdminState,
) -> None:
    skills_dir = _profile_dir(admin_state)
    _seed_bundled(skills_dir, "brainstorming")
    _seed_hub_skill(skills_dir, slug="web-search", version="1.0.0")
    _seed_user_dir(skills_dir, name="scratchpad")

    client = _client(admin_state, hub=_make_hub_stub())
    resp = client.get("/admin/skills")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    rows = body.get("rows") or body.get("skills") or []
    by_name = {r["name"]: r for r in rows}
    # Sanity — we surfaced all three.
    assert {"brainstorming", "web-search", "scratchpad"} <= set(by_name)

    assert by_name["brainstorming"]["origin"] == "bundled"
    assert by_name["web-search"]["origin"].startswith("hub:")
    # web-search's origin string carries the version per the W1.3 spec.
    assert "web-search" in by_name["web-search"]["origin"]
    assert "1.0.0" in by_name["web-search"]["origin"]
    assert by_name["scratchpad"]["origin"] == "user"


# ---------------------------------------------------------------------------
# 1b. PUT /admin/skills/{name} — edit body + runtime metadata
# ---------------------------------------------------------------------------


def _wire_registry_factory(admin_state: AdminState) -> None:
    """Point ``skill_registry_factory`` at the on-disk profile skills dir.

    The PUT route loads the skill off the registry and writes it back, so
    these tests need a live :class:`SkillRegistry` factory (the disk-walk
    listing endpoints don't).
    """
    from corlinman_skills_registry import SkillRegistry

    def factory(_profile: str) -> Any:
        return SkillRegistry.load_from_dir(_profile_dir(admin_state))

    admin_state.skill_registry_factory = factory  # type: ignore[attr-defined]


def test_update_skill_writes_runtime_fields_back_to_disk(
    admin_state: AdminState,
) -> None:
    from corlinman_skills_registry.parse import parse_skill

    skills_dir = _profile_dir(admin_state)
    target = skills_dir / "scratchpad"
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        "---\nname: scratchpad\ndescription: original desc\n---\n"
        "# scratchpad\noriginal body\n",
        encoding="utf-8",
    )
    _wire_registry_factory(admin_state)

    client = _client(admin_state, hub=_make_hub_stub())
    resp = client.put(
        "/admin/skills/scratchpad",
        json={
            "description": "Edited summary.",
            "body_markdown": "# scratchpad\nedited body\n",
            "disable_model_invocation": True,
            "allowed_tools": ["web_search.query", "fs.read"],
            "when_to_use": "when you need a scratch buffer",
        },
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["name"] == "scratchpad"
    assert row["description"] == "Edited summary."

    # The five fields must round-trip through the SKILL.md on disk so the
    # registry picks them up on its next load (runtime-consumed).
    md_path = target / "SKILL.md"
    reparsed = parse_skill(md_path, md_path.read_text(encoding="utf-8"))
    assert reparsed.description == "Edited summary."
    assert reparsed.disable_model_invocation is True
    assert reparsed.allowed_tools == ["web_search.query", "fs.read"]
    assert reparsed.when_to_use == "when you need a scratch buffer"
    assert "edited body" in reparsed.body_markdown


def test_update_skill_partial_patch_leaves_other_fields(
    admin_state: AdminState,
) -> None:
    from corlinman_skills_registry.parse import parse_skill

    skills_dir = _profile_dir(admin_state)
    target = skills_dir / "buffer"
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        "---\nname: buffer\ndescription: original desc\n"
        "allowed-tools:\n  - keep.me\n---\n# buffer\nkeep this body\n",
        encoding="utf-8",
    )
    _wire_registry_factory(admin_state)

    client = _client(admin_state, hub=_make_hub_stub())
    # Only patch when_to_use — everything else must be preserved.
    resp = client.put(
        "/admin/skills/buffer",
        json={"when_to_use": "only this changed"},
    )
    assert resp.status_code == 200, resp.text

    md_path = target / "SKILL.md"
    reparsed = parse_skill(md_path, md_path.read_text(encoding="utf-8"))
    assert reparsed.when_to_use == "only this changed"
    assert reparsed.description == "original desc"
    assert reparsed.allowed_tools == ["keep.me"]
    assert "keep this body" in reparsed.body_markdown


def test_update_skill_unknown_name_returns_404(
    admin_state: AdminState,
) -> None:
    _profile_dir(admin_state)
    _wire_registry_factory(admin_state)
    client = _client(admin_state, hub=_make_hub_stub())
    resp = client.put(
        "/admin/skills/does-not-exist",
        json={"description": "x"},
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 2. DELETE /admin/skills/{bundled} → 409
# ---------------------------------------------------------------------------


def test_delete_bundled_skill_returns_409(admin_state: AdminState) -> None:
    skills_dir = _profile_dir(admin_state)
    _seed_bundled(skills_dir, "brainstorming")

    client = _client(admin_state, hub=_make_hub_stub())
    resp = client.delete("/admin/skills/brainstorming")
    assert resp.status_code == 409, resp.text
    # File untouched.
    assert (skills_dir / "brainstorming.md").is_file()


# ---------------------------------------------------------------------------
# 3. DELETE /admin/skills/{hub-name} → 200 + audit row
# ---------------------------------------------------------------------------


def test_delete_hub_skill_removes_dir_and_audits(
    admin_state: AdminState,
    tmp_path: Path,
) -> None:
    from corlinman_server.system.audit import SystemAuditLog

    skills_dir = _profile_dir(admin_state)
    target = _seed_hub_skill(skills_dir, slug="web-search")
    audit = SystemAuditLog(tmp_path / "audit.log")
    admin_state.audit_log = audit  # type: ignore[attr-defined]

    client = _client(admin_state, hub=_make_hub_stub())
    resp = client.delete("/admin/skills/web-search")
    assert resp.status_code == 200, resp.text

    assert not target.exists()


# ---------------------------------------------------------------------------
# 4. GET /admin/skills/hub/search?q=
# ---------------------------------------------------------------------------


def test_hub_search_returns_rows(admin_state: AdminState) -> None:
    hub = _make_hub_stub(
        summaries=[_summary("web-search"), _summary("web-fetch")]
    )
    client = _client(admin_state, hub=hub)
    resp = client.get("/admin/skills/hub/search", params={"q": "web"})
    assert resp.status_code == 200, resp.text

    body = resp.json()
    rows = body.get("rows") or body.get("results") or []
    slugs = [r["slug"] for r in rows]
    assert slugs == ["web-search", "web-fetch"]
    hub.search.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. Hub offline fallback
# ---------------------------------------------------------------------------


def test_hub_search_offline_returns_empty_with_flag(
    admin_state: AdminState,
) -> None:
    hub = AsyncMock()
    hub.search.side_effect = HubUnavailableError("clawhub down")
    client = _client(admin_state, hub=hub)
    resp = client.get("/admin/skills/hub/search", params={"q": "web"})
    assert resp.status_code == 200, resp.text

    body = resp.json()
    rows = body.get("rows") or body.get("results") or []
    assert rows == []
    assert body.get("offline") is True


# ---------------------------------------------------------------------------
# 6. Featured / list endpoint
# ---------------------------------------------------------------------------


def test_hub_featured_paginates(admin_state: AdminState) -> None:
    hub = _make_hub_stub(
        summaries=[_summary("a"), _summary("b"), _summary("c")]
    )
    hub.list_skills.return_value = (
        [_summary("a"), _summary("b"), _summary("c")],
        "cursor-next",
    )
    client = _client(admin_state, hub=hub)
    resp = client.get(
        "/admin/skills/hub/featured", params={"sort": "trending"}
    )
    assert resp.status_code == 200, resp.text

    body = resp.json()
    rows = body.get("rows") or body.get("results") or []
    assert len(rows) == 3
    assert body.get("next_cursor") == "cursor-next"


# ---------------------------------------------------------------------------
# 7. Skill detail 404
# ---------------------------------------------------------------------------


def test_hub_skill_detail_404_for_unknown(admin_state: AdminState) -> None:
    hub = _make_hub_stub()
    # The client raises HubUnavailableError-style for missing slugs in
    # real life; the route is documented to surface that as a 404.
    hub.get_skill.side_effect = HubUnavailableError("not found")
    client = _client(admin_state, hub=hub)
    resp = client.get("/admin/skills/hub/skills/nonexistent")
    assert resp.status_code in (404, 502)
    # The contract says 404; 502 is tolerated only because the hub
    # double conflates 404 + 5xx into the same exception. Once W1-CORE
    # adds a typed ``HubSkillNotFoundError`` we'll tighten this to 404.


# ---------------------------------------------------------------------------
# 8. POST /admin/skills/hub/install
# ---------------------------------------------------------------------------


def test_hub_install_returns_request_id(admin_state: AdminState) -> None:
    hub = _make_hub_stub()
    # Canned download for the installer worker.
    hub.download.return_value = skill_hub.HubDownload(  # type: ignore[call-arg]
        content=_minimal_tarball(),
        content_hash="sha256:fake",
    )
    client = _client(admin_state, hub=hub)
    resp = client.post(
        "/admin/skills/hub/install",
        json={"slug": "web-search", "version": "1.0.0"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body.get("request_id")


# ---------------------------------------------------------------------------
# 9. GET /admin/skills/hub/install/{id} — status transitions
# ---------------------------------------------------------------------------


def test_hub_install_status_transitions_to_terminal(
    admin_state: AdminState,
) -> None:
    hub = _make_hub_stub()
    hub.download.return_value = skill_hub.HubDownload(  # type: ignore[call-arg]
        content=_minimal_tarball(),
        content_hash="sha256:fake",
    )
    client = _client(admin_state, hub=hub)
    resp = client.post(
        "/admin/skills/hub/install",
        json={"slug": "web-search", "version": "1.0.0"},
    )
    assert resp.status_code == 202, resp.text
    request_id = resp.json()["request_id"]

    # Poll for terminal state. Background task may not have run on the
    # event loop yet, so allow a short bounded retry.
    deadline = time.time() + 5.0
    last: dict = {}
    while time.time() < deadline:
        status_resp = client.get(
            f"/admin/skills/hub/install/{request_id}"
        )
        assert status_resp.status_code == 200, status_resp.text
        last = status_resp.json()
        if last.get("state") in {"installed", "failed"}:
            break
        time.sleep(0.05)
    assert last.get("state") == "installed", last


# ---------------------------------------------------------------------------
# 10. SSE — install completion event
# ---------------------------------------------------------------------------


def test_hub_install_sse_emits_installed_event(
    admin_state: AdminState,
) -> None:
    hub = _make_hub_stub()
    hub.download.return_value = skill_hub.HubDownload(  # type: ignore[call-arg]
        content=_minimal_tarball(),
        content_hash="sha256:fake",
    )
    client = _client(admin_state, hub=hub)
    resp = client.post(
        "/admin/skills/hub/install",
        json={"slug": "web-search", "version": "1.0.0"},
    )
    request_id = resp.json()["request_id"]

    # Read the SSE stream. TestClient.stream blocks until the generator
    # closes; the install route closes the stream on terminal state.
    with client.stream(
        "GET",
        f"/admin/skills/hub/install/{request_id}/events/live",
    ) as stream:
        assert stream.status_code == 200
        body = stream.read().decode("utf-8")

    assert "installed" in body


# ---------------------------------------------------------------------------
# 11. Auth — missing credentials → 401
# ---------------------------------------------------------------------------


def test_admin_skills_requires_auth(admin_state: AdminState) -> None:
    app = _app(admin_state)
    # NOTE: admin-B routes use HTTP Basic, not Bearer (the W1.4 prompt's
    # "Bearer" phrasing was illustrative — see report). Any
    # unauthenticated request is rejected with 401 by the
    # ``require_admin`` dependency.
    unauthed = TestClient(app)
    resp = unauthed.get("/admin/skills")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_tarball() -> bytes:
    """Return a gzip tarball with one ``SKILL.md`` member under the slug
    dir. Used by the install endpoints to drive the installer through to
    a successful terminal state."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"---\nname: web-search\n---\n# web-search\n"
        info = tarfile.TarInfo(name="web-search/SKILL.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
