"""Sync-plugin execution — extracted from
:mod:`corlinman_server.gateway.grpc.plugin_invoker`.

Owns the spawn-per-call JSON-RPC stdio child path (``sync`` / ``async``
plugins) plus its small helpers. MUST NOT import the source module
(no cycle); the source re-imports :func:`invoke_sync_plugin` from here.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import structlog
from corlinman_grpc.agent_client import ToolInvocation

__all__ = ["invoke_sync_plugin"]

log = structlog.get_logger(__name__)


def _error_invocation(code: str, message: str, duration_ms: int = 0) -> ToolInvocation:
    """Build an ``is_error`` :class:`ToolInvocation` with a stable body."""
    return ToolInvocation(
        content=json.dumps({"error": code, "message": message}),
        is_error=True,
        duration_ms=duration_ms,
    )


def _decode_args(args_json: bytes) -> Any:
    """Decode the OpenAI ``arguments`` JSON. Empty / blank → ``{}``.

    Returns the parsed object on success; raises :class:`ValueError`
    with a human-readable message on malformed JSON so the caller can
    fold it into a tool-level error result.
    """
    raw = args_json.decode("utf-8", errors="replace").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"tool arguments are not valid JSON: {exc}") from exc


async def invoke_sync_plugin(
    entry: Any,
    tool: str,
    args: Any,
    *,
    timeout_ms: int,
) -> ToolInvocation:
    """Run one ``sync`` plugin tool call as a spawn-per-call stdio child.

    ``entry`` is a :class:`corlinman_providers.plugins.PluginEntry`. The
    child is launched from ``entry.manifest.entry_point`` with the
    manifest dir as CWD; one JSON-RPC request line is written, stdin is
    half-closed, and one response line is read back and decoded.

    Never raises — every failure (spawn error, timeout, malformed
    response, JSON-RPC error) is mapped to an ``is_error``
    :class:`ToolInvocation`.
    """
    from corlinman_providers.plugins import parse_response_line

    manifest = entry.manifest
    ep = manifest.entry_point
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": tool,
        "params": args,
    }
    request_line = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")

    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            ep.command,
            *ep.args,
            cwd=str(entry.plugin_dir()),
            env=_child_env(ep.env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        return _error_invocation(
            "plugin_spawn_failed",
            f"could not launch plugin {manifest.name!r}: {exc}",
        )

    deadline_s = max(timeout_ms, 1) / 1000.0
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=request_line),
            timeout=deadline_s,
        )
    except TimeoutError:
        with _SuppressProcessErrors():
            proc.kill()
        with _SuppressProcessErrors():
            await proc.wait()
        elapsed = int((time.monotonic() - started) * 1000)
        return _error_invocation(
            "plugin_timeout",
            f"plugin {manifest.name!r} did not respond within {timeout_ms}ms",
            elapsed,
        )

    elapsed = int((time.monotonic() - started) * 1000)

    if not stdout.strip():
        err_tail = stderr.decode("utf-8", errors="replace").strip()[-512:]
        return _error_invocation(
            "plugin_no_output",
            (
                f"plugin {manifest.name!r} exited without a JSON-RPC line "
                f"(rc={proc.returncode}); stderr: {err_tail or '<empty>'}"
            ),
            elapsed,
        )

    # Take the first newline-delimited line — sync plugins answer with
    # exactly one JSON-RPC response per request.
    first_line = stdout.split(b"\n", 1)[0]
    try:
        output = parse_response_line(first_line, elapsed)
    except Exception as exc:  # malformed plugin output
        return _error_invocation(
            "plugin_bad_response",
            f"plugin {manifest.name!r} returned an undecodable line: {exc}",
            elapsed,
        )

    if output.kind == "error":
        return ToolInvocation(
            content=json.dumps(
                {
                    "error": "plugin_error",
                    "code": output.code,
                    "message": output.message,
                }
            ),
            is_error=True,
            duration_ms=output.duration_ms,
        )
    if output.kind == "accepted_for_later":
        # Async-style ``task_id`` from a plugin the registry classified
        # as sync — surface it verbatim so the model can poll, but it is
        # not an error.
        return ToolInvocation(
            content=json.dumps({"status": "accepted", "task_id": output.task_id}),
            is_error=False,
            duration_ms=output.duration_ms,
        )

    # Success — ``content`` is the JSON-RPC ``result`` payload bytes.
    body = output.content or b"null"
    return ToolInvocation(
        content=body.decode("utf-8", errors="replace"),
        is_error=False,
        duration_ms=output.duration_ms,
    )


def _child_env(extra: dict[str, str]) -> dict[str, str]:
    """Build the child process environment: inherit the gateway's env,
    then layer the manifest's ``entry_point.env`` on top."""
    env = os.environ.copy()
    env.update(extra)
    return env


class _SuppressProcessErrors:
    """Tiny ctx mgr swallowing :class:`ProcessLookupError` / :class:`OSError`
    from killing / reaping an already-dead child."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, _t: object, exc: BaseException | None, _tb: object) -> bool:
        return isinstance(exc, (ProcessLookupError, OSError))
