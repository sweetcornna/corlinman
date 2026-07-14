#!/usr/bin/env python3
"""corlinman docker upgrade helper — runs OUTSIDE the container it replaces.

The in-container ``DockerUpgrader`` used to stop/recreate the ``corlinman``
container *from inside that same container*, killing its own orchestration
mid-swap (and, on the SDK path, after the old container was already
removed — no rollback possible). This helper is the fix, mirroring the
native systemd-helper pattern:

1. The gateway pulls the new image, captures the running container's spec
   into ``$CORLINMAN_DATA_DIR/.upgrade-request`` and launches THIS script
   as a detached one-shot container (using the gateway's *current* image,
   which is guaranteed to carry the script — so rollbacks to versions
   predating it still work), with the docker socket + data volume mounted.
2. The helper performs the swap:
   stop ``corlinman`` → rename to ``corlinman-previous`` (kept as the
   instant-rollback slot — sub2api's ``.backup`` analog) → create + start
   the new container from the captured spec → wait healthy → assert the
   reported ``/health`` version equals the target.
3. On any failure it restores ``corlinman-previous`` and reports
   ``rolled_back: true``. Every transition is written atomically to
   ``$CORLINMAN_DATA_DIR/.upgrade-status`` (same contract as
   ``deploy/corlinman-upgrader.sh``); the restarted gateway's boot
   finalizer mirrors the terminal verdict into its state store.

``action: "rollback_instant"`` swaps the current container with the kept
``corlinman-previous`` (no pull, no spec replay) — the two names simply
trade places, so a second rollback undoes the first.

Stdlib-only on purpose: it talks to the Docker Engine REST API over the
unix socket via ``http.client`` (the runtime image ships no docker CLI,
no jq, and this script must not depend on the venv's third-party
packages surviving a version skew).
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("CORLINMAN_DATA_DIR", "/data"))
DOCKER_SOCK = os.environ.get("CORLINMAN_DOCKER_SOCK", "/var/run/docker.sock")
REQUEST_FILE = DATA_DIR / ".upgrade-request"
PROCESSED_FILE = DATA_DIR / ".upgrade-request.processed"
STATUS_FILE = DATA_DIR / ".upgrade-status"

# Engine API version prefix. v1.41 (Docker 20.10) is old enough to be
# universally available and new enough for everything used here.
API = "/v1.41"

_LOG_CAP_BYTES = 4096
_STOP_TIMEOUT_S = 30
_START_GRACE_S = 2.0
_HEALTH_SUSTAIN_S = 10.0


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Log:
    """Rolling in-memory log; tail lands in every status write."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, line: str) -> None:
        self.lines.append(line)
        print(line, flush=True)

    def tail(self) -> str:
        text = "\n".join(self.lines) + "\n"
        encoded = text.encode("utf-8")[-_LOG_CAP_BYTES:]
        return encoded.decode("utf-8", errors="ignore")


LOG = _Log()


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


class EngineError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"engine {status}: {message}")
        self.status = status


def _engine(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 60.0,
    raw: bool = False,
) -> Any:
    """One Docker Engine API request. Raises :class:`EngineError` on 4xx/5xx."""
    conn = _UnixHTTPConnection(DOCKER_SOCK, timeout)
    try:
        payload = None
        headers = {}
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, API + path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 400:
            try:
                message = json.loads(data).get("message", "")
            except (json.JSONDecodeError, ValueError, AttributeError):
                message = data[:200].decode("utf-8", errors="ignore")
            raise EngineError(resp.status, message)
        if raw:
            return data
        if not data:
            return None
        try:
            return json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return None
    finally:
        conn.close()


def write_status(
    request_id: str,
    state: str,
    *,
    error: str | None = None,
    started_at: int,
    rolled_back: bool | None = None,
    version_verified: bool | None = None,
) -> None:
    """Atomic single-document status write (same contract as the native
    helper; the gateway's poller + boot finalizer both read it)."""
    payload: dict[str, Any] = {
        "request_id": request_id,
        "state": state,
        "error": error,
        "started_at": started_at,
        "finished_at": _now_ms() if state in ("succeeded", "failed") else None,
        "log_excerpt": LOG.tail(),
    }
    if rolled_back is not None:
        payload["rolled_back"] = rolled_back
    if version_verified is not None:
        payload["version_verified"] = version_verified
    tmp = STATUS_FILE.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, json.dumps(payload, ensure_ascii=False, indent=2).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, STATUS_FILE)


# ---------------------------------------------------------------------------
# Container primitives
# ---------------------------------------------------------------------------


def _inspect(name: str) -> dict[str, Any] | None:
    try:
        result = _engine("GET", f"/containers/{name}/json")
        return result if isinstance(result, dict) else None
    except EngineError as exc:
        if exc.status == 404:
            return None
        raise


def _stop(name: str) -> None:
    try:
        _engine(
            "POST",
            f"/containers/{name}/stop?t={_STOP_TIMEOUT_S}",
            timeout=_STOP_TIMEOUT_S + 30,
        )
    except EngineError as exc:
        if exc.status not in (304, 404):  # already stopped / gone is fine
            raise


def _rename(old: str, new: str) -> None:
    _engine("POST", f"/containers/{old}/rename?name={new}")


def _remove(name: str, *, force: bool = True) -> None:
    try:
        _engine("DELETE", f"/containers/{name}?force={'true' if force else 'false'}")
    except EngineError as exc:
        if exc.status != 404:
            raise


def _create(name: str, payload: dict[str, Any]) -> str:
    result = _engine("POST", f"/containers/create?name={name}", payload)
    container_id = (result or {}).get("Id")
    if not isinstance(container_id, str):
        raise EngineError(500, "create returned no container id")
    return container_id


def _start(name: str) -> None:
    try:
        _engine("POST", f"/containers/{name}/start")
    except EngineError as exc:
        if exc.status != 304:  # already started
            raise


def _connect_networks(container_id: str, networks: dict[str, Any]) -> None:
    """Best-effort re-attach of secondary networks + aliases.

    The create payload's ``NetworkingConfig`` can only carry ONE endpoint;
    every additional network from the captured spec is connected here.
    Failures are logged, not fatal — the primary network is what serving
    traffic depends on.
    """
    for net_name, endpoint in networks.items():
        try:
            _engine(
                "POST",
                f"/networks/{net_name}/connect",
                {"Container": container_id, "EndpointConfig": endpoint},
            )
        except EngineError as exc:
            if exc.status == 403 and "already exists" in str(exc):
                continue
            LOG.add(f"[warn] network connect {net_name} failed: {exc}")


def _exec_capture(name: str, cmd: list[str], *, timeout: float = 15.0) -> str:
    """Run ``cmd`` inside a running container, return combined output.

    Docker multiplexes stdout/stderr into 8-byte-header frames when the
    exec has no TTY; strip the headers.
    """
    created = _engine(
        "POST",
        f"/containers/{name}/exec",
        {"AttachStdout": True, "AttachStderr": True, "Cmd": cmd},
        timeout=timeout,
    )
    exec_id = (created or {}).get("Id")
    if not isinstance(exec_id, str):
        raise EngineError(500, "exec create returned no id")
    stream = _engine(
        "POST",
        f"/exec/{exec_id}/start",
        {"Detach": False, "Tty": False},
        timeout=timeout,
        raw=True,
    )
    output = bytearray()
    view = memoryview(bytes(stream or b""))
    offset = 0
    while offset + 8 <= len(view):
        _, _, _, _, size = struct.unpack(">BBBBI", view[offset : offset + 8])
        offset += 8
        output.extend(view[offset : offset + size])
        offset += size
    if not output and stream:
        # Non-multiplexed fallback (some engines return plain bytes).
        output.extend(bytes(stream))
    return output.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Health + version assertion
# ---------------------------------------------------------------------------


def _wait_healthy(name: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    running_since: float | None = None
    while time.monotonic() < deadline:
        attrs = _inspect(name)
        state = (attrs or {}).get("State") or {}
        health = (state.get("Health") or {}).get("Status")
        running = bool(state.get("Running"))
        if health == "healthy":
            return True
        if health is None and running:
            if running_since is None:
                running_since = time.monotonic()
            elif time.monotonic() - running_since >= _HEALTH_SUSTAIN_S:
                return True
        else:
            running_since = None
        time.sleep(1.0)
    return False


def _normalize_version(value: str) -> str:
    value = value.strip()
    return value[1:] if value[:1] in ("v", "V") else value


def _assert_version(name: str, target: str, port: int) -> bool | None:
    """``True``/``False`` = assertion ran; ``None`` = couldn't run (no curl
    in an older image, exec failure) — treated as "unknown", not fatal."""
    try:
        raw = _exec_capture(
            name, ["curl", "-fsS", f"http://localhost:{port}/health"]
        )
        payload = json.loads(raw.strip() or "{}")
        reported = payload.get("version")
    except (EngineError, OSError, json.JSONDecodeError, ValueError) as exc:
        LOG.add(f"[warn] version probe failed (skipping assertion): {exc}")
        return None
    if not isinstance(reported, str) or not reported:
        # Pre-PR1 images have no version field on /health — unknown.
        LOG.add("[warn] /health carries no version field; skipping assertion")
        return None
    ok = _normalize_version(reported) == _normalize_version(target)
    LOG.add(f"[info] version assertion: reported={reported} target={target} ok={ok}")
    return ok


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


def _restore_previous(current: str, previous: str) -> bool:
    """Failure path: remove the (broken) new container, promote the kept
    previous one back. Returns ``True`` when the old service is running."""
    try:
        _remove(current)
    except EngineError as exc:
        LOG.add(f"[warn] removing failed container: {exc}")
    try:
        _rename(previous, current)
        _start(current)
        LOG.add("[ok] previous container restored")
        return True
    except EngineError as exc:
        LOG.add(f"[fail] rollback restore failed: {exc}")
        return False


def _run_upgrade(req: dict[str, Any], started_at: int) -> int:
    request_id = req["request_id"]
    current = str(req.get("container_name") or "corlinman")
    previous = str(req.get("previous_name") or f"{current}-previous")
    target = str(req.get("target_version") or req.get("tag") or "")
    port = int(req.get("health_port") or 6005)
    health_timeout = float(req.get("health_timeout_s") or 120)
    create_payload = req.get("create_payload")
    extra_networks = req.get("extra_networks") or {}
    if not isinstance(create_payload, dict):
        write_status(request_id, "failed", error="create_payload_missing", started_at=started_at)
        return 1

    if _inspect(current) is None:
        write_status(request_id, "failed", error="container_not_found", started_at=started_at)
        return 1

    # Clear the previous rollback slot — this upgrade mints a new one.
    _remove(previous)

    # Guarded stop+rename: a failure between "stopped" and "renamed"
    # (engine timeout on stop — an OSError, not EngineError — or a rename
    # 409) previously escaped to the top-level handler with the service
    # STOPPED and nothing to restart it. Restart the original in place
    # and report; nothing has been replaced yet.
    LOG.add(f"[info] stopping {current}")
    try:
        _stop(current)
        _rename(current, previous)
    except (EngineError, OSError) as exc:
        LOG.add(f"[fail] stop/rename before swap: {exc}; restarting original")
        restored = False
        try:
            # Whichever name the container ended up under, bring it back.
            if _inspect(current) is not None:
                _start(current)
            elif _inspect(previous) is not None:
                _rename(previous, current)
                _start(current)
            restored = _inspect(current) is not None
        except (EngineError, OSError) as restart_exc:
            LOG.add(f"[fail] restart of original failed: {restart_exc}")
        write_status(
            request_id, "failed",
            error=f"swap_prepare_failed: {exc}"[:300],
            started_at=started_at, rolled_back=restored,
        )
        return 1
    LOG.add(f"[ok] kept rollback slot: {previous}")

    try:
        container_id = _create(current, create_payload)
        _start(current)
        LOG.add(f"[ok] started {current} ({container_id[:12]})")
        if isinstance(extra_networks, dict) and extra_networks:
            _connect_networks(container_id, extra_networks)
    except EngineError as exc:
        LOG.add(f"[fail] create/start new container: {exc}")
        rolled = _restore_previous(current, previous)
        write_status(
            request_id, "failed",
            error=f"recreate_failed: {exc}"[:300],
            started_at=started_at, rolled_back=rolled,
        )
        return 1

    time.sleep(_START_GRACE_S)
    if not _wait_healthy(current, health_timeout):
        LOG.add(f"[fail] {current} not healthy within {health_timeout:.0f}s")
        rolled = _restore_previous(current, previous)
        write_status(
            request_id, "failed", error="healthcheck_timeout",
            started_at=started_at, rolled_back=rolled,
        )
        return 1

    verified = _assert_version(current, target, port)
    if verified is False:
        LOG.add("[fail] new container reports the WRONG version")
        rolled = _restore_previous(current, previous)
        write_status(
            request_id, "failed", error="version_assertion_failed",
            started_at=started_at, rolled_back=rolled, version_verified=False,
        )
        return 1

    LOG.add("[ok] upgrade complete; previous kept for instant rollback")
    write_status(
        request_id, "succeeded",
        started_at=started_at, version_verified=verified,
    )
    return 0


def _run_rollback_instant(req: dict[str, Any], started_at: int) -> int:
    request_id = req["request_id"]
    current = str(req.get("container_name") or "corlinman")
    previous = str(req.get("previous_name") or f"{current}-previous")
    target = str(req.get("target_version") or "")
    port = int(req.get("health_port") or 6005)
    health_timeout = float(req.get("health_timeout_s") or 120)
    swap_tmp = f"{current}-swap-tmp"

    if _inspect(previous) is None:
        write_status(request_id, "failed", error="rollback_slot_missing", started_at=started_at)
        return 1

    LOG.add(f"[info] instant rollback: swapping {current} <-> {previous}")
    _remove(swap_tmp)
    _stop(current)
    # Track how far the 3-rename swap got so a mid-way failure is judged
    # by what actually happened, not blanket-failed: once the target has
    # been promoted to ``current`` (step >= 2) the rollback is de-facto
    # done even if the housekeeping rename of the old container failed.
    step = 0
    try:
        _rename(current, swap_tmp)
        step = 1
        _rename(previous, current)
        step = 2
        _rename(swap_tmp, previous)
        step = 3
        _start(current)
    except (EngineError, OSError) as exc:
        if step >= 2:
            LOG.add(
                f"[warn] housekeeping rename failed after promotion: {exc}; "
                f"old container stranded as {swap_tmp} (reclaimed by the "
                "next upgrade). Continuing with the promoted target."
            )
            try:
                _start(current)
            except (EngineError, OSError) as start_exc:
                LOG.add(f"[fail] promoted target failed to start: {start_exc}")
                write_status(
                    request_id, "failed",
                    error=f"rollback_swap_failed: {start_exc}"[:300],
                    started_at=started_at,
                )
                return 1
            # Fall through to the shared health/version verdict below.
        else:
            LOG.add(f"[fail] swap failed mid-way: {exc}; attempting restore")
            # Best-effort un-tangle: whichever container holds the tmp
            # name goes back to being current.
            try:
                if _inspect(swap_tmp) is not None and _inspect(current) is None:
                    _rename(swap_tmp, current)
                _start(current)
            except (EngineError, OSError) as restore_exc:
                LOG.add(f"[fail] restore failed: {restore_exc}")
            write_status(
                request_id, "failed",
                error=f"rollback_swap_failed: {exc}"[:300],
                started_at=started_at,
            )
            return 1

    time.sleep(_START_GRACE_S)
    if not _wait_healthy(current, health_timeout):
        # The rollback target never came up — swap BACK so the version
        # that was serving before this request keeps serving, instead of
        # leaving the box on an unhealthy container (Codex #122 review).
        LOG.add(
            f"[fail] rollback target unhealthy after {health_timeout:.0f}s; "
            "restoring the previously running container"
        )
        restored = False
        try:
            _stop(current)
            _rename(current, swap_tmp)
            _rename(previous, current)
            _rename(swap_tmp, previous)
            _start(current)
            restored = _wait_healthy(current, health_timeout)
        except EngineError as exc:
            LOG.add(f"[fail] restore swap failed: {exc}")
        write_status(
            request_id, "failed", error="healthcheck_timeout",
            started_at=started_at, rolled_back=restored,
        )
        return 1
    verified = _assert_version(current, target, port) if target else None
    LOG.add("[ok] rollback complete; the two containers traded places")
    write_status(
        request_id, "succeeded",
        started_at=started_at, version_verified=verified,
    )
    return 0


def main() -> int:
    started_at = _now_ms()
    if not REQUEST_FILE.is_file():
        print("no upgrade request — nothing to do", flush=True)
        return 0
    try:
        req = json.loads(REQUEST_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"fatal: unreadable request file: {exc}", file=sys.stderr)
        REQUEST_FILE.replace(PROCESSED_FILE)
        return 1
    if not isinstance(req, dict) or req.get("mode") != "docker":
        print("fatal: not a docker upgrade request", file=sys.stderr)
        REQUEST_FILE.replace(PROCESSED_FILE)
        return 1
    request_id = str(req.get("request_id") or "")
    if not request_id:
        REQUEST_FILE.replace(PROCESSED_FILE)
        return 1

    write_status(request_id, "running", started_at=started_at)
    try:
        if req.get("action") == "rollback_instant":
            code = _run_rollback_instant(req, started_at)
        else:
            code = _run_upgrade(req, started_at)
    except Exception as exc:  # noqa: BLE001 — always leave a verdict
        LOG.add(f"[fail] unexpected: {exc}")
        write_status(
            request_id, "failed",
            error=f"helper_exception: {exc}"[:300], started_at=started_at,
        )
        code = 1
    # Success removes the request; failure parks it so the systemd-style
    # "don't refire on the same garbage" semantics match the native helper.
    try:
        if code == 0:
            REQUEST_FILE.unlink(missing_ok=True)
            PROCESSED_FILE.unlink(missing_ok=True)
        elif REQUEST_FILE.is_file():
            REQUEST_FILE.replace(PROCESSED_FILE)
    except OSError:
        pass
    return code


if __name__ == "__main__":
    sys.exit(main())
