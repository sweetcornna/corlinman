"""Tests for :class:`corlinman_server.system.upgrader.DockerUpgrader` (v2).

The rebuilt docker path hands the swap to a detached helper container —
the gateway process itself must NEVER stop/remove the ``corlinman``
container (the old self-destruct flaw). These tests drive the
orchestration against a fake docker client and assert the handoff
contract: request-file contents, helper launch args, status-file
mirroring, and cancellation semantics.

We never import the real ``docker`` package — the impl module defers
the import inside ``_make_client`` so test environments without docker
installed can still import everything. All tests inject a fake client
factory.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.system.upgrader import (
    DockerUpgrader,
    UpgradeAlreadyRunning,
    UpgradeStateStore,
)
from corlinman_server.system.upgrader import docker_upgrader as du_module
from corlinman_server.system.upgrader.docker_upgrader import (
    _DEFAULT_REPO,
    _HELPER_CONTAINER_NAME,
)


def test_default_repo_points_at_current_owner() -> None:
    """Guard against the pre-transfer GHCR namespace resurfacing: the repo
    moved ymylive → sweetcornna in 2026-05, and pulling the old namespace
    means every default-config one-click docker upgrade 404s."""
    assert _DEFAULT_REPO == "ghcr.io/sweetcornna/corlinman"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeContainer:
    """Stand-in for ``docker.models.containers.Container``."""

    def __init__(
        self,
        *,
        image_id: str = "sha256:currentimage",
        image_ref: str = "ghcr.io/sweetcornna/corlinman:1.0.0",
        data_dir: str = "/data",
        env: list[str] | None = None,
    ) -> None:
        self.attrs: dict[str, Any] = {
            "Image": image_id,
            "Config": {
                "Image": image_ref,
                "Env": env
                or [
                    "CORLINMAN_DATA_DIR=/data",
                    "PORT=6005",
                    # The OLD image's baked release stamp — must NOT be
                    # copied into the new container (it would defeat the
                    # helper's version assertion).
                    "CORLINMAN_VERSION=1.0.0",
                ],
                "Labels": {"com.docker.compose.service": "corlinman"},
                "ExposedPorts": {"6005/tcp": {}},
                "Healthcheck": {"Test": ["CMD", "curl", "-fsS", "/health"]},
                "User": "corlinman",
                "WorkingDir": "/app",
            },
            "HostConfig": {
                "Binds": ["/var/run/docker.sock:/var/run/docker.sock"],
                "PortBindings": {"6005/tcp": [{"HostPort": "6005"}]},
                "RestartPolicy": {"Name": "unless-stopped"},
                "NetworkMode": "compose_default",
            },
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": "corlinman-data",
                    "Source": "/var/lib/docker/volumes/corlinman-data/_data",
                    "Destination": data_dir,
                }
            ],
            "NetworkSettings": {
                "Networks": {
                    "compose_default": {
                        "Aliases": ["corlinman", "abcdef123456"],
                    },
                    "obs_net": {"Aliases": ["corlinman-obs"]},
                }
            },
            "State": {"Running": True, "Health": {"Status": "healthy"}},
        }
        self.stopped = False
        self.removed = False

    def stop(self, timeout: int = 30) -> None:
        self.stopped = True

    def remove(self, force: bool = False) -> None:
        self.removed = True


class FakeAPIClient:
    """Stand-in for ``docker.client.DockerClient.api``."""

    def __init__(
        self,
        pull_events: list[dict[str, Any]] | None = None,
        pull_error: Exception | None = None,
        pull_gate: threading.Event | None = None,
    ) -> None:
        self.pull_events = pull_events or [{"status": "Pulling", "id": "l1"}]
        self.pull_error = pull_error
        self.pull_gate = pull_gate
        self.pull_calls: list[tuple[str, str]] = []

    def pull(
        self, repo: str, *, tag: str, stream: bool, decode: bool
    ) -> Iterator[dict[str, Any]]:
        self.pull_calls.append((repo, tag))
        if self.pull_gate is not None:
            self.pull_gate.wait(timeout=5.0)
        if self.pull_error is not None:
            raise self.pull_error
        yield from self.pull_events


class FakeContainersAPI:
    def __init__(self, containers: dict[str, FakeContainer]) -> None:
        self.containers = containers
        self.run_calls: list[dict[str, Any]] = []

    def get(self, name: str) -> FakeContainer:
        try:
            return self.containers[name]
        except KeyError:
            raise RuntimeError(f"container {name!r} not found") from None

    def run(self, **kwargs: Any) -> FakeContainer:
        self.run_calls.append(kwargs)
        helper = FakeContainer()
        self.containers[str(kwargs.get("name"))] = helper
        return helper


class FakeDockerClient:
    """Stand-in for ``docker.client.DockerClient``."""

    def __init__(
        self,
        *,
        ping_ok: bool = True,
        container: FakeContainer | None = None,
        pull_events: list[dict[str, Any]] | None = None,
        pull_error: Exception | None = None,
        pull_gate: threading.Event | None = None,
    ) -> None:
        self.ping_ok = ping_ok
        self.api = FakeAPIClient(
            pull_events=pull_events,
            pull_error=pull_error,
            pull_gate=pull_gate,
        )
        containers = {}
        if container is not None:
            containers["corlinman"] = container
        self.containers = FakeContainersAPI(containers)

    def ping(self) -> bool:
        if not self.ping_ok:
            raise RuntimeError("docker daemon unreachable")
        return True


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> UpgradeStateStore:
    return UpgradeStateStore(tmp_path / ".upgrade-state.json")


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(du_module, "_STATUS_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(du_module, "_PROGRESS_POLL_SECONDS", 0.01)


def _make_upgrader(
    store: UpgradeStateStore,
    client: FakeDockerClient,
    tmp_path: Path,
) -> DockerUpgrader:
    return DockerUpgrader(
        store=store,
        repo="ghcr.io/sweetcornna/corlinman",
        container_name="corlinman",
        data_dir=tmp_path,
        docker_client_factory=lambda: client,
    )


async def _drain_until_terminal(
    store: UpgradeStateStore, request_id: str, timeout_s: float = 5.0
):
    async def _wait():
        while True:
            status = await store.get(request_id)
            if status is not None and status.is_terminal():
                return status
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(_wait(), timeout=timeout_s)


async def _wait_for_phase(
    store: UpgradeStateStore, request_id: str, phase: str, timeout_s: float = 5.0
):
    async def _wait():
        while True:
            status = await store.get(request_id)
            if status is not None and (
                status.phase == phase or status.is_terminal()
            ):
                return status
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(_wait(), timeout=timeout_s)


def _write_helper_status(
    tmp_path: Path, request_id: str, state: str, **extra: Any
) -> None:
    payload = {
        "request_id": str(uuid.UUID(request_id)),
        "state": state,
        "error": extra.pop("error", None),
        "started_at": 1,
        "finished_at": 2 if state in ("succeeded", "failed") else None,
        "log_excerpt": extra.pop("log_excerpt", ""),
        **extra,
    }
    (tmp_path / ".upgrade-status").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_available_true_when_ping_ok(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    upg = _make_upgrader(store, FakeDockerClient(ping_ok=True), tmp_path)
    assert await upg.is_available() is True


@pytest.mark.asyncio
async def test_is_available_false_when_ping_raises(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    upg = _make_upgrader(store, FakeDockerClient(ping_ok=False), tmp_path)
    assert await upg.is_available() is False


@pytest.mark.asyncio
async def test_is_available_false_when_factory_raises(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    def boom() -> Any:
        raise RuntimeError("no socket")

    upg = DockerUpgrader(
        store=store, data_dir=tmp_path, docker_client_factory=boom
    )
    assert await upg.is_available() is False


# ---------------------------------------------------------------------------
# start / single-flight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_returns_immediately_and_records_request(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    client = FakeDockerClient(container=FakeContainer())
    upg = _make_upgrader(store, client, tmp_path)

    req = await upg.start("v1.2.0", actor="alice")

    # GHCR tags carry no leading v.
    assert req.tag == "1.2.0"
    assert req.mode == "docker"
    status = await store.get(req.request_id)
    assert status is not None
    assert status.state in ("queued", "running", "failed")
    # Rollback context captured up front (this fake container has no
    # matching data mount, so the run ends in prepare_failed — the
    # before_version stamp must be there regardless).
    terminal = await _drain_until_terminal(store, req.request_id)
    assert terminal.before_version  # non-empty release version


@pytest.mark.asyncio
async def test_start_raises_when_one_in_flight(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    gate = threading.Event()
    client = FakeDockerClient(container=FakeContainer(), pull_gate=gate)
    upg = _make_upgrader(store, client, tmp_path)

    req = await upg.start("v1.2.0", actor="alice")
    with pytest.raises(UpgradeAlreadyRunning) as excinfo:
        await upg.start("v1.3.0", actor="bob")
    assert excinfo.value.in_flight.request_id == req.request_id
    gate.set()
    _write_helper_status(tmp_path, req.request_id, "succeeded")
    await _drain_until_terminal(store, req.request_id)


# ---------------------------------------------------------------------------
# The handoff contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_hands_off_to_helper_and_mirrors_success(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    container = FakeContainer(data_dir=str(tmp_path))
    client = FakeDockerClient(
        container=container,
        pull_events=[
            {"status": "Downloading", "id": "l1",
             "progressDetail": {"current": 1, "total": 2}},
            {"status": "Pull complete", "id": "l1"},
        ],
    )
    upg = _make_upgrader(store, client, tmp_path)

    req = await upg.start("v1.2.0", actor="alice")
    await _wait_for_phase(store, req.request_id, "handoff")

    # --- request file contract -------------------------------------------
    request_file = tmp_path / ".upgrade-request"
    assert request_file.is_file()
    payload = json.loads(request_file.read_text())
    assert payload["request_id"] == str(uuid.UUID(req.request_id))
    assert payload["mode"] == "docker"
    assert payload["action"] == "upgrade"
    assert payload["image_ref"] == "ghcr.io/sweetcornna/corlinman:1.2.0"
    assert payload["before_image"] == "sha256:currentimage"
    assert payload["target_version"] == "1.2.0"
    assert payload["previous_name"] == "corlinman-previous"
    assert payload["health_port"] == 6005
    create = payload["create_payload"]
    assert create["Image"] == "ghcr.io/sweetcornna/corlinman:1.2.0"
    assert create["HostConfig"]["RestartPolicy"] == {"Name": "unless-stopped"}
    # The old image's baked CORLINMAN_VERSION must be stripped (it would
    # override the new image's stamp and fail the version assertion);
    # runtime env survives.
    assert "CORLINMAN_VERSION=1.0.0" not in create["Env"]
    assert "CORLINMAN_DATA_DIR=/data" in create["Env"]
    assert "PORT=6005" in create["Env"]
    # Primary network with the auto-generated hex alias stripped…
    endpoints = create["NetworkingConfig"]["EndpointsConfig"]
    assert endpoints == {"compose_default": {"Aliases": ["corlinman"]}}
    # …and the secondary network parked for post-start connect.
    assert payload["extra_networks"] == {
        "obs_net": {"Aliases": ["corlinman-obs"]}
    }

    # --- helper launch contract -------------------------------------------
    assert len(client.containers.run_calls) == 1
    run_kwargs = client.containers.run_calls[0]
    assert run_kwargs["image"] == "sha256:currentimage"  # CURRENT image
    assert run_kwargs["name"] == _HELPER_CONTAINER_NAME
    assert run_kwargs["detach"] is True
    assert run_kwargs["user"] == "0"
    assert run_kwargs["entrypoint"][0].endswith("python")
    assert run_kwargs["entrypoint"][1] == "/app/upgrade_helper.py"
    volumes = run_kwargs["volumes"]
    assert volumes["/var/run/docker.sock"]["bind"] == "/var/run/docker.sock"
    assert volumes["corlinman-data"]["bind"] == "/data"

    # --- the gateway must NEVER kill its own container --------------------
    assert container.stopped is False
    assert container.removed is False

    # --- helper status mirrored into the store ----------------------------
    _write_helper_status(
        tmp_path, req.request_id, "succeeded",
        version_verified=True, log_excerpt="[ok] upgrade complete\n",
    )
    terminal = await _drain_until_terminal(store, req.request_id)
    assert terminal.state == "succeeded"
    assert terminal.version_verified is True
    assert "upgrade complete" in terminal.log_excerpt


@pytest.mark.asyncio
async def test_helper_failure_with_rollback_is_mirrored(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    client = FakeDockerClient(container=FakeContainer(data_dir=str(tmp_path)))
    upg = _make_upgrader(store, client, tmp_path)

    req = await upg.start("1.2.0", actor="alice")
    await _wait_for_phase(store, req.request_id, "handoff")
    _write_helper_status(
        tmp_path, req.request_id, "failed",
        error="healthcheck_timeout", rolled_back=True,
    )

    terminal = await _drain_until_terminal(store, req.request_id)
    assert terminal.state == "failed"
    assert terminal.error == "healthcheck_timeout"
    assert terminal.rolled_back is True


@pytest.mark.asyncio
async def test_rollback_instant_skips_pull(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    client = FakeDockerClient(container=FakeContainer(data_dir=str(tmp_path)))
    upg = _make_upgrader(store, client, tmp_path)

    req = await upg.start(
        "1.1.0", actor="alice", allow_downgrade=True, action="rollback_instant"
    )
    await _wait_for_phase(store, req.request_id, "handoff")

    assert client.api.pull_calls == []  # no pull for instant rollback
    payload = json.loads((tmp_path / ".upgrade-request").read_text())
    assert payload["action"] == "rollback_instant"
    request = store.get_request_sync(req.request_id)
    assert request is not None and request.allow_downgrade is True
    _write_helper_status(tmp_path, req.request_id, "succeeded")
    await _drain_until_terminal(store, req.request_id)


@pytest.mark.asyncio
async def test_pull_failure_lands_in_failed_with_upstream_message(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    client = FakeDockerClient(
        container=FakeContainer(),
        pull_error=RuntimeError("pull access denied"),
    )
    upg = _make_upgrader(store, client, tmp_path)

    req = await upg.start("1.2.0", actor="alice")
    terminal = await _drain_until_terminal(store, req.request_id)

    assert terminal.state == "failed"
    assert terminal.error is not None
    assert "image_pull_failed" in terminal.error
    assert "pull access denied" in terminal.error
    assert not client.containers.run_calls  # never reached the helper


@pytest.mark.asyncio
async def test_pull_event_with_error_field_fails(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    client = FakeDockerClient(
        container=FakeContainer(),
        pull_events=[{"errorDetail": {"message": "manifest unknown"}}],
    )
    upg = _make_upgrader(store, client, tmp_path)

    req = await upg.start("9.9.9", actor="alice")
    terminal = await _drain_until_terminal(store, req.request_id)

    assert terminal.state == "failed"
    assert terminal.error is not None
    assert "manifest unknown" in terminal.error


@pytest.mark.asyncio
async def test_prepare_failure_when_data_mount_missing(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    """The data volume must be discoverable to hand files to the helper."""
    container = FakeContainer(data_dir="/some/other/dir")
    client = FakeDockerClient(container=container)
    upg = _make_upgrader(store, client, tmp_path)

    req = await upg.start("1.2.0", actor="alice")
    terminal = await _drain_until_terminal(store, req.request_id)

    assert terminal.state == "failed"
    assert terminal.error is not None
    assert "prepare_failed" in terminal.error
    assert container.stopped is False


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_during_pull_lands_in_cancelled(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    gate = threading.Event()
    client = FakeDockerClient(
        container=FakeContainer(data_dir=str(tmp_path)), pull_gate=gate
    )
    upg = _make_upgrader(store, client, tmp_path)

    req = await upg.start("1.2.0", actor="alice")
    assert await upg.cancel(req.request_id) is True
    gate.set()

    terminal = await _drain_until_terminal(store, req.request_id)
    assert terminal.state == "cancelled"
    assert not client.containers.run_calls  # helper never launched
    assert not (tmp_path / ".upgrade-request").exists()
    # The slot is free again.
    assert await store.current_in_flight() is None


@pytest.mark.asyncio
async def test_cancel_after_terminal_returns_false(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    client = FakeDockerClient(
        container=FakeContainer(),
        pull_error=RuntimeError("nope"),
    )
    upg = _make_upgrader(store, client, tmp_path)
    req = await upg.start("1.2.0", actor="alice")
    await _drain_until_terminal(store, req.request_id)

    assert await upg.cancel(req.request_id) is False
    assert await upg.cancel("unknown-id") is False


@pytest.mark.asyncio
async def test_cancel_after_handoff_returns_false(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    client = FakeDockerClient(container=FakeContainer(data_dir=str(tmp_path)))
    upg = _make_upgrader(store, client, tmp_path)
    req = await upg.start("1.2.0", actor="alice")
    await _wait_for_phase(store, req.request_id, "handoff")

    assert await upg.cancel(req.request_id) is False

    _write_helper_status(tmp_path, req.request_id, "succeeded")
    await _drain_until_terminal(store, req.request_id)


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_yields_snapshots_and_terminates(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    client = FakeDockerClient(container=FakeContainer(data_dir=str(tmp_path)))
    upg = _make_upgrader(store, client, tmp_path)

    req = await upg.start("1.2.0", actor="alice")
    await _wait_for_phase(store, req.request_id, "handoff")
    _write_helper_status(tmp_path, req.request_id, "succeeded")

    frames = []
    async for frame in upg.progress(req.request_id):
        frames.append(frame)
    assert frames  # at least one snapshot
    assert frames[-1].state == "succeeded"


@pytest.mark.asyncio
async def test_progress_unknown_request_id_is_empty(
    store: UpgradeStateStore, tmp_path: Path
) -> None:
    upg = _make_upgrader(store, FakeDockerClient(), tmp_path)
    frames = [f async for f in upg.progress("nope")]
    assert frames == []


def test_protocol_satisfied() -> None:
    from corlinman_server.system.upgrader.protocol import UpgraderProtocol

    assert isinstance(
        DockerUpgrader(
            store=UpgradeStateStore(Path("/tmp/x-unused.json")),
            data_dir=Path("/tmp"),
            docker_client_factory=lambda: None,
        ),
        UpgraderProtocol,
    )