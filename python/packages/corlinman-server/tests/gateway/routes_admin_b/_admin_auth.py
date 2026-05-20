"""Test helpers for admin-B routes mounted behind real admin auth."""

from __future__ import annotations

import base64
from typing import Any

from corlinman_server.gateway.routes_admin_a._session_store import (
    AdminSessionStore,
)
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "rootroot"
_ADMIN_PASSWORD_HASH = hash_password(ADMIN_PASSWORD)
_AUTH_HEADER = {
    "Authorization": "Basic "
    + base64.b64encode(
        f"{ADMIN_USERNAME}:{ADMIN_PASSWORD}".encode()
    ).decode("ascii")
}


def configure_admin_auth(state: Any) -> Any:
    """Populate a routes_admin_b AdminState with real admin auth fields."""
    state.admin_username = ADMIN_USERNAME  # type: ignore[attr-defined]
    state.admin_password_hash = _ADMIN_PASSWORD_HASH  # type: ignore[attr-defined]
    state.session_store = AdminSessionStore(86_400)  # type: ignore[attr-defined]
    return state


def authenticated_test_client(app: FastAPI) -> TestClient:
    """Create a TestClient that authenticates via HTTP Basic."""
    return TestClient(app, headers=_AUTH_HEADER)
