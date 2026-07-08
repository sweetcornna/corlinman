"""Tests for the sandbox ``Environment`` seam (Wave D part 1).

The selector + local backend are the parity default: existing tool tests
(``test_coding_tools.py``, ``test_shell_tasks.py``, ``test_gf_new_tools_repl.py``)
are the behavioral guard; these cover the new seam directly — backend
selection, a local spawn round-trip, handle termination, and the
dispatcher's never-raise envelope on an unknown backend.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from corlinman_agent.coding import (
    DockerEnvironment,
    Environment,
    LocalEnvironment,
    get_environment,
)
from corlinman_agent.coding.environment import (
    _DEFAULT_SANDBOX_IMAGE,
    ENV_SANDBOX_BACKEND,
    ENV_SANDBOX_IMAGE,
    ENV_SANDBOX_USER,
    DaemonUnavailableError,
    _docker_run_argv,
)
from corlinman_agent.coding.shell import dispatch_run_shell


def _args(**kw: object) -> bytes:
    return json.dumps(kw).encode("utf-8")


# ---------------------------------------------------------------------------
# selector
# ---------------------------------------------------------------------------


def test_selector_defaults_to_local_when_unset() -> None:
    # Empty injected env → no backend key → the local default.
    assert isinstance(get_environment(env={}), LocalEnvironment)


def test_selector_default_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_SANDBOX_BACKEND, raising=False)
    assert isinstance(get_environment(), LocalEnvironment)


def test_selector_explicit_local_and_empty_string() -> None:
    assert isinstance(
        get_environment(env={ENV_SANDBOX_BACKEND: "local"}), LocalEnvironment
    )
    assert isinstance(get_environment(env={ENV_SANDBOX_BACKEND: ""}), LocalEnvironment)
    # Case / whitespace insensitive, mirroring the journal-backend selector.
    assert isinstance(
        get_environment(env={ENV_SANDBOX_BACKEND: "  LOCAL  "}), LocalEnvironment
    )


def test_selector_docker_returns_docker_stub() -> None:
    env = get_environment(env={ENV_SANDBOX_BACKEND: "docker"})
    assert isinstance(env, DockerEnvironment)
    assert isinstance(env, Environment)


def test_selector_unknown_backend_raises_naming_env_var() -> None:
    with pytest.raises(RuntimeError) as exc:
        get_environment(env={ENV_SANDBOX_BACKEND: "bogus"})
    # The message names the env var so an operator can fix the config.
    assert ENV_SANDBOX_BACKEND in str(exc.value)
    assert "bogus" in str(exc.value)


# ---------------------------------------------------------------------------
# Docker backend — argv builder (pure, no docker/daemon contact)
# ---------------------------------------------------------------------------


def test_docker_argv_shell_has_hardening_flags_and_mount(tmp_path: Path) -> None:
    argv = _docker_run_argv(
        name="corlinman-sbx-abc",
        workspace=tmp_path,
        image=_DEFAULT_SANDBOX_IMAGE,
        exec_argv=["/bin/sh", "-c", "echo hi"],
        interactive=False,
        env={},
    )
    assert argv[0] == "docker"
    assert argv[1] == "run"
    assert "--rm" in argv
    # Container name is the kill token — passed via --name.
    assert argv[argv.index("--name") + 1] == "corlinman-sbx-abc"
    # Workspace bind-mount uses the ABSOLUTE path, mounted at /workspace, cwd there.
    assert argv[argv.index("-v") + 1] == f"{tmp_path}:/workspace"
    assert argv[argv.index("-w") + 1] == "/workspace"
    # Hardening knobs copied in spirit from shadow-tester's sandbox.
    for flag in (
        "--security-opt=no-new-privileges",
        "--pids-limit=64",
        "--memory=2g",
        "--memory-swap=2g",
    ):
        assert flag in argv, flag
    # rlimits are docker --ulimit flags (no preexec_fn on the docker path).
    assert "nofile=256:256" in argv
    assert "cpu=60:60" in argv
    assert "fsize=104857600:104857600" in argv
    # Image, then the in-container command, come last.
    assert argv[-4:] == [_DEFAULT_SANDBOX_IMAGE, "/bin/sh", "-c", "echo hi"]


def test_docker_argv_image_override_and_default(tmp_path: Path) -> None:
    default_argv = _docker_run_argv(
        name="n",
        workspace=tmp_path,
        image=_DEFAULT_SANDBOX_IMAGE,
        exec_argv=["/bin/sh", "-c", "true"],
        interactive=False,
        env={},
    )
    assert _DEFAULT_SANDBOX_IMAGE in default_argv
    custom_argv = _docker_run_argv(
        name="n",
        workspace=tmp_path,
        image="ghcr.io/acme/sbx:1",
        exec_argv=["/bin/sh", "-c", "true"],
        interactive=False,
        env={},
    )
    # The image is the token immediately preceding the in-container command
    # (exec_argv == ["/bin/sh", "-c", "true"], so the image is argv[-4]).
    assert custom_argv[-4] == "ghcr.io/acme/sbx:1"
    assert default_argv[-4] == _DEFAULT_SANDBOX_IMAGE


def test_docker_argv_interactive_only_adds_stdin_flag(tmp_path: Path) -> None:
    without = _docker_run_argv(
        name="n",
        workspace=tmp_path,
        image="img",
        exec_argv=["/bin/sh", "-c", "true"],
        interactive=False,
        env={},
    )
    assert "-i" not in without
    with_i = _docker_run_argv(
        name="n",
        workspace=tmp_path,
        image="img",
        exec_argv=["/bin/sh", "-c", "true"],
        interactive=True,
        env={},
    )
    assert "-i" in with_i
    # ``-i`` sits before the image, never a ``-t`` alongside it.
    assert "-t" not in with_i
    assert with_i.index("-i") < with_i.index("img")


def test_docker_argv_env_forward_limited_to_lang_lc_tz(tmp_path: Path) -> None:
    argv = _docker_run_argv(
        name="n",
        workspace=tmp_path,
        image="img",
        exec_argv=["/bin/sh", "-c", "true"],
        interactive=False,
        env={
            "LANG": "en_US.UTF-8",
            "LC_ALL": "C",
            "TZ": "UTC",
            # These MUST NOT be forwarded — the image owns them, and PATH/HOME
            # leakage would confuse the container.
            "PATH": "/host/bin",
            "HOME": "/root",
            "USER": "cornna",
            "OPENAI_API_KEY": "secret",
        },
    )
    assert "-e" in argv
    forwarded = {argv[i + 1] for i, tok in enumerate(argv) if tok == "-e"}
    assert forwarded == {"LANG=en_US.UTF-8", "LC_ALL=C", "TZ=UTC"}
    # Secrets / host-owned vars never appear anywhere in the argv.
    joined = " ".join(argv)
    assert "OPENAI_API_KEY" not in joined
    assert "/host/bin" not in joined
    assert "USER=" not in joined


def test_docker_argv_repl_is_direct_interpreter_argv(tmp_path: Path) -> None:
    argv = _docker_run_argv(
        name="n",
        workspace=tmp_path,
        image="img",
        exec_argv=["python3", "-u", "-i", "-q"],
        interactive=True,
        env={},
    )
    # REPL runs the interpreter directly (no /bin/sh -c wrapper), always -i,
    # never a tty (-t would corrupt the repl marker/prompt framing). The
    # interpreter argv is the tail, immediately after the image.
    assert argv[-6:] == ["-i", "img", "python3", "-u", "-i", "-q"]
    assert "/bin/sh" not in argv
    assert "-t" not in argv


def test_docker_argv_user_flag_only_when_set(tmp_path: Path) -> None:
    # Default: no --user — the image's own default user (root for the stock
    # python image) applies.
    plain = _docker_run_argv(
        name="n",
        workspace=tmp_path,
        image="img",
        exec_argv=["/bin/sh", "-c", "true"],
        interactive=False,
        env={},
    )
    assert "--user" not in plain
    remapped = _docker_run_argv(
        name="n",
        workspace=tmp_path,
        image="img",
        exec_argv=["/bin/sh", "-c", "true"],
        interactive=False,
        env={},
        user="1000:1000",
    )
    assert remapped[remapped.index("--user") + 1] == "1000:1000"
    # The flag belongs to docker run, so it must precede the image token.
    assert remapped.index("--user") < remapped.index("img")


# ---------------------------------------------------------------------------
# Docker backend — daemon-absent gate (ungated: monkeypatch which)
# ---------------------------------------------------------------------------


async def test_docker_spawn_without_binary_raises_daemon_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    env = DockerEnvironment()
    with pytest.raises(DaemonUnavailableError) as exc:
        await env.spawn_shell("echo hi", workspace=tmp_path)
    # Actionable: names the binary and how to recover.
    msg = str(exc.value)
    assert "docker binary not found" in msg
    assert ENV_SANDBOX_BACKEND in msg
    # DaemonUnavailableError IS an OSError, so the dispatchers' spawn_failed
    # envelope paths handle it with zero site changes.
    assert isinstance(exc.value, OSError)
    with pytest.raises(DaemonUnavailableError):
        await env.spawn_repl(workspace=tmp_path)


# ---------------------------------------------------------------------------
# Docker backend — spawn wiring (ungated: fake docker binary + fake exec)
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess handle."""

    def __init__(self) -> None:
        self.returncode: int | None = None


async def test_docker_spawn_shell_wires_argv_and_image_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import corlinman_agent.coding.environment as env_mod

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setenv(ENV_SANDBOX_IMAGE, "ghcr.io/acme/sbx:2")
    captured: dict[str, object] = {}

    async def _fake_exec(*argv: str, **kw: object) -> _FakeProc:
        captured["argv"] = list(argv)
        captured["kw"] = kw
        return _FakeProc()

    monkeypatch.setattr(env_mod.asyncio, "create_subprocess_exec", _fake_exec)

    handle = await DockerEnvironment().spawn_shell("echo hi", workspace=tmp_path)
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "docker"
    assert "ghcr.io/acme/sbx:2" in argv
    assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]
    # No stdin pipe requested → no -i and stdin left inheriting.
    assert "-i" not in argv
    assert "stdin" not in captured["kw"]  # type: ignore[operator]
    # The container name is the kill token stored on the handle.
    from corlinman_agent.coding.environment import DockerSpawnedProcess

    assert isinstance(handle, DockerSpawnedProcess)


async def test_docker_spawn_shell_stdin_adds_interactive_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import corlinman_agent.coding.environment as env_mod

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    captured: dict[str, object] = {}

    async def _fake_exec(*argv: str, **kw: object) -> _FakeProc:
        captured["argv"] = list(argv)
        captured["kw"] = kw
        return _FakeProc()

    monkeypatch.setattr(env_mod.asyncio, "create_subprocess_exec", _fake_exec)

    await DockerEnvironment().spawn_shell(
        "cat", workspace=tmp_path, stdin=asyncio.subprocess.PIPE
    )
    assert "-i" in captured["argv"]  # type: ignore[operator]
    assert captured["kw"]["stdin"] == asyncio.subprocess.PIPE  # type: ignore[index]


async def test_docker_spawn_user_remap_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import corlinman_agent.coding.environment as env_mod

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setenv(ENV_SANDBOX_USER, "1000:1000")
    captured: dict[str, object] = {}

    async def _fake_exec(*argv: str, **kw: object) -> _FakeProc:
        captured["argv"] = list(argv)
        return _FakeProc()

    monkeypatch.setattr(env_mod.asyncio, "create_subprocess_exec", _fake_exec)

    await DockerEnvironment().spawn_shell("echo hi", workspace=tmp_path)
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--user") + 1] == "1000:1000"
    # The repl spawn honours the same knob.
    await DockerEnvironment().spawn_repl(workspace=tmp_path)
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--user") + 1] == "1000:1000"


async def test_docker_spawn_repl_wires_direct_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import corlinman_agent.coding.environment as env_mod

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    captured: dict[str, object] = {}

    async def _fake_exec(*argv: str, **kw: object) -> _FakeProc:
        captured["argv"] = list(argv)
        captured["kw"] = kw
        return _FakeProc()

    monkeypatch.setattr(env_mod.asyncio, "create_subprocess_exec", _fake_exec)

    await DockerEnvironment().spawn_repl(workspace=tmp_path)
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[-4:] == ["python3", "-u", "-i", "-q"]
    assert "/bin/sh" not in argv
    assert "-t" not in argv
    # REPL always pipes stdin.
    assert captured["kw"]["stdin"] == asyncio.subprocess.PIPE  # type: ignore[index]


# ---------------------------------------------------------------------------
# Docker backend — real round-trip (GATED on a working docker daemon)
# ---------------------------------------------------------------------------


def _docker_ready() -> bool:
    """True iff docker can actually run the sandbox image.

    Runs (and thereby pre-pulls) the image once so the gated tests below
    are fast and target a real container. A daemon that is down / unreachable
    / cannot pull returns False → the caller skips (a real assertion failure
    is never masked, only infrastructure absence)."""
    try:
        res = subprocess.run(
            ["docker", "run", "--rm", _DEFAULT_SANDBOX_IMAGE, "true"],
            capture_output=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


_DOCKER_UNAVAILABLE = shutil.which("docker") is None


@pytest.mark.skipif(_DOCKER_UNAVAILABLE, reason="docker not available")
async def test_docker_spawn_shell_real_roundtrip(tmp_path: Path) -> None:
    if not _docker_ready():
        pytest.skip("docker daemon unreachable or image unpullable")
    handle = await DockerEnvironment().spawn_shell("echo hi", workspace=tmp_path)
    stdout, _ = await asyncio.wait_for(handle.proc.communicate(), timeout=60)
    assert handle.proc.returncode == 0
    assert b"hi" in stdout


@pytest.mark.skipif(_DOCKER_UNAVAILABLE, reason="docker not available")
async def test_docker_handle_kill_terminates_container(tmp_path: Path) -> None:
    if not _docker_ready():
        pytest.skip("docker daemon unreachable or image unpullable")
    handle = await DockerEnvironment().spawn_shell("sleep 300", workspace=tmp_path)
    # Give the container a beat to start; if the client already exited, the
    # daemon refused the run — skip rather than assert on infra.
    await asyncio.sleep(1.0)
    if handle.proc.returncode is not None:
        pytest.skip("docker run did not start a container")
    handle.kill()
    await asyncio.wait_for(handle.proc.wait(), timeout=30)
    assert handle.proc.returncode is not None


@pytest.mark.skipif(_DOCKER_UNAVAILABLE, reason="docker not available")
async def test_dispatch_run_shell_docker_backend_success_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not _docker_ready():
        pytest.skip("docker daemon unreachable or image unpullable")
    monkeypatch.setenv(ENV_SANDBOX_BACKEND, "docker")
    res = json.loads(
        await dispatch_run_shell(
            args_json=_args(command="echo hi"), workspace=tmp_path
        )
    )
    assert res["exit_code"] == 0
    assert "hi" in res["output"]
    assert "error" not in res


# ---------------------------------------------------------------------------
# LocalEnvironment spawn round-trip
# ---------------------------------------------------------------------------


async def test_local_spawn_shell_roundtrip(tmp_path: Path) -> None:
    handle = await LocalEnvironment().spawn_shell("echo hi", workspace=tmp_path)
    stdout, _ = await handle.proc.communicate()
    assert handle.proc.returncode == 0
    assert b"hi" in stdout


async def test_local_spawn_shell_runs_in_workspace(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("")
    handle = await LocalEnvironment().spawn_shell("ls", workspace=tmp_path)
    stdout, _ = await handle.proc.communicate()
    assert b"marker.txt" in stdout


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX setsid / killpg only apply on POSIX",
)
async def test_handle_kill_terminates_sleeping_child(tmp_path: Path) -> None:
    handle = await LocalEnvironment().spawn_shell("sleep 30", workspace=tmp_path)
    assert handle.proc.returncode is None
    handle.kill()
    await asyncio.wait_for(handle.proc.wait(), timeout=5.0)
    assert handle.proc.returncode is not None


# ---------------------------------------------------------------------------
# dispatcher never-raises on an unknown backend
# ---------------------------------------------------------------------------


async def test_dispatch_run_shell_bogus_backend_returns_spawn_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown backend must fold into a ``spawn_failed`` envelope, not
    crash the dispatcher (the selector's RuntimeError is wrapped)."""
    monkeypatch.setenv(ENV_SANDBOX_BACKEND, "bogus")
    res = json.loads(
        await dispatch_run_shell(args_json=_args(command="echo hi"), workspace=tmp_path)
    )
    assert "spawn_failed" in res["error"]
    assert "exit_code" not in res


async def test_dispatch_run_shell_bogus_backend_background_returns_spawn_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background path is likewise never-raise on an unknown backend."""
    from corlinman_agent.coding.shell_tasks import reset_registry

    monkeypatch.setenv(ENV_SANDBOX_BACKEND, "bogus")
    reset_registry()
    try:
        res = json.loads(
            await dispatch_run_shell(
                args_json=_args(command="echo hi", run_in_background=True),
                workspace=tmp_path,
                session_key="s1",
            )
        )
        assert "spawn_failed" in res["error"]
        assert "task_id" not in res
    finally:
        reset_registry()


# ---------------------------------------------------------------------------
# repl no-spawn-when-disabled still holds through the seam
# ---------------------------------------------------------------------------


async def test_repl_disabled_never_touches_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``execute_code`` disabled, the dispatcher returns early — the
    seam's ``spawn_repl`` is never reached and no session is created."""
    from corlinman_agent.coding.repl import _ENABLE_ENV, _SESSIONS, dispatch_execute_code

    monkeypatch.delenv(_ENABLE_ENV, raising=False)
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(tmp_path))

    # A spawn would only happen via get_environment().spawn_repl(); trip a
    # failure if the seam were reached while disabled.
    def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("get_environment must not be called while disabled")

    monkeypatch.setattr("corlinman_agent.coding.repl.get_environment", _boom)

    out = json.loads(
        await dispatch_execute_code(args_json=_args(code="print(1)"), session_key="k")
    )
    assert out["error"] == "execute_code_disabled"
    assert "k" not in _SESSIONS


# ---------------------------------------------------------------------------
# D2 hardening — docker client setsid, docker-handle teardown, task reap seam
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32", reason="setsid preexec_fn is POSIX-only"
)
async def test_docker_spawn_gives_client_its_own_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The docker CLIENT is spawned with ``preexec_fn=os.setsid`` so a legacy
    killpg over its group can never reach the server's process group."""
    import corlinman_agent.coding.environment as env_mod

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    captured: dict[str, object] = {}

    async def _fake_exec(*argv: str, **kw: object) -> _FakeProc:
        captured["kw"] = kw
        return _FakeProc()

    monkeypatch.setattr(env_mod.asyncio, "create_subprocess_exec", _fake_exec)

    await DockerEnvironment().spawn_shell("echo hi", workspace=tmp_path)
    assert captured["kw"]["preexec_fn"] is os.setsid  # type: ignore[index]
    captured.clear()
    await DockerEnvironment().spawn_repl(workspace=tmp_path)
    assert captured["kw"]["preexec_fn"] is os.setsid  # type: ignore[index]


@pytest.mark.skipif(
    sys.platform == "win32", reason="setsid preexec_fn is POSIX-only"
)
async def test_local_spawn_uses_rlimit_preexec_not_bare_setsid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LOCAL backend keeps ``_preexec_apply_rlimits`` (rlimits + setsid),
    NOT a bare ``os.setsid`` — the docker-only change must not bleed across."""
    import corlinman_agent.coding.environment as env_mod
    from corlinman_agent.coding.environment import _preexec_apply_rlimits

    captured: dict[str, object] = {}

    async def _fake_shell(_cmd: str, **kw: object) -> _FakeProc:
        captured["kw"] = kw
        return _FakeProc()

    monkeypatch.setattr(env_mod.asyncio, "create_subprocess_shell", _fake_shell)

    await LocalEnvironment().spawn_shell("echo hi", workspace=tmp_path)
    assert captured["kw"]["preexec_fn"] is _preexec_apply_rlimits  # type: ignore[index]
    assert captured["kw"]["preexec_fn"] is not os.setsid  # type: ignore[index]


def test_docker_handle_kill_and_reap_invoke_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``kill`` runs ``docker kill`` THEN a host group-kill (unblocks the
    dispatcher's wait even with a hung daemon); ``reap`` additionally sweeps
    the orphan group. Order matters: docker kill first."""
    import corlinman_agent.coding.environment as env_mod
    from corlinman_agent.coding.environment import DockerSpawnedProcess

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        env_mod, "_docker_kill", lambda name: calls.append(("docker_kill", name))
    )
    monkeypatch.setattr(
        env_mod, "kill_process_group", lambda p: calls.append(("killpg", p))
    )
    monkeypatch.setattr(
        env_mod, "reap_orphan_group", lambda p: calls.append(("orphan", p))
    )

    proc = _FakeProc()
    handle = DockerSpawnedProcess(proc, "corlinman-sbx-xyz")  # type: ignore[arg-type]
    handle.kill()
    assert calls == [("docker_kill", "corlinman-sbx-xyz"), ("killpg", proc)]
    calls.clear()
    handle.reap()
    assert calls == [
        ("docker_kill", "corlinman-sbx-xyz"),
        ("killpg", proc),
        ("orphan", proc),
    ]


def test_reap_task_dispatches_on_handle_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ShellTaskRegistry._reap_task`` reaps the module-level group for every
    handle kind, and additionally calls ``handle.reap()`` ONLY for a non-local
    backend — a ``LocalSpawnedProcess`` (or ``None``) leaves ``handle.reap``
    untouched so local behaviour stays byte-identical."""
    import corlinman_agent.coding.shell_tasks as st_mod
    from corlinman_agent.coding.environment import (
        LocalSpawnedProcess,
        SpawnedProcess,
    )
    from corlinman_agent.coding.shell_tasks import ShellTask, ShellTaskRegistry

    reaped: list[object] = []
    monkeypatch.setattr(
        st_mod.ShellTaskRegistry,
        "_reap",
        staticmethod(lambda p: reaped.append(p)),
    )

    handle_reaps: list[object] = []

    class _RecordingHandle(SpawnedProcess):
        def __init__(self, proc: object) -> None:
            self.proc = proc  # type: ignore[assignment]

        def kill(self) -> None:  # pragma: no cover — not exercised here
            pass

        def reap(self) -> None:
            handle_reaps.append(self.proc)

    def _task(proc: object, handle: object) -> ShellTask:
        return ShellTask(
            task_id="t",
            command="c",
            session_key="",
            started_at_ms=0,
            _proc=proc,  # type: ignore[arg-type]
            _handle=handle,  # type: ignore[arg-type]
        )

    reg = ShellTaskRegistry()

    # Non-local handle → both the group reap AND the container teardown.
    nonlocal_proc = _FakeProc()
    reg._reap_task(_task(nonlocal_proc, _RecordingHandle(nonlocal_proc)))
    assert reaped == [nonlocal_proc]
    assert handle_reaps == [nonlocal_proc]

    # Local handle → ONLY the group reap; handle.reap() left untouched.
    reaped.clear()
    handle_reaps.clear()
    local_proc = _FakeProc()
    reg._reap_task(_task(local_proc, LocalSpawnedProcess(local_proc)))  # type: ignore[arg-type]
    assert reaped == [local_proc]
    assert handle_reaps == []

    # No handle → still reaps the group.
    reaped.clear()
    none_proc = _FakeProc()
    reg._reap_task(_task(none_proc, None))
    assert reaped == [none_proc]
    assert handle_reaps == []
