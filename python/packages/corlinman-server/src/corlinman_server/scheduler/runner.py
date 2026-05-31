"""Async tick loop + per-job dispatcher + subprocess wrapper.

Python port of:

* ``rust/crates/corlinman-scheduler/src/jobs.rs`` — config-shape
  dataclasses + ``JobSpec`` / ``ActionSpec`` runtime types.
* ``rust/crates/corlinman-scheduler/src/runtime.rs`` — :func:`spawn`,
  :class:`SchedulerHandle`, :func:`dispatch`, the per-job tick loop.
* ``rust/crates/corlinman-scheduler/src/subprocess.rs`` — the
  :class:`SubprocessOutcome` enum + :func:`run_subprocess` helper.

The three Rust files collapse into one Python module because the
brief explicitly asks for a 3-module decomposition (``cron.py``,
``runner.py``, ``persistence.py``) — keeping subprocess + runtime
together here matches the typical Python "one module per
responsibility" rule while still keeping each section under a
screenful.

Hook events flow through :mod:`corlinman_hooks` (the workspace's
Python port of ``corlinman-hooks``). The Rust crate emits
``HookEvent::EngineRunCompleted`` / ``::EngineRunFailed`` on the bus
shared with the gateway; the Python port emits the exact same two
variants (``HookEvent.EngineRunCompleted`` / ``.EngineRunFailed``)
through the same bus type so the gateway's evolution-observer code
folds the outcomes in transparently.

Cancellation flows through an :class:`asyncio.Event` (the Python
analogue of ``tokio_util::sync::CancellationToken``). The spawn
returns a handle whose :meth:`SchedulerHandle.join_all` waits for
every per-job task to exit; the gateway shutdown path flips the
cancel event and then awaits ``join_all``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, cast

from corlinman_hooks import HookBus, HookEvent

from corlinman_server.scheduler.cron import Schedule, next_after, parse

if TYPE_CHECKING:
    # Typing-only: the common base of every ``HookEvent`` variant — the
    # ``HookEvent.EngineRun*`` constructors return concrete variant types
    # (siblings of ``HookEvent``), and ``HookBus.emit`` accepts the base.
    from corlinman_hooks.event import _HookEventBase

_logger = logging.getLogger("corlinman_server.scheduler")


# ---------------------------------------------------------------------------
# Config-shape dataclasses (mirror corlinman_core::config::Scheduler*).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobAction:
    """Discriminated union of the three job actions.

    Mirrors the Rust ``JobAction`` enum (``RunAgent``, ``RunTool``,
    ``Subprocess``). Python's tagged-union idiom is a frozen dataclass
    with a ``kind`` discriminant plus the per-kind fields nullable;
    the constructors :meth:`subprocess`, :meth:`run_agent`,
    :meth:`run_tool` keep call sites readable.

    Only :attr:`kind` ``"subprocess"`` is end-to-end (matches the Rust
    Wave 2-B reality); the other two surface as ``unsupported_action``
    failures on the bus when fired.
    """

    kind: str
    # subprocess fields
    command: str | None = None
    args: tuple[str, ...] = ()
    timeout_secs: int = 600  # default mirrors `default_subprocess_timeout_secs`
    working_dir: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    # run_agent field
    prompt: str | None = None
    # run_tool fields
    plugin: str | None = None
    tool: str | None = None
    tool_args: object = None  # serde_json::Value analog — opaque

    @classmethod
    def subprocess(
        cls,
        command: str,
        args: Sequence[str] = (),
        timeout_secs: int = 600,
        working_dir: Path | str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> JobAction:
        """Build a ``Subprocess`` action.

        ``timeout_secs`` defaults to 600 (10 min) to match the Rust
        ``default_subprocess_timeout_secs`` serde default. ``env``
        defaults to an empty mapping; entries are merged over the
        inherited environment at spawn time."""
        return cls(
            kind="subprocess",
            command=command,
            args=tuple(args),
            timeout_secs=timeout_secs,
            working_dir=(Path(working_dir) if working_dir is not None else None),
            env=dict(env) if env else {},
        )

    @classmethod
    def run_agent(cls, prompt: str) -> JobAction:
        """Build a ``RunAgent`` action (not yet implemented at dispatch)."""
        return cls(kind="run_agent", prompt=prompt)

    @classmethod
    def run_tool(cls, plugin: str, tool: str, args: object = None) -> JobAction:
        """Build a ``RunTool`` action (not yet implemented at dispatch)."""
        return cls(kind="run_tool", plugin=plugin, tool=tool, tool_args=args)


@dataclass(frozen=True)
class SchedulerJob:
    """One ``[[scheduler.jobs]]`` table entry.

    Mirrors the Rust ``SchedulerJob`` struct. ``timezone`` is accepted
    for parity with the TOML schema; the Python port treats it as
    advisory (croniter's tz handling differs from the Rust ``cron``
    crate's, so we keep everything in UTC and surface tz support in a
    follow-up wave if a user actually files a bug).
    """

    name: str
    cron: str
    action: JobAction
    timezone: str | None = None


@dataclass(frozen=True)
class SchedulerConfig:
    """Whole ``[scheduler]`` config block.

    Mirrors the Rust ``SchedulerConfig``. ``jobs`` defaults to empty
    so a config with no scheduler block produces a no-op scheduler.
    """

    jobs: tuple[SchedulerJob, ...] = ()


# ---------------------------------------------------------------------------
# Runtime-side specs (cron expression already parsed).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    """Runtime-side action carrier. Same kinds as :class:`JobAction`
    but post-validation: no Optional-everywhere shape, fields are
    typed per kind via the discriminant. We keep a single dataclass
    (rather than three subclasses) because the dispatch table reads
    cleaner as one ``if/elif`` chain than as a class hierarchy."""

    kind: str  # "subprocess" | "run_agent" | "run_tool"
    command: str | None = None
    args: tuple[str, ...] = ()
    timeout_secs: int = 600
    working_dir: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    prompt: str | None = None
    plugin: str | None = None
    tool: str | None = None
    tool_args: object = None


@dataclass(frozen=True)
class JobSpec:
    """A scheduler job after validation. Holds the parsed cron
    :class:`Schedule` so the tick loop never re-parses the expression."""

    name: str
    cron: Schedule
    action: ActionSpec

    @classmethod
    def from_config(cls, job: SchedulerJob) -> JobSpec | None:
        """Mirror of Rust ``JobSpec::from_config``.

        Returns ``None`` (with a ``warning`` log) when the cron
        expression fails to parse — the caller should drop the job
        rather than abort scheduler startup, exactly as the Rust crate
        does. Tests assert both branches.
        """
        try:
            schedule = parse(job.cron)
        except Exception as exc:  # noqa: BLE001 - mirror Rust's catch-all
            _logger.warning(
                "scheduler: dropping job with unparseable cron",
                extra={"job": job.name, "cron": job.cron, "error": str(exc)},
            )
            return None
        action = ActionSpec(
            kind=job.action.kind,
            command=job.action.command,
            args=job.action.args,
            timeout_secs=job.action.timeout_secs,
            working_dir=job.action.working_dir,
            env=job.action.env,
            prompt=job.action.prompt,
            plugin=job.action.plugin,
            tool=job.action.tool,
            tool_args=job.action.tool_args,
        )
        return cls(name=job.name, cron=schedule, action=action)


# ---------------------------------------------------------------------------
# Subprocess execution (mirrors src/subprocess.rs).
# ---------------------------------------------------------------------------


class SubprocessOutcomeKind(str, Enum):
    """Discriminant for :class:`SubprocessOutcome`. Matches the Rust
    enum variant names 1:1 so the wire/log surfaces look the same."""

    SUCCESS = "success"
    NON_ZERO_EXIT = "non_zero_exit"
    TIMEOUT = "timeout"
    SPAWN_FAILED = "spawn_failed"


@dataclass(frozen=True)
class SubprocessOutcome:
    """Outcome of one subprocess firing. Enum-shaped so callers can
    pattern-match on :attr:`kind` and pick the right ``error_kind``
    string for ``EngineRunFailed``.

    Field meanings per ``kind``:

    * ``SUCCESS``: ``duration_secs`` set.
    * ``NON_ZERO_EXIT``: ``duration_secs`` + optional ``exit_code``.
    * ``TIMEOUT``: ``duration_secs`` is the timeout we hit.
    * ``SPAWN_FAILED``: ``error`` is the OS error message.
    """

    kind: SubprocessOutcomeKind
    duration_secs: float = 0.0
    exit_code: int | None = None
    error: str | None = None


async def _forward_stream(stream: asyncio.StreamReader, job: str, run_id: str, level: int, label: str) -> None:
    """Forward a piped child stdout/stderr line-by-line into logging.

    Mirrors the Rust ``BufReader::lines`` + ``tracing::{info,warn}!``
    loop. Each line carries the job + run_id + stream label so multiple
    concurrent jobs are distinguishable in logs.
    """
    while True:
        try:
            raw = await stream.readline()
        except (asyncio.CancelledError, ValueError):
            # ValueError can be raised when the pipe is closed mid-read
            # on some Python versions; treat as end-of-stream.
            return
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            # Skip empty lines so the log isn't padded with blanks.
            continue
        _logger.log(
            level,
            "scheduler: subprocess %s: %s",
            label,
            line,
            extra={"job": job, "run_id": run_id, "stream": label},
        )


async def run_subprocess(
    job: str,
    run_id: str,
    command: str,
    args: Sequence[str],
    timeout_secs: int,
    working_dir: Path | None,
    env: Mapping[str, str],
) -> SubprocessOutcome:
    """Spawn ``command args`` and wait up to ``timeout_secs`` for it.

    Behaviour matches the Rust :func:`run_subprocess`:

    * stdout/stderr piped + forwarded to :mod:`logging` line-by-line
      (stdout at INFO, stderr at WARNING).
    * :func:`asyncio.wait_for` wraps the child wait. On expiry we send
      SIGKILL (``Process.kill()``) and return :class:`SubprocessOutcome`
      ``TIMEOUT``. The strong kill is deliberate — a graceful SIGTERM
      could let a wedged engine outlive the schedule's next firing.
    * ``Command::spawn``-equivalent failures (binary not on PATH,
      missing working dir, etc.) surface as ``SPAWN_FAILED`` so the
      caller emits ``EngineRunFailed { error_kind: "spawn_failed" }``
      without the gateway crashing.

    ``env`` is merged over the inherited environment (parity with the
    Rust ``Command::env`` behaviour — only the explicit entries are
    overridden; PATH/etc. inherit). The Rust side uses
    ``BTreeMap<String, String>`` so iteration is deterministic; Python
    dicts preserve insertion order which is good enough for the same
    "tests see a stable PATH" effect.
    """
    started = time.monotonic()

    merged_env = dict(os.environ)
    for k, v in env.items():
        merged_env[k] = v

    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=(str(working_dir) if working_dir is not None else None),
            env=merged_env,
        )
    except (OSError, FileNotFoundError) as exc:
        # `FileNotFoundError` is the common "missing binary" case; the
        # base `OSError` catches permission denied, missing working
        # dir, etc. Wrap into the SPAWN_FAILED variant.
        _logger.error(
            "scheduler: subprocess spawn failed",
            extra={"job": job, "run_id": run_id, "command": command, "error": str(exc)},
        )
        return SubprocessOutcome(
            kind=SubprocessOutcomeKind.SPAWN_FAILED,
            error=str(exc),
            duration_secs=time.monotonic() - started,
        )

    # Spawn the per-stream forwarders. They exit cleanly when the
    # child closes its end of the pipe; we keep references so we can
    # `await` them after the child exits (avoids racing on a pipe
    # close-vs-read).
    fwd_tasks: list[asyncio.Task[None]] = []
    if proc.stdout is not None:
        fwd_tasks.append(
            asyncio.create_task(
                _forward_stream(proc.stdout, job, run_id, logging.INFO, "stdout"),
                name=f"scheduler-fwd-stdout-{run_id}",
            )
        )
    if proc.stderr is not None:
        fwd_tasks.append(
            asyncio.create_task(
                _forward_stream(proc.stderr, job, run_id, logging.WARNING, "stderr"),
                name=f"scheduler-fwd-stderr-{run_id}",
            )
        )

    timeout = max(1, int(timeout_secs))
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        _logger.error(
            "scheduler: subprocess timed out; sending SIGKILL",
            extra={"job": job, "run_id": run_id, "timeout_secs": timeout},
        )
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        # Reap so the OS releases the slot. Bound the post-kill wait
        # so a wedged kernel can't park us forever.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5)
        # Drain the forwarders before returning so log lines from
        # straggling stdout buffers aren't reordered after the
        # outcome log.
        for t in fwd_tasks:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
        return SubprocessOutcome(kind=SubprocessOutcomeKind.TIMEOUT, duration_secs=float(timeout))

    elapsed = time.monotonic() - started
    # Wait for the forwarders to drain. They exit on EOF, which the
    # `proc.wait()` above is the natural barrier for.
    for t in fwd_tasks:
        with contextlib.suppress(BaseException):
            await t

    if rc == 0:
        return SubprocessOutcome(kind=SubprocessOutcomeKind.SUCCESS, duration_secs=elapsed)
    return SubprocessOutcome(
        kind=SubprocessOutcomeKind.NON_ZERO_EXIT,
        duration_secs=elapsed,
        exit_code=int(rc) if rc is not None else None,
    )


# ---------------------------------------------------------------------------
# Dispatcher (mirrors runtime::dispatch + emit_outcome).
# ---------------------------------------------------------------------------


async def dispatch(spec: JobSpec, bus: HookBus, app_state: object | None = None) -> None:
    """Run a single firing of ``spec`` and emit the matching hook event.

    Public so an admin "fire now" endpoint can reuse it later (the Rust
    crate exposes the same surface for the same reason); the per-job
    tick loop calls this on every wake.

    R3-002: ``run_tool`` firings are routed to the live
    :data:`~corlinman_server.scheduler.builtins.BUILTIN_ACTIONS` registry
    by the ``"<plugin>.<tool>"`` key. The admin "fire now" route
    (``gateway/routes_admin_b/scheduler.py``) calls ``run_builtin()``
    directly, so the manual path always worked; the bug was that the
    scheduler tick loop dropped every default ``run_tool`` job into the
    ``unsupported_action`` branch without ever consulting the registry.

    ``app_state`` is threaded through into the :class:`BuiltinContext`
    each builtin reads off — it stays ``None`` for unit tests and the
    builtins gracefully degrade (``checker_unavailable`` etc.). The
    tick-loop wiring can pass a live ``app.state`` here once the
    scheduler is owned by the gateway lifecycle.
    """
    run_id = uuid.uuid4().hex
    if spec.action.kind == "subprocess":
        _logger.info(
            "scheduler: subprocess job firing",
            extra={"job": spec.name, "run_id": run_id, "command": spec.action.command},
        )
        assert spec.action.command is not None  # noqa: S101 - shape-asserted by ActionSpec
        outcome = await run_subprocess(
            spec.name,
            run_id,
            spec.action.command,
            spec.action.args,
            spec.action.timeout_secs,
            spec.action.working_dir,
            spec.action.env,
        )
        await _emit_outcome(bus, spec.name, run_id, outcome)
        _err_kind = (
            None
            if outcome.kind is SubprocessOutcomeKind.SUCCESS
            else outcome.kind.value
        )
        await _maybe_record(
            app_state,
            job_name=spec.name,
            run_id=run_id,
            action_kind="subprocess",
            outcome_kind=outcome.kind.value,
            error_kind=_err_kind,
            exit_code=outcome.exit_code,
            duration_ms=int(outcome.duration_secs * 1000),
        )
        return

    if spec.action.kind == "run_tool":
        # Local import keeps the builtins package out of the scheduler
        # module's import-time graph (the builtins themselves import
        # gateway-side handles which would otherwise pull a circular
        # dependency at runner.py module load).
        from corlinman_server.scheduler.builtins import (
            BUILTIN_ACTIONS,
            BuiltinContext,
            run_builtin,
        )

        plugin = spec.action.plugin
        tool = spec.action.tool
        if plugin is None or tool is None:
            _logger.warning(
                "scheduler: run_tool action missing plugin/tool fields",
                extra={"job": spec.name, "run_id": run_id, "plugin": plugin, "tool": tool},
            )
            await _emit_failed(bus, run_id, "unsupported_action", None)
            return

        builtin_name = f"{plugin}.{tool}"
        if builtin_name not in BUILTIN_ACTIONS:
            _logger.warning(
                "scheduler: run_tool action has no registered builtin",
                extra={"job": spec.name, "run_id": run_id, "builtin_name": builtin_name},
            )
            await _emit_failed(bus, run_id, "unsupported_action", None)
            return

        _logger.info(
            "scheduler: run_tool job firing",
            extra={"job": spec.name, "run_id": run_id, "builtin_name": builtin_name},
        )
        started = time.monotonic()
        ctx = BuiltinContext(app_state=app_state, run_id=run_id, name=spec.name)
        # ``run_builtin`` is documented as never-raising — it wraps any
        # exception into a ``{"ok": False, "reason": ...}`` envelope. Use
        # it (rather than calling the action directly) so the contract
        # stays in one place.
        result = await run_builtin(builtin_name, ctx)
        duration_ms = int((time.monotonic() - started) * 1000)
        _ok = isinstance(result, dict) and bool(result.get("ok"))
        if _ok:
            event: _HookEventBase = HookEvent.EngineRunCompleted(
                run_id=run_id, proposals_generated=0, duration_ms=duration_ms
            )
        else:
            reason = (result or {}).get("reason") if isinstance(result, dict) else None
            _logger.error(
                "scheduler: run_tool builtin reported failure",
                extra={
                    "job": spec.name,
                    "run_id": run_id,
                    "builtin_name": builtin_name,
                    "reason": reason,
                },
            )
            event = HookEvent.EngineRunFailed(
                run_id=run_id, error_kind="builtin_not_ok", exit_code=None
            )
        try:
            await bus.emit(event)
        except Exception as exc:  # noqa: BLE001 - any emit failure is non-fatal
            _logger.warning(
                "scheduler: hook emit failed",
                extra={"job": spec.name, "run_id": run_id, "error": str(exc)},
            )
        await _maybe_record(
            app_state,
            job_name=spec.name,
            run_id=run_id,
            action_kind="run_tool",
            outcome_kind="success" if _ok else "non_zero_exit",
            error_kind=None if _ok else "builtin_not_ok",
            exit_code=None,
            duration_ms=duration_ms,
        )
        return

    if spec.action.kind == "run_agent":
        # WP15: run_agent dispatch — invoke the agent runner when one
        # is registered on ``app_state``. The gateway lifespan sets
        # ``app.state.agent_runner_fn`` to an async callable that
        # accepts a prompt string and returns a result dict.
        #
        # Lookup order:
        #  1. ``app_state.agent_runner_fn`` — the preferred wiring path
        #     set by the gateway startup lifespan (a coroutine function).
        #  2. ``app_state.agent_runner`` — legacy attribute name kept for
        #     back-compat with deployments that set it directly.
        #  3. Neither present → surface ``runner_not_registered`` on the
        #     hook bus so the operator can see the job fired but the
        #     runner wasn't wired, rather than a generic "unsupported".
        prompt = spec.action.prompt
        if not prompt:
            _logger.warning(
                "scheduler: run_agent action has empty prompt; skipping",
                extra={"job": spec.name, "run_id": run_id},
            )
            await _emit_failed(bus, run_id, "run_agent_empty_prompt", None)
            return

        runner_fn = None
        if app_state is not None:
            runner_fn = getattr(app_state, "agent_runner_fn", None)
            if runner_fn is None:
                runner_fn = getattr(app_state, "agent_runner", None)

        if runner_fn is None:
            _logger.warning(
                "scheduler: run_agent has no registered runner; "
                "set app.state.agent_runner_fn to wire it",
                extra={"job": spec.name, "run_id": run_id},
            )
            await _emit_failed(bus, run_id, "runner_not_registered", None)
            return

        _logger.info(
            "scheduler: run_agent job firing",
            extra={"job": spec.name, "run_id": run_id, "prompt_preview": prompt[:200]},
        )
        started = time.monotonic()
        _agent_err: str | None = None
        try:
            result = await runner_fn(prompt)
            duration_ms = int((time.monotonic() - started) * 1000)
            # The wired ``agent_runner_fn`` returns a result dict carrying
            # an ``ok`` flag (chat_service_unavailable / chat_error fold
            # into ``ok: False``). Honour it so a soft failure surfaces as
            # EngineRunFailed rather than masquerading as a completed run.
            _ok = True
            if isinstance(result, dict) and result.get("ok") is False:
                _ok = False
                _agent_err = str(result.get("error") or "run_agent_failed")
            if _ok:
                _logger.info(
                    "scheduler: run_agent job completed",
                    extra={
                        "job": spec.name,
                        "run_id": run_id,
                        "duration_ms": duration_ms,
                        "result_type": type(result).__name__,
                    },
                )
                agent_event: _HookEventBase = HookEvent.EngineRunCompleted(
                    run_id=run_id, proposals_generated=0, duration_ms=duration_ms
                )
                # Channel delivery seam: surface the reply on the structlog
                # feed (the same best-effort delivery seam the gateway's
                # restart-broadcast uses) so an operator sees scheduled-run
                # output. A future outbound-handle wave routes through here.
                _reply = (
                    result.get("reply") if isinstance(result, dict) else None
                )
                if isinstance(_reply, str) and _reply.strip():
                    _logger.info(
                        "scheduler: run_agent reply",
                        extra={
                            "job": spec.name,
                            "run_id": run_id,
                            "reply_preview": _reply[:500],
                        },
                    )
            else:
                _logger.error(
                    "scheduler: run_agent runner reported failure",
                    extra={
                        "job": spec.name,
                        "run_id": run_id,
                        "duration_ms": duration_ms,
                        "error": _agent_err,
                    },
                )
                agent_event = HookEvent.EngineRunFailed(
                    run_id=run_id,
                    error_kind=_agent_err or "run_agent_failed",
                    exit_code=None,
                )
        except Exception as exc:  # noqa: BLE001 — surface on bus, never crash scheduler
            duration_ms = int((time.monotonic() - started) * 1000)
            _agent_err = "run_agent_exception"
            _logger.error(
                "scheduler: run_agent job raised",
                extra={
                    "job": spec.name,
                    "run_id": run_id,
                    "duration_ms": duration_ms,
                    "error": str(exc),
                },
            )
            agent_event = HookEvent.EngineRunFailed(
                run_id=run_id, error_kind="run_agent_exception", exit_code=None
            )
        await _maybe_record(
            app_state,
            job_name=spec.name,
            run_id=run_id,
            action_kind="run_agent",
            outcome_kind="success" if _agent_err is None else "non_zero_exit",
            error_kind=_agent_err,
            exit_code=None,
            duration_ms=duration_ms,
        )
        try:
            await bus.emit(agent_event)
        except Exception as exc:  # noqa: BLE001 — emit failures are non-fatal
            _logger.warning(
                "scheduler: hook emit failed",
                extra={"job": spec.name, "run_id": run_id, "error": str(exc)},
            )
        return

    # Unknown action kind — surface as unsupported_action on the bus so the
    # gateway's evolution observer sees the failure rather than a silent drop.
    _logger.warning(
        "scheduler: action kind not yet implemented; skipping fire",
        extra={"job": spec.name, "run_id": run_id, "kind": spec.action.kind},
    )
    await _emit_failed(bus, run_id, "unsupported_action", None)


async def _emit_outcome(bus: HookBus, job: str, run_id: str, outcome: SubprocessOutcome) -> None:
    """Translate a :class:`SubprocessOutcome` into the right hook event.

    Best-effort: hook-bus emit failures are caught and logged but not
    propagated — mirrors the gateway's "hooks never crash the caller"
    stance and the Rust ``if let Err(...) = bus.emit(...)`` pattern.
    """
    duration_ms = int(outcome.duration_secs * 1000)
    if outcome.kind is SubprocessOutcomeKind.SUCCESS:
        _logger.info(
            "scheduler: subprocess job completed",
            extra={"job": job, "run_id": run_id, "duration_ms": duration_ms},
        )
        # Wave 2-B doesn't parse engine stdout for a proposals count
        # yet; report 0 so the schema is honoured (Rust does the same).
        event: _HookEventBase = HookEvent.EngineRunCompleted(
            run_id=run_id, proposals_generated=0, duration_ms=duration_ms
        )
    elif outcome.kind is SubprocessOutcomeKind.NON_ZERO_EXIT:
        _logger.error(
            "scheduler: subprocess job exited non-zero",
            extra={
                "job": job,
                "run_id": run_id,
                "exit_code": outcome.exit_code,
                "duration_ms": duration_ms,
            },
        )
        event = HookEvent.EngineRunFailed(
            run_id=run_id, error_kind="exit_code", exit_code=outcome.exit_code
        )
    elif outcome.kind is SubprocessOutcomeKind.TIMEOUT:
        _logger.error(
            "scheduler: subprocess job timed out",
            extra={"job": job, "run_id": run_id, "duration_ms": duration_ms},
        )
        event = HookEvent.EngineRunFailed(run_id=run_id, error_kind="timeout", exit_code=None)
    elif outcome.kind is SubprocessOutcomeKind.SPAWN_FAILED:
        _logger.error(
            "scheduler: subprocess job spawn failed",
            extra={"job": job, "run_id": run_id, "error": outcome.error},
        )
        event = HookEvent.EngineRunFailed(
            run_id=run_id, error_kind="spawn_failed", exit_code=None
        )
    else:  # pragma: no cover - exhaustive over the enum
        raise AssertionError(f"unknown SubprocessOutcomeKind: {outcome.kind}")

    try:
        await bus.emit(event)
    except Exception as exc:  # noqa: BLE001 - any emit failure is non-fatal
        _logger.warning(
            "scheduler: hook emit failed",
            extra={"job": job, "run_id": run_id, "error": str(exc)},
        )


async def _emit_failed(bus: HookBus, run_id: str, error_kind: str, exit_code: int | None) -> None:
    """Helper for the unsupported-action branch (no outcome to wrap)."""
    try:
        await bus.emit(
            HookEvent.EngineRunFailed(run_id=run_id, error_kind=error_kind, exit_code=exit_code)
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "scheduler: hook emit failed", extra={"run_id": run_id, "error": str(exc)}
        )


async def _maybe_record(
    app_state: object | None,
    *,
    job_name: str,
    run_id: str,
    action_kind: str,
    outcome_kind: str,
    error_kind: str | None,
    exit_code: int | None,
    duration_ms: int,
) -> None:
    """Persist a firing to the SchedulerStore parked on ``app_state``.

    Best-effort: no store reachable, or a write error, both skip silently
    — persistence is for history + missed-run catch-up across restarts,
    never load-bearing for the firing itself. This is the production
    caller that fills ``scheduler_runs`` so :func:`_maybe_catch_up` has a
    last-fire timestamp to read on the next boot.
    """
    store = _resolve_scheduler_store(app_state)
    if store is None:
        return
    record_raw = getattr(store, "record_raw", None)
    if record_raw is None:
        return
    try:
        await record_raw(
            job_name=job_name,
            run_id=run_id,
            action_kind=action_kind,
            outcome_kind=outcome_kind,
            error_kind=error_kind,
            exit_code=exit_code,
            duration_ms=duration_ms,
        )
    except Exception as exc:  # noqa: BLE001 — history is never load-bearing
        _logger.warning(
            "scheduler: run-history record failed",
            extra={"job": job_name, "run_id": run_id, "error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Per-job tick loop + spawn (mirrors runtime::run_job_loop + ::spawn).
# ---------------------------------------------------------------------------


class SchedulerHandle:
    """Handle to a running scheduler.

    Mirrors the Rust ``SchedulerHandle`` — holds the per-job
    :class:`asyncio.Task` references so the gateway shutdown path can
    await them after flipping the cancel event.
    """

    __slots__ = ("_app_state", "_bus", "_cancel", "_specs", "_tasks")

    def __init__(
        self,
        tasks: list[asyncio.Task[None]],
        cancel: asyncio.Event,
        *,
        specs: Mapping[str, JobSpec] | None = None,
        bus: HookBus | None = None,
        app_state: object | None = None,
    ) -> None:
        self._tasks = tasks
        self._cancel = cancel
        # ``specs``/``bus``/``app_state`` back the out-of-band
        # :meth:`trigger` ("fire now") path; the per-job tick loops keep
        # their own references, so a handle built without them (e.g. a
        # unit test that only inspects ``tasks``) still works.
        self._specs: dict[str, JobSpec] = dict(specs) if specs else {}
        self._bus = bus
        self._app_state = app_state

    @property
    def tasks(self) -> list[asyncio.Task[None]]:
        """The per-job tick tasks. Read-only for inspection; tests use
        this to assert "spawn returned N tasks for N parseable jobs"."""
        return list(self._tasks)

    async def trigger(self, name: str) -> None:
        """Fire job ``name`` immediately, out-of-band of its cron.

        The admin "fire now" route (``routes_admin_b/scheduler.py``)
        probes ``hasattr(sched, "trigger")`` and prefers this path. It
        reuses the same :func:`dispatch` call the tick loop runs, so a
        manual trigger and a scheduled firing are byte-identical
        (same ``app_state``, same hook event on the bus).

        Raises :class:`KeyError` if no job by that name is registered;
        the route turns that into a typed error envelope.
        """
        spec = self._specs.get(name)
        if spec is None or self._bus is None:
            raise KeyError(name)
        await dispatch(spec, self._bus, self._app_state)

    @property
    def cancel_event(self) -> asyncio.Event:
        """The cancellation flag shared with all tick loops. Flipping
        this stops every per-job loop at its next select-point."""
        return self._cancel

    def cancel(self) -> None:
        """Flip the cancel event. Convenience for tests; the gateway
        shutdown path flips its own event (passed into :func:`spawn`)."""
        self._cancel.set()

    async def join_all(self) -> None:
        """Drain every task, swallowing per-task errors.

        Mirrors the Rust ``SchedulerHandle::join_all`` — the gateway
        shutdown path calls this; tests typically inspect tasks
        directly via :attr:`tasks`.
        """
        if not self._tasks:
            return
        # ``gather(return_exceptions=True)`` so one task's CancelledError
        # doesn't mask another's normal exit.
        await asyncio.gather(*self._tasks, return_exceptions=True)


async def _sleep_until(deadline: float, cancel: asyncio.Event) -> bool:
    """Sleep until ``deadline`` (monotonic seconds) or until cancel fires.

    Returns ``True`` if the sleep was interrupted by cancel, ``False``
    if the deadline elapsed normally. The two-arm select mirrors the
    Rust ``tokio::select! { cancel.cancelled(); sleep(wait); }``
    pattern.
    """
    now = time.monotonic()
    wait = max(0.0, deadline - now)
    if wait <= 0:
        return cancel.is_set()
    cancel_task = asyncio.create_task(cancel.wait(), name="scheduler-cancel-wait")
    sleep_task = asyncio.create_task(asyncio.sleep(wait), name="scheduler-sleep")
    try:
        done, _pending = await asyncio.wait(
            {cancel_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for t in (cancel_task, sleep_task):
            if not t.done():
                t.cancel()
                with contextlib.suppress(BaseException):
                    await t
    return cancel_task in done


#: Default missed-run catch-up grace window (seconds). When the gateway
#: restarts and a job's scheduled firing was missed during the downtime,
#: the job fires once immediately *iff* the missed firing is within this
#: window. Older misses are skipped (we don't replay a backlog). Operators
#: override via ``CORLINMAN_SCHEDULER_CATCHUP_GRACE_SECS``.
_CATCHUP_GRACE_SECS_DEFAULT: int = 3600


def _resolve_catchup_grace_secs() -> int:
    raw = os.environ.get("CORLINMAN_SCHEDULER_CATCHUP_GRACE_SECS", "")
    if not raw:
        return _CATCHUP_GRACE_SECS_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return _CATCHUP_GRACE_SECS_DEFAULT


def _resolve_scheduler_store(app_state: object | None) -> object | None:
    """Best-effort resolve a :class:`SchedulerStore`-shaped handle off
    ``app_state`` for last-fire lookups. Returns ``None`` (no catch-up)
    when nothing usable is parked — keeps unit tests + degraded boots
    catch-up-free without any wiring."""
    if app_state is None:
        return None
    store = cast(object | None, getattr(app_state, "scheduler_store", None))
    if store is not None:
        return store
    handle = getattr(app_state, "corlinman_scheduler_handle", None)
    if handle is not None:
        store = cast(object | None, getattr(handle, "store", None))
        return store if store is not None else None
    return None


async def _maybe_catch_up(
    spec: JobSpec,
    bus: HookBus,
    app_state: object | None,
) -> None:
    """Fire ``spec`` once on startup if its previous scheduled firing was
    missed during downtime and falls within the catch-up grace window.

    Reads the last recorded firing for ``spec.name`` from a
    SchedulerStore parked on ``app_state`` (when available). Computes the
    most-recent scheduled firing that is at-or-before ``now``; if that
    firing happened *after* the last recorded run and is within the grace
    window, dispatch once. Best-effort + fully defensive: no store, no
    history, parse trouble, or an out-of-grace miss all skip silently.
    """
    from datetime import datetime, timedelta

    store = _resolve_scheduler_store(app_state)
    if store is None:
        return
    list_for_job = getattr(store, "list_for_job", None)
    if list_for_job is None:
        return
    try:
        recent = await list_for_job(spec.name, 1)
    except Exception as exc:  # noqa: BLE001 — catch-up is never load-bearing
        _logger.warning(
            "scheduler: catch-up last-fire lookup failed",
            extra={"job": spec.name, "error": str(exc)},
        )
        return
    last_fired_ms: int | None = None
    if recent:
        last_fired_ms = getattr(recent[0], "fired_at_ms", None)

    now_wall = datetime.now(tz=UTC)
    grace = _resolve_catchup_grace_secs()
    # Walk back from now to find the most-recent scheduled firing at-or-
    # before now. ``next_after`` only yields strictly-future firings, so
    # we step back a window and take the latest firing that is <= now.
    lookback = now_wall - timedelta(seconds=max(grace, 60) + 86400)
    cursor = lookback
    last_due: datetime | None = None
    # Bounded walk: at most ~1500 steps even for a per-minute cron over a
    # 24h+grace lookback — cheap and can't spin (next_after is strictly
    # increasing, and we break once we pass ``now``).
    for _ in range(2000):
        nxt = next_after(spec.cron, cursor)
        if nxt is None or nxt > now_wall:
            break
        last_due = nxt
        cursor = nxt
    if last_due is None:
        return
    missed_age = (now_wall - last_due).total_seconds()
    if missed_age > grace:
        return  # too old — don't replay stale backlog
    if last_fired_ms is not None:
        last_due_ms = int(last_due.timestamp() * 1000)
        if last_fired_ms >= last_due_ms:
            return  # already ran this firing — not actually missed
    _logger.info(
        "scheduler: catching up missed firing",
        extra={
            "job": spec.name,
            "missed_due_at": last_due.isoformat(),
            "missed_age_secs": int(missed_age),
        },
    )
    await dispatch(spec, bus, app_state)


async def _run_job_loop(
    spec: JobSpec,
    bus: HookBus,
    cancel: asyncio.Event,
    app_state: object | None = None,
    *,
    catch_up: bool = True,
) -> None:
    """Per-job tick loop. Mirrors Rust ``runtime::run_job_loop``.

    Responsibilities:

    * (R4 gap goals-cron) catch up a single missed firing on startup when
      ``catch_up`` is set and a SchedulerStore is reachable off
      ``app_state`` — see :func:`_maybe_catch_up`;
    * compute the next firing relative to *wall clock* ``utcnow``;
    * sleep until then (or until cancel fires);
    * dispatch on the action;
    * loop.

    A schedule that never has another firing (cron expression valid
    but astronomically impossible, e.g. Feb 30) breaks the loop with
    a ``warning`` log — we don't want to busy-spin asking for
    :func:`next_after`.
    """
    from datetime import datetime

    _logger.info("scheduler: job loop started", extra={"job": spec.name})
    if catch_up and not cancel.is_set():
        try:
            await _maybe_catch_up(spec, bus, app_state)
        except Exception as exc:  # noqa: BLE001 — never crash the loop
            _logger.warning(
                "scheduler: catch-up failed",
                extra={"job": spec.name, "error": str(exc)},
            )
    while True:
        if cancel.is_set():
            _logger.info("scheduler: cancelled; exiting", extra={"job": spec.name})
            return
        now_wall = datetime.now(tz=UTC)
        nxt = next_after(spec.cron, now_wall)
        if nxt is None:
            _logger.warning(
                "scheduler: cron has no upcoming firing; exiting job loop",
                extra={"job": spec.name},
            )
            return
        wait_secs = max(0.0, (nxt - now_wall).total_seconds())
        deadline_mono = time.monotonic() + wait_secs
        _logger.debug(
            "scheduler: next firing computed",
            extra={
                "job": spec.name,
                "next_fire_at": nxt.isoformat(),
                "wait_secs": int(wait_secs),
            },
        )
        cancelled = await _sleep_until(deadline_mono, cancel)
        if cancelled:
            _logger.info(
                "scheduler: cancelled while sleeping; exiting", extra={"job": spec.name}
            )
            return
        # Re-check the cancel flag before firing — the sleep could
        # have completed in the same tick as a cancel signal. Mirrors
        # the Rust ``if cancel.is_cancelled() { return; }`` guard.
        if cancel.is_set():
            _logger.info(
                "scheduler: cancelled before fire; exiting", extra={"job": spec.name}
            )
            return
        await dispatch(spec, bus, app_state)


def spawn(
    cfg: SchedulerConfig,
    bus: HookBus,
    cancel: asyncio.Event | None = None,
    app_state: object | None = None,
    *,
    catch_up: bool = True,
) -> SchedulerHandle:
    """Spawn one tick task per ``cfg.jobs`` entry.

    Returns a :class:`SchedulerHandle` aggregating the per-job tasks.
    Jobs whose cron fails to parse are dropped with a warning; the
    rest of the scheduler continues. A config with zero parseable
    jobs returns a handle with an empty task list (no-op scheduler).

    ``app_state`` is threaded into every firing via :func:`dispatch`
    so ``run_tool`` builtins (``system.update_check`` /
    ``evolution.darwin_curate``) read a live ``app.state`` instead of
    degrading to ``checker_unavailable``. It stays ``None`` for unit
    tests; the gateway lifespan passes ``app.state``.

    Mirrors the Rust :func:`spawn` 1:1 except for one Python-flavour
    convenience: ``cancel`` is optional — when omitted we make a
    fresh :class:`asyncio.Event` so unit tests can spawn a scheduler
    without threading a cancel event through. The gateway shutdown
    path always passes its own.
    """
    if cancel is None:
        cancel = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    specs: dict[str, JobSpec] = {}
    for job in cfg.jobs:
        spec = JobSpec.from_config(job)
        if spec is None:
            continue
        specs[job.name] = spec
        tasks.append(
            asyncio.create_task(
                _run_job_loop(spec, bus, cancel, app_state, catch_up=catch_up),
                name=f"scheduler-{job.name}",
            )
        )
    return SchedulerHandle(
        tasks=tasks, cancel=cancel, specs=specs, bus=bus, app_state=app_state
    )


__all__ = [
    "ActionSpec",
    "JobAction",
    "JobSpec",
    "SchedulerConfig",
    "SchedulerHandle",
    "SchedulerJob",
    "SubprocessOutcome",
    "SubprocessOutcomeKind",
    "dispatch",
    "run_subprocess",
    "spawn",
]
