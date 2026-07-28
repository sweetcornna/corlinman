from __future__ import annotations

import asyncio
import json

import pytest
from corlinman_server.gateway.qq_instances.runtime import (
    QqIdentityRegistry,
    QqRuntimeRegistry,
)
from corlinman_server.system.napcat_manager.models import (
    ManagerResponse,
    NapCatDescriptor,
    NapCatObservedState,
)


class FakeManager:
    def __init__(
        self,
        *,
        fail_bind: bool = False,
        bind_gate: asyncio.Event | None = None,
    ) -> None:
        self.requests: list[tuple[str, str, dict[str, object]]] = []
        self.fail_bind = fail_bind
        self.bind_gate = bind_gate

    async def request(self, operation: str, instance_id: str, **kwargs: object) -> ManagerResponse:
        self.requests.append((operation, instance_id, kwargs))
        if operation == "bind_uin" and self.bind_gate is not None:
            await self.bind_gate.wait()
        if operation == "bind_uin" and self.fail_bind:
            return ManagerResponse(
                ok=False,
                request_id="bind_uin",
                error_code="instance_conflict",
            )
        if operation == "inspect" and not any(
            op in {"adopt", "provision"} and identity == instance_id
            for op, identity, _ in self.requests
        ):
            return ManagerResponse(
                ok=False,
                request_id="inspect",
                error_code="instance_not_found",
            )
        if operation == "adopt":
            return ManagerResponse(
                ok=False,
                request_id="adopt",
                error_code="instance_not_found",
            )
        return ManagerResponse(
            ok=True,
            request_id=operation,
            observed=NapCatObservedState(instance_id, "native", 1, "running"),
            descriptor=NapCatDescriptor(
                instance_id,
                1,
                f"ws://{instance_id}:3001",
                f"http://{instance_id}:6099",
                "onebot-secret",
                "webui-secret",
            ),
        )


async def _fake_run(params: object, cancel: asyncio.Event) -> None:
    await cancel.wait()


def _fleet(*ids: str, managed: bool = True) -> dict[str, object]:
    return {
        "qq": {
            "default_instance": ids[0] if ids else "",
            "instances": {
                identity: {
                    "enabled": True,
                    "connection_mode": "managed" if managed else "external",
                    **(
                        {}
                        if managed
                        else {
                            "ws_url": f"ws://{identity}:3001",
                            "access_token": f"token-{identity}",
                        }
                    ),
                }
                for identity in ids
            },
        }
    }


def test_identity_registry_rejects_duplicate_and_mismatch() -> None:
    registry = QqIdentityRegistry()

    assert registry.verify("a", 100, 100)
    assert not registry.verify("b", 100, None)
    assert not registry.verify("a", 200, 100)
    registry.release("a")
    assert registry.verify("b", 100, None)


@pytest.mark.asyncio
async def test_reconcile_starts_and_stops_independent_instances() -> None:
    manager = FakeManager()
    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=_fake_run,
    )

    await registry.reconcile(_fleet("a", "b"))
    await asyncio.sleep(0)

    assert set(registry.handles()) == {"a", "b"}
    operations = [request[:2] for request in manager.requests]
    assert operations.count(("adopt", "a")) == 1
    assert operations.count(("provision", "a")) == 1
    assert operations.count(("adopt", "b")) == 0
    assert operations.count(("provision", "b")) == 1

    await registry.reconcile(_fleet("b"))
    assert set(registry.handles()) == {"b"}
    await registry.stop_all()


@pytest.mark.asyncio
async def test_explicit_inspect_failure_never_falls_back_to_another_operation() -> None:
    manager = FakeManager()

    async def fail_inspect(
        operation: str, instance_id: str, **kwargs: object
    ) -> ManagerResponse:
        manager.requests.append((operation, instance_id, kwargs))
        return ManagerResponse(
            ok=False,
            request_id=operation,
            error_code="manager_unavailable",
        )

    manager.request = fail_inspect  # type: ignore[method-assign]
    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=_fake_run,
    )

    with pytest.raises(RuntimeError, match="manager_unavailable"):
        await registry.reconcile(_fleet("a"))

    assert [request[:2] for request in manager.requests] == [("inspect", "a")]


@pytest.mark.asyncio
async def test_managed_start_failure_is_a_reconcile_failure() -> None:
    manager = FakeManager()
    manager.fail_bind = False

    async def fail_request(operation: str, instance_id: str, **kwargs: object) -> ManagerResponse:
        manager.requests.append((operation, instance_id, kwargs))
        return ManagerResponse(
            ok=False,
            request_id=operation,
            error_code="manager_unavailable",
        )

    manager.request = fail_request  # type: ignore[method-assign]
    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=_fake_run,
    )

    with pytest.raises(RuntimeError, match="could not be started"):
        await registry.reconcile(_fleet("a"))

    assert registry.handles() == {}


@pytest.mark.asyncio
async def test_failed_changed_instance_restores_entire_previous_fleet(tmp_path) -> None:
    sidecar = tmp_path / "py-config.json"
    manager = FakeManager()
    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=_fake_run,
    )
    first = _fleet("a", "b")
    await registry.reconcile_and_write_sidecar(
        first,
        config={"channels": first},
        path=sidecar,
    )
    previous = {identity: handle.fingerprint for identity, handle in registry.handles().items()}
    registry.handles()["a"].transport["expected_uin"] = "10001"
    await registry.write_sidecar(config={"channels": first}, path=sidecar)
    assert json.loads(sidecar.read_text())["qq_onebot_instances"]["a"]
    real_request = manager.request
    failures_remaining = 1

    async def fail_b(operation: str, instance_id: str, **kwargs: object) -> ManagerResponse:
        nonlocal failures_remaining
        if instance_id == "b" and operation == "inspect" and failures_remaining > 0:
            failures_remaining -= 1
            return ManagerResponse(
                ok=False,
                request_id=operation,
                error_code="manager_unavailable",
            )
        return await real_request(operation, instance_id, **kwargs)

    manager.request = fail_b  # type: ignore[method-assign]
    changed = _fleet("a", "b")
    # Transport-level edits — behavior-only keys would hot-apply without
    # touching the manager and never trip the injected failure.
    changed["qq"]["instances"]["a"]["access_token"] = "rotated-a"  # type: ignore[index]
    changed["qq"]["instances"]["b"]["access_token"] = "rotated-b"  # type: ignore[index]

    with pytest.raises(RuntimeError, match="could not be started"):
        await registry.reconcile(changed)

    assert {
        identity: handle.fingerprint for identity, handle in registry.handles().items()
    } == previous
    assert registry.handles()["a"].transport["expected_uin"] is None
    assert registry.sidecar_transports() == {}
    assert json.loads(sidecar.read_text())["qq_onebot_instances"] == {}
    await registry.stop_all()


@pytest.mark.asyncio
async def test_external_restart_replaces_cancelled_runtime() -> None:
    seen: list[object] = []

    async def fake_run(params: object, cancel: asyncio.Event) -> None:
        seen.append(params)
        await cancel.wait()

    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        run_qq=fake_run,
    )
    await registry.reconcile(_fleet("a", managed=False))
    await asyncio.sleep(0)
    old_task = registry.handles()["a"].task

    assert await registry.restart("a") is True
    for _ in range(10):
        if len(seen) == 2:
            break
        await asyncio.sleep(0)

    new_handle = registry.handles()["a"]
    assert new_handle.task is not old_task
    assert not new_handle.task.done()
    assert len(seen) == 2
    await registry.stop_all()


@pytest.mark.asyncio
async def test_default_change_rebinds_compatibility_health() -> None:
    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        run_qq=_fake_run,
    )
    first = _fleet("a", "b", managed=False)
    await registry.reconcile(first)
    old_a = registry.handles()["a"].task
    old_b = registry.handles()["b"].task

    changed = _fleet("a", "b", managed=False)
    changed["qq"]["default_instance"] = "b"  # type: ignore[index]
    await registry.reconcile(changed)

    assert registry.handles()["a"].task is not old_a
    assert registry.handles()["b"].task is not old_b
    assert registry.handles()["a"].default_instance is False
    assert registry.handles()["b"].default_instance is True
    await registry.stop_all()


@pytest.mark.asyncio
async def test_behavior_change_hot_applies_without_restart() -> None:
    """A behavior-only edit (reply policy) mutates the RUNNING channel's
    config dict in place — no restart, no WS drop. This is what makes an
    admin save of keywords / policy apply without disconnecting the bot."""
    manager = FakeManager()
    captured: list[object] = []

    async def fake_run(params: object, cancel: asyncio.Event) -> None:
        captured.append(params)
        await cancel.wait()

    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=fake_run,
    )
    config = _fleet("a", "b")
    await registry.reconcile(config)
    for _ in range(10):
        if len(captured) >= 2:
            break
        await asyncio.sleep(0)
    before_a = registry.handles()["a"].task
    before_b = registry.handles()["b"].task
    before_fp = registry.handles()["a"].fingerprint
    live = registry.handles()["a"].live_config
    assert live is not None
    # The channel task reads the SAME dict object the handle tracks.
    assert any(getattr(p, "config", None) is live for p in captured)

    instances = config["qq"]["instances"]  # type: ignore[index]
    instances["a"]["group_reply_policy"] = "all"  # type: ignore[index]
    await registry.reconcile(config)

    handle_a = registry.handles()["a"]
    assert handle_a.task is before_a  # NOT restarted
    assert registry.handles()["b"].task is before_b
    assert handle_a.fingerprint != before_fp  # bookkeeping advanced
    assert live["group_reply_policy"] == "all"  # applied in place
    # Managed-descriptor transports injected at start survive the swap.
    assert live["ws_url"] == "ws://a:3001"
    await registry.stop_all()


@pytest.mark.asyncio
async def test_monitors_change_hot_applies_without_restart() -> None:
    """The monitor rule list is a behavior key — editing it hot-applies
    into the running instance (the resident digest loop re-reads it)."""
    manager = FakeManager()
    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=_fake_run,
    )
    config = _fleet("a")
    await registry.reconcile(config)
    before = registry.handles()["a"].task

    instances = config["qq"]["instances"]  # type: ignore[index]
    instances["a"]["monitors"] = [  # type: ignore[index]
        {
            "id": "m1",
            "sources": [{"group": "123", "focus_user_ids": ["7"]}],
            "schedule_type": "interval",
            "interval_minutes": 60,
            "target_type": "group",
            "target_id": "456",
        }
    ]
    await registry.reconcile(config)
    handle = registry.handles()["a"]
    assert handle.task is before  # hot-applied, not restarted
    live = handle.live_config
    assert live is not None and live["monitors"][0]["id"] == "m1"
    await registry.stop_all()


@pytest.mark.asyncio
async def test_transport_change_still_restarts() -> None:
    """Transport-level keys (access_token / ws_url) keep restart
    semantics — and so does any UNKNOWN key (fail-safe default)."""
    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        run_qq=_fake_run,
    )
    config = _fleet("a", managed=False)
    await registry.reconcile(config)
    before = registry.handles()["a"].task

    instances = config["qq"]["instances"]  # type: ignore[index]
    instances["a"]["access_token"] = "rotated"  # type: ignore[index]
    await registry.reconcile(config)
    after_transport = registry.handles()["a"].task
    assert after_transport is not before

    instances["a"]["some_future_snapshot_key"] = 1  # type: ignore[index]
    await registry.reconcile(config)
    assert registry.handles()["a"].task is not after_transport
    await registry.stop_all()


@pytest.mark.asyncio
async def test_reconcile_writes_managed_transport_after_identity_is_bound(
    tmp_path, monkeypatch
) -> None:
    sidecar = tmp_path / "py-config.json"
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(sidecar))
    manager = FakeManager()
    seen: list[object] = []

    async def fake_run(params: object, cancel: asyncio.Event) -> None:
        seen.append(params)
        await cancel.wait()

    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=fake_run,
    )
    channels = _fleet("a")
    await registry.reconcile_and_write_sidecar(
        channels,
        config={"channels": channels},
        path=sidecar,
    )
    assert json.loads(sidecar.read_text())["qq_onebot_instances"] == {}

    for _ in range(10):
        if seen:
            break
        await asyncio.sleep(0)
    params = seen[0]
    assert params.identity_guard(10001) is None  # type: ignore[attr-defined]
    assert params.identity_ready() is False  # type: ignore[attr-defined]
    for _ in range(20):
        rendered = json.loads(sidecar.read_text())
        if rendered["qq_onebot_instances"]:
            break
        await asyncio.sleep(0)

    assert params.identity_ready() is True  # type: ignore[attr-defined]
    assert rendered["qq_onebot_instances"]["a"]["expected_uin"] == "10001"
    await registry.stop_all()


@pytest.mark.asyncio
async def test_runtime_params_use_private_descriptor_without_logging_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = FakeManager()
    seen: list[object] = []

    async def fake_run(params: object, cancel: asyncio.Event) -> None:
        seen.append(params)
        await cancel.wait()

    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=fake_run,
    )
    await registry.reconcile(_fleet("a"))
    for _ in range(10):
        if seen:
            break
        await asyncio.sleep(0)

    assert seen
    params = seen[0]
    assert params.config["ws_url"] == "ws://a:3001"  # type: ignore[attr-defined]
    assert params.config["access_token"] == "onebot-secret"  # type: ignore[attr-defined]
    assert registry.sidecar_transports() == {}
    assert "onebot-secret" not in caplog.text

    handle = registry.handles()["a"]
    assert handle.transport["expected_uin"] is None
    assert params.identity_guard(10001) is None  # type: ignore[attr-defined]
    for _ in range(10):
        if registry.handles()["a"].transport["expected_uin"] == "10001":
            break
        await asyncio.sleep(0)
    assert params.identity_ready() is True  # type: ignore[attr-defined]
    assert registry.sidecar_transports()["a"]["expected_uin"] == "10001"
    await registry.stop_all()


@pytest.mark.asyncio
async def test_external_identity_is_published_before_becoming_ready(tmp_path) -> None:
    sidecar = tmp_path / "external.json"
    seen: list[object] = []

    async def fake_run(params: object, cancel: asyncio.Event) -> None:
        seen.append(params)
        await cancel.wait()

    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        run_qq=fake_run,
    )
    channels = _fleet("a", managed=False)
    await registry.reconcile_and_write_sidecar(
        channels,
        config={"channels": channels},
        path=sidecar,
    )
    assert json.loads(sidecar.read_text())["qq_onebot_instances"] == {}
    for _ in range(10):
        if seen:
            break
        await asyncio.sleep(0)

    params = seen[0]
    assert params.identity_guard(10001) is None  # type: ignore[attr-defined]
    for _ in range(20):
        if params.identity_ready():  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0)

    assert params.identity_ready() is True  # type: ignore[attr-defined]
    rendered = json.loads(sidecar.read_text())
    assert rendered["qq_onebot_instances"]["a"]["expected_uin"] == "10001"
    await registry.stop_all()


@pytest.mark.asyncio
async def test_configured_expected_uin_is_not_published_before_live_verification(
    tmp_path,
) -> None:
    sidecar = tmp_path / "expected.json"
    manager = FakeManager()
    seen: list[object] = []

    async def fake_run(params: object, cancel: asyncio.Event) -> None:
        seen.append(params)
        await cancel.wait()

    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=fake_run,
    )
    channels = _fleet("a")
    channels["qq"]["instances"]["a"]["expected_uin"] = "10001"  # type: ignore[index]
    await registry.reconcile_and_write_sidecar(
        channels,
        config={"channels": channels},
        path=sidecar,
    )

    assert registry.sidecar_transports() == {}
    assert json.loads(sidecar.read_text())["qq_onebot_instances"] == {}
    for _ in range(10):
        if seen:
            break
        await asyncio.sleep(0)
    params = seen[0]
    assert params.identity_guard(10001) is None  # type: ignore[attr-defined]
    for _ in range(20):
        if params.identity_ready():  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0)
    assert params.identity_ready() is True  # type: ignore[attr-defined]
    await registry.stop_all()


@pytest.mark.asyncio
async def test_stale_identity_task_cannot_publish_into_replacement_runtime(
    tmp_path,
) -> None:
    sidecar = tmp_path / "stale.json"
    bind_gate = asyncio.Event()
    manager = FakeManager(bind_gate=bind_gate)
    seen: list[object] = []

    async def fake_run(params: object, cancel: asyncio.Event) -> None:
        seen.append(params)
        await cancel.wait()

    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=fake_run,
    )
    first = _fleet("a")
    await registry.reconcile_and_write_sidecar(
        first,
        config={"channels": first},
        path=sidecar,
    )
    for _ in range(10):
        if seen:
            break
        await asyncio.sleep(0)
    old_params = seen[0]
    assert old_params.identity_guard(10001) is None  # type: ignore[attr-defined]
    for _ in range(10):
        if any(op == "bind_uin" for op, _identity, _kwargs in manager.requests):
            break
        await asyncio.sleep(0)

    replacement = _fleet("a")
    # Transport-level edit — a behavior-only key would hot-apply into the
    # SAME runtime and this test is about a REPLACED one.
    replacement["qq"]["instances"]["a"]["access_token"] = "rotated"  # type: ignore[index]
    reconcile_task = asyncio.create_task(registry.reconcile(replacement))
    await asyncio.sleep(0)
    bind_gate.set()
    await reconcile_task
    for _ in range(10):
        if len(seen) == 2:
            break
        await asyncio.sleep(0)

    assert registry.sidecar_transports() == {}
    assert json.loads(sidecar.read_text())["qq_onebot_instances"] == {}
    assert len(seen) == 2
    assert seen[1].identity_ready() is False  # type: ignore[attr-defined]
    await registry.stop_all()


@pytest.mark.asyncio
async def test_managed_identity_remains_blocked_when_manager_bind_fails(
    tmp_path,
) -> None:
    sidecar = tmp_path / "custom-py-config.json"
    manager = FakeManager(fail_bind=True)
    seen: list[object] = []

    async def fake_run(params: object, cancel: asyncio.Event) -> None:
        seen.append(params)
        await cancel.wait()

    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=fake_run,
    )
    channels = _fleet("a")
    await registry.reconcile_and_write_sidecar(
        channels,
        config={"channels": channels},
        path=sidecar,
    )
    for _ in range(10):
        if seen:
            break
        await asyncio.sleep(0)

    params = seen[0]
    assert params.identity_guard(10001) is None  # type: ignore[attr-defined]
    for _ in range(20):
        handle = registry.handles()["a"]
        if handle.health["account_last_error"] == "identity_bind_failed":
            break
        await asyncio.sleep(0)

    assert params.identity_ready() is False  # type: ignore[attr-defined]
    assert registry.sidecar_transports() == {}
    assert json.loads(sidecar.read_text())["qq_onebot_instances"] == {}
    await registry.stop_all()


@pytest.mark.asyncio
async def test_sidecar_preflight_failure_preserves_running_runtime(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corlinman_server.gateway.lifecycle import py_config

    sidecar = tmp_path / "py-config.json"
    manager = FakeManager()
    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=_fake_run,
    )
    first = _fleet("a")
    await registry.reconcile_and_write_sidecar(
        first,
        config={"channels": first},
        path=sidecar,
    )
    original_task = registry.handles()["a"].task
    original_body = sidecar.read_text()
    real_write = py_config.write_py_config_sync

    def fail_new_config(config, path, **kwargs) -> None:
        if config.get("revision") == "new":
            raise OSError("sidecar unavailable")
        real_write(config, path, **kwargs)

    monkeypatch.setattr(py_config, "write_py_config_sync", fail_new_config)
    replacement = _fleet("a")
    replacement["qq"]["instances"]["a"]["group_reply_policy"] = "all"  # type: ignore[index]

    with pytest.raises(OSError, match="sidecar unavailable"):
        await registry.reconcile_and_write_sidecar(
            replacement,
            config={"revision": "new", "channels": replacement},
            path=sidecar,
        )

    assert registry.handles()["a"].task is original_task
    assert sidecar.read_text() == original_body
    await registry.stop_all()


@pytest.mark.asyncio
async def test_sidecar_preflight_does_not_replace_live_verified_transport(
    tmp_path,
) -> None:
    sidecar = tmp_path / "py-config.json"
    manager = FakeManager()
    seen: list[object] = []

    async def fake_run(params: object, cancel: asyncio.Event) -> None:
        seen.append(params)
        await cancel.wait()

    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=fake_run,
    )
    first = _fleet("a")
    await registry.reconcile_and_write_sidecar(
        first,
        config={"channels": first},
        path=sidecar,
    )
    for _ in range(10):
        if seen:
            break
        await asyncio.sleep(0)
    seen[0].identity_guard(10001)  # type: ignore[attr-defined]
    for _ in range(20):
        if json.loads(sidecar.read_text())["qq_onebot_instances"]:
            break
        await asyncio.sleep(0)
    verified_body = sidecar.read_text()

    await registry.reconcile_and_write_sidecar(
        first,
        config={"channels": first},
        path=sidecar,
    )

    assert sidecar.read_text() == verified_body
    await registry.stop_all()


@pytest.mark.asyncio
async def test_identity_bind_rewrites_the_configured_sidecar_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "configured.json"
    unrelated = tmp_path / "unrelated.json"
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(unrelated))
    manager = FakeManager()
    seen: list[object] = []

    async def fake_run(params: object, cancel: asyncio.Event) -> None:
        seen.append(params)
        await cancel.wait()

    registry = QqRuntimeRegistry(
        model="gpt-test",
        chat_service=object(),
        manager=manager,  # type: ignore[arg-type]
        run_qq=fake_run,
    )
    channels = _fleet("a")
    await registry.reconcile_and_write_sidecar(
        channels,
        config={"channels": channels},
        path=configured,
    )
    for _ in range(10):
        if seen:
            break
        await asyncio.sleep(0)
    seen[0].identity_guard(10001)  # type: ignore[attr-defined]
    for _ in range(20):
        if json.loads(configured.read_text())["qq_onebot_instances"]:
            break
        await asyncio.sleep(0)

    assert json.loads(configured.read_text())["qq_onebot_instances"]["a"]
    assert not unrelated.exists()
    await registry.stop_all()
