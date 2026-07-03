"""Background shell tasks — ``run_shell(run_in_background=true)`` lifecycle.

Dim 4 of the claude-code parity program. A background shell task is a
detached child process spawned with the SAME confinement as the
foreground ``run_shell`` (workspace cwd, POSIX rlimits + ``setsid``, env
whitelist — see :mod:`.shell`), whose combined stdout+stderr is streamed
to a workspace log file. The model discovers progress by **polling**
:func:`dispatch_shell_task_output` and terminates a task with
:func:`dispatch_shell_task_kill` — there is no auto-rewake on completion
(the journal-notification seam is unbuilt; discovery is via polling,
matching claude-code's ``BashOutput`` tool).

## Lifecycle

``run`` → one of ``completed`` (exit 0) / ``failed`` (non-zero exit or a
pump error) / ``killed`` (explicit :meth:`ShellTaskRegistry.kill` or a
process-wide :meth:`~ShellTaskRegistry.shutdown`) / ``expired`` (the
max-lifetime watchdog reaped an overrunning task).

## Registry

:class:`ShellTaskRegistry` is a process-wide singleton
(:func:`get_registry`). It enforces a concurrency cap
(``CORLINMAN_SHELL_TASKS_MAX``, default 8), retains a bounded window of
terminal records (:data:`_TERMINAL_CAP`, oldest evicted), and runs a
per-task max-lifetime watchdog
(``CORLINMAN_SHELL_TASK_MAX_LIFETIME_S``, default 1800s). All env-tunable
knobs are read *live* at spawn time so an operator (or a test) can adjust
them without restarting the process.

## Security caveat

A background task is a real shell — the module docstring of :mod:`.shell`
applies verbatim. The concurrency cap + lifetime watchdog + per-shell
rlimits bound the blast radius; they are NOT a security boundary.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from corlinman_agent.coding._common import (
    CodingArgsInvalidError,
    decode_args,
    resolve_workspace,
    workspace_rel,
)
from corlinman_agent.coding.shell import (
    _SHELL_LOG_DIR,
    _build_child_env,
    _preexec_apply_rlimits,
    kill_process_group,
)

logger = structlog.get_logger(__name__)

SHELL_TASK_OUTPUT_TOOL: str = "shell_task_output"
SHELL_TASK_KILL_TOOL: str = "shell_task_kill"

#: Default concurrency cap — the max number of simultaneously *running*
#: background tasks. Overridable per-spawn via ``CORLINMAN_SHELL_TASKS_MAX``.
_DEFAULT_MAX_CONCURRENT: int = 8
#: Default max wall-clock lifetime (seconds) before the watchdog reaps a
#: task and stamps it ``expired``. Overridable per-spawn via
#: ``CORLINMAN_SHELL_TASK_MAX_LIFETIME_S``.
_DEFAULT_MAX_LIFETIME_S: float = 1800.0
#: How many terminal records to retain for polling after a task finishes.
#: Oldest evicted first — a runaway spawner cannot grow this unboundedly.
_TERMINAL_CAP: int = 64
#: Bytes read from the child pipe per pump iteration before flushing to the
#: spill file. 64 KiB balances syscall count against poll latency.
_PUMP_CHUNK_BYTES: int = 65536

#: Terminal states — a task in one of these never transitions again.
_TERMINAL_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "killed", "expired"}
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _env_max_concurrent() -> int:
    raw = os.environ.get("CORLINMAN_SHELL_TASKS_MAX")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _DEFAULT_MAX_CONCURRENT


def _env_max_lifetime_s() -> float:
    raw = os.environ.get("CORLINMAN_SHELL_TASK_MAX_LIFETIME_S")
    if raw:
        try:
            # Floor at a small positive value so a misconfigured 0 can't
            # disable the watchdog entirely (or divide-by-zero a caller).
            return max(0.1, float(raw))
        except ValueError:
            pass
    return _DEFAULT_MAX_LIFETIME_S


class ShellTaskQuotaExceeded(Exception):
    """Raised by :meth:`ShellTaskRegistry.spawn` when the concurrency cap
    is already full.

    :func:`corlinman_agent.coding.shell.dispatch_run_shell` folds this into
    a ``{"error": "shell_tasks_busy: ..."}`` envelope so the model observes
    the cap rather than the exception.
    """

    def __init__(self, *, active: int, ceiling: int) -> None:
        super().__init__(
            f"background shell task quota exceeded: {active}/{ceiling} running"
        )
        self.active = active
        self.ceiling = ceiling


@dataclass
class ShellTask:
    """One background shell command + its process/pump handles.

    ``status`` starts at ``running`` and moves once to a terminal state.
    ``log_path`` is workspace-relative (what the model sees); ``_log_abs``
    is the absolute spill path the pump appends to and :meth:`read` seeks.
    """

    task_id: str
    command: str
    session_key: str
    started_at_ms: int
    status: str = "running"
    exit_code: int | None = None
    log_path: str = ""
    _proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _pump: asyncio.Task[None] | None = field(default=None, repr=False)
    _log_abs: Path | None = field(default=None, repr=False)


class ShellTaskRegistry:
    """Process-wide registry of background shell tasks.

    Owns the in-memory task map, serialises all mutations behind a single
    :class:`asyncio.Lock`, and drives one streaming pump + one lifetime
    watchdog per task. Construct directly in tests for isolation; the
    production wire-up shares one instance via :func:`get_registry`.
    """

    __slots__ = ("_lock", "_tasks", "_terminal_ids")

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # All known tasks (running + retained terminal), keyed by task_id.
        self._tasks: dict[str, ShellTask] = {}
        # FIFO of terminal task ids for bounded retention / eviction.
        self._terminal_ids: deque[str] = deque()

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def running_count(self) -> int:
        """Number of tasks currently in the ``running`` state."""
        return sum(1 for t in self._tasks.values() if t.status == "running")

    def get(self, task_id: str) -> ShellTask | None:
        return self._tasks.get(task_id)

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    async def spawn(
        self,
        *,
        command: str,
        session_key: str = "",
        workspace: Path | None = None,
    ) -> ShellTask:
        """Spawn ``command`` detached and register a running task.

        Raises :class:`ShellTaskQuotaExceeded` when the concurrency cap is
        already full, or :class:`OSError` if the subprocess fails to spawn
        (the caller folds both into an envelope). Returns the seeded
        ``running`` task immediately — the streaming pump + lifetime
        watchdog run in the background.
        """
        ws = resolve_workspace(workspace)
        # POSIX-only: apply rlimits + setsid before exec. Same confinement
        # as the foreground path — background tasks are not privileged.
        spawn_kwargs: dict[str, Any] = {
            "cwd": str(ws),
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
            "env": _build_child_env(),
        }
        if sys.platform != "win32":
            spawn_kwargs["preexec_fn"] = _preexec_apply_rlimits

        max_lifetime = _env_max_lifetime_s()
        async with self._lock:
            ceiling = _env_max_concurrent()
            active = sum(1 for t in self._tasks.values() if t.status == "running")
            if active >= ceiling:
                raise ShellTaskQuotaExceeded(active=active, ceiling=ceiling)

            proc = await asyncio.create_subprocess_shell(command, **spawn_kwargs)
            task_id = uuid.uuid4().hex[:16]
            log_abs = ws / _SHELL_LOG_DIR / f"shell_task_{task_id}.log"
            # Pre-create the (empty) spill file so log_path points at a real
            # file even before the pump writes its first chunk.
            try:
                log_abs.parent.mkdir(parents=True, exist_ok=True)
                log_abs.touch(exist_ok=True)
            except OSError as exc:  # pragma: no cover — disk-full / readonly
                logger.warning(
                    "shell_task.spill_precreate_failed",
                    error=str(exc),
                    task_id=task_id,
                )
            task = ShellTask(
                task_id=task_id,
                command=command,
                session_key=session_key,
                started_at_ms=_now_ms(),
                status="running",
                log_path=workspace_rel(ws, log_abs),
                _proc=proc,
                _log_abs=log_abs,
            )
            self._tasks[task_id] = task
            task._pump = asyncio.create_task(
                self._pump(task, max_lifetime),
                name=f"shell_task.pump:{task_id}",
            )
        logger.info(
            "shell_task.spawned",
            task_id=task_id,
            command=command[:200],
            session_key=session_key,
        )
        return task

    # ------------------------------------------------------------------
    # Pump — stream combined output + stamp the terminal state
    # ------------------------------------------------------------------

    async def _pump(self, task: ShellTask, max_lifetime_s: float) -> None:
        """Stream the child's output to the spill file, then stamp terminal.

        Wrapped in :func:`asyncio.wait_for` so an overrunning task is
        reaped by the max-lifetime watchdog and stamped ``expired``. Never
        raises out of the asyncio task — a stray exception would only log
        ``Task exception was never retrieved``.
        """
        try:
            await asyncio.wait_for(self._pump_body(task), timeout=max_lifetime_s)
        except TimeoutError:
            # Lifetime watchdog fired — kill the whole group and stamp expired.
            proc = task._proc
            if proc is not None:
                kill_process_group(proc)
                try:
                    await proc.wait()
                except ProcessLookupError:  # pragma: no cover — race
                    pass
            async with self._lock:
                if task.status == "running":
                    task.status = "expired"
                    task.exit_code = None
                    self._retire(task)
            logger.info("shell_task.expired", task_id=task.task_id)
        except asyncio.CancelledError:
            # Shutdown / explicit cancel — leave the (already-stamped) row.
            raise
        except Exception as exc:  # noqa: BLE001 — pump must never raise upward
            logger.exception("shell_task.pump_failed", task_id=task.task_id)
            async with self._lock:
                if task.status == "running":
                    task.status = "failed"
                    task.exit_code = None
                    self._retire(task)
            _ = exc

    async def _pump_body(self, task: ShellTask) -> None:
        """Append the child's combined output to the spill file, flush per
        chunk, then record the exit code once it terminates."""
        proc = task._proc
        if proc is None or proc.stdout is None or task._log_abs is None:
            return
        with open(task._log_abs, "ab") as fh:
            while True:
                chunk = await proc.stdout.read(_PUMP_CHUNK_BYTES)
                if not chunk:
                    break
                fh.write(chunk)
                fh.flush()
        rc = await proc.wait()
        async with self._lock:
            # A kill / watchdog may have already stamped a terminal state;
            # only the natural-exit path claims a still-``running`` task.
            if task.status == "running":
                task.status = "completed" if rc == 0 else "failed"
                task.exit_code = rc
                self._retire(task)
        logger.info(
            "shell_task.finished",
            task_id=task.task_id,
            exit_code=rc,
            status=task.status,
        )

    def _retire(self, task: ShellTask) -> None:
        """Record ``task`` in the bounded terminal window (caller holds the
        lock). Evicts the oldest terminal record when the cap is hit."""
        while len(self._terminal_ids) >= _TERMINAL_CAP:
            evicted = self._terminal_ids.popleft()
            self._tasks.pop(evicted, None)
        self._terminal_ids.append(task.task_id)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(
        self, task_id: str, offset: int
    ) -> tuple[str, int, str, int | None] | None:
        """Read the spill file from ``offset`` for ``task_id``.

        Returns ``(text_from_offset, new_offset, status, exit_code)`` or
        ``None`` when the task id is unknown (or was evicted). ``offset`` is
        a byte offset; the returned ``new_offset`` is where the next poll
        should resume. A partial multi-byte tail is decoded with
        ``errors="replace"`` so a boundary split never raises.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return None
        text = ""
        new_offset = max(0, offset)
        log_abs = task._log_abs
        if log_abs is not None:
            try:
                with open(log_abs, "rb") as fh:
                    fh.seek(new_offset)
                    data = fh.read()
                text = data.decode("utf-8", errors="replace")
                new_offset += len(data)
            except FileNotFoundError:
                # Spill not created yet (spawn pre-creates it, so this is a
                # rare race) — no output available at this offset.
                pass
        return (text, new_offset, task.status, task.exit_code)

    # ------------------------------------------------------------------
    # Kill
    # ------------------------------------------------------------------

    async def kill(self, task_id: str) -> ShellTask | None:
        """Terminate a running task's process group and stamp ``killed``.

        Returns the task snapshot (``killed`` for a live task, or its
        existing terminal state if it already finished), or ``None`` when
        the task id is unknown — the caller maps ``None`` to
        ``task_not_found``.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.status in _TERMINAL_STATES:
                return task
            proc = task._proc
            if proc is not None:
                kill_process_group(proc)
            task.status = "killed"
            task.exit_code = None
            self._retire(task)
        # Reap outside the lock — the pump's terminal-stamp is guarded on
        # ``running`` so it will skip the now-``killed`` row.
        if proc is not None:
            try:
                await proc.wait()
            except ProcessLookupError:  # pragma: no cover — race
                pass
        logger.info("shell_task.killed", task_id=task_id)
        return task

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Kill every running task and await its pump.

        Exported for future gateway-lifecycle wiring (mirrors the subagent
        dispatcher's ``shutdown``). Idempotent — a second call finds no
        running tasks. Best-effort ``atexit`` fallback lives in
        :func:`_atexit_shutdown` for the no-event-loop interpreter-exit case.
        """
        async with self._lock:
            running = [t for t in self._tasks.values() if t.status == "running"]
            for t in running:
                if t._proc is not None:
                    kill_process_group(t._proc)
                t.status = "killed"
                t.exit_code = None
                self._retire(t)
            pumps = [t._pump for t in running if t._pump is not None]
        # Await the pumps outside the lock so their EOF handling (which
        # re-acquires the lock) can complete without deadlocking.
        for pump in pumps:
            try:
                await pump
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_REGISTRY: ShellTaskRegistry | None = None


def get_registry() -> ShellTaskRegistry:
    """Return the process-wide :class:`ShellTaskRegistry`, creating it lazily."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ShellTaskRegistry()
    return _REGISTRY


def reset_registry() -> None:
    """Drop the module singleton (test seam). Does NOT kill running tasks —
    callers that need a clean teardown ``await registry.shutdown()`` first."""
    global _REGISTRY
    _REGISTRY = None


def _atexit_shutdown() -> None:
    """Best-effort synchronous kill of running tasks at interpreter exit.

    ``atexit`` runs with no event loop, so we cannot ``await`` the async
    :meth:`ShellTaskRegistry.shutdown`; we just SIGKILL each running
    process group directly. Swallows everything — interpreter teardown
    must not raise.
    """
    reg = _REGISTRY
    if reg is None:
        return
    try:
        for task in list(reg._tasks.values()):
            if task.status == "running" and task._proc is not None:
                try:
                    kill_process_group(task._proc)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001 — never raise at exit
        pass


atexit.register(_atexit_shutdown)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


def shell_task_output_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": SHELL_TASK_OUTPUT_TOOL,
            "description": (
                "Read new output from a background shell task started by "
                "run_shell(run_in_background=true). Pass the task_id and an "
                "offset (0 the first time, then the new_offset from the "
                "previous call) to page through the combined stdout+stderr "
                "as it streams. Returns the task status and, once finished, "
                "the exit_code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task_id returned by run_shell.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "Byte offset to resume from (default 0). Pass "
                            "the previous call's new_offset to read only new "
                            "output."
                        ),
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    }


def shell_task_kill_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": SHELL_TASK_KILL_TOOL,
            "description": (
                "Terminate a background shell task started by "
                "run_shell(run_in_background=true). Kills the whole process "
                "group. Returns the task_id and its final status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task_id returned by run_shell.",
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    }


# ---------------------------------------------------------------------------
# Dispatchers — JSON envelopes, never raise (mirrors shell.py)
# ---------------------------------------------------------------------------


def dispatch_shell_task_output(*, args_json: bytes | str) -> str:
    """Read a background task's streamed output. JSON envelope; never raises."""
    try:
        raw = decode_args(args_json)
    except CodingArgsInvalidError as exc:
        return json.dumps({"error": f"args_invalid: {exc.message}"})

    task_id = raw.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return json.dumps({"error": "args_invalid: missing or empty 'task_id'"})
    task_id = task_id.strip()

    offset = raw.get("offset", 0)
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    reg = get_registry()
    result = reg.read(task_id, offset)
    if result is None:
        return json.dumps({"error": "task_not_found", "task_id": task_id})
    text, new_offset, status, exit_code = result
    task = reg.get(task_id)
    envelope: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "output": text,
        "new_offset": new_offset,
        "log_path": task.log_path if task is not None else "",
    }
    if exit_code is not None:
        envelope["exit_code"] = exit_code
    return json.dumps(envelope, ensure_ascii=False)


async def dispatch_shell_task_kill(*, args_json: bytes | str) -> str:
    """Terminate a background task. JSON envelope; never raises."""
    try:
        raw = decode_args(args_json)
    except CodingArgsInvalidError as exc:
        return json.dumps({"error": f"args_invalid: {exc.message}"})

    task_id = raw.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return json.dumps({"error": "args_invalid: missing or empty 'task_id'"})
    task_id = task_id.strip()

    try:
        task = await get_registry().kill(task_id)
    except Exception as exc:  # noqa: BLE001 — dispatcher must never raise
        logger.exception("shell_task_kill.unexpected", task_id=task_id)
        return json.dumps({"error": f"kill_failed: {exc}", "task_id": task_id})
    if task is None:
        return json.dumps({"error": "task_not_found", "task_id": task_id})
    return json.dumps({"task_id": task.task_id, "status": task.status})


__all__ = [
    "SHELL_TASK_KILL_TOOL",
    "SHELL_TASK_OUTPUT_TOOL",
    "ShellTask",
    "ShellTaskQuotaExceeded",
    "ShellTaskRegistry",
    "dispatch_shell_task_kill",
    "dispatch_shell_task_output",
    "get_registry",
    "reset_registry",
    "shell_task_kill_tool_schema",
    "shell_task_output_tool_schema",
]
