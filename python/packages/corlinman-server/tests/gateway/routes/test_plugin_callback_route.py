"""Route-level tests for ``gateway/routes/plugin_callback.py`` (TEST-007).

The async-plugin callback route is mounted in stub form by the
production composition root: :func:`build_app_router` passes
``registry=None`` when no ``AsyncTaskRegistry`` is wired, which is the
documented behaviour today (ARCH_DEBT C4-PLUGIN-ASYNC). This file pins
that contract:

* **501 ``not_implemented`` envelope** — with ``registry=None`` every
  callback (both the canonical ``/v1/plugins/callback/{task_id}`` path
  and the legacy ``/plugin-callback/{task_id}`` alias) returns 501 with
  the Rust-compatible envelope.
* **auth-gated** — the canonical ``/v1/*`` path is behind the api-key
  middleware. Unauthenticated → 401; with a real minted tenant API key
  the request reaches the handler and returns the same 501 (so we prove
  the gate is the *only* thing standing between an attacker and the
  parked-task-injection surface, and that a legitimate caller passes it).

The R2-001 alias-gate test already covers the legacy
``/plugin-callback/{task_id}`` 401 path; here we cover the canonical
``/v1/*`` path end-to-end through :func:`build_app`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_server.gateway.lifecycle.entrypoint import build_app  # noqa: E402
from corlinman_server.gateway.routes.plugin_callback import router  # noqa: E402
from corlinman_server.tenancy import AdminDb, TenantId  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# 501 not_implemented envelope (registry None — the documented default)
# ---------------------------------------------------------------------------


def _stub_client() -> TestClient:
    app = fastapi.FastAPI()
    app.include_router(router(None))
    return TestClient(app)


def test_callback_v1_returns_501_not_implemented_envelope() -> None:
    with _stub_client() as client:
        resp = client.post("/v1/plugins/callback/tsk_anything", json={})
    assert resp.status_code == 501, resp.text
    body = resp.json()
    assert body["error"] == "not_implemented"
    assert body["route"] == "/v1/plugins/callback/{task_id}"
    assert "AsyncTaskRegistry" in body["message"]


def test_callback_legacy_alias_returns_501_not_implemented() -> None:
    with _stub_client() as client:
        resp = client.post("/plugin-callback/tsk_anything", json={})
    assert resp.status_code == 501, resp.text
    assert resp.json()["error"] == "not_implemented"


def test_callback_501_even_with_empty_body() -> None:
    """The 501 short-circuits before any body parse — an empty body
    (some plugins signal completion with none) still 501s, never 400."""
    with _stub_client() as client:
        resp = client.post("/v1/plugins/callback/tsk_anything", content=b"")
    assert resp.status_code == 501, resp.text
    assert resp.json()["error"] == "not_implemented"


# ---------------------------------------------------------------------------
# auth-gated through the production build_app boot path
# ---------------------------------------------------------------------------


def test_canonical_callback_requires_auth(tmp_path: Path) -> None:
    """Unauthenticated POST to the canonical ``/v1/*`` path → 401."""
    app = build_app(config_path=None, data_dir=tmp_path)
    with TestClient(app) as client:
        resp = client.post("/v1/plugins/callback/tsk_anything", json={})
    assert resp.status_code == 401, resp.text
    assert resp.json().get("error") == "unauthorized"


def test_authenticated_callback_passes_gate_and_returns_501(
    tmp_path: Path,
) -> None:
    """A real minted tenant API key passes the api-key gate and reaches
    the (stub) handler → 501 ``not_implemented``. Proves the gate is the
    only barrier, and that the production mount really is the ``None``
    stub form (C4-PLUGIN-ASYNC)."""
    app = build_app(config_path=None, data_dir=tmp_path)
    with TestClient(app) as client:
        admin_db: AdminDb | None = getattr(
            app.state, "corlinman_admin_db", None
        )
        assert admin_db is not None, "lifespan must open the tenants AdminDb"
        portal = client.portal
        assert portal is not None

        async def _mint() -> str:
            tenant = TenantId.new("acme")
            await admin_db.create_tenant(tenant, "Acme Inc", created_at=0)
            minted = await admin_db.mint_api_key(
                tenant, "operator", "route-tests", None
            )
            return minted.token

        token: str = portal.call(_mint)

        resp = client.post(
            "/v1/plugins/callback/tsk_anything",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 501, resp.text
    body: dict[str, Any] = resp.json()
    assert body["error"] == "not_implemented"
