"""Concrete plugin invoker — runs a tool call against a real plugin.

This is the gateway-assembly half of the tool-execution split documented
in :mod:`corlinman_grpc.agent_client.tool_executor`. The
:class:`~corlinman_grpc.agent_client.RegistryToolExecutor` lives in
``corlinman-grpc`` and stays free of any plugin import; this module owns
the :class:`corlinman_providers.plugins.PluginRegistry` knowledge and
exposes a :data:`~corlinman_grpc.agent_client.PluginInvoker`-shaped
callable the executor delegates to.

What "real execution" means here
--------------------------------

A plugin tool call is dispatched by:

1. resolving the plugin name to a :class:`PluginEntry` in the registry;
2. checking the tool name is one the plugin's manifest advertises;
3. building a JSON-RPC 2.0 request (the ``openai_function`` protocol —
   the tool name is the JSON-RPC ``method``, the OpenAI ``arguments``
   object is ``params``);
4. running the plugin's ``entry_point`` as a spawn-per-call ``sync``
   stdio child: write one request line, half-close stdin, read one
   response line;
5. decoding that line with
   :func:`corlinman_providers.plugins.parse_response_line` into a
   :class:`PluginOutput` and folding it into a
   :class:`~corlinman_grpc.agent_client.ToolInvocation`.

``service`` plugins (long-lived gRPC) and ``mcp`` plugins are out of
scope for this parcel — they need a running supervisor / MCP bridge. The
invoker returns a clear, non-crashing error result for those so the
reasoning loop still makes progress; the
:class:`~corlinman_grpc.agent_client.RegistryToolExecutor` wraps the
whole thing so a raised exception can never reach the chat stream.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import structlog
from corlinman_grpc.agent_client import ToolInvocation

__all__ = [
    "DEFAULT_TOOL_TIMEOUT_MS",
    "build_registry_invoker",
    "invoke_sync_plugin",
]

log = structlog.get_logger(__name__)

#: Fallback per-call deadline (ms) when a manifest does not pin
#: ``[communication].timeout_ms``. Mirrors a conservative sync-plugin
#: budget — plugins that need longer must declare it explicitly.
DEFAULT_TOOL_TIMEOUT_MS = 30_000


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


def build_registry_invoker(registry: Any | None) -> Any:
    """Build a :data:`~corlinman_grpc.agent_client.PluginInvoker` bound to
    ``registry``.

    ``registry`` is a :class:`corlinman_providers.plugins.PluginRegistry`
    (or ``None``). The returned async callable has the
    ``(plugin, tool, args_json) -> ToolInvocation`` shape the
    :class:`~corlinman_grpc.agent_client.RegistryToolExecutor` expects.

    Degradation
    -----------

    * ``registry is None`` → every call returns a
      ``plugin_registry_unavailable`` error invocation (the gateway has
      no plugins assembled; the loop still progresses).
    * plugin name not in the registry → ``plugin_not_found``.
    * tool not advertised by the plugin's manifest → ``tool_not_found``.
    * non-``sync`` plugin types → ``unsupported_plugin_type`` (service /
      mcp need a supervisor / bridge — out of this parcel's scope).

    None of these raise; the executor would catch it anyway, but
    returning a structured result keeps the model's next round useful.
    """

    async def _invoke(plugin: str, tool: str, args_json: bytes) -> ToolInvocation:
        if registry is None:
            return _error_invocation(
                "plugin_registry_unavailable",
                "no plugin registry is wired into the gateway",
            )

        entry = registry.get(plugin)
        if entry is None:
            # OpenAI tool calls collapse plugin == tool == function.name
            # (see ReasoningLoop._finalise_tool_call), so the agent often
            # sends the *tool* name as the plugin. Fall back to a scan of
            # every registered plugin's advertised tools.
            entry = _resolve_by_tool(registry, tool if tool else plugin)
        if entry is None:
            return _error_invocation(
                "plugin_not_found",
                f"no registered plugin or tool named {plugin!r}",
            )

        manifest = entry.manifest
        tool_name = tool or plugin
        advertised = {t.name for t in manifest.capabilities.tools}
        if advertised and tool_name not in advertised:
            return _error_invocation(
                "tool_not_found",
                (
                    f"plugin {manifest.name!r} does not advertise a tool "
                    f"named {tool_name!r}; known: {sorted(advertised)}"
                ),
            )

        plugin_type = str(getattr(manifest.plugin_type, "value", manifest.plugin_type))
        if plugin_type != "sync":
            return _error_invocation(
                "unsupported_plugin_type",
                (
                    f"plugin {manifest.name!r} is {plugin_type!r}; the gateway "
                    "tool executor currently runs only spawn-per-call 'sync' "
                    "plugins"
                ),
            )

        try:
            args = _decode_args(args_json)
        except ValueError as exc:
            return _error_invocation("bad_tool_arguments", str(exc))

        timeout_ms = manifest.communication.timeout_ms or DEFAULT_TOOL_TIMEOUT_MS
        log.debug(
            "plugin_invoker.dispatch",
            plugin=manifest.name,
            tool=tool_name,
            timeout_ms=timeout_ms,
        )
        return await invoke_sync_plugin(
            entry, tool_name, args, timeout_ms=timeout_ms
        )

    return _invoke


def _resolve_by_tool(registry: Any, tool_name: str) -> Any | None:
    """Find the plugin whose manifest advertises ``tool_name``.

    The first match in the registry's alphabetical listing wins — tool
    names are expected to be unique across plugins; a collision is a
    manifest-authoring bug the registry's diagnostics already flag.
    """
    for entry in registry.list():
        for tool in entry.manifest.capabilities.tools:
            if tool.name == tool_name:
                return entry
    return None
