from __future__ import annotations

import base64
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_a import AdminState, build_router, set_admin_state
from corlinman_server.gateway.routes_admin_a._session_store import AdminSessionStore
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from corlinman_server.gateway.routes_admin_b.infra._scheduler_lib import (
    NewJobBody,
    _store_job,
)
from corlinman_server.gateway.routes_admin_b.state import AdminState as SchedulerState
from corlinman_server.scheduler.builtins.qzone_daily import QZONE_DAILY_BUILTIN_NAME
from corlinman_server.system.napcat_manager.models import (
    ManagerResponse,
    NapCatObservedState,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _auth() -> str:
    encoded = base64.b64encode(b"admin:rootroot").decode("ascii")
    return f"Basic {encoded}"


class _Writer:
    def __init__(self, state: AdminState) -> None:
        self.state = state

    async def __call__(self, channels: dict[str, Any]) -> None:
        self.state.channels_config = deepcopy(channels)

    async def mutate(self, mutator: Any) -> Any:
        candidate, result = mutator(deepcopy(self.state.channels_config or {}), {})
        await self(candidate)
        return result


class _Manager:
    def __init__(self) -> None:
        self.retained: set[str] = set()
        self.requests: list[tuple[str, str, int | None]] = []
        self.fail: set[str] = set()

    async def request(
        self,
        operation: str,
        instance_id: str,
        *,
        generation: int | None = None,
        **_kwargs: object,
    ):
        self.requests.append((operation, instance_id, generation))
        if operation not in {"inspect", "provision", "adopt"} and generation != 1:
            return ManagerResponse(
                ok=False,
                request_id=operation,
                error_code="generation_conflict",
            )
        if operation in self.fail:
            return ManagerResponse(
                ok=False,
                request_id=operation,
                error_code="manager_failed",
            )
        if operation == "remove_runtime":
            self.retained.add(instance_id)
        elif operation == "restore":
            if instance_id not in self.retained:
                return ManagerResponse(
                    ok=False,
                    request_id=operation,
                    error_code="resource_not_owned",
                )
            self.retained.remove(instance_id)
        elif operation == "purge_login_state":
            if instance_id not in self.retained:
                return ManagerResponse(
                    ok=False,
                    request_id=operation,
                    error_code="resource_not_owned",
                )
            self.retained.remove(instance_id)
        return ManagerResponse(
            ok=True,
            request_id=operation,
            observed=NapCatObservedState(
                instance_id,
                "native",
                1,
                "retained" if instance_id in self.retained else "running",
                retained=instance_id in self.retained,
            ),
        )


@pytest.fixture()
def setup(tmp_path: Path):
    scheduler = SchedulerState(data_dir=tmp_path)
    (tmp_path / "chat-history-marker").write_text("keep", encoding="utf-8")
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
                        "connection_mode": "managed",
                        "humanlike": {"enabled": True, "persona_id": "grantley"},
                    },
                    "bot-b": {
                        "display_name": "Bot B",
                        "enabled": True,
                        "connection_mode": "managed",
                    },
                },
            }
        },
        scheduler_admin_state=scheduler,
        napcat_manager=_Manager(),
    )
    state.channels_writer = _Writer(state)
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app, headers={"Authorization": _auth()}) as client:
        yield state, scheduler, client
    set_admin_state(None)


def _add_reference(scheduler: SchedulerState, instance_id: str) -> None:
    _store_job(
        scheduler,
        NewJobBody(
            name=f"{instance_id}.grantley.daily_qzone",
            cron="0 9 * * *",
            action_type=QZONE_DAILY_BUILTIN_NAME,
            persona_id="grantley",
            prompt_template="post",
            qq_instance_id=instance_id,
        ),
    )


def test_delete_is_blocked_by_qzone_reference(setup) -> None:
    state, scheduler, client = setup
    scheduler.config_loader = lambda: {"channels": state.channels_config}
    _add_reference(scheduler, "bot-a")

    impact = client.get("/admin/channels/qq/instances/bot-a/deletion-impact")
    assert impact.status_code == 200
    assert impact.json()["can_delete"] is False

    deleted = client.request(
        "DELETE",
        "/admin/channels/qq/instances/bot-a",
        json={"new_default": "bot-b"},
    )
    assert deleted.status_code == 409
    assert deleted.json()["detail"]["error"] == "qq_instance_referenced"
    assert state.napcat_manager.requests == []


def test_migrate_then_delete_preserves_config_and_login_state(setup) -> None:
    state, scheduler, client = setup
    scheduler.config_loader = lambda: {"channels": state.channels_config}
    _add_reference(scheduler, "bot-a")

    migrated = client.post(
        "/admin/channels/qq/instances/bot-a/migrate",
        json={"target_instance_id": "bot-b"},
    )
    assert migrated.status_code == 200, migrated.text
    assert migrated.json()["jobs"][0]["qq_instance_id"] == "bot-b"

    deleted = client.request(
        "DELETE",
        "/admin/channels/qq/instances/bot-a",
        json={"new_default": "bot-b"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["retained"] is True
    assert "bot-a" not in state.channels_config["qq"]["instances"]
    assert state.channels_config["qq"]["default_instance"] == "bot-b"
    assert state.napcat_manager.retained == {"bot-a"}

    retained = state.data_dir / "qq-instances-retained.json"
    text = retained.read_text(encoding="utf-8")
    assert "humanlike" in text
    assert retained.stat().st_mode & 0o777 == 0o600


def test_restore_recovers_settings_and_runtime(setup) -> None:
    state, _scheduler, client = setup
    deleted = client.request(
        "DELETE",
        "/admin/channels/qq/instances/bot-a",
        json={"new_default": "bot-b"},
    )
    assert deleted.status_code == 200

    restored = client.post(
        "/admin/channels/qq/instances/bot-a/restore",
        json={"make_default": True},
    )

    assert restored.status_code == 200, restored.text
    assert restored.json()["is_default"] is True
    assert ("restore", "bot-a", 1) in state.napcat_manager.requests
    values = state.channels_config["qq"]["instances"]["bot-a"]
    assert values["humanlike"] == {"enabled": True, "persona_id": "grantley"}
    assert state.napcat_manager.retained == set()
    assert not (state.data_dir / "qq-instances-retained.json").exists()


def test_purge_requires_exact_confirmation_and_preserves_history_contract(setup) -> None:
    state, _scheduler, client = setup
    assert client.request(
        "DELETE",
        "/admin/channels/qq/instances/bot-a",
        json={"new_default": "bot-b"},
    ).status_code == 200

    mismatch = client.post(
        "/admin/channels/qq/instances/bot-a/purge",
        json={"confirm_instance_id": "bot-b"},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["error"] == "purge_confirmation_mismatch"

    purged = client.post(
        "/admin/channels/qq/instances/bot-a/purge",
        json={"confirm_instance_id": "bot-a"},
    )
    assert purged.status_code == 200, purged.text
    assert purged.json()["purged"] == ["login_state", "manager_credentials"]
    assert purged.json()["preserved"] == ["chat", "memory", "audit"]
    assert ("purge_login_state", "bot-a", 1) in state.napcat_manager.requests
    assert state.napcat_manager.retained == set()
    assert (state.data_dir / "chat-history-marker").read_text(encoding="utf-8") == "keep"


def test_external_delete_restore_never_calls_manager(setup) -> None:
    state, _scheduler, client = setup
    state.channels_config["qq"]["instances"]["bot-a"]["connection_mode"] = "external"

    deleted = client.request(
        "DELETE",
        "/admin/channels/qq/instances/bot-a",
        json={"new_default": "bot-b"},
    )
    restored = client.post(
        "/admin/channels/qq/instances/bot-a/restore",
        json={"make_default": False},
    )

    assert deleted.status_code == 200
    assert deleted.json()["retained"] is False
    assert restored.status_code == 200, restored.text
    assert state.channels_config["qq"]["instances"]["bot-a"]["connection_mode"] == "external"
    assert state.napcat_manager.requests == []


def test_external_purge_is_rejected_without_manager_call(setup) -> None:
    state, _scheduler, client = setup
    state.channels_config["qq"]["instances"]["bot-a"]["connection_mode"] = "external"
    assert client.request(
        "DELETE",
        "/admin/channels/qq/instances/bot-a",
        json={"new_default": "bot-b"},
    ).status_code == 200

    purged = client.post(
        "/admin/channels/qq/instances/bot-a/purge",
        json={"confirm_instance_id": "bot-a"},
    )

    assert purged.status_code == 409
    assert purged.json()["detail"]["error"] == "external_instance_not_owned"
    assert state.napcat_manager.requests == []


def test_manager_inspect_failure_performs_no_mutation(setup) -> None:
    state, _scheduler, client = setup
    state.napcat_manager.fail.add("inspect")

    deleted = client.request(
        "DELETE",
        "/admin/channels/qq/instances/bot-a",
        json={"new_default": "bot-b"},
    )

    assert deleted.status_code == 409
    assert state.napcat_manager.requests == [("inspect", "bot-a", None)]
    assert "bot-a" in state.channels_config["qq"]["instances"]
    assert not (state.data_dir / "qq-instances-retained.json").exists()


def test_manager_failure_leaves_config_active(setup) -> None:
    state, _scheduler, client = setup
    state.napcat_manager.fail.add("remove_runtime")

    deleted = client.request(
        "DELETE",
        "/admin/channels/qq/instances/bot-a",
        json={"new_default": "bot-b"},
    )

    assert deleted.status_code == 409
    assert "bot-a" in state.channels_config["qq"]["instances"]
    assert not (state.data_dir / "qq-instances-retained.json").exists()
