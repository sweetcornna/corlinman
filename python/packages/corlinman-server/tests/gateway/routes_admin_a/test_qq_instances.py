from __future__ import annotations

import base64
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_a import AdminState, build_router, set_admin_state
from corlinman_server.gateway.routes_admin_a import qq_instances as qq_instance_routes
from corlinman_server.gateway.routes_admin_a._session_store import AdminSessionStore
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from corlinman_server.gateway.routes_admin_b._napcat_lib import NapcatError
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _auth() -> str:
    encoded = base64.b64encode(b"admin:rootroot").decode("ascii")
    return f"Basic {encoded}"


class _Writer:
    def __init__(self, state: AdminState) -> None:
        self.state = state
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, channels: dict[str, Any]) -> None:
        candidate = deepcopy(channels)
        self.calls.append(candidate)
        self.state.channels_config = candidate

    async def mutate(self, mutator: Any) -> Any:
        candidate, result = mutator(deepcopy(self.state.channels_config or {}), {})
        await self(candidate)
        return result


@pytest.fixture()
def setup(tmp_path: Path):
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config={
            "qq": {
                "default_instance": "bot-a",
                "instances": {
                    "bot-a": {
                        "display_name": "Bot A",
                        "enabled": True,
                        "connection_mode": "external",
                        "ws_url": "ws://bot-a:3001",
                        "napcat_url": "http://bot-a:6099",
                        "access_token": "onebot-a-secret",
                        "napcat_access_token": "webui-a-secret",
                        "group_whitelist": [100],
                        "group_keywords": {"100": ["alpha"]},
                    },
                    "bot-b": {
                        "display_name": "Bot B",
                        "enabled": False,
                        "connection_mode": "managed",
                    },
                },
            }
        },
    )
    state.channels_writer = _Writer(state)
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app, headers={"Authorization": _auth()}) as client:
        yield state, client
    set_admin_state(None)


def test_list_get_and_secrets_are_instance_scoped(setup) -> None:
    _state, client = setup

    response = client.get("/admin/channels/qq/instances")

    assert response.status_code == 200, response.text
    rows = response.json()["instances"]
    assert [row["instance_id"] for row in rows] == ["bot-a", "bot-b"]
    bot_a = rows[0]
    assert bot_a["is_default"] is True
    assert "access_token" not in bot_a["config"]
    assert bot_a["secrets"]["access_token"]["is_set"] is True
    assert rows[1]["secrets"] == {}

    get_b = client.get("/admin/channels/qq/instances/bot-b")
    assert get_b.status_code == 200
    assert get_b.json()["display_name"] == "Bot B"


def test_create_patch_and_set_default_use_revisions(setup) -> None:
    state, client = setup
    revision = client.get("/admin/channels/qq/instances").json()["revision"]

    created = client.post(
        "/admin/channels/qq/instances",
        headers={"If-Match": f'"{revision}"'},
        json={"instance_id": "bot-c", "display_name": "Bot C"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["connection_mode"] == "managed"
    assert state.channels_config["qq"]["instances"]["bot-c"]["enabled"] is False

    stale = client.patch(
        "/admin/channels/qq/instances/bot-c",
        headers={"If-Match": revision},
        json={"enabled": True},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "revision_conflict"

    current = created.json()["revision"]
    patched = client.patch(
        "/admin/channels/qq/instances/bot-c",
        headers={"If-Match": current},
        json={"enabled": True},
    )
    assert patched.status_code == 200, patched.text
    defaulted = client.post(
        "/admin/channels/qq/instances/bot-c/set-default",
        headers={"If-Match": patched.json()["revision"]},
    )
    assert defaulted.status_code == 200
    assert defaulted.json()["is_default"] is True


def test_account_config_keywords_and_humanlike_do_not_touch_sibling(setup) -> None:
    state, client = setup
    before_b = deepcopy(state.channels_config["qq"]["instances"]["bot-b"])

    config = client.put(
        "/admin/channels/qq/instances/bot-a/config",
        json={
            "ids": {"group_whitelist": ["200"]},
            "urls": {"group_reply_policy": "all"},
        },
    )
    assert config.status_code == 200, config.text
    assert config.json()["wrote"] == ["group_reply_policy", "group_whitelist"]

    keywords = client.put(
        "/admin/channels/qq/instances/bot-a/keywords",
        json={"group_keywords": {"200": ["beta"]}},
    )
    assert keywords.status_code == 200
    assert keywords.json()["group_keywords"] == {"200": ["beta"]}

    humanlike = client.put(
        "/admin/channels/qq/instances/bot-a/humanlike",
        json={"enabled": False, "persona_id": None},
    )
    assert humanlike.status_code == 200
    assert state.channels_config["qq"]["instances"]["bot-b"] == before_b


def test_explicit_missing_instance_never_falls_back(setup) -> None:
    _state, client = setup

    response = client.get("/admin/channels/qq/instances/missing/status")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "qq_instance_not_found"


def test_legacy_aliases_mutate_explicit_default_only(setup) -> None:
    state, client = setup
    before_b = deepcopy(state.channels_config["qq"]["instances"]["bot-b"])

    status = client.get("/admin/channels/qq/status")
    assert status.status_code == 200
    assert status.json()["config_keys"]["ws_url"] == "ws://bot-a:3001"

    config = client.put(
        "/admin/channels/qq/config",
        json={"ids": {"group_whitelist": ["300"]}},
    )
    assert config.status_code == 200, config.text
    assert state.channels_config["qq"]["instances"]["bot-a"]["group_whitelist"] == [300]

    keywords = client.post(
        "/admin/channels/qq/keywords",
        json={"group_keywords": {"300": ["default-only"]}},
    )
    assert keywords.status_code == 200
    assert state.channels_config["qq"]["instances"]["bot-a"]["group_keywords"] == {
        "300": ["default-only"]
    }

    humanlike = client.put(
        "/admin/channels/qq/humanlike",
        json={"enabled": False, "persona_id": None},
    )
    assert humanlike.status_code == 200
    assert state.channels_config["qq"]["instances"]["bot-a"]["humanlike"] == {
        "enabled": False,
        "persona_id": None,
    }
    assert state.channels_config["qq"]["instances"]["bot-b"] == before_b


def test_reconnect_redacts_napcat_failure_detail(
    setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _state, client = setup
    secret = "http://user:password@private-napcat:6099/private?token=secret"

    class _FailingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self):
            raise NapcatError("napcat_unreachable", secret, status=418)

        async def __aexit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(qq_instance_routes, "_NapcatClient", _FailingClient)

    response = client.post("/admin/channels/qq/instances/bot-a/reconnect")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "error": "reconnect_failed",
        "message": "failed to reconnect the QQ instance",
    }
    assert secret not in response.text


def test_status_reads_exact_runtime_health(setup) -> None:
    state, client = setup

    class _Registry:
        def health(self, instance_id: str) -> dict[str, Any] | None:
            return {"online": instance_id == "bot-a", "account_qq": 10001}

    state.qq_runtime_registry = _Registry()

    response = client.get("/admin/channels/qq/instances/bot-a/status")

    assert response.status_code == 200
    assert response.json()["runtime"] == {"online": True, "account_qq": 10001}
