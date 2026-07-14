"""Docker-mode :class:`UpgraderProtocol` implementation.

Architecture (rebuilt — v2, sub2api-modeled)
--------------------------------------------

The original implementation stopped/recreated the ``corlinman`` container
*from inside that same container*: stopping the old container killed the
orchestration mid-swap, and the SDK path had already removed the old
container — a failed upgrade left the box with nothing running and no
rollback. The rebuild converges on the native systemd-helper pattern:

1. **Gateway (this class)** — pulls the new image (streamed progress),
   captures the running container's spec, persists rollback context
   (``before_version`` on the status record), writes
   ``$DATA_DIR/.upgrade-request`` and launches ``docker/upgrade_helper.py``
   as a **detached one-shot container** (running the gateway's *current*
   image, docker socket + data volume mounted). Then it just mirrors the
   helper's status file until the helper kills this very container.
2. **Helper (outside the doomed container)** — stop → rename to
   ``corlinman-previous`` (kept: the instant-rollback slot) → create/start
   the new container → wait healthy → **assert the reported ``/health``
   version equals the target** → terminal status. Any failure restores
   ``corlinman-previous`` and reports ``rolled_back: true``.
3. **Boot finalizer** (``finalizer.py``) — the restarted gateway settles
   the record: version assertion → ``succeeded``, helper verdict mirror,
   stall fallback.

``is_available`` still gates on a read-write docker socket — without the
opt-in socket mount (``docker-compose.selfupdate.yml``) the admin routes
degrade to the copy-paste commands exactly as before.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import structlog

from corlinman_server.system.upgrader.protocol import (
    UpgradeAlreadyRunning,
    UpgraderProtocol,
    UpgraderUnavailable,
)
from corlinman_server.system.upgrader.state import (
    UpgradeRequest,
    UpgradeStateStore,
    UpgradeStatus,
)

logger = structlog.get_logger(__name__)


__all__ = ["DockerUpgrader"]


# How long a cached is_available() verdict lives. The socket appearing /
# vanishing is rare; 30s keeps the admin poll cheap.
_AVAILABILITY_CACHE_TTL_SECONDS = 30.0

# Poll interval for :meth:`DockerUpgrader.progress`. 500ms is the upper
# bound of "feels live" without flooding the SSE stream.
_PROGRESS_POLL_SECONDS = 0.5

# Helper handoff windows: the helper should write its first status within
# the stall timeout (it starts in seconds); the overall cap matches the
# native helper's systemd TimeoutStartSec.
_HELPER_STALL_TIMEOUT_SECONDS = 60.0
_HELPER_OVERALL_TIMEOUT_SECONDS = 600.0
_STATUS_POLL_INTERVAL_SECONDS = 1.0

# Passed to the helper: window for the NEW container to become healthy.
_HELPER_HEALTH_TIMEOUT_SECONDS = 120.0

# Default upstream repo + container name (mirrors docker-compose.yml).
_DEFAULT_REPO = "ghcr.io/sweetcornna/corlinman"
_DEFAULT_CONTAINER_NAME = "corlinman"

_HELPER_CONTAINER_NAME = "corlinman-upgrade-helper"
_HELPER_SCRIPT_PATH = "/app/upgrade_helper.py"
_HELPER_PYTHON = "/opt/venv/bin/python"

_REQUEST_FILE_NAME = ".upgrade-request"
_STATUS_FILE_NAME = ".upgrade-status"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _strip_v(tag: str) -> str:
    return tag[1:] if tag[:1] in ("v", "V") else tag


class _UpgradeCancelled(Exception):
    """Internal: the operator aborted before the point of no return."""


# ---------------------------------------------------------------------------
# Impl
# ---------------------------------------------------------------------------


class DockerUpgrader:
    """Implements :class:`UpgraderProtocol` via docker-py + a detached
    helper container (see module docstring for the full architecture).

    1. :meth:`is_available` — ``docker.from_env().ping()`` with a 30s
       success-cache. Returns ``False`` (no raise) on any error so the
       admin route can downgrade the UI gracefully.
    2. :meth:`start` — single-flight guard via the store, mint request,
       spawn background task, return immediately so the HTTP handler can
       reply ``202``.
    3. The background task: pull → capture spec → write request file →
       launch helper → mirror its status file. The helper stops this
       container mid-mirror by design; the boot finalizer settles the
       record afterwards.
    4. :meth:`progress` — poll the store every 500ms, yield snapshots,
       terminate on terminal state.
    5. :meth:`cancel` — abort while still on this side of the handoff
       (queued / pulling / preparing). After the helper launches there is
       no safe abort.
    """

    mode = "docker"

    def __init__(
        self,
        *,
        store: UpgradeStateStore,
        repo: str = _DEFAULT_REPO,
        container_name: str = _DEFAULT_CONTAINER_NAME,
        data_dir: Path | None = None,
        compose_file: str | None = None,  # legacy kwarg, no longer used
        docker_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._store = store
        self._repo = repo
        self._container_name = container_name
        self._data_dir = Path(
            data_dir
            if data_dir is not None
            else os.environ.get("CORLINMAN_DATA_DIR", "/data")
        )
        del compose_file  # accepted for wiring back-compat only
        # Allow tests to inject a fake client without monkeypatching the
        # docker module — keeps the import lazy and the surface explicit.
        self._client_factory = docker_client_factory
        self._availability_cache: tuple[bool, float] | None = None
        # Hold strong refs to spawned tasks so they aren't garbage-collected
        # mid-flight (CPython logs a "Task was destroyed" warning when an
        # unawaited task is gc'd).
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Cancellation flags per request; threading.Event because the pull
        # loop runs inside asyncio.to_thread.
        self._cancel_events: dict[str, threading.Event] = {}
        # Requests that already handed off to the helper — no safe abort.
        self._handoff_done: set[str] = set()

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

    async def start(
        self,
        target_tag: str,
        actor: str,
        *,
        allow_downgrade: bool = False,
        action: str = "upgrade",
    ) -> UpgradeRequest:
        """Mint a request + spawn the background upgrade task.

        Single-flight is enforced here: if any status in the store is
        ``queued``/``running`` we raise :class:`UpgradeAlreadyRunning`
        and the route maps to ``409``. Terminal states (``succeeded`` /
        ``failed`` / ``stalled``) do NOT block a fresh request — a
        ``stalled`` upgrade is retryable, not a permanent lock (BUG-02).

        GHCR image tags carry no ``v`` (release-image.yml semver pattern
        ``{{version}}``), so the tag is normalized to the stripped form.
        """
        in_flight = await self._store.current_in_flight()
        if in_flight is not None:
            raise UpgradeAlreadyRunning(in_flight)
        req = UpgradeRequest(
            request_id=uuid.uuid4().hex,
            tag=_strip_v(target_tag),
            requested_at=_now_ms(),
            requested_by=actor,
            mode="docker",
            allow_downgrade=allow_downgrade,
            action=action,
        )
        await self._store.begin(req)
        # Rollback context: the version we're upgrading AWAY from.
        try:
            from corlinman_server.system.app_version import resolve_app_version

            await self._store.update(
                req.request_id, before_version=resolve_app_version()
            )
        except Exception:  # noqa: BLE001 — cosmetic metadata only
            pass
        self._cancel_events[req.request_id] = threading.Event()
        task = asyncio.create_task(
            self._run_upgrade(req), name=f"docker-upgrade-{req.request_id}"
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return req

    async def cancel(self, request_id: str) -> bool:
        """Abort iff the request is still on this side of the handoff.

        Honest semantics: once the helper container is launched the swap
        is out of our hands — return ``False`` (the route maps that to
        ``409 not_cancellable``). Cancelling during pull interrupts the
        layer stream at the next chunk.
        """
        status = await self._store.get(request_id)
        if status is None or status.is_terminal():
            return False
        if request_id in self._handoff_done:
            return False
        event = self._cancel_events.get(request_id)
        if event is None:
            return False
        event.set()
        return True

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
        """End-to-end orchestration up to the helper handoff. Never raises."""
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

        try:
            self._check_cancelled(req.request_id)

            # --- 1. Pull (skipped for instant rollback) -------------------
            if req.action != "rollback_instant":
                await self._store.update(
                    req.request_id, phase="pulling", error=None
                )
                # Capture the running loop here (we ARE on the loop's
                # thread); the worker thread can't look it up itself.
                loop = asyncio.get_running_loop()
                try:
                    await asyncio.to_thread(
                        self._pull_with_progress, client, req, loop
                    )
                except _UpgradeCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    await self._fail(req.request_id, "image_pull_failed", exc)
                    return

            self._check_cancelled(req.request_id)

            # --- 2. Capture spec + write the helper request ---------------
            await self._store.update(req.request_id, phase="preparing")
            try:
                context = await asyncio.to_thread(
                    self._prepare_handoff, client, req
                )
            except Exception as exc:  # noqa: BLE001
                await self._fail(req.request_id, "prepare_failed", exc)
                return

            self._check_cancelled(req.request_id)

            # --- 3. Launch the detached helper ----------------------------
            try:
                await asyncio.to_thread(self._launch_helper, client, context)
            except Exception as exc:  # noqa: BLE001
                self._delete_request_file()
                await self._fail(req.request_id, "helper_launch_failed", exc)
                return
            # Point of no return — the helper owns the swap now.
            self._handoff_done.add(req.request_id)
            await self._store.update(req.request_id, phase="handoff")
            await self._append_log(
                req.request_id,
                "[ok] handed off to upgrade helper container; this gateway "
                "will restart shortly\n",
            )
        except _UpgradeCancelled:
            self._delete_request_file()
            await self._store.update(
                req.request_id,
                state="cancelled",
                phase="cancelled",
                error=None,
                finished_at=_now_ms(),
            )
            await self._append_log(
                req.request_id, "[ok] upgrade cancelled by operator\n"
            )
            return
        finally:
            self._cancel_events.pop(req.request_id, None)

        # --- 4. Mirror the helper's status file until we die ---------------
        await self._mirror_helper_status(req)

    def _check_cancelled(self, request_id: str) -> None:
        event = self._cancel_events.get(request_id)
        if event is not None and event.is_set():
            raise _UpgradeCancelled

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
        — there is NO event loop in the worker thread itself. The
        per-request cancel event is checked once per stream chunk.
        """
        cancel_event = self._cancel_events.get(req.request_id)
        stream = client.api.pull(
            self._repo, tag=req.tag, stream=True, decode=True
        )
        # Layer state: keeps the latest known status line per layer so
        # we can render a compact "tail" rather than a thousand-line log.
        layer_state: dict[str, str] = {}
        last_emit = 0.0
        for raw in stream:
            if cancel_event is not None and cancel_event.is_set():
                raise _UpgradeCancelled
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
    # Handoff preparation
    # ------------------------------------------------------------------

    def _prepare_handoff(
        self, client: Any, req: UpgradeRequest
    ) -> dict[str, Any]:
        """Capture the running container's spec + write ``.upgrade-request``.

        Returns the launch context for :meth:`_launch_helper`:
        ``{"own_image": ..., "data_mount_source": ...}``.
        """
        container = client.containers.get(self._container_name)
        attrs = container.attrs or {}
        own_image = (attrs.get("Image") or "").strip()
        if not own_image:
            raise RuntimeError("could not resolve the running image id")

        config = attrs.get("Config") or {}
        create_payload, extra_networks = self._build_create_payload(
            attrs, f"{self._repo}:{req.tag}"
        )
        data_mount_source = self._find_data_mount_source(attrs)

        request_payload: dict[str, Any] = {
            # Helper files carry the dashed UUID form (native-helper
            # convention); the store keeps the dashless one. The boot
            # finalizer matches either.
            "request_id": str(uuid.UUID(req.request_id)),
            "mode": "docker",
            "action": req.action,
            "tag": req.tag,
            "image_ref": f"{self._repo}:{req.tag}",
            "container_name": self._container_name,
            "previous_name": f"{self._container_name}-previous",
            "before_image": own_image,
            "target_version": _strip_v(req.tag),
            "health_port": self._health_port(config),
            "health_timeout_s": _HELPER_HEALTH_TIMEOUT_SECONDS,
            "requested_by": req.requested_by,
            "requested_at": req.requested_at,
            "create_payload": create_payload,
            "extra_networks": extra_networks,
        }
        self._atomic_write_json(
            self._data_dir / _REQUEST_FILE_NAME, request_payload
        )
        return {
            "own_image": own_image,
            "data_mount_source": data_mount_source,
        }

    @staticmethod
    def _health_port(config: dict[str, Any]) -> int:
        for env_entry in config.get("Env") or []:
            if isinstance(env_entry, str) and env_entry.startswith("PORT="):
                try:
                    return int(env_entry.split("=", 1)[1])
                except ValueError:
                    break
        return 6005

    @staticmethod
    def _build_create_payload(
        attrs: dict[str, Any], new_image: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Turn ``docker inspect`` output into a ``POST /containers/create``
        body targeting ``new_image``, plus the secondary networks the
        helper re-connects after start (the create API takes only one
        endpoint)."""
        config = attrs.get("Config") or {}
        host_config = dict(attrs.get("HostConfig") or {})
        payload: dict[str, Any] = {
            "Image": new_image,
            "Env": config.get("Env") or [],
            "Cmd": config.get("Cmd"),
            "Entrypoint": config.get("Entrypoint"),
            "Labels": config.get("Labels") or {},
            "ExposedPorts": config.get("ExposedPorts") or {},
            "Healthcheck": config.get("Healthcheck"),
            "User": config.get("User") or "",
            "WorkingDir": config.get("WorkingDir") or "",
            "HostConfig": host_config,
        }
        networks = dict(
            ((attrs.get("NetworkSettings") or {}).get("Networks")) or {}
        )
        networking_config: dict[str, Any] = {}
        extra_networks: dict[str, Any] = {}
        for index, (net_name, endpoint) in enumerate(networks.items()):
            endpoint = endpoint or {}
            aliases = [
                alias
                for alias in (endpoint.get("Aliases") or [])
                # Drop the auto-generated short-container-id alias.
                if not (len(alias) == 12 and all(c in "0123456789abcdef" for c in alias))
            ]
            endpoint_config: dict[str, Any] = {}
            if aliases:
                endpoint_config["Aliases"] = aliases
            if index == 0:
                networking_config = {
                    "EndpointsConfig": {net_name: endpoint_config}
                }
            else:
                extra_networks[net_name] = endpoint_config
        if networking_config:
            payload["NetworkingConfig"] = networking_config
        return payload, extra_networks

    def _find_data_mount_source(self, attrs: dict[str, Any]) -> str:
        """Host-side source (bind path or volume name) of the data dir.

        The helper container mounts the same source at ``/data`` so both
        processes share ``.upgrade-request`` / ``.upgrade-status``.
        """
        wanted = str(self._data_dir)
        for mount in attrs.get("Mounts") or []:
            if not isinstance(mount, dict):
                continue
            if mount.get("Destination") != wanted:
                continue
            source = mount.get("Name") or mount.get("Source")
            if isinstance(source, str) and source:
                return source
        raise RuntimeError(
            f"no mount found for data dir {wanted!r} — is the data volume "
            "mounted into this container?"
        )

    def _launch_helper(self, client: Any, context: dict[str, Any]) -> None:
        """Fire the detached one-shot helper container."""
        # Clear any stale helper from a previous attempt.
        try:
            stale = client.containers.get(_HELPER_CONTAINER_NAME)
            stale.remove(force=True)
        except Exception:  # noqa: BLE001 — not found / already gone
            pass
        volumes = {
            "/var/run/docker.sock": {
                "bind": "/var/run/docker.sock",
                "mode": "rw",
            },
            context["data_mount_source"]: {"bind": "/data", "mode": "rw"},
        }
        client.containers.run(
            image=context["own_image"],
            entrypoint=[_HELPER_PYTHON, _HELPER_SCRIPT_PATH],
            name=_HELPER_CONTAINER_NAME,
            detach=True,
            auto_remove=True,
            user="0",
            environment=["CORLINMAN_DATA_DIR=/data"],
            volumes=volumes,
            restart_policy={"Name": "no"},
            network_mode="none",
        )

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def _delete_request_file(self) -> None:
        try:
            (self._data_dir / _REQUEST_FILE_NAME).unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Helper status mirroring
    # ------------------------------------------------------------------

    async def _mirror_helper_status(self, req: UpgradeRequest) -> None:
        """Mirror ``.upgrade-status`` into the store until terminal.

        In the happy path the helper stops THIS container mid-loop and
        the boot finalizer settles the record. This mirror exists for the
        early failures (helper never started, request rejected) and for
        the log tail while the pull→swap window lasts.
        """
        status_path = self._data_dir / _STATUS_FILE_NAME
        helper_request_id = str(uuid.UUID(req.request_id))
        first_seen = time.monotonic()
        deadline = first_seen + _HELPER_OVERALL_TIMEOUT_SECONDS
        last_payload: dict[str, Any] | None = None
        ever_observed = False
        while True:
            now = time.monotonic()
            if now > deadline:
                await self._safe_update(
                    req.request_id,
                    state="failed",
                    phase="timeout",
                    error="overall_timeout",
                    finished_at=_now_ms(),
                )
                return
            payload = self._read_status_file(status_path, helper_request_id)
            if payload is not None:
                ever_observed = True
                if payload != last_payload:
                    last_payload = payload
                    await self._apply_helper_payload(req, payload)
                    if str(payload.get("state")) in ("succeeded", "failed"):
                        return
            elif (
                not ever_observed
                and now - first_seen > _HELPER_STALL_TIMEOUT_SECONDS
            ):
                await self._safe_update(
                    req.request_id,
                    state="stalled",
                    phase="stalled",
                    error="helper_container_never_reported",
                    finished_at=_now_ms(),
                )
                return
            await asyncio.sleep(_STATUS_POLL_INTERVAL_SECONDS)

    @staticmethod
    def _read_status_file(
        status_path: Path, expected_request_id: str
    ) -> dict[str, Any] | None:
        try:
            raw = status_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("request_id") != expected_request_id:
            return None
        return payload

    async def _apply_helper_payload(
        self, req: UpgradeRequest, payload: dict[str, Any]
    ) -> None:
        state_raw = str(payload.get("state", "running"))
        state = (
            state_raw
            if state_raw in ("queued", "running", "succeeded", "failed")
            else "running"
        )
        fields: dict[str, Any] = {"state": state, "phase": state}
        finished = payload.get("finished_at")
        if isinstance(finished, int):
            fields["finished_at"] = finished
        elif state in ("succeeded", "failed"):
            fields["finished_at"] = _now_ms()
        err = payload.get("error")
        if err is None or isinstance(err, str):
            fields["error"] = err
        log_excerpt = payload.get("log_excerpt")
        if isinstance(log_excerpt, str):
            fields["log_excerpt"] = log_excerpt
        rolled_back = payload.get("rolled_back")
        if isinstance(rolled_back, bool):
            fields["rolled_back"] = rolled_back
        version_verified = payload.get("version_verified")
        if isinstance(version_verified, bool):
            fields["version_verified"] = version_verified
        await self._safe_update(req.request_id, **fields)

    async def _safe_update(self, request_id: str, **fields: Any) -> None:
        try:
            await self._store.update(request_id, **fields)
        except KeyError:
            return

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


# Tell the static checker DockerUpgrader satisfies UpgraderProtocol.
_: type[UpgraderProtocol] = DockerUpgrader  # type: ignore[assignment]
