"""Docker SDK + ``docker compose`` shell-out impl of :class:`UpgraderProtocol`.

W1.1 of ``docs/PLAN_ONE_CLICK_UPGRADE.md`` §2 Wave 1/W1.1.

Design contract
---------------

The orchestration runs *inside* the gateway container against the
mounted ``/var/run/docker.sock`` (W3.1 wires the bind). For the
container-recreate step we prefer shelling out to ``docker compose``
when the binary is on ``$PATH``: it preserves env/volume parity with the
operator's compose file at zero plumbing cost. When the compose CLI is
missing (e.g. minimal host) we fall back to the SDK by introspecting the
existing ``corlinman`` container and calling
:meth:`docker.client.containers.run` with the same spec but the new
image. Both paths are documented inline.

The ``docker`` SDK import is deferred to method bodies so this module
imports cleanly in test environments that don't have ``docker-py``
installed (see ``tests/system/upgrader/test_docker_upgrader.py``).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import structlog

from corlinman_server.system.upgrader.protocol import (
    UpgradeAlreadyRunning,
    UpgraderUnavailable,
)
from corlinman_server.system.upgrader.state import (
    UpgradeRequest,
    UpgradeStateStore,
    UpgradeStatus,
)

logger = structlog.get_logger(__name__)


__all__ = ["DockerUpgrader"]


# How long ``is_available`` caches a successful daemon ping. The admin
# UI polls aggressively while the upgrade page is open; without a cache
# every poll hits the docker socket which is wasteful.
_AVAILABILITY_CACHE_TTL_SECONDS = 30.0

# Poll interval for :meth:`DockerUpgrader.progress`. 500ms is the upper
# bound of "feels live" without flooding the SSE stream.
_PROGRESS_POLL_SECONDS = 0.5

# Healthcheck window. The orchestration declares success when the new
# container reports ``healthy`` (or ``running`` for >= the sustain
# window) within ``_HEALTH_TIMEOUT_SECONDS``.
_HEALTH_TIMEOUT_SECONDS = 60.0
_HEALTH_SUSTAIN_SECONDS = 10.0
_HEALTH_POLL_SECONDS = 1.0

# Default upstream repo + container name (mirrors docker-compose.yml).
_DEFAULT_REPO = "ghcr.io/ymylive/corlinman"
_DEFAULT_CONTAINER_NAME = "corlinman"


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Impl
# ---------------------------------------------------------------------------


class DockerUpgrader:
    """Implements :class:`UpgraderProtocol` via docker-py + ``docker compose``.

    Behaviour summary (full contract in module docstring):

    1. :meth:`is_available` — ``docker.from_env().ping()`` with a 30s
       success-cache. Returns ``False`` (no raise) on any error so the
       admin route can downgrade the UI gracefully.
    2. :meth:`start` — single-flight guard via the store, mint request,
       spawn background task, return immediately so the HTTP handler can
       reply ``202``.
    3. The background task: pull → recreate → healthcheck → terminal.
       Every error path lands in ``state="failed"`` with an
       operator-readable ``error`` message — nothing bubbles up.
    4. :meth:`progress` — poll the store every 500ms, yield snapshots,
       terminate on terminal state.
    """

    def __init__(
        self,
        *,
        store: UpgradeStateStore,
        repo: str = _DEFAULT_REPO,
        container_name: str = _DEFAULT_CONTAINER_NAME,
        compose_file: str | None = None,
        docker_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._store = store
        self._repo = repo
        self._container_name = container_name
        self._compose_file = compose_file
        # Allow tests to inject a fake client without monkeypatching the
        # docker module — keeps the import lazy and the surface explicit.
        self._client_factory = docker_client_factory
        self._availability_cache: tuple[bool, float] | None = None
        # Hold strong refs to spawned tasks so they aren't garbage-collected
        # mid-flight (CPython logs a "Task was destroyed" warning when an
        # unawaited task is gc'd).
        self._background_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Client factory
    # ------------------------------------------------------------------

    def _make_client(self) -> Any:
        """Return a docker client, deferring the SDK import.

        Tests inject ``docker_client_factory`` so the real ``docker``
        package is never imported. In production we lazy-import
        ``docker`` so this module remains import-safe on hosts without
        the SDK installed (e.g. CI sandboxes).
        """
        if self._client_factory is not None:
            return self._client_factory()
        try:
            import docker  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - environment-specific
            raise UpgraderUnavailable(
                f"docker SDK not installed: {exc}"
            ) from exc
        return docker.from_env(timeout=120)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # UpgraderProtocol
    # ------------------------------------------------------------------

    async def is_available(self) -> bool:
        """Cache-aware availability probe.

        Returns ``False`` on any exception so the admin layer can detect
        "socket not mounted" / "daemon dead" without try/except plumbing.
        """
        now = time.monotonic()
        if self._availability_cache is not None:
            cached_value, cached_at = self._availability_cache
            if now - cached_at < _AVAILABILITY_CACHE_TTL_SECONDS:
                return cached_value
        try:
            client = await asyncio.to_thread(self._make_client)
            await asyncio.to_thread(client.ping)
            self._availability_cache = (True, now)
            return True
        except Exception as exc:  # noqa: BLE001 — we want to swallow ALL
            logger.warning("docker_upgrader.unavailable", error=str(exc))
            self._availability_cache = (False, now)
            return False

    async def start(self, target_tag: str, actor: str) -> UpgradeRequest:
        """Mint a request + spawn the background upgrade task.

        Single-flight is enforced here: if any status in the store is
        ``queued``/``running`` we raise :class:`UpgradeAlreadyRunning`
        and the route maps to ``409``. Terminal states (``succeeded`` /
        ``failed`` / ``stalled``) do NOT block a fresh request — a
        ``stalled`` upgrade is retryable, not a permanent lock (BUG-02).
        """
        in_flight = await self._store.current_in_flight()
        if in_flight is not None:
            raise UpgradeAlreadyRunning(in_flight)
        req = UpgradeRequest(
            request_id=uuid.uuid4().hex,
            tag=target_tag,
            requested_at=_now_ms(),
            requested_by=actor,
            mode="docker",
        )
        await self._store.begin(req)
        task = asyncio.create_task(
            self._run_upgrade(req), name=f"docker-upgrade-{req.request_id}"
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return req

    async def progress(
        self, request_id: str
    ) -> AsyncIterator[UpgradeStatus]:
        """Poll the store, yielding snapshots until terminal state."""
        initial = await self._store.get(request_id)
        if initial is None:
            return
        last_state: str | None = None
        last_phase: str | None = None
        last_log_len = -1
        while True:
            status = await self._store.get(request_id)
            if status is None:
                return
            changed = (
                status.state != last_state
                or status.phase != last_phase
                or len(status.log_excerpt) != last_log_len
            )
            if changed or last_state is None:
                yield status
                last_state = status.state
                last_phase = status.phase
                last_log_len = len(status.log_excerpt)
            if status.is_terminal():
                return
            await asyncio.sleep(_PROGRESS_POLL_SECONDS)

    # ------------------------------------------------------------------
    # Background work
    # ------------------------------------------------------------------

    async def _run_upgrade(self, req: UpgradeRequest) -> None:
        """End-to-end upgrade orchestration. Never raises."""
        await self._store.update(
            req.request_id,
            state="running",
            phase="starting",
            started_at=_now_ms(),
        )
        try:
            client = await asyncio.to_thread(self._make_client)
        except Exception as exc:  # noqa: BLE001
            await self._fail(req.request_id, "docker_sock_unavailable", exc)
            return

        # --- 1. Pull ----------------------------------------------------
        await self._store.update(
            req.request_id, phase="pulling", error=None
        )
        # Capture the running loop here (we ARE on the loop's thread);
        # the worker thread can't look it up itself.
        loop = asyncio.get_running_loop()
        try:
            await asyncio.to_thread(
                self._pull_with_progress, client, req, loop
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail(req.request_id, "image_pull_failed", exc)
            return

        # --- 2. Inspect old container -----------------------------------
        await self._store.update(req.request_id, phase="inspecting")
        old_spec: dict[str, Any] | None
        try:
            old_spec = await asyncio.to_thread(
                self._inspect_container, client
            )
        except Exception as exc:  # noqa: BLE001
            # Inspect failing is non-fatal IF compose CLI is available
            # (compose will recreate from scratch). Otherwise we can't
            # mirror the spec so we fail.
            if not self._compose_cli_available():
                await self._fail(req.request_id, "inspect_failed", exc)
                return
            old_spec = None
            await self._append_log(
                req.request_id,
                f"[warn] inspect failed, relying on compose: {exc}\n",
            )

        # --- 3. Recreate ------------------------------------------------
        await self._store.update(req.request_id, phase="recreating")
        try:
            if self._compose_cli_available():
                await self._recreate_via_compose(req)
            else:
                if old_spec is None:
                    raise RuntimeError(
                        "compose CLI missing and old container spec "
                        "unavailable — cannot recreate"
                    )
                await asyncio.to_thread(
                    self._recreate_via_sdk, client, old_spec, req.tag
                )
        except Exception as exc:  # noqa: BLE001
            await self._fail(req.request_id, "recreate_failed", exc)
            return

        # --- 4. Healthcheck ---------------------------------------------
        await self._store.update(req.request_id, phase="healthcheck")
        try:
            ok = await self._wait_healthy(client)
        except Exception as exc:  # noqa: BLE001
            await self._fail(req.request_id, "healthcheck_error", exc)
            return
        if not ok:
            await self._fail(
                req.request_id,
                "healthcheck_timeout",
                RuntimeError(
                    f"container did not become healthy within "
                    f"{_HEALTH_TIMEOUT_SECONDS:.0f}s"
                ),
            )
            return

        # --- 5. Success -------------------------------------------------
        await self._store.update(
            req.request_id,
            state="succeeded",
            phase="done",
            finished_at=_now_ms(),
            error=None,
        )
        await self._append_log(req.request_id, "[ok] upgrade complete\n")

    # ------------------------------------------------------------------
    # Pull + progress folding
    # ------------------------------------------------------------------

    def _pull_with_progress(
        self,
        client: Any,
        req: UpgradeRequest,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Stream pull events from docker-py and fold them into log_excerpt.

        ``docker_client.api.pull(repo, tag=tag, stream=True, decode=True)``
        yields one dict per layer-status transition::

            {"status": "Downloading",
             "progressDetail": {"current": 12345, "total": 98765},
             "id": "abc"}

        We collapse to one summary line per layer, replacing on update
        (in-memory state per ``id``) and emit a compact tail to the log
        excerpt: ``"abc: Downloading 12345/98765"``.

        This method runs synchronously inside ``asyncio.to_thread``; the
        caller passes the running loop so we can push log updates to the
        (asyncio-locked) store via :func:`asyncio.run_coroutine_threadsafe`
        — there is NO event loop in the worker thread itself.
        """
        stream = client.api.pull(
            self._repo, tag=req.tag, stream=True, decode=True
        )
        # Layer state: keeps the latest known status line per layer so
        # we can render a compact "tail" rather than a thousand-line log.
        layer_state: dict[str, str] = {}
        last_emit = 0.0
        for raw in stream:
            if not isinstance(raw, dict):
                continue
            err = raw.get("error") or raw.get("errorDetail", {}).get("message")
            if err:
                raise RuntimeError(str(err))
            status = raw.get("status") or ""
            layer_id = raw.get("id") or "_"
            progress_detail = raw.get("progressDetail") or {}
            current = progress_detail.get("current")
            total = progress_detail.get("total")
            if isinstance(current, int) and isinstance(total, int) and total:
                line = (
                    f"{layer_id}: {status} {current}/{total} "
                    f"({100 * current // total}%)"
                )
            elif status:
                line = f"{layer_id}: {status}"
            else:
                continue
            layer_state[layer_id] = line
            # Throttle log flushes to ~5/s so we don't ping the store on
            # every layer byte. We always emit the FINAL state below.
            now = time.monotonic()
            if now - last_emit < 0.2:
                continue
            last_emit = now
            snapshot = "\n".join(layer_state.values()) + "\n"
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._store.append_log(req.request_id, snapshot),
                    loop,
                )
                fut.result(timeout=2.0)
            except Exception:  # noqa: BLE001 — best-effort
                # RuntimeError("Event loop is closed") fires when the
                # gateway shuts down mid-upgrade; the log line just gets
                # dropped, the orchestration carries on.
                pass
        # Final flush with whatever the layer state ended up at.
        final = "\n".join(layer_state.values()) + "\n[ok] pull complete\n"
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._store.append_log(req.request_id, final), loop
            )
            fut.result(timeout=2.0)
        except Exception:  # noqa: BLE001 — best-effort
            # Loop closed during teardown — drop the trailing log line.
            pass

    # ------------------------------------------------------------------
    # Inspect + recreate
    # ------------------------------------------------------------------

    def _inspect_container(self, client: Any) -> dict[str, Any]:
        """Capture the running container's config so the SDK fallback
        can mirror env/ports/volumes/healthcheck when compose is absent.
        """
        container = client.containers.get(self._container_name)
        attrs = container.attrs or {}
        host_cfg = attrs.get("HostConfig") or {}
        config = attrs.get("Config") or {}
        return {
            "image": config.get("Image"),
            "env": config.get("Env") or [],
            "labels": config.get("Labels") or {},
            "ports": config.get("ExposedPorts") or {},
            "port_bindings": host_cfg.get("PortBindings") or {},
            "binds": host_cfg.get("Binds") or [],
            "restart_policy": host_cfg.get("RestartPolicy") or {},
            "network_mode": host_cfg.get("NetworkMode"),
            "healthcheck": config.get("Healthcheck"),
            "old_container": container,
        }

    def _compose_cli_available(self) -> bool:
        """``docker compose version`` exits 0 *and* the binary is on PATH."""
        if shutil.which("docker") is None:
            return False
        try:
            import subprocess  # noqa: PLC0415 — narrow scope

            result = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            return result.returncode == 0
        except (OSError, ValueError):
            return False

    async def _recreate_via_compose(self, req: UpgradeRequest) -> None:
        """Shell out to ``docker compose up -d --no-deps corlinman``.

        We pass ``CORLINMAN_TAG`` as the env override so the compose
        file's ``image: ghcr.io/ymylive/corlinman:${CORLINMAN_TAG:-latest}``
        picks up the new tag. The compose file path is configurable via
        the ``compose_file`` ctor arg (deploy mounts ``/app/compose``).
        """
        import subprocess  # noqa: PLC0415

        env = dict(os.environ)
        env["CORLINMAN_TAG"] = req.tag
        argv = ["docker", "compose"]
        if self._compose_file:
            argv.extend(["-f", self._compose_file])
        argv.extend(["up", "-d", "--no-deps", self._container_name])
        proc = await asyncio.to_thread(
            subprocess.run,
            argv,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
            check=False,
        )
        log = (proc.stdout or "") + (proc.stderr or "")
        if log:
            await self._append_log(req.request_id, log)
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker compose exited with {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
            )

    def _recreate_via_sdk(
        self, client: Any, old_spec: dict[str, Any], target_tag: str
    ) -> None:
        """Fallback path: remove old container + ``containers.run`` new one.

        Mirrors env / ports / volumes / restart policy / healthcheck
        from the inspect snapshot. Less-perfect than compose recreate
        (e.g. networks aliases not perfectly preserved) but adequate
        when the operator hasn't installed the compose CLI in the host.
        """
        new_image = f"{self._repo}:{target_tag}"
        old = old_spec.get("old_container")
        if old is not None:
            try:
                old.stop(timeout=30)
            except Exception:  # noqa: BLE001 — already stopped is fine
                pass
            try:
                old.remove(force=True)
            except Exception:  # noqa: BLE001
                pass
        client.containers.run(
            image=new_image,
            name=self._container_name,
            detach=True,
            environment=old_spec.get("env") or [],
            labels=old_spec.get("labels") or {},
            ports=old_spec.get("port_bindings") or {},
            volumes=old_spec.get("binds") or [],
            restart_policy=old_spec.get("restart_policy")
            or {"Name": "unless-stopped"},
            network_mode=old_spec.get("network_mode"),
            healthcheck=old_spec.get("healthcheck"),
        )

    # ------------------------------------------------------------------
    # Healthcheck
    # ------------------------------------------------------------------

    async def _wait_healthy(self, client: Any) -> bool:
        """Poll the new container; pass when ``healthy`` or sustained ``running``.

        Returns ``True`` on success, ``False`` on timeout. The caller
        translates ``False`` into ``state="failed"`` with
        ``error="healthcheck_timeout"``.
        """
        deadline = time.monotonic() + _HEALTH_TIMEOUT_SECONDS
        running_since: float | None = None
        while time.monotonic() < deadline:
            try:
                container = await asyncio.to_thread(
                    client.containers.get, self._container_name
                )
                attrs = await asyncio.to_thread(getattr, container, "attrs")
            except Exception:  # noqa: BLE001 — container may be mid-recreate
                await asyncio.sleep(_HEALTH_POLL_SECONDS)
                continue
            state = (attrs or {}).get("State") or {}
            health = state.get("Health") or {}
            health_status = health.get("Status")
            running = bool(state.get("Running"))
            if health_status == "healthy":
                return True
            if health_status is None and running:
                # No healthcheck defined — accept after sustain window.
                if running_since is None:
                    running_since = time.monotonic()
                elif time.monotonic() - running_since >= _HEALTH_SUSTAIN_SECONDS:
                    return True
            else:
                running_since = None
            await asyncio.sleep(_HEALTH_POLL_SECONDS)
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _append_log(self, request_id: str, chunk: str) -> None:
        await self._store.append_log(request_id, chunk)

    async def _fail(
        self, request_id: str, code: str, exc: BaseException
    ) -> None:
        message = f"{code}: {exc}"[:500]
        logger.warning(
            "docker_upgrader.failed",
            request_id=request_id,
            code=code,
            error=str(exc),
        )
        try:
            await self._store.update(
                request_id,
                state="failed",
                phase=code,
                finished_at=_now_ms(),
                error=message,
            )
            await self._store.append_log(
                request_id, f"[fail] {message}\n"
            )
        except Exception:  # noqa: BLE001 — store failure shouldn't crash
            logger.exception("docker_upgrader.fail_state_write_failed")
