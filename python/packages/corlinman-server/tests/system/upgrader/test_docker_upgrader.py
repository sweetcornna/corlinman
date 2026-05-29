"""Tests for :class:`corlinman_server.system.upgrader.DockerUpgrader`.

W1.1 of ``docs/PLAN_ONE_CLICK_UPGRADE.md`` §2 Wave 1/W1.1.

We never import the real ``docker`` package — the impl module defers
the import inside ``_make_client`` so test environments without docker
installed can still import everything. All tests inject a fake client
factory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from corlinman_server.system.upgrader import (
    DockerUpgrader,
    UpgradeAlreadyRunning,
    UpgradeRequest,
    UpgradeStateStore,
)
from corlinman_server.system.upgrader.docker_upgrader import (
    _HEALTH_TIMEOUT_SECONDS,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeContainer:
    """Stand-in for ``docker.models.containers.Container``.

    ``attrs`` is mutable so tests can flip health state mid-test.
    """

    def __init__(
        self,
        *,
        image: str = "ghcr.io/ymylive/corlinman:v1.0.0",
        env: list[str] | None = None,
        health_status: str | None = "starting",
        running: bool = True,
    ) -> None:
        self.attrs: dict[str, Any] = {
            "Config": {
                "Image": image,
                "Env": env or [],
                "Labels": {},
                "ExposedPorts": {"6005/tcp": {}},
                "Healthcheck": {"Test": ["CMD", "curl", "-fsS", "/health"]},
            },
            "HostConfig": {
                "Binds": ["~/.corlinman:/data"],
                "PortBindings": {"6005/tcp": [{"HostPort": "6005"}]},
                "RestartPolicy": {"Name": "unless-stopped"},
                "NetworkMode": "bridge",
            },
            "State": {
                "Running": running,
                "Health": (
                    {"Status": health_status}
                    if health_status is not None
                    else {}
                ),
            },
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
    ) -> None:
        self.pull_events = pull_events or []
        self.pull_error = pull_error
        self.pull_calls: list[tuple[str, str]] = []

    def pull(
        self, repo: str, *, tag: str, stream: bool, decode: bool
    ) -> Iterator[dict[str, Any]]:
        self.pull_calls.append((repo, tag))
        if self.pull_error is not None:
            raise self.pull_error
        # Optionally embed an error event in the stream to exercise the
        # mid-stream failure path.
        yield from self.pull_events


class FakeContainersAPI:
    def __init__(self, container: FakeContainer | None) -> None:
        self.container = container
        self.run_calls: list[dict[str, Any]] = []

    def get(self, name: str) -> FakeContainer:
        if self.container is None:
            raise RuntimeError(f"container {name} not found")
        return self.container

    def run(self, **kwargs: Any) -> FakeContainer:
        self.run_calls.append(kwargs)
        # The new container is "healthy" immediately — caller can override
        # by reaching into ``self.container.attrs`` after construction.
        self.container = FakeContainer(health_status="healthy")
        return self.container


class FakeDockerClient:
    """Stand-in for ``docker.client.DockerClient``."""

    def __init__(
        self,
        *,
        ping_ok: bool = True,
        container: FakeContainer | None = None,
        pull_events: list[dict[str, Any]] | None = None,
        pull_error: Exception | None = None,
    ) -> None:
        self.ping_ok = ping_ok
        self.api = FakeAPIClient(pull_events=pull_events, pull_error=pull_error)
        self.containers = FakeContainersAPI(container)

    def ping(self) -> bool:
        if not self.ping_ok:
            raise RuntimeError("docker daemon unreachable")
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> UpgradeStateStore:
    return UpgradeStateStore(tmp_path / ".upgrade-state.json")


def _make_upgrader(
    store: UpgradeStateStore,
    client: FakeDockerClient,
    *,
    compose_file: str | None = None,
) -> DockerUpgrader:
    """Construct a DockerUpgrader pointed at the fake client.

    We force-disable compose CLI detection via monkeypatch in tests
    where we need to exercise the SDK-fallback path; default leaves it
    enabled if ``docker compose version`` happens to exit 0 on the
    runner.
    """
    return DockerUpgrader(
        store=store,
        repo="ghcr.io/ymylive/corlinman",
        container_name="corlinman",
        compose_file=compose_file,
        docker_client_factory=lambda: client,
    )


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


async def test_is_available_true_when_ping_ok(
    store: UpgradeStateStore,
) -> None:
    upg = _make_upgrader(store, FakeDockerClient(ping_ok=True))
    assert await upg.is_available() is True
    # Cache hit on second call — should still be True.
    assert await upg.is_available() is True


async def test_is_available_false_when_ping_raises(
    store: UpgradeStateStore,
) -> None:
    upg = _make_upgrader(store, FakeDockerClient(ping_ok=False))
    assert await upg.is_available() is False


async def test_is_available_false_when_factory_raises(
    store: UpgradeStateStore,
) -> None:
    def boom() -> Any:
        raise RuntimeError("no socket")

    upg = DockerUpgrader(
        store=store,
        docker_client_factory=boom,
    )
    assert await upg.is_available() is False


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


async def test_start_returns_immediately_and_records_request(
    store: UpgradeStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``start`` writes UpgradeRequest + spawns task; doesn't block."""
    client = FakeDockerClient(
        container=FakeContainer(health_status="healthy"),
        pull_events=[{"id": "abc", "status": "Pull complete"}],
    )
    upg = _make_upgrader(store, client)
    # Force compose CLI off so we go through SDK fallback (which we can
    # control via the fake client).
    monkeypatch.setattr(
        upg, "_compose_cli_available", lambda: False
    )

    # Reduce healthcheck poll so the background task finishes fast.
    monkeypatch.setattr(
        "corlinman_server.system.upgrader.docker_upgrader._HEALTH_POLL_SECONDS",
        0.01,
    )

    started_at = asyncio.get_event_loop().time()
    req = await upg.start("v1.2.0", actor="alice")
    elapsed = asyncio.get_event_loop().time() - started_at
    # 200ms is generous — the docs say "tens of milliseconds".
    assert elapsed < 0.2

    assert req.tag == "v1.2.0"
    assert req.requested_by == "alice"
    assert req.mode == "docker"

    # The status row exists and is at least queued.
    snap = await store.get(req.request_id)
    assert snap is not None
    assert snap.state in ("queued", "running", "succeeded")


async def test_start_raises_when_one_in_flight(
    store: UpgradeStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-flight guard fires on the second concurrent start."""
    # Deliberately seed an in-flight status so start() short-circuits.
    from corlinman_server.system.upgrader.state import UpgradeRequest

    existing = UpgradeRequest(
        request_id="prev",
        tag="v1.1.0",
        requested_at=1,
        requested_by="bob",
        mode="docker",
    )
    await store.begin(existing)
    # Leave as "queued" — that counts as in-flight.

    client = FakeDockerClient()
    upg = _make_upgrader(store, client)
    monkeypatch.setattr(upg, "_compose_cli_available", lambda: False)

    with pytest.raises(UpgradeAlreadyRunning) as excinfo:
        await upg.start("v1.2.0", actor="alice")
    assert excinfo.value.in_flight.request_id == "prev"


# ---------------------------------------------------------------------------
# Background task — happy + failure paths
# ---------------------------------------------------------------------------


async def _drain_until_terminal(
    store: UpgradeStateStore, request_id: str, *, timeout: float = 5.0
) -> Any:
    """Spin on the store until the status reaches a terminal state."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = await store.get(request_id)
        if status is not None and status.is_terminal():
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"upgrade did not reach terminal state within {timeout}s "
        f"(last={await store.get(request_id)})"
    )


async def test_happy_path_pull_recreate_healthcheck_succeeds(
    store: UpgradeStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pull_events = [
        {
            "id": "layer1",
            "status": "Downloading",
            "progressDetail": {"current": 5, "total": 10},
        },
        {
            "id": "layer1",
            "status": "Download complete",
        },
        {
            "id": "layer1",
            "status": "Pull complete",
        },
    ]
    client = FakeDockerClient(
        container=FakeContainer(health_status="healthy"),
        pull_events=pull_events,
    )
    upg = _make_upgrader(store, client)
    monkeypatch.setattr(upg, "_compose_cli_available", lambda: False)
    monkeypatch.setattr(
        "corlinman_server.system.upgrader.docker_upgrader._HEALTH_POLL_SECONDS",
        0.01,
    )

    req = await upg.start("v1.2.0", actor="alice")
    terminal = await _drain_until_terminal(store, req.request_id)

    assert terminal.state == "succeeded"
    assert terminal.phase == "done"
    assert terminal.error is None
    # Pull progress recorded into the log.
    assert "layer1" in terminal.log_excerpt
    # SDK fallback was used → containers.run was called.
    assert client.containers.run_calls
    new_image = client.containers.run_calls[0]["image"]
    assert new_image == "ghcr.io/ymylive/corlinman:v1.2.0"


async def test_pull_failure_lands_in_failed_with_upstream_message(
    store: UpgradeStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeDockerClient(
        container=FakeContainer(),
        pull_error=RuntimeError("manifest unknown"),
    )
    upg = _make_upgrader(store, client)
    monkeypatch.setattr(upg, "_compose_cli_available", lambda: False)

    req = await upg.start("v9.9.9", actor="alice")
    terminal = await _drain_until_terminal(store, req.request_id)

    assert terminal.state == "failed"
    assert terminal.phase == "image_pull_failed"
    assert terminal.error is not None
    assert "manifest unknown" in terminal.error


async def test_pull_event_with_error_field_fails(
    store: UpgradeStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docker-py reports auth/etc failures as in-stream ``error`` events."""
    client = FakeDockerClient(
        container=FakeContainer(),
        pull_events=[
            {"id": "abc", "status": "Pulling"},
            {"error": "unauthorized: authentication required"},
        ],
    )
    upg = _make_upgrader(store, client)
    monkeypatch.setattr(upg, "_compose_cli_available", lambda: False)

    req = await upg.start("v1.2.0", actor="alice")
    terminal = await _drain_until_terminal(store, req.request_id)

    assert terminal.state == "failed"
    assert terminal.error is not None
    assert "unauthorized" in terminal.error


async def test_healthcheck_timeout_lands_in_failed(
    store: UpgradeStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate a container that never becomes healthy."""
    client = FakeDockerClient(
        container=FakeContainer(health_status="unhealthy", running=True),
        pull_events=[{"id": "abc", "status": "Pull complete"}],
    )
    upg = _make_upgrader(store, client)
    monkeypatch.setattr(upg, "_compose_cli_available", lambda: False)
    # Shrink the healthcheck window so the test completes in <1s instead
    # of the production 60s.
    monkeypatch.setattr(
        "corlinman_server.system.upgrader.docker_upgrader._HEALTH_TIMEOUT_SECONDS",
        0.2,
    )
    monkeypatch.setattr(
        "corlinman_server.system.upgrader.docker_upgrader._HEALTH_POLL_SECONDS",
        0.02,
    )

    # Override the FakeContainersAPI.run to return a container that stays
    # unhealthy throughout.
    def run_stays_unhealthy(**kwargs: Any) -> FakeContainer:
        unhealthy = FakeContainer(health_status="unhealthy", running=True)
        client.containers.container = unhealthy
        return unhealthy

    client.containers.run = run_stays_unhealthy  # type: ignore[assignment]

    req = await upg.start("v1.2.0", actor="alice")
    terminal = await _drain_until_terminal(store, req.request_id, timeout=5.0)

    assert terminal.state == "failed"
    assert terminal.phase == "healthcheck_timeout"
    assert terminal.error is not None
    assert "healthy" in terminal.error or "timeout" in terminal.error.lower()


async def test_recreate_failure_lands_in_failed(
    store: UpgradeStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeDockerClient(
        container=FakeContainer(),
        pull_events=[{"id": "abc", "status": "Pull complete"}],
    )

    def broken_run(**kwargs: Any) -> Any:
        raise RuntimeError("port already allocated")

    client.containers.run = broken_run  # type: ignore[assignment]

    upg = _make_upgrader(store, client)
    monkeypatch.setattr(upg, "_compose_cli_available", lambda: False)

    req = await upg.start("v1.2.0", actor="alice")
    terminal = await _drain_until_terminal(store, req.request_id)

    assert terminal.state == "failed"
    assert terminal.phase == "recreate_failed"
    assert terminal.error is not None
    assert "port already allocated" in terminal.error


# ---------------------------------------------------------------------------
# progress iterator
# ---------------------------------------------------------------------------


async def test_progress_yields_snapshots_and_terminates(
    store: UpgradeStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``progress`` emits at least one snapshot and stops on terminal."""
    client = FakeDockerClient(
        container=FakeContainer(health_status="healthy"),
        pull_events=[{"id": "abc", "status": "Pull complete"}],
    )
    upg = _make_upgrader(store, client)
    monkeypatch.setattr(upg, "_compose_cli_available", lambda: False)
    monkeypatch.setattr(
        "corlinman_server.system.upgrader.docker_upgrader._HEALTH_POLL_SECONDS",
        0.01,
    )
    # Shorten progress poll so the test finishes promptly.
    monkeypatch.setattr(
        "corlinman_server.system.upgrader.docker_upgrader._PROGRESS_POLL_SECONDS",
        0.02,
    )

    req = await upg.start("v1.2.0", actor="alice")

    snapshots: list[Any] = []
    async for snap in upg.progress(req.request_id):
        snapshots.append(snap)
        if len(snapshots) > 50:  # safety stop
            break

    assert len(snapshots) >= 1
    last = snapshots[-1]
    assert last.is_terminal()
    assert last.state == "succeeded"


async def test_progress_unknown_request_id_is_empty(
    store: UpgradeStateStore,
) -> None:
    upg = _make_upgrader(store, FakeDockerClient())
    snaps = [s async for s in upg.progress("nope")]
    assert snaps == []


async def test_progress_loop_exits_on_stalled(
    store: UpgradeStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #R3-005.

    ``stalled`` is terminal (helper wrote ``finished_at`` and stopped).
    The progress async generator MUST return — otherwise every SSE
    observer leaks a task at the 500 ms tick until disconnect.
    """
    monkeypatch.setattr(
        "corlinman_server.system.upgrader.docker_upgrader._PROGRESS_POLL_SECONDS",
        0.02,
    )
    upg = _make_upgrader(store, FakeDockerClient())
    req = UpgradeRequest(
        request_id="req-stalled",
        tag="v1.2.0",
        requested_at=0,
        requested_by="ops",
        mode="docker",
    )
    await store.begin(req)
    await store.update(
        req.request_id,
        state="stalled",
        phase="stalled",
        finished_at=1,
    )

    async def drain() -> list[Any]:
        return [s async for s in upg.progress(req.request_id)]

    snaps = await asyncio.wait_for(drain(), timeout=1.0)
    assert snaps, "expected at least one snapshot before terminal"
    assert snaps[-1].state == "stalled"


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------


def test_health_timeout_constant_is_60s() -> None:
    """Sanity: the documented 60s healthcheck timeout matches code."""
    assert _HEALTH_TIMEOUT_SECONDS == 60.0


def test_protocol_satisfied() -> None:
    """DockerUpgrader satisfies the runtime-checkable protocol."""
    from corlinman_server.system.upgrader import UpgraderProtocol

    upg = DockerUpgrader(
        store=UpgradeStateStore(Path("/tmp/whatever-not-touched.json")),
        docker_client_factory=lambda: MagicMock(),
    )
    assert isinstance(upg, UpgraderProtocol)
