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
        command="sleep 5", session_key="s1", workspace=tmp_path
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
        session_key="s",
        workspace=tmp_path,
    )
    tid = task.task_id

    # First window: only the pre-sleep line is on disk.
    def _has_one() -> Any:
        r = registry.read(tid, 0)
        return r if (r and "one" in r[0]) else None

    r1 = await _poll(_has_one)
    assert r1 is not None, "first chunk never appeared"
    text1, off1, status1, exit1 = r1
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
    text2, off2, status2, exit2 = r2
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
        command="exit 3", session_key="s", workspace=tmp_path
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
        session_key="s",
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
# MAX_CONCURRENT cap + terminal-retention eviction + lifetime watchdog
# ---------------------------------------------------------------------------


async def test_max_concurrent_cap_raises(
    registry: ShellTaskRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceeding the concurrency cap raises :class:`ShellTaskQuotaExceeded`."""
    monkeypatch.setenv("CORLINMAN_SHELL_TASKS_MAX", "1")
    await registry.spawn(command="sleep 5", session_key="s", workspace=tmp_path)
    with pytest.raises(ShellTaskQuotaExceeded):
        await registry.spawn(
            command="sleep 5", session_key="s", workspace=tmp_path
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
            command=f"echo {marker}", session_key="s", workspace=tmp_path
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
        command="sleep 30", session_key="s", workspace=tmp_path
    )

    def _expired() -> Any:
        r = registry.read(task.task_id, 0)
        return r if (r and r[2] == "expired") else None

    r = await _poll(_expired, timeout=6.0)
    assert r is not None, "watchdog never fired"
    assert r[2] == "expired"
    assert r[3] is None


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
        o = json.loads(
            dispatch_shell_task_output(args_json=_args(task_id=tid, offset=0))
        )
        return o if "streamed" in o["output"] else None

    out = await _poll(_seen)
    assert out is not None
    assert out["task_id"] == tid
    assert out["status"] in ("running", "completed")
    assert out["new_offset"] > 0
    assert out["log_path"].startswith(".corlinman/shell_task_")

    # Unknown task id → task_not_found on both tools.
    unk = json.loads(dispatch_shell_task_output(args_json=_args(task_id="nope")))
    assert unk["error"] == "task_not_found"
    unk_k = json.loads(
        await dispatch_shell_task_kill(args_json=_args(task_id="nope"))
    )
    assert unk_k["error"] == "task_not_found"

    # Kill terminates the still-running sleep.
    killed = json.loads(
        await dispatch_shell_task_kill(args_json=_args(task_id=tid))
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
