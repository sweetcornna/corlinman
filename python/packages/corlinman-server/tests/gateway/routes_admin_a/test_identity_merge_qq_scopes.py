"""QQ-instance memory rehoming during operator identity merges."""

from __future__ import annotations

import base64
from typing import Any

from corlinman_server.gateway.routes_admin_a import (
    AdminState,
    build_router,
    set_admin_state,
)
from corlinman_server.gateway.routes_admin_a._session_store import AdminSessionStore
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _basic_auth_header() -> str:
    token = base64.b64encode(b"admin:rootroot").decode("ascii")
    return f"Basic {token}"


class _IdentityStore:
    async def merge_users(self, into: Any, source: Any, decided_by: str) -> str:
        assert str(into) == "winner"
        assert str(source) == "loser"
        assert decided_by == "operator"
        return "winner"


class _Kernel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def list_instance_scopes_for_user(self, user_id: str) -> list[str]:
        assert user_id == "loser"
        return ["qq-instance:deleted-bot:loser"]

    async def merge_scope_user(self, old: str, new: str) -> int:
        self.calls.append((old, new))
        if old == "qq-instance:bot-a:loser":
            raise RuntimeError("one scope failed")
        return 1


class _Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def list_instance_namespace_prefixes_for_user(
        self, user_id: str
    ) -> list[str]:
        assert user_id == "loser"
        return ["facts/default/qq-instance:host-only:loser"]

    async def rename_namespace_prefix(self, old: str, new: str) -> int:
        self.calls.append((old, new))
        return 1


def test_merge_rehomes_default_and_every_configured_qq_instance(tmp_path) -> None:
    kernel = _Kernel()
    host = _Host()
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config={
            "qq": {
                "default_instance": "default",
                "instances": {
                    "default": {"enabled": True},
                    "bot-a": {"enabled": True},
                    "bot-b": {"enabled": False},
                },
            }
        },
    )
    state.identity_store = _IdentityStore()
    state.memory_kernel = kernel
    state.memory_host = host
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(build_router())
        with TestClient(app, headers={"Authorization": _basic_auth_header()}) as client:
            response = client.post(
                "/admin/identity/merge",
                json={
                    "into_user_id": "winner",
                    "from_user_id": "loser",
                    "decided_by": "operator",
                },
            )
    finally:
        set_admin_state(None)

    assert response.status_code == 200, response.text
    assert response.json() == {"surviving_user_id": "winner"}
    assert kernel.calls == [
        ("loser", "winner"),
        ("qq-instance:bot-a:loser", "qq-instance:bot-a:winner"),
        ("qq-instance:bot-b:loser", "qq-instance:bot-b:winner"),
        ("qq-instance:deleted-bot:loser", "qq-instance:deleted-bot:winner"),
        ("qq-instance:host-only:loser", "qq-instance:host-only:winner"),
    ]
    assert host.calls == [
        ("facts/default/loser", "facts/default/winner"),
        (
            "facts/default/qq-instance:bot-a:loser",
            "facts/default/qq-instance:bot-a:winner",
        ),
        (
            "facts/default/qq-instance:bot-b:loser",
            "facts/default/qq-instance:bot-b:winner",
        ),
        (
            "facts/default/qq-instance:deleted-bot:loser",
            "facts/default/qq-instance:deleted-bot:winner",
        ),
        (
            "facts/default/qq-instance:host-only:loser",
            "facts/default/qq-instance:host-only:winner",
        ),
    ]
    assert all("qq-instance:default:" not in value for call in host.calls for value in call)
