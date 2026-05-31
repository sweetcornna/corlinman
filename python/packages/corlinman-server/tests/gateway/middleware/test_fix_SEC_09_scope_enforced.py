"""SEC-09: API-key ``scope`` must be enforced, not just stored.

The bug: ``tenant_api_keys.scope`` was stored but neither
``ApiKeyAuthMiddleware.dispatch`` nor ``require_api_key()`` compared it
against any route-required scope, so a key minted with e.g.
``scope="embeddings"`` could authenticate against ``/v1/chat``.

Fix contract: ``/v1/*`` requires the ``"chat"`` scope (which existing
prod keys hold). A key whose scope set does not include ``"chat"`` must
get a 403 ``insufficient_scope``; a ``scope="chat"`` key still reaches
``/v1/chat``.
"""

from __future__ import annotations

from dataclasses import dataclass

from corlinman_server.gateway.middleware import (
    AuthenticatedApiKey,
    install_api_key_middleware,
    require_api_key,
)
from corlinman_server.tenancy import ApiKeyRow, TenantId, default_tenant
from fastapi import FastAPI
from fastapi.testclient import TestClient


@dataclass
class _FakeAdminDb:
    """Returns a row whose scope is configurable per token."""

    scope_for: dict[str, str] | None = None

    async def verify_api_key(self, token: str) -> ApiKeyRow | None:
        table = self.scope_for or {}
        if token not in table:
            return None
        return ApiKeyRow(
            key_id=f"key_{token}",
            tenant_id=default_tenant(),
            username="alice",
            scope=table[token],
            label=None,
            token_hash="dummy",
            created_at_ms=0,
            last_used_at_ms=None,
            revoked_at_ms=None,
        )

    async def get_admin(self, tenant: TenantId, username: str):  # pragma: no cover
        return None


def _app() -> FastAPI:
    app = FastAPI()
    install_api_key_middleware(
        app,
        admin_db=_FakeAdminDb(  # type: ignore[arg-type]
            scope_for={"chat-key": "chat", "embed-key": "embeddings"}
        ),
    )

    @app.get("/v1/chat")
    def chat(auth: AuthenticatedApiKey = require_api_key()) -> dict[str, str]:
        return {"scope": auth.api_key.scope}

    return app


def test_chat_scope_key_reaches_v1_chat() -> None:
    client = TestClient(_app())
    resp = client.get("/v1/chat", headers={"Authorization": "Bearer chat-key"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "chat"


def test_non_chat_scope_key_gets_403() -> None:
    client = TestClient(_app())
    resp = client.get("/v1/chat", headers={"Authorization": "Bearer embed-key"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["reason"] == "insufficient_scope"
