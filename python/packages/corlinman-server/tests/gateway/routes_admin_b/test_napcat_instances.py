from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from corlinman_server.gateway.qq_instances import QqInstanceAdminService
from corlinman_server.gateway.routes_admin_a.state import AdminState as AdminAState
from corlinman_server.gateway.routes_admin_b import napcat, napcat_instances
from corlinman_server.gateway.routes_admin_b import state as admin_b_state
from corlinman_server.gateway.routes_admin_b._napcat_lib import (
    NapcatError,
    QqAccount,
    QrcodeOut,
    StatusOut,
    _accounts_path_for_instance,
)
from corlinman_server.gateway.routes_admin_b.state import AdminState as AdminBState
from fastapi import FastAPI

from ._admin_auth import authenticated_test_client, configure_admin_auth


class _Writer:
    def __init__(self, state: AdminAState) -> None:
        self.state = state

    async def __call__(self, channels: dict[str, Any]) -> None:
        self.state.channels_config = channels


class _ManagedManager:
    def __init__(self) -> None:
        self.generation = 1
        self.provisioned = False
        self.requests: list[tuple[str, str]] = []

    async def request(self, operation: str, instance_id: str, **_kwargs: object):
        from corlinman_server.system.napcat_manager.models import (
            ManagerResponse,
            NapCatDescriptor,
        )

        self.requests.append((operation, instance_id))
        if operation == "inspect" and not self.provisioned:
            return ManagerResponse(
                ok=False,
                request_id=operation,
                error_code="instance_not_found",
            )
        if operation in {"adopt", "provision"}:
            self.provisioned = True
        return ManagerResponse(
            ok=True,
            request_id=operation,
            descriptor=NapCatDescriptor(
                instance_id=instance_id,
                generation=self.generation,
                ws_url=f"ws://{instance_id}:3001",
                http_url=f"http://{instance_id}:6099",
                access_token="onebot-private",
                napcat_access_token="napcat-private",
            ),
        )


class _FakeNapcatClient:
    status = StatusOut(status="waiting")
    qr_calls = 0
    quick_calls: ClassVar[list[str]] = []
    ensures: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def __init__(self, base_url: str, access_token: str | None) -> None:
        self.base_url = base_url
        self.access_token = access_token

    async def __aenter__(self) -> _FakeNapcatClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def request_qrcode(self) -> QrcodeOut:
        type(self).qr_calls += 1
        return QrcodeOut(
            token="ignored-upstream-token",
            qrcode_url=f"https://qq.example/qr/{self.base_url.rsplit('/', 1)[-1]}",
            expires_at=10**15,
        )

    async def check_status(self) -> StatusOut:
        return type(self).status

    async def quick_login(self, uin: str) -> StatusOut:
        type(self).quick_calls.append(uin)
        return StatusOut(
            status="confirmed",
            account=QqAccount(uin=uin, nickname="Bot", last_login_at=1),
        )


@pytest.fixture()
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _FakeNapcatClient.status = StatusOut(status="waiting")
    _FakeNapcatClient.qr_calls = 0
    _FakeNapcatClient.quick_calls = []
    _FakeNapcatClient.ensures = []

    admin_a = AdminAState(
        data_dir=tmp_path,
        channels_config={
            "qq": {
                "default_instance": "bot-a",
                "instances": {
                    "bot-a": {
                        "enabled": True,
                        "connection_mode": "external",
                        "napcat_url": "http://bot-a:6099",
                        "ws_url": "ws://bot-a:3001",
                    },
                    "bot-b": {
                        "enabled": True,
                        "connection_mode": "external",
                        "napcat_url": "http://bot-b:6099",
                        "ws_url": "ws://bot-b:3002",
                    },
                },
            }
        },
    )
    admin_a.channels_writer = _Writer(admin_a)
    qq_admin = QqInstanceAdminService(admin_a)
    admin_a.qq_instance_admin = qq_admin

    state = configure_admin_auth(AdminBState(data_dir=tmp_path))
    state.qq_instance_admin = qq_admin
    admin_b_state.set_admin_state(state)

    async def fake_ensure(client: Any, desired: dict[str, Any]) -> bool:
        _FakeNapcatClient.ensures.append((client.base_url, desired))
        return True

    monkeypatch.setattr(napcat_instances, "_NapcatClient", _FakeNapcatClient)
    monkeypatch.setattr(
        napcat_instances,
        "_ensure_onebot_websocket_server",
        fake_ensure,
    )

    app = FastAPI()
    app.include_router(napcat_instances.router())
    client = authenticated_test_client(app)
    try:
        yield admin_a, state, client
    finally:
        admin_b_state.set_admin_state(None)


def test_attempts_are_bound_to_exact_instance(setup) -> None:
    _admin_a, _state, client = setup

    first = client.post("/admin/channels/qq/instances/bot-a/login-attempts")
    second = client.post("/admin/channels/qq/instances/bot-b/login-attempts")

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    a = first.json()
    b = second.json()
    assert a["attempt_id"] != b["attempt_id"]
    assert a["qrcode_url"].endswith("bot-a:6099")
    assert b["qrcode_url"].endswith("bot-b:6099")
    wrong = client.get(
        f"/admin/channels/qq/instances/bot-b/login-attempts/{a['attempt_id']}"
    )
    assert wrong.status_code == 404


def test_new_attempt_supersedes_prior_instance_attempt(setup) -> None:
    _admin_a, _state, client = setup
    first = client.post("/admin/channels/qq/instances/bot-a/login-attempts").json()
    second = client.post("/admin/channels/qq/instances/bot-a/login-attempts").json()

    old = client.get(
        f"/admin/channels/qq/instances/bot-a/login-attempts/{first['attempt_id']}"
    )
    current = client.get(
        f"/admin/channels/qq/instances/bot-a/login-attempts/{second['attempt_id']}"
    )

    assert old.status_code == 200
    assert old.json()["status"] == "superseded"
    assert current.status_code == 200
    assert current.json()["status"] == "waiting"
    assert _FakeNapcatClient.qr_calls == 2


def test_attempt_store_keeps_only_token_digest(setup) -> None:
    _admin_a, state, client = setup
    created = client.post("/admin/channels/qq/instances/bot-a/login-attempts").json()
    token = created["attempt_id"]

    assert token not in repr(state.qq_login_attempts._rows)
    assert state.qq_login_attempts.get(
        "bot-a",
        token,
        owner="authorization:Basic YWRtaW46cm9vdHJvb3Q=",
    ) is not None


def test_confirmed_poll_ensures_exact_server_and_records_history(setup) -> None:
    admin_a, _state, client = setup
    created = client.post("/admin/channels/qq/instances/bot-b/login-attempts").json()
    _FakeNapcatClient.status = StatusOut(
        status="confirmed",
        account=QqAccount(uin="20002", nickname="Second", last_login_at=2),
    )

    polled = client.get(
        f"/admin/channels/qq/instances/bot-b/login-attempts/{created['attempt_id']}"
    )

    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "confirmed"
    assert _FakeNapcatClient.ensures[0][0] == "http://bot-b:6099"
    assert _FakeNapcatClient.ensures[0][1]["name"] == "corlinman-bot-b"
    assert _FakeNapcatClient.ensures[0][1]["port"] == 3002
    history = client.get("/admin/channels/qq/instances/bot-b/accounts")
    assert history.status_code == 200
    assert history.json()["accounts"][0]["uin"] == "20002"
    assert (admin_a.data_dir / "qq-accounts" / "bot-b.json").exists()


def test_confirmed_poll_is_terminal_and_side_effects_run_once(setup) -> None:
    _admin_a, _state, client = setup
    created = client.post("/admin/channels/qq/instances/bot-b/login-attempts").json()
    _FakeNapcatClient.status = StatusOut(
        status="confirmed",
        account=QqAccount(uin="20002", nickname="Second", last_login_at=2),
    )
    path = (
        f"/admin/channels/qq/instances/bot-b/login-attempts/{created['attempt_id']}"
    )

    assert client.get(path).status_code == 200
    assert client.get(path).status_code == 200

    assert len(_FakeNapcatClient.ensures) == 1
    assert _FakeNapcatClient.qr_calls == 1


def test_quick_login_targets_requested_instance(setup) -> None:
    _admin_a, _state, client = setup

    response = client.post(
        "/admin/channels/qq/instances/bot-b/quick-login",
        json={"uin": "20002"},
    )

    assert response.status_code == 200, response.text
    assert _FakeNapcatClient.quick_calls == ["20002"]
    assert _FakeNapcatClient.ensures[0][0] == "http://bot-b:6099"


def test_singleton_login_aliases_resolve_explicit_default(setup) -> None:
    _admin_a, _state, _client = setup
    app = FastAPI()
    app.include_router(napcat.router())
    app.include_router(napcat_instances.router())
    client = authenticated_test_client(app)

    created = client.post("/admin/channels/qq/qrcode")
    assert created.status_code == 200, created.text
    assert set(created.json()) == {
        "token",
        "image_base64",
        "qrcode_url",
        "expires_at",
    }
    token = created.json()["token"]
    assert token

    polled = client.get(
        "/admin/channels/qq/qrcode/status",
        params={"token": token},
    )
    assert polled.status_code == 200, polled.text
    assert polled.json() == {"status": "waiting", "account": None, "message": None}

    accounts = client.get("/admin/channels/qq/accounts")
    assert accounts.status_code == 200

    quick = client.post(
        "/admin/channels/qq/quick-login",
        json={"uin": "10001"},
    )
    assert quick.status_code == 200
    assert _FakeNapcatClient.ensures[-1][0] == "http://bot-a:6099"


def test_singleton_alias_fails_closed_for_empty_canonical_fleet(setup) -> None:
    admin_a, _state, _client = setup
    admin_a.channels_config["qq"] = {"instances": {}}
    app = FastAPI()
    app.include_router(napcat.router())
    app.include_router(napcat_instances.router())
    client = authenticated_test_client(app)

    response = client.post("/admin/channels/qq/qrcode")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "default_instance_not_configured"
    assert _FakeNapcatClient.qr_calls == 0


def test_revoke_attempt_is_exact_and_idempotent_failure(setup) -> None:
    _admin_a, _state, client = setup
    created = client.post("/admin/channels/qq/instances/bot-a/login-attempts").json()
    path = (
        f"/admin/channels/qq/instances/bot-a/login-attempts/{created['attempt_id']}"
    )

    assert client.delete(path).status_code == 204
    assert client.delete(path).status_code == 404


def test_disabled_managed_instance_is_provisioned_for_login(setup) -> None:
    admin_a, _state, client = setup
    manager = _ManagedManager()
    admin_a.channels_config["qq"]["instances"]["bot-b"] = {
        "enabled": False,
        "connection_mode": "managed",
    }
    admin_a.napcat_manager = manager

    response = client.post("/admin/channels/qq/instances/bot-b/login-attempts")

    assert response.status_code == 201, response.text
    assert manager.requests[:2] == [("inspect", "bot-b"), ("provision", "bot-b")]


def test_default_managed_instance_attempts_adoption_before_provision(setup) -> None:
    admin_a, _state, client = setup
    manager = _ManagedManager()
    admin_a.channels_config["qq"]["instances"]["default"] = {
        "enabled": False,
        "connection_mode": "managed",
    }
    admin_a.channels_config["qq"]["default_instance"] = "default"
    admin_a.napcat_manager = manager

    response = client.post("/admin/channels/qq/instances/default/login-attempts")

    assert response.status_code == 201, response.text
    assert manager.requests[:2] == [("inspect", "default"), ("adopt", "default")]


def test_default_managed_instance_provisions_only_when_adoption_is_absent(setup) -> None:
    admin_a, _state, client = setup

    class _NoLegacyManager(_ManagedManager):
        async def request(
            self, operation: str, instance_id: str, **kwargs: object
        ):
            if operation == "adopt":
                from corlinman_server.system.napcat_manager.models import ManagerResponse

                self.requests.append((operation, instance_id))
                return ManagerResponse(
                    ok=False,
                    request_id=operation,
                    error_code="instance_not_found",
                )
            return await super().request(operation, instance_id, **kwargs)

    manager = _NoLegacyManager()
    admin_a.channels_config["qq"]["instances"]["default"] = {
        "enabled": False,
        "connection_mode": "managed",
    }
    admin_a.channels_config["qq"]["default_instance"] = "default"
    admin_a.napcat_manager = manager

    response = client.post("/admin/channels/qq/instances/default/login-attempts")

    assert response.status_code == 201, response.text
    assert manager.requests[:3] == [
        ("inspect", "default"),
        ("adopt", "default"),
        ("provision", "default"),
    ]


def test_managed_endpoint_does_not_provision_on_manager_failure(setup) -> None:
    admin_a, _state, client = setup

    class _UnavailableManager(_ManagedManager):
        async def request(
            self, operation: str, instance_id: str, **_kwargs: object
        ):
            from corlinman_server.system.napcat_manager.models import ManagerResponse

            self.requests.append((operation, instance_id))
            return ManagerResponse(
                ok=False,
                request_id=operation,
                error_code="manager_unavailable",
            )

    manager = _UnavailableManager()
    admin_a.channels_config["qq"]["instances"]["bot-a"] = {
        "enabled": False,
        "connection_mode": "managed",
    }
    admin_a.napcat_manager = manager

    response = client.post("/admin/channels/qq/instances/bot-a/login-attempts")

    assert response.status_code == 503
    assert manager.requests == [("inspect", "bot-a")]


def test_attempt_fails_closed_after_managed_runtime_replacement(setup) -> None:
    admin_a, _state, client = setup
    manager = _ManagedManager()
    manager.provisioned = True
    admin_a.channels_config["qq"]["instances"]["bot-a"] = {
        "enabled": False,
        "connection_mode": "managed",
    }
    admin_a.napcat_manager = manager
    created = client.post(
        "/admin/channels/qq/instances/bot-a/login-attempts"
    ).json()
    manager.generation = 2

    response = client.get(
        f"/admin/channels/qq/instances/bot-a/login-attempts/{created['attempt_id']}"
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "login_attempt_stale"
    assert _FakeNapcatClient.ensures == []


def test_napcat_errors_are_redacted_from_instance_routes(
    setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _admin_a, _state, client = setup
    secret = "http://user:password@private-napcat:6099/private?token=secret"

    class _FailingNapcatClient(_FakeNapcatClient):
        async def request_qrcode(self) -> QrcodeOut:
            raise NapcatError("napcat_unreachable", secret, status=418)

        async def quick_login(self, uin: str) -> StatusOut:
            del uin
            raise NapcatError("napcat_upstream_error", secret, status=418)

    monkeypatch.setattr(napcat_instances, "_NapcatClient", _FailingNapcatClient)

    create = client.post("/admin/channels/qq/instances/bot-a/login-attempts")
    quick = client.post(
        "/admin/channels/qq/instances/bot-a/quick-login",
        json={"uin": "10001"},
    )

    assert create.status_code == 503
    assert create.json()["detail"] == {
        "error": "napcat_unreachable",
        "message": "NapCat is unreachable",
    }
    assert quick.status_code == 502
    assert quick.json()["detail"] == {
        "error": "napcat_upstream_error",
        "message": "NapCat request failed",
    }
    assert secret not in create.text
    assert secret not in quick.text


def test_instance_router_does_not_register_canonical_diagnostics_twice(setup) -> None:
    _admin_a, _state, _client = setup
    app = FastAPI()
    app.include_router(napcat_instances.router())

    paths = [
        route.path
        for route in app.routes
        if route.path
        == "/admin/channels/qq/instances/{instance_id}/napcat/diagnostics"
    ]

    assert paths == []


def test_changing_default_does_not_mix_account_history(tmp_path: Path) -> None:
    legacy = tmp_path / "qq-accounts.json"
    legacy.write_text("[]", encoding="utf-8")
    state = AdminAState(data_dir=tmp_path)

    assert _accounts_path_for_instance(
        state,
        "default",
        default_instance=True,
    ) == legacy
    assert _accounts_path_for_instance(
        state,
        "bot-a",
        default_instance=True,
    ) == tmp_path / "qq-accounts" / "bot-a.json"


def test_terminal_attempt_does_not_block_lifecycle_or_destroy_cache(setup) -> None:
    _admin_a, state, client = setup
    created = client.post("/admin/channels/qq/instances/bot-a/login-attempts").json()
    row = state.qq_login_attempts.get(
        "bot-a",
        created["attempt_id"],
        owner="authorization:Basic YWRtaW46cm9vdHJvb3Q=",
    )
    assert row is not None
    row.status = "confirmed"
    row.account = QqAccount(uin="10001", nickname="Bot", last_login_at=1)

    assert state.qq_login_attempts.has_instance("bot-a") is False
    cached = client.get(
        f"/admin/channels/qq/instances/bot-a/login-attempts/{created['attempt_id']}"
    )
    assert cached.status_code == 200
    assert cached.json()["status"] == "confirmed"
    assert cached.json()["account"]["uin"] == "10001"


def test_quick_login_supersedes_active_qr_attempt(setup) -> None:
    _admin_a, _state, client = setup
    created = client.post("/admin/channels/qq/instances/bot-a/login-attempts").json()

    quick = client.post(
        "/admin/channels/qq/instances/bot-a/quick-login",
        json={"uin": "10001"},
    )
    old = client.get(
        f"/admin/channels/qq/instances/bot-a/login-attempts/{created['attempt_id']}"
    )

    assert quick.status_code == 200
    assert old.status_code == 200
    assert old.json()["status"] == "superseded"
    assert old.json()["message"] == "quick login completed for this QQ instance"
    assert len(_FakeNapcatClient.ensures) == 1


def test_confirmed_login_rejects_configured_uin_mismatch(setup) -> None:
    admin_a, _state, client = setup
    admin_a.channels_config["qq"]["instances"]["bot-a"]["expected_uin"] = 10001
    created = client.post("/admin/channels/qq/instances/bot-a/login-attempts").json()
    _FakeNapcatClient.status = StatusOut(
        status="confirmed",
        account=QqAccount(uin="20002", nickname="Wrong", last_login_at=2),
    )

    response = client.get(
        f"/admin/channels/qq/instances/bot-a/login-attempts/{created['attempt_id']}"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error": "identity_mismatch",
        "message": "the logged-in QQ account does not match this instance",
        "expected_uin": 10001,
    }
    assert _FakeNapcatClient.ensures == []
    assert _FakeNapcatClient.quick_calls == []
    assert not (admin_a.data_dir / "qq-accounts" / "bot-a.json").exists()
