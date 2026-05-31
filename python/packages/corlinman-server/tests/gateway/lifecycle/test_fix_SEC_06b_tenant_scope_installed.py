"""SEC-06b: the tenant-scope middleware must actually be installed.

The bug: ``install_tenant_scope_middleware`` (and ``TenantScopeMiddleware``)
were exported but never called in the app build, so api-key handlers read
the raw ``?tenant=`` query param and a request with no tenant context had
no resolved scope.

This test builds the app and asserts:
  * ``TenantScopeMiddleware`` is in the ASGI middleware stack, and
  * a request with no explicit tenant resolves to the ``"default"``
    tenant (single-tenant happy path), via a small probe route.
"""

from __future__ import annotations

from pathlib import Path

from corlinman_server.gateway.lifecycle.entrypoint import build_app
from corlinman_server.gateway.middleware import TenantScopeMiddleware
from corlinman_server.tenancy import TenantId, default_tenant
from fastapi import Request
from fastapi.testclient import TestClient


def _stack_has(app: object, cls: type) -> bool:
    return any(
        mw.cls is cls for mw in getattr(app, "user_middleware", [])
    )


def test_tenant_scope_middleware_is_installed(tmp_path: Path) -> None:
    app = build_app(config_path=None, data_dir=tmp_path)
    assert _stack_has(app, TenantScopeMiddleware), (
        "TenantScopeMiddleware must be in the app middleware stack"
    )


def test_no_tenant_resolves_to_default(tmp_path: Path) -> None:
    app = build_app(config_path=None, data_dir=tmp_path)

    @app.get("/_probe/tenant")
    async def _probe(request: Request) -> dict[str, str]:
        tenant = getattr(request.state, "tenant", None)
        return {"tenant": tenant.as_str() if isinstance(tenant, TenantId) else ""}

    with TestClient(app) as client:
        # No ?tenant= → must resolve to the default tenant, never 401/403.
        resp = client.get("/_probe/tenant")
        assert resp.status_code == 200, resp.text
        assert resp.json()["tenant"] == default_tenant().as_str()

        # A ?tenant=other on a disabled (single-tenant) deployment is
        # ignored entirely — still resolves to default (no override).
        resp2 = client.get("/_probe/tenant?tenant=other")
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["tenant"] == default_tenant().as_str()
