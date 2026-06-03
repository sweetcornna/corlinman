"""``GET /admin/config/schema`` advertises the ``subagent`` section.

Regression for the gap where the config JSON-Schema's ``known_sections``
list omitted ``subagent`` — so the admin UI's section navigator never
scaffolded a chip for the ``[subagent]`` table even though the runtime
reads ``[subagent] max_concurrent_per_tenant`` from it. The schema must
surface ``subagent`` as one of its top-level object properties (alongside
the other canonical sections) so the UI form generator can render it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from corlinman_server.gateway.routes_admin_b import config as config_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI

from ._admin_auth import authenticated_test_client, configure_admin_auth


@pytest.fixture()
def admin_state() -> Iterator[AdminState]:
    state = AdminState()
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


def test_config_schema_includes_subagent_section(
    admin_state: AdminState,
) -> None:
    app = FastAPI()
    app.include_router(config_routes.router())
    client = authenticated_test_client(app)

    resp = client.get("/admin/config/schema")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    props = body.get("properties") or {}
    # The canonical sections must all be present, and crucially ``subagent``
    # which the runtime reads its per-tenant ceiling from.
    assert "subagent" in props, sorted(props)
    assert props["subagent"]["type"] == "object"
