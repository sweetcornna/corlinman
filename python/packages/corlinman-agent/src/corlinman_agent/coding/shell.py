"""Builtin ``run_shell`` tool — execute a shell command.

The command runs with the agent workspace as its working directory, a
wall-clock timeout, and a capped combined stdout+stderr buffer.

## Security note

``run_shell`` is a **real shell** — it is not chrooted or containerised.
The workspace is only its *cwd*; a command can still read paths outside
it. This matches hermes-agent's terminal model: confinement of the file
tools is enforced, the shell's blast radius is the deployment's
responsibility (run the agent as a low-privilege user, in a container,
or disable this tool). A small denylist blocks the most obvious
foot-guns; it is a guard-rail, not a sandbox.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import structlog

from corlinman_agent.coding._common import (
    CodingArgsInvalidError,
    decode_args,
    resolve_workspace,
)

logger = structlog.get_logger(__name__)

RUN_SHELL_TOOL: str = "run_shell"

#: Default / hard-max command timeout (seconds).
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120
#: Cap on combined stdout+stderr returned to the model (chars).
_MAX_OUTPUT_CHARS = 30_000

#: Obvious destructive / privilege patterns refused outright. This is a
#: guard-rail against accidents, not a security boundary.
_DENY = re.compile(
    r"\brm\s+-rf?\s+(/|~|\$HOME)|\b(shutdown|reboot|mkfs|:\(\)\s*\{)",
    re.IGNORECASE,
)


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
    if _DENY.search(command):
        return json.dumps(
            {"command": command, "error": "command_refused: destructive pattern"}
        )

    timeout = raw.get("timeout", _DEFAULT_TIMEOUT)
    try:
        timeout = min(_MAX_TIMEOUT, max(1, int(timeout)))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT

    ws = resolve_workspace(workspace)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(ws),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return json.dumps({"command": command, "error": f"spawn_failed: {exc}"})

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
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
    if truncated:
        output = output[:_MAX_OUTPUT_CHARS] + "\n…(output truncated)"
    return json.dumps(
        {
            "command": command,
            "exit_code": proc.returncode,
            "output": output,
            "truncated": truncated,
        },
        ensure_ascii=False,
    )


__all__ = [
    "RUN_SHELL_TOOL",
    "dispatch_run_shell",
    "run_shell_tool_schema",
]
