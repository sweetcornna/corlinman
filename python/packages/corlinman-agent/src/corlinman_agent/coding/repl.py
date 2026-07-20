"""Opt-in ``execute_code`` tool — a persistent-session Python REPL.

This is the in-process code-execution surface hermes-agent / Claude Code
expose (downgraded — :func:`~corlinman_agent.coding.shell.run_shell`
already covers the basic "run a command" case). It runs short Python
snippets in a **persistent** subprocess so state (imports, variables,
defined functions) carries across calls within a session, letting the
model batch round-trips instead of re-establishing context every time.

## DISABLED BY DEFAULT

``execute_code`` is **off** unless the operator opts in by setting
``CORLINMAN_ENABLE_EXECUTE_CODE`` to a truthy value (``1`` / ``true`` /
``yes`` / ``on``, case-insensitive). When disabled, the dispatcher
returns a clear ``{"error": "execute_code_disabled", ...}`` envelope and
never spawns anything. This mirrors the conservative posture of
``run_shell`` — code execution is a real capability with a real blast
radius, so it is not handed to a model implicitly.

## Security model

Same backstops as :mod:`.shell`, reused deliberately:

* a **minimal env whitelist** (``_build_child_env``) so provider API
  keys / OAuth tokens / hook secrets are stripped from the child;
* **POSIX resource limits** + ``setsid`` (``_preexec_apply_rlimits``)
  so a runaway snippet cannot DoS the host and the whole process group
  can be reaped on timeout;
* a wall-clock **timeout** per ``execute_code`` call;
* a capped output buffer fed back to the model.

The persistent interpreter is a plain ``python -i`` (or
``$CORLINMAN_PYTHON`` / ``sys.executable``) child reading driver
commands on stdin. It is NOT a sandbox — see the :mod:`.shell` module
docstring; the real isolation lives in the deployment.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import structlog

from corlinman_agent.coding._common import (
    CodingArgsInvalidError,
    decode_args,
    resolve_workspace,
)
from corlinman_agent.coding.environment import (
    SpawnedProcess,
    get_environment,
)

logger = structlog.get_logger(__name__)

EXECUTE_CODE_TOOL: str = "execute_code"

#: Env flag that opts the operator in. Truthy → enabled.
_ENABLE_ENV: str = "CORLINMAN_ENABLE_EXECUTE_CODE"

#: Default / hard-max per-call timeout (seconds). Matches run_shell.
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 60

#: Cap on combined stdout+stderr returned to the model (chars).
_MAX_OUTPUT_CHARS = 16_000

#: Unique sentinel printed after each snippet so the driver loop knows
#: where one execution's output ends. A random suffix per session makes
#: it effectively impossible for snippet output to forge the marker.
_DONE_MARKER_PREFIX = "__CORLINMAN_EXEC_DONE__"

#: Driver function compiled+exec'd into the child interpreter once at
#: spawn. Defines ``__corlinman_exec__(src, marker)`` which runs ``src``
#: under ``exec`` in a persistent globals dict (so state carries across
#: calls), capturing stdout+stderr, formatting any traceback as text
#: (a raising snippet must not kill the long-lived interpreter), then
#: printing ``marker`` so the reader can delimit one call's output.
_DRIVER_SRC = (
    "import sys, io, traceback\n"
    "def __corlinman_exec__(src, marker):\n"
    "    g = globals().setdefault('__corlinman_repl_ns__', {})\n"
    "    buf = io.StringIO()\n"
    "    o, e = sys.stdout, sys.stderr\n"
    "    sys.stdout = sys.stderr = buf\n"
    "    try:\n"
    "        exec(compile(src, '<execute_code>', 'exec'), g)\n"
    "    except BaseException:\n"
    "        traceback.print_exc()\n"
    "    finally:\n"
    "        sys.stdout, sys.stderr = o, e\n"
    "        o.write(buf.getvalue())\n"
    "        o.write('\\n' + marker + '\\n')\n"
    "        o.flush()\n"
)


def _enabled() -> bool:
    """True iff the operator opted code-execution in via the env flag."""
    raw = os.environ.get(_ENABLE_ENV, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def execute_code_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped tool descriptor for ``execute_code``."""
    return {
        "type": "function",
        "function": {
            "name": EXECUTE_CODE_TOOL,
            "description": (
                "Execute a Python snippet in a persistent interpreter "
                "session. Variables, imports, and functions defined in one "
                "call are available in the next. Returns combined "
                "stdout+stderr. Use this to compute, transform data, or "
                "drive a multi-step analysis without re-establishing state."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Python source to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            f"Timeout in seconds (default {_DEFAULT_TIMEOUT}, "
                            f"max {_MAX_TIMEOUT})."
                        ),
                    },
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        },
    }


class _ReplSession:
    """A persistent Python interpreter child driven over stdin/stdout.

    Holds one long-lived subprocess. Each :meth:`execute` wraps the
    caller's snippet so its combined stdout+stderr is captured and a
    unique done-marker is printed when it finishes, letting the reader
    delimit one execution's output. The interpreter is spawned lazily on
    first use and respawned if it has died.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._proc: asyncio.subprocess.Process | None = None
        self._handle: SpawnedProcess | None = None
        self._marker = f"{_DONE_MARKER_PREFIX}{uuid.uuid4().hex}"
        self._lock = asyncio.Lock()

    async def _ensure_proc(self) -> asyncio.subprocess.Process:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            return proc
        # Spawn the persistent interpreter through the sandbox seam. This is
        # only reached from :meth:`execute`, which the dispatcher gates on
        # ``CORLINMAN_ENABLE_EXECUTE_CODE`` — nothing spawns while disabled.
        handle = await get_environment().spawn_repl(workspace=self._workspace)
        self._handle = handle
        proc = handle.proc
        self._proc = proc
        # Bootstrap the driver function ONCE. ``-i`` reads one *logical*
        # line at a time from the pipe; a multi-line compound statement
        # (``try:``/``finally:``) pasted over a pipe is unreliable because
        # the REPL needs a blank continuation line to close the block.
        # So we send the driver body as a single physical line: an
        # ``exec(compile(<json-literal>, ...))`` that defines the helper.
        # Every later ``execute`` is then a single-line *call* to it.
        assert proc.stdin is not None
        driver_src = json.dumps(_DRIVER_SRC)
        bootstrap = f"exec(compile({driver_src}, '<repl-driver>', 'exec'))\n"
        proc.stdin.write(bootstrap.encode("utf-8"))
        await proc.stdin.drain()
        return proc

    def _wrap(self, code: str) -> str:
        """Build the single-physical-line driver *call* for ``code``.

        The heavy lifting (stdout/stderr capture, ``exec`` into the shared
        namespace, traceback formatting, marker emission) lives in the
        :data:`_DRIVER_SRC` function bootstrapped on the child once. Here
        we just JSON-encode the snippet + marker and emit a one-line call,
        which the ``-i`` REPL evaluates atomically.
        """
        return (
            f"__corlinman_exec__({json.dumps(code)}, {json.dumps(self._marker)})\n"
        )

    def _kill(self) -> None:
        # Termination travels with the handle — the local backend SIGKILLs
        # the whole process group (setsid leader), same as the old private
        # killpg copy. Never raises.
        handle = self._handle
        if handle is None:
            return
        handle.kill()

    async def execute(self, code: str, timeout: int) -> tuple[str, bool]:
        """Run ``code`` in the session. Returns ``(output, timed_out)``.

        On timeout the interpreter is killed (it may be wedged in the
        snippet) and respawned on the next call, so a runaway snippet
        does not poison the session permanently.
        """
        async with self._lock:
            proc = await self._ensure_proc()
            assert proc.stdin is not None and proc.stdout is not None
            payload = self._wrap(code).encode("utf-8")
            try:
                proc.stdin.write(payload)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # Child died mid-write — respawn next call.
                self._kill()
                self._proc = None
                self._handle = None
                return ("error: interpreter pipe broken; session reset", False)

            marker_b = self._marker.encode("utf-8")
            collected: list[bytes] = []
            timed_out = False
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            # EOF — child exited unexpectedly.
                            break
                        # The ``-i`` REPL echoes its ``>>> `` / ``... ``
                        # prompts to the merged stream, so the marker may
                        # arrive prefixed by prompt noise. Match on the
                        # marker appearing anywhere in the line and drop any
                        # leading prompt fragment that precedes it.
                        if marker_b in line:
                            head = line.split(marker_b, 1)[0]
                            head = head.replace(b">>> ", b"").replace(b"... ", b"")
                            if head.strip():
                                collected.append(head)
                            break
                        collected.append(line)
            except TimeoutError:
                timed_out = True
                self._kill()
                self._proc = None
                self._handle = None

            # Strip the interpreter's own prompt fragments from the
            # captured output — they're protocol noise, not the snippet's.
            output = b"".join(collected).decode("utf-8", errors="replace")
            output = output.replace(">>> ", "").replace("... ", "")
            return (output, timed_out)

    async def close(self) -> None:
        async with self._lock:
            self._kill()
            self._proc = None
            self._handle = None


#: Process-wide session registry, keyed by an opaque session id. Lets a
#: chat session keep one persistent interpreter across many tool calls.
#: ``None`` key → an anonymous shared session (one-shot HTTP callers).
_SESSIONS: dict[str, _ReplSession] = {}


def _session_for(session_key: str | None, workspace: Path) -> _ReplSession:
    key = session_key or "__anon__"
    sess = _SESSIONS.get(key)
    if sess is None:
        sess = _ReplSession(workspace)
        _SESSIONS[key] = sess
    return sess


async def dispatch_execute_code(
    *,
    args_json: bytes | str,
    workspace: Path | None = None,
    session_key: str | None = None,
) -> str:
    """Execute a Python snippet in a persistent session. Never raises.

    Returns an ``{"error": "execute_code_disabled", ...}`` envelope
    unchanged unless ``CORLINMAN_ENABLE_EXECUTE_CODE`` is truthy.
    """
    if not _enabled():
        return json.dumps(
            {
                "error": "execute_code_disabled",
                "message": (
                    "code execution is disabled. Set "
                    f"{_ENABLE_ENV}=1 to enable it (operator opt-in)."
                ),
            }
        )

    try:
        raw = decode_args(args_json)
    except CodingArgsInvalidError as exc:
        return json.dumps({"error": f"args_invalid: {exc.message}"})

    code = raw.get("code")
    if not isinstance(code, str) or not code.strip():
        return json.dumps({"error": "args_invalid: missing or empty 'code'"})

    timeout = raw.get("timeout", _DEFAULT_TIMEOUT)
    try:
        timeout = min(_MAX_TIMEOUT, max(1, int(timeout)))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT

    ws = resolve_workspace(workspace)
    session = _session_for(session_key, ws)

    try:
        output, timed_out = await session.execute(code, timeout)
    except Exception as exc:  # noqa: BLE001 — dispatcher must never raise
        logger.exception("execute_code.unexpected")
        return json.dumps({"error": f"run_failed: {exc}"})

    truncated = len(output) > _MAX_OUTPUT_CHARS
    if truncated:
        output = "…(output truncated)\n" + output[-_MAX_OUTPUT_CHARS:]

    logger.info(
        "execute_code.executed",
        chars=len(output),
        timed_out=timed_out,
        truncated=truncated,
    )

    if timed_out:
        return json.dumps(
            {
                "error": f"timeout: killed after {timeout}s; session reset",
                "output": output,
                "truncated": truncated,
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "output": output,
            "truncated": truncated,
            "ran_at_ms": int(time.time() * 1000),
        },
        ensure_ascii=False,
    )


__all__ = [
    "EXECUTE_CODE_TOOL",
    "dispatch_execute_code",
    "execute_code_tool_schema",
]
