"""PERF-03 repro: ``GET /admin/curator/profiles`` fetches curator_state
one-row-at-a-time (``await curator_repo.get(slug)`` per slug) instead of a
single bulk query.

With N profiles the endpoint issues N separate SELECTs per poll, and the
endpoint is UI-polled. Acceptance: the listing fetches curator rows in a
single bulk call, not one ``get`` per slug.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from corlinman_evolution_store import (
    CuratorStateRepo,
    EvolutionStore,
    SignalsRepo,
)
from corlinman_server.gateway.routes_admin_b.infra import curator as curator_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from corlinman_server.profiles import ProfileStore
from corlinman_skills_registry import SkillRegistry
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[EvolutionStore]:
    db_path = tmp_path / "evolution-perf03.sqlite"
    s = await EvolutionStore.open(db_path)
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def profile_store(tmp_path: Path) -> ProfileStore:
    profiles_root = tmp_path / "profiles"
    s = ProfileStore(profiles_root)
    for slug in ("alpha", "bravo", "charlie", "delta", "echo"):
        s.create(slug=slug, display_name=slug.title())
        (profiles_root / slug / "skills").mkdir(parents=True, exist_ok=True)
    return s


@pytest.fixture
def skill_registry_factory(tmp_path: Path):
    def _factory(slug: str) -> SkillRegistry:
        return SkillRegistry.load_from_dir(
            tmp_path / "profiles" / slug / "skills"
        )

    return _factory


def test_profiles_listing_uses_single_bulk_query(
    tmp_path: Path,
    store: EvolutionStore,
    profile_store: ProfileStore,
    skill_registry_factory,
) -> None:
    repo = CuratorStateRepo(store.conn)

    # Instrument both the per-slug ``get`` and the bulk ``list_all`` so we
    # can prove the route stops doing one SELECT per profile.
    get_calls: list[str] = []
    list_all_calls: list[int] = []
    real_get = repo.get
    real_list_all = repo.list_all

    async def _counting_get(profile_slug, **kwargs):  # noqa: ANN001, ANN003
        get_calls.append(profile_slug)
        return await real_get(profile_slug, **kwargs)

    async def _counting_list_all(**kwargs):  # noqa: ANN003
        list_all_calls.append(1)
        return await real_list_all(**kwargs)

    repo.get = _counting_get  # type: ignore[method-assign]
    repo.list_all = _counting_list_all  # type: ignore[method-assign]

    state = AdminState(
        data_dir=tmp_path,
        profile_store=profile_store,
        curator_state_repo=repo,
        signals_repo=SignalsRepo(store.conn),
        skill_registry_factory=skill_registry_factory,
    )
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(curator_routes.router())
        client: TestClient = authenticated_test_client(app)
        resp = client.get("/admin/curator/profiles")
        assert resp.status_code == 200, resp.text
        rows = resp.json()["profiles"]
        assert {r["slug"] for r in rows} == {
            "alpha",
            "bravo",
            "charlie",
            "delta",
            "echo",
        }
    finally:
        set_admin_state(None)

    # The bug: one ``get`` per slug. The fix: a single bulk ``list_all``.
    assert get_calls == [], (
        f"route issued per-slug get() calls: {get_calls}"
    )
    assert len(list_all_calls) == 1, (
        f"expected exactly one bulk list_all(), got {len(list_all_calls)}"
    )
