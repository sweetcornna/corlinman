"""Tests for the background shell-task registry + the ``shell_task_*`` tools.

Dim 4 (claude-code parity): ``run_shell(run_in_background=true)`` spawns a
long-running command detached, spills its combined stdout+stderr to a
workspace log file, and lets the model poll it via ``shell_task_output`` /
terminate it via ``shell_task_kill``.

Every test passes an explicit ``workspace=tmp_path`` (registry-level tests)
or resets the module singleton (dispatcher-level tests) so nothing touches
the real ``CORLINMAN_AGENT_WORKSPACE``. The registry spawns real processes,
so the POSIX-only ``setsid`` / ``killpg`` paths are guarded exactly like the
foreground ``run_shell`` tests (``test_coding_tools.py`` :286).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from corlinman_agent.coding import shell_tasks
from corlinman_agent.coding.shell import dispatch_run_shell
from corlinman_agent.coding.shell_tasks import (
    ShellTaskQuotaExceeded,
    ShellTaskRegistry,
    dispatch_shell_task_kill,
    dispatch_shell_task_output,
    get_registry,
    reset_registry,
)


def _args(**kw: object) -> bytes:
    return json.dumps(kw).encode("utf-8")


async def _poll(
    fn: Callable[[], Any], *, timeout: float = 6.0, interval: float = 0.02
) -> Any:
    """Poll ``fn`` until it returns a truthy value or ``timeout`` elapses.

    Returns the truthy value, or the final (falsy) call result on timeout so
    the caller's assertion produces a useful message.
    """
    deadline = time.monotonic() + timeout
    val = fn()
    while not val and time.monotonic() < deadline:
        await asyncio.sleep(interval)
        val = fn()
    return val


@pytest.fixture
async def registry() -> Any:
    """A fresh, isolated :class:`ShellTaskRegistry` torn down after the test."""
    reg = ShellTaskRegistry()
    try:
        yield reg
    finally:
        await reg.shutdown()


@pytest.fixture
async def singleton_reset() -> Any:
    """Reset the module singleton around a dispatcher-level test."""
    reset_registry()
    try:
        yield
    finally:
        reg = shell_tasks._REGISTRY
        if reg is not None:
            await reg.shutdown()
        reset_registry()


# ---------------------------------------------------------------------------
# spawn — returns fast; task runs in the background
# ---------------------------------------------------------------------------


async def test_spawn_returns_fast_while_command_runs(
    registry: ShellTaskRegistry, tmp_path: Path
) -> None:
    """``spawn`` returns immediately with a running task while ``sleep`` runs."""
    t0 = time.monotonic()
    task = await registry.spawn(
        command="sleep 5", session_key="", workspace=tmp_path
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"spawn blocked for {elapsed:.2f}s"
    assert task.status == "running"
    assert task.task_id
    assert task.log_path.startswith(".corlinman/shell_task_")
    # A read confirms the running status while the sleep is in flight.
    snap = registry.read(task.task_id, 0)
    assert snap is not None
    assert snap[2] == "running"


async def test_spawn_foreground_unaffected(tmp_path: Path) -> None:
    """The foreground ``dispatch_run_shell`` path is unchanged by bg mode."""
    res = json.loads(
        await dispatch_run_shell(
            args_json=_args(command="echo hello-fg"), workspace=tmp_path
        )
    )
    assert res["exit_code"] == 0
    assert "hello-fg" in res["output"]
    assert "task_id" not in res


# ---------------------------------------------------------------------------
# read — offset semantics + terminal stamping
# ---------------------------------------------------------------------------


async def test_read_offset_returns_disjoint_chunks(
    registry: ShellTaskRegistry, tmp_path: Path
) -> None:
    """Two successive reads return disjoint chunks with an advancing offset."""
    # A SINGLE exec'd process that emits two bursts with an in-process
    # sleep between them. Kept fork-free (no shell operators) so the
    # inherited RLIMIT_NPROC can't fail a mid-shell fork on a busy host —
    # mirrors the ``python3 -c`` style of the foreground shell tests.
    task = await registry.spawn(
        command=(
            "python3 -c \"import sys, time; "
            "sys.stdout.write('one\\n'); sys.stdout.flush(); "
            "time.sleep(2); "
            "sys.stdout.write('two\\n'); sys.stdout.flush()\""
        ),
        session_key="",
        workspace=tmp_path,
    )
    tid = task.task_id

    # First window: only the pre-sleep line is on disk.
    def _has_one() -> Any:
        r = registry.read(tid, 0)
        return r if (r and "one" in r[0]) else None

    r1 = await _poll(_has_one)
    assert r1 is not None, "first chunk never appeared"
    text1, off1, status1, exit1, _more1 = r1
    assert "one" in text1
    assert "two" not in text1
    assert off1 == len(text1.encode("utf-8"))
    assert status1 == "running"
    assert exit1 is None

    # Second window: read FROM the advanced offset — disjoint from the first.
    def _completed() -> Any:
        r = registry.read(tid, off1)
        return r if (r and r[2] == "completed") else None

    r2 = await _poll(_completed, timeout=8.0)
    assert r2 is not None, "task never completed"
    text2, off2, status2, exit2, _more2 = r2
    assert "two" in text2
    assert "one" not in text2
    assert off2 > off1
    assert status2 == "completed"
    assert exit2 == 0


async def test_read_unknown_task_returns_none(
    registry: ShellTaskRegistry,
) -> None:
    assert registry.read("does-not-exist", 0) is None


async def test_read_records_nonzero_exit_as_failed(
    registry: ShellTaskRegistry, tmp_path: Path
) -> None:
    task = await registry.spawn(
        command="exit 3", session_key="", workspace=tmp_path
    )

    def _terminal() -> Any:
        r = registry.read(task.task_id, 0)
        return r if (r and r[2] != "running") else None

    r = await _poll(_terminal)
    assert r is not None
    assert r[2] == "failed"
    assert r[3] == 3


# ---------------------------------------------------------------------------
# kill — process-group termination (mirrors run_shell :286)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX setsid / killpg only apply on POSIX",
)
async def test_kill_terminates_forked_children(
    registry: ShellTaskRegistry, tmp_path: Path
) -> None:
    """``kill`` reaps the whole process group, not just the shell wrapper."""
    task = await registry.spawn(
        command="python3 -c 'import time; time.sleep(30)'",
        session_key="",
        workspace=tmp_path,
    )
    t0 = time.monotonic()
    killed = await registry.kill(task.task_id)
    elapsed = time.monotonic() - t0
    assert killed is not None
    assert killed.status == "killed"
    assert elapsed < 5.0, f"kill reap took {elapsed:.2f}s — process tree leak?"
    snap = registry.read(task.task_id, 0)
    assert snap is not None
    assert snap[2] == "killed"


async def test_kill_unknown_task_returns_none(
    registry: ShellTaskRegistry,
) -> None:
    assert await registry.kill("does-not-exist") is None


# ---------------------------------------------------------------------------
# Codex #112 (1): per-session ownership on read/kill
# ---------------------------------------------------------------------------


async def test_read_hides_task_from_other_session(
    registry: ShellTaskRegistry, tmp_path: Path
) -> None:
    """A task recorded under one session is invisible (``None`` — behaves as
    task_not_found, no existence leak) to a different session; the owning
    session still reads it."""
    task = await registry.spawn(
        command="sleep 5", session_key="owner", workspace=tmp_path
    )
    # Owner sees it.
    assert registry.read(task.task_id, 0, expected_session_key="owner") is not None
    # Cross-session caller is denied without leaking existence.
    assert registry.read(task.task_id, 0, expected_session_key="intruder") is None


async def test_kill_hides_task_from_other_session(
    registry: ShellTaskRegistry, tmp_path: Path
) -> None:
    """A cross-session kill returns ``None`` and leaves the task running;
    only the owning session can terminate it."""
    task = await registry.spawn(
        command="sleep 5", session_key="owner", workspace=tmp_path
    )
    # Cross-session kill is a no-op that mimics task_not_found.
    assert await registry.kill(task.task_id, expected_session_key="intruder") is None
    snap = registry.read(task.task_id, 0, expected_session_key="owner")
    assert snap is not None
    assert snap[2] == "running", "cross-session kill must not touch the task"
    # Owner kill works.
    killed = await registry.kill(task.task_id, expected_session_key="owner")
    assert killed is not None
    assert killed.status == "killed"


async def test_empty_session_task_accessible_from_any_caller(
    registry: ShellTaskRegistry, tmp_path: Path
) -> None:
    """A task recorded with an empty session_key (direct library callers)
    stays accessible to any caller, even one supplying a session key."""
    task = await registry.spawn(
        command="sleep 5", session_key="", workspace=tmp_path
    )
    assert registry.read(task.task_id, 0, expected_session_key="whoever") is not None
    killed = await registry.kill(task.task_id, expected_session_key="whoever")
    assert killed is not None
    assert killed.status == "killed"


async def test_dispatchers_enforce_session_ownership(
    singleton_reset: None, tmp_path: Path
) -> None:
    """The output/kill dispatchers thread the caller's session key so a
    leaked task_id from another session resolves to task_not_found."""
    res = json.loads(
        await dispatch_run_shell(
            args_json=_args(command="sleep 5", run_in_background=True),
            workspace=tmp_path,
            session_key="owner",
        )
    )
    tid = res["task_id"]
    # Cross-session poll + kill both hide the task.
    cross_out = json.loads(
        dispatch_shell_task_output(
            args_json=_args(task_id=tid), session_key="intruder"
        )
    )
    assert cross_out["error"] == "task_not_found"
    cross_kill = json.loads(
        await dispatch_shell_task_kill(
            args_json=_args(task_id=tid), session_key="intruder"
        )
    )
    assert cross_kill["error"] == "task_not_found"
    # Owner poll + kill succeed.
    owner_out = json.loads(
        dispatch_shell_task_output(
            args_json=_args(task_id=tid), session_key="owner"
        )
    )
    assert owner_out["task_id"] == tid
    owner_kill = json.loads(
        await dispatch_shell_task_kill(
            args_json=_args(task_id=tid), session_key="owner"
        )
    )
    assert owner_kill["status"] == "killed"


# ---------------------------------------------------------------------------
# MAX_CONCURRENT cap + terminal-retention eviction + lifetime watchdog
# ---------------------------------------------------------------------------


async def test_max_concurrent_cap_raises(
    registry: ShellTaskRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceeding the concurrency cap raises :class:`ShellTaskQuotaExceeded`."""
    monkeypatch.setenv("CORLINMAN_SHELL_TASKS_MAX", "1")
    await registry.spawn(command="sleep 5", session_key="", workspace=tmp_path)
    with pytest.raises(ShellTaskQuotaExceeded):
        await registry.spawn(
            command="sleep 5", session_key="", workspace=tmp_path
        )


async def test_max_concurrent_cap_clean_envelope(
    singleton_reset: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatcher folds the cap breach into a clean error envelope."""
    monkeypatch.setenv("CORLINMAN_SHELL_TASKS_MAX", "1")
    first = json.loads(
        await dispatch_run_shell(
            args_json=_args(command="sleep 5", run_in_background=True),
            workspace=tmp_path,
        )
    )
    assert first["status"] == "running"
    second = json.loads(
        await dispatch_run_shell(
            args_json=_args(command="sleep 5", run_in_background=True),
            workspace=tmp_path,
        )
    )
    assert "task_id" not in second
    assert "error" in second
    assert "shell_tasks_busy" in second["error"]


async def test_terminal_retention_evicts_oldest(
    registry: ShellTaskRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded terminal deque evicts the oldest finished record."""
    monkeypatch.setattr(shell_tasks, "_TERMINAL_CAP", 2)
    ids: list[str] = []
    for marker in ("a", "b", "c"):
        task = await registry.spawn(
            command=f"echo {marker}", session_key="", workspace=tmp_path
        )
        ids.append(task.task_id)

        def _done(tid: str = task.task_id) -> Any:
            r = registry.read(tid, 0)
            return r if (r and r[2] != "running") else None

        assert await _poll(_done) is not None
    # With cap=2 and three terminal records, the first is evicted.
    assert registry.read(ids[0], 0) is None
    assert registry.read(ids[1], 0) is not None
    assert registry.read(ids[2], 0) is not None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX setsid / killpg only apply on POSIX",
)
async def test_lifetime_watchdog_expires_overrunning_task(
    registry: ShellTaskRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task past the max lifetime is killed and stamped ``expired``."""
    monkeypatch.setenv("CORLINMAN_SHELL_TASK_MAX_LIFETIME_S", "0.5")
    task = await registry.spawn(
        command="sleep 30", session_key="", workspace=tmp_path
    )

    def _expired() -> Any:
        r = registry.read(task.task_id, 0)
        return r if (r and r[2] == "expired") else None

    r = await _poll(_expired, timeout=6.0)
    assert r is not None, "watchdog never fired"
    assert r[2] == "expired"
    assert r[3] is None


# ---------------------------------------------------------------------------
# Codex #112 (2): background log-size cap
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX setsid / killpg only apply on POSIX",
)
async def test_log_cap_kills_and_stamps_log_capped(
    registry: ShellTaskRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task whose output floods past the size cap is killed, stamped
    ``log_capped``, and its log file stays bounded."""
    monkeypatch.setenv("CORLINMAN_SHELL_TASK_MAX_LOG_BYTES", "4096")  # env floor
    # Flood stdout, then sleep so the pump trips the cap while the child is
    # still alive (fork-free single exec, like the offset test).
    task = await registry.spawn(
        command=(
            "python3 -c \"import sys, time; "
            "sys.stdout.write('x' * 500000); sys.stdout.flush(); "
            "time.sleep(30)\""
        ),
        session_key="",
        workspace=tmp_path,
    )

    def _capped() -> Any:
        r = registry.read(task.task_id, 0)
        return r if (r and r[2] == "log_capped") else None

    r = await _poll(_capped, timeout=8.0)
    assert r is not None, "log cap never tripped"
    assert r[2] == "log_capped"
    assert r[3] is None
    # Log bounded: cap + at most one pump chunk of slack + the marker line.
    log_abs = task._log_abs
    assert log_abs is not None
    size = log_abs.stat().st_size
    assert size <= 4096 + shell_tasks._PUMP_CHUNK_BYTES + 128, (
        f"log grew unbounded: {size} bytes"
    )
    # Process actually dead — reaped before the terminal stamp.
    assert task._proc is not None
    assert task._proc.returncode is not None


# ---------------------------------------------------------------------------
# Codex #112 (3): kill the child when spill setup fails
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX setsid / killpg only apply on POSIX",
)
async def test_spill_setup_failure_kills_child(
    registry: ShellTaskRegistry, tmp_path: Path
) -> None:
    """If the pump can't open the spill file (``.corlinman`` is a FILE, so
    a path *under* it is unopenable) the task ends ``failed`` AND the child
    is reaped — never left running outside the concurrency cap."""
    # Occupy the spill directory path with a regular file.
    (tmp_path / ".corlinman").write_text("not a dir")
    task = await registry.spawn(
        command="python3 -c 'import time; time.sleep(30)'",
        session_key="",
        workspace=tmp_path,
    )

    def _failed() -> bool:
        return task.status == "failed"

    ok = await _poll(_failed)
    assert ok, f"pump never stamped failed (status={task.status})"
    assert task.status == "failed"
    # The child must be dead, not orphaned outside the cap.
    assert task._proc is not None
    assert task._proc.returncode is not None
    # And polling the unreadable spill must honour the never-raise
    # envelope contract: read() returns (no output, real status) instead
    # of leaking NotADirectoryError up through the tool dispatcher.
    result = registry.read(task.task_id, 0)
    assert result is not None
    text, _new_offset, status, _exit, _more = result
    assert text == ""
    assert status == "failed"


# ---------------------------------------------------------------------------
# shell_task_output / shell_task_kill dispatchers
# ---------------------------------------------------------------------------


async def test_output_and_kill_dispatchers_roundtrip(
    singleton_reset: None, tmp_path: Path
) -> None:
    """The two new tool dispatchers poll output and terminate a bg task."""
    # Fork-free single command (see the offset test) so the streamed line
    # is emitted before a long in-process sleep, independent of RLIMIT_NPROC.
    res = json.loads(
        await dispatch_run_shell(
            args_json=_args(
                command=(
                    "python3 -c \"import sys, time; "
                    "sys.stdout.write('streamed\\n'); sys.stdout.flush(); "
                    "time.sleep(5)\""
                ),
                run_in_background=True,
            ),
            workspace=tmp_path,
            session_key="s7",
        )
    )
    tid = res["task_id"]
    assert res["status"] == "running"
    assert res["note"].startswith("poll with shell_task_output")

    def _seen() -> Any:
        # The owning session polls its own task (ownership is enforced).
        o = json.loads(
            dispatch_shell_task_output(
                args_json=_args(task_id=tid, offset=0), session_key="s7"
            )
        )
        return o if "output" in o and "streamed" in o["output"] else None

    out = await _poll(_seen)
    assert out is not None
    assert out["task_id"] == tid
    assert out["status"] in ("running", "completed")
    assert out["new_offset"] > 0
    assert out["has_more"] is False
    assert out["log_path"].startswith(".corlinman/shell_task_")

    # A DIFFERENT session cannot poll this task — reported as not found.
    other = json.loads(
        dispatch_shell_task_output(
            args_json=_args(task_id=tid, offset=0), session_key="intruder"
        )
    )
    assert other["error"] == "task_not_found"

    # Unknown task id → task_not_found on both tools.
    unk = json.loads(dispatch_shell_task_output(args_json=_args(task_id="nope")))
    assert unk["error"] == "task_not_found"
    unk_k = json.loads(
        await dispatch_shell_task_kill(args_json=_args(task_id="nope"))
    )
    assert unk_k["error"] == "task_not_found"

    # The owning session kills the still-running sleep.
    killed = json.loads(
        await dispatch_shell_task_kill(
            args_json=_args(task_id=tid), session_key="s7"
        )
    )
    assert killed["task_id"] == tid
    assert killed["status"] == "killed"


async def test_output_dispatcher_missing_task_id(
    singleton_reset: None,
) -> None:
    res = json.loads(dispatch_shell_task_output(args_json=_args()))
    assert "args_invalid" in res["error"]


async def test_kill_dispatcher_missing_task_id(
    singleton_reset: None,
) -> None:
    res = json.loads(await dispatch_shell_task_kill(args_json=_args()))
    assert "args_invalid" in res["error"]


def test_get_registry_is_singleton() -> None:
    reset_registry()
    try:
        assert get_registry() is get_registry()
    finally:
        reset_registry()


# ---------------------------------------------------------------------------
# Codex #112 round 2 — session ownership, bounded reads, daemonize reap
# ---------------------------------------------------------------------------


async def test_owned_task_rejects_empty_session_poll(
    registry: ShellTaskRegistry, tmp_path: Path
) -> None:
    """The gateway normalizes no-session requests to '' — an OWNED task must
    reject that too, or the ownership tag isn't a real boundary (Codex #112 r2)."""
    task = await registry.spawn(
        command="sleep 5", session_key="owner", workspace=tmp_path
    )
    # Empty (normalized no-session) caller is a mismatch → task_not_found.
    assert registry.read(task.task_id, 0, expected_session_key="") is None
    assert await registry.kill(task.task_id, expected_session_key="") is None
    # The owner still sees it.
    assert registry.read(task.task_id, 0, expected_session_key="owner") is not None


async def test_read_caps_response_and_pages(
    registry: ShellTaskRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read returns at most the cap; has_more drives paging (Codex #112 r2)."""
    monkeypatch.setenv("CORLINMAN_SHELL_TASK_READ_MAX_BYTES", "4096")  # floor
    task = await registry.spawn(
        command="python3 -c \"import sys; sys.stdout.write('x'*20000)\"",
        session_key="",
        workspace=tmp_path,
    )

    def _done() -> Any:
        r = registry.read(task.task_id, 0)
        return r if (r and r[2] != "running") else None

    assert await _poll(_done) is not None
    # First read: exactly the cap, has_more True, offset advanced by the cap.
    text, new_offset, _status, _exit, has_more = registry.read(task.task_id, 0)
    assert len(text.encode("utf-8")) == 4096
    assert new_offset == 4096
    assert has_more is True
    # Page the rest from the advanced offset.
    total = len(text.encode("utf-8"))
    off = new_offset
    for _ in range(10):
        t2, off, _s, _e, more = registry.read(task.task_id, off)
        total += len(t2.encode("utf-8"))
        if not more:
            break
    assert total == 20000  # every byte eventually paged through


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX setsid / killpg only apply on POSIX",
)
async def test_reap_orphan_group_kills_setsid_group() -> None:
    """``reap_orphan_group`` SIGKILLs a whole setsid group by pid — the
    mechanism the pump uses so a daemonized child can't outlive its task,
    even after the leader zombie is reaped (Codex #112 r2). Direct, so it
    doesn't depend on a shell fork the host's RLIMIT_NPROC might refuse."""
    import os as _os

    from corlinman_agent.coding.shell import reap_orphan_group

    proc = await asyncio.create_subprocess_exec(
        "sleep",
        "30",
        preexec_fn=_os.setsid,  # group leader, pgid == pid
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    pgid = proc.pid
    assert _os.killpg(pgid, 0) is None  # group is alive
    reap_orphan_group(proc)
    await proc.wait()
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        _os.killpg(pgid, 0)  # ESRCH → the group is gone


async def test_pump_reaps_group_on_natural_exit(
    registry: ShellTaskRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pump reaps the process group on a NATURAL exit — not just on
    kill/watchdog — so a daemonized child is swept even when the task
    completes on its own (Codex #112 r2). Spies the reap call so the wiring
    is pinned without needing a real orphan (fork-limited on some hosts)."""
    reaped: list[int] = []
    import corlinman_agent.coding.shell_tasks as st_mod

    real = st_mod.reap_orphan_group
    monkeypatch.setattr(
        st_mod, "reap_orphan_group", lambda p: (reaped.append(p.pid), real(p))[0]
    )
    task = await registry.spawn(
        command="python3 -c \"print('done')\"", session_key="", workspace=tmp_path
    )

    def _completed() -> Any:
        r = registry.read(task.task_id, 0)
        return r if (r and r[2] == "completed") else None

    assert await _poll(_completed) is not None, "task never completed"
    assert reaped, "pump did not reap the process group on natural exit"


# ---------------------------------------------------------------------------
# Codex #112 round 3 — non-bool bg flag, oversized offset, log-cap reap
# ---------------------------------------------------------------------------


async def test_run_in_background_non_bool_rejected(tmp_path: Path) -> None:
    """A non-boolean run_in_background (e.g. the string "false", which is
    truthy) is rejected — never silently detached (Codex #112 r3)."""
    for bad in ("false", "true", 1, 0):
        res = json.loads(
            await dispatch_run_shell(
                args_json=_args(command="echo x", run_in_background=bad),
                workspace=tmp_path,
            )
        )
        assert "args_invalid" in res.get("error", ""), f"{bad!r} not rejected"
        assert "run_in_background" in res["error"]
    # A real bool still works both ways.
    fg = json.loads(
        await dispatch_run_shell(
            args_json=_args(command="echo fg", run_in_background=False),
            workspace=tmp_path,
        )
    )
    assert fg["exit_code"] == 0 and "task_id" not in fg


async def test_read_oversized_offset_is_safe(
    registry: ShellTaskRegistry, tmp_path: Path
) -> None:
    """An absurd offset (past the platform file-offset type) must not escape
    the never-raise envelope — seek's ValueError/OverflowError is caught
    (Codex #112 r3)."""
    task = await registry.spawn(
        command="echo hi", session_key="", workspace=tmp_path
    )

    def _done() -> Any:
        r = registry.read(task.task_id, 0)
        return r if (r and r[2] != "running") else None

    assert await _poll(_done) is not None
    # 2**64 overflows off_t on every platform — read() must degrade (empty
    # output + real status), not raise out of the never-raise envelope.
    result = registry.read(task.task_id, 2**64)
    assert result is not None
    text, _off, status, _exit, has_more = result
    assert text == ""
    assert has_more is False
    assert status == "completed"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX setsid / killpg only apply on POSIX",
)
async def test_log_cap_path_reaps_orphan_group(
    registry: ShellTaskRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log-cap branch also does the direct pgid reap, so a daemonized
    writer that outlives the wrapper is swept (Codex #112 r3)."""
    reaped: list[int] = []
    import corlinman_agent.coding.shell_tasks as st_mod

    real = st_mod.reap_orphan_group
    monkeypatch.setattr(
        st_mod, "reap_orphan_group", lambda p: (reaped.append(p.pid), real(p))[0]
    )
    monkeypatch.setenv("CORLINMAN_SHELL_TASK_MAX_LOG_BYTES", "4096")  # floor, trips fast
    task = await registry.spawn(
        command="python3 -c \"import sys\nwhile True: sys.stdout.write('y'*4096); sys.stdout.flush()\"",
        session_key="",
        workspace=tmp_path,
    )

    def _capped() -> Any:
        r = registry.read(task.task_id, 0)
        return r if (r and r[2] == "log_capped") else None

    assert await _poll(_capped, timeout=8.0) is not None, "log cap never tripped"
    assert reaped, "log-cap path did not run the orphan-group reap"


# ---------------------------------------------------------------------------
# Codex #112 round 4 — reap consistency (watchdog/kill), evicted-log delete
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX setsid / killpg only apply on POSIX"
)
async def test_watchdog_expired_path_reaps_group(
    registry: ShellTaskRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifetime-watchdog (expired) path reaps the whole group, not just
    the wrapper (Codex #112 r4)."""
    reaped: list[int] = []
    import corlinman_agent.coding.shell_tasks as st_mod

    real = st_mod.ShellTaskRegistry._reap
    monkeypatch.setattr(
        st_mod.ShellTaskRegistry,
        "_reap",
        staticmethod(lambda p: (reaped.append(p.pid), real(p))[1]),
    )
    monkeypatch.setenv("CORLINMAN_SHELL_TASK_MAX_LIFETIME_S", "0.2")
    task = await registry.spawn(
        command="python3 -c 'import time; time.sleep(30)'",
        session_key="",
        workspace=tmp_path,
    )

    def _expired() -> Any:
        r = registry.read(task.task_id, 0)
        return r if (r and r[2] == "expired") else None

    assert await _poll(_expired, timeout=5.0) is not None, "watchdog never fired"
    assert reaped, "expired path did not reap the group"


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX setsid / killpg only apply on POSIX"
)
async def test_kill_path_reaps_group(
    registry: ShellTaskRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit kill reaps the whole group via the shared _reap (Codex #112 r4)."""
    reaped: list[int] = []
    import corlinman_agent.coding.shell_tasks as st_mod

    real = st_mod.ShellTaskRegistry._reap
    monkeypatch.setattr(
        st_mod.ShellTaskRegistry,
        "_reap",
        staticmethod(lambda p: (reaped.append(p.pid), real(p))[1]),
    )
    task = await registry.spawn(
        command="python3 -c 'import time; time.sleep(30)'",
        session_key="",
        workspace=tmp_path,
    )
    killed = await registry.kill(task.task_id)
    assert killed is not None and killed.status == "killed"
    assert reaped, "kill path did not reap the group"


async def test_evicted_task_log_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evicting a terminal record deletes its spill file so the retention cap
    also bounds workspace disk (Codex #112 r4)."""
    import corlinman_agent.coding.shell_tasks as st_mod

    monkeypatch.setattr(st_mod, "_TERMINAL_CAP", 1)  # evict after the 2nd
    reg = st_mod.ShellTaskRegistry()
    try:
        t1 = await reg.spawn(command="echo a", session_key="", workspace=tmp_path)

        def _t1_done() -> Any:
            r = reg.read(t1.task_id, 0)
            return r if (r and r[2] != "running") else None

        assert await _poll(_t1_done) is not None
        log1 = tmp_path / t1.log_path
        assert log1.exists()

        # A second terminal task evicts the first (cap=1) → its log is removed.
        t2 = await reg.spawn(command="echo b", session_key="", workspace=tmp_path)

        def _t2_done() -> Any:
            r = reg.read(t2.task_id, 0)
            return r if (r and r[2] != "running") else None

        assert await _poll(_t2_done) is not None
        assert not log1.exists(), "evicted task's spill file was left on disk"
    finally:
        await reg.shutdown()
