"""Real :class:`ToolExecutor` — bridges ``ServerFrame.tool_call`` onto an
injected plugin invoker.

Replaces the M1/M2 :class:`PlaceholderExecutor`
(``agent_client.client.PlaceholderExecutor``), whose
``awaiting_plugin_runtime`` payload only acknowledged a call without ever
running anything. The reasoning loop short-circuited on that placeholder
(:func:`corlinman_agent.reasoning_loop._is_awaiting_placeholder`), so an
agent that asked for a tool never made multi-round progress.

Layering
--------

``agent_client`` deliberately never imports the wider Python plane (no
``corlinman-providers``, no ``corlinman-server``) — see the package
docstring. So the *real* executor cannot import a plugin registry
directly either. Instead it depends on a tiny structural contract,
:class:`PluginInvoker`: a callable that, given a plugin name + tool name
+ args bytes, returns a :class:`ToolInvocation`. The gateway assembly
layer (``corlinman-server``) supplies the concrete invoker that knows
about :class:`corlinman_providers.plugins.PluginRegistry`. This mirrors
the Rust ``ToolExecutor`` / plugin-runtime split 1:1.

Graceful degradation
--------------------

:class:`RegistryToolExecutor` never raises out of :meth:`execute`. Every
failure mode — no invoker wired, plugin not found, invoker raised,
invoker timed out — is folded into a ``ToolResult`` with
``is_error=True`` carrying a structured ``{"error": ...}`` JSON body, so
the reasoning loop keeps a real (non-placeholder) result to feed the
model's next round instead of crashing the stream.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from corlinman_grpc._generated.corlinman.v1 import agent_pb2

logger = structlog.get_logger(__name__)

__all__ = [
    "PluginInvoker",
    "RegistryToolExecutor",
    "ToolInvocation",
    "error_result",
]


# ---------------------------------------------------------------------------
# Invoker contract.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolInvocation:
    """Outcome of one plugin tool invocation.

    ``content`` is the stringified result payload that becomes the
    ``role="tool"`` message content fed back into the reasoning loop —
    always a ``str`` so the loop never has to guess an encoding.

    ``is_error`` marks a tool-level failure (the plugin ran but returned
    a JSON-RPC error, or the runtime could not reach the plugin); the
    reasoning loop still feeds it so the model can react.

    ``duration_ms`` is best-effort wall-clock; ``0`` when the invoker did
    not measure it.
    """

    content: str
    is_error: bool = False
    duration_ms: int = 0


#: A plugin invoker resolves + runs one tool call. Async; takes the
#: plugin name, tool name and the raw OpenAI ``arguments`` JSON bytes,
#: and returns a :class:`ToolInvocation`. Implementations live in the
#: gateway assembly layer (they own the :class:`PluginRegistry`).
PluginInvoker = Callable[[str, str, bytes], Awaitable[ToolInvocation]]


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def error_result(
    call: agent_pb2.ToolCall,
    *,
    code: str,
    message: str,
    duration_ms: int = 0,
) -> agent_pb2.ToolResult:
    """Build an ``is_error`` :class:`agent_pb2.ToolResult` for ``call``.

    The body is a stable ``{"error": <code>, "message": <message>,
    "plugin": ..., "tool": ...}`` JSON object. Distinct from the M2
    ``awaiting_plugin_runtime`` placeholder — the reasoning loop treats
    this as a *real* result (it is not detected by
    ``_is_awaiting_placeholder``), so the model gets a genuine failure
    signal instead of a silent doom-loop guard.
    """
    payload = {
        "error": code,
        "message": message,
        "plugin": call.plugin,
        "tool": call.tool,
    }
    return agent_pb2.ToolResult(
        call_id=call.call_id,
        result_json=json.dumps(payload).encode("utf-8"),
        is_error=True,
        duration_ms=int(duration_ms),
    )


# ---------------------------------------------------------------------------
# RegistryToolExecutor.
# ---------------------------------------------------------------------------


class RegistryToolExecutor:
    """Production :class:`~corlinman_grpc.agent_client.ToolExecutor`.

    Delegates every call to an injected :data:`PluginInvoker`. The
    executor itself owns no plugin state — that keeps ``agent_client``
    free of a ``corlinman-providers`` import while still letting the
    gateway run real plugins.

    Parameters
    ----------
    invoker:
        The plugin invoker. ``None`` is permitted — the executor then
        degrades every call to a clear ``executor_not_wired`` error
        result rather than crashing. This is the safe default for a
        deployment that has no plugin registry assembled yet.
    timeout_s:
        Hard wall-clock cap for a single invocation. On timeout the call
        yields an ``is_error`` ``tool_timeout`` result. ``None`` disables
        the cap (the invoker is then responsible for its own deadline).
    """

    def __init__(
        self,
        invoker: PluginInvoker | None = None,
        *,
        timeout_s: float | None = 30.0,
    ) -> None:
        self._invoker = invoker
        self._timeout_s = timeout_s

    @property
    def is_wired(self) -> bool:
        """Whether a real invoker is attached. ``False`` means every
        call degrades to an ``executor_not_wired`` error result."""
        return self._invoker is not None

    async def execute(self, call: agent_pb2.ToolCall) -> agent_pb2.ToolResult:
        """Run one tool call, never raising.

        Returns a :class:`agent_pb2.ToolResult` whose ``call_id`` echoes
        the request. Any failure is mapped to an ``is_error`` result so
        the reasoning loop always has something concrete to feed back.
        """
        if self._invoker is None:
            logger.debug(
                "tool_executor.not_wired",
                plugin=call.plugin,
                tool=call.tool,
                call_id=call.call_id,
            )
            return error_result(
                call,
                code="executor_not_wired",
                message=(
                    "no plugin invoker is wired into the gateway; "
                    "tool call observed but cannot be executed"
                ),
            )

        started = time.monotonic()
        try:
            if self._timeout_s is not None:
                invocation = await asyncio.wait_for(
                    self._invoker(call.plugin, call.tool, bytes(call.args_json)),
                    timeout=self._timeout_s,
                )
            else:
                invocation = await self._invoker(
                    call.plugin, call.tool, bytes(call.args_json)
                )
        except TimeoutError:
            elapsed = int((time.monotonic() - started) * 1000)
            logger.warning(
                "tool_executor.timeout",
                plugin=call.plugin,
                tool=call.tool,
                call_id=call.call_id,
                timeout_s=self._timeout_s,
            )
            return error_result(
                call,
                code="tool_timeout",
                message=f"plugin tool timed out after {self._timeout_s}s",
                duration_ms=elapsed,
            )
        except asyncio.CancelledError:
            # Cancellation is structural — let it propagate so the
            # surrounding stream tears down cleanly.
            raise
        except Exception as exc:  # any invoker failure is non-fatal
            elapsed = int((time.monotonic() - started) * 1000)
            logger.warning(
                "tool_executor.invoker_failed",
                plugin=call.plugin,
                tool=call.tool,
                call_id=call.call_id,
                error=str(exc),
            )
            return error_result(
                call,
                code="tool_invocation_failed",
                message=str(exc),
                duration_ms=elapsed,
            )

        duration_ms = invocation.duration_ms or int(
            (time.monotonic() - started) * 1000
        )
        logger.debug(
            "tool_executor.executed",
            plugin=call.plugin,
            tool=call.tool,
            call_id=call.call_id,
            is_error=invocation.is_error,
            duration_ms=duration_ms,
        )
        return agent_pb2.ToolResult(
            call_id=call.call_id,
            result_json=invocation.content.encode("utf-8"),
            is_error=invocation.is_error,
            duration_ms=duration_ms,
        )
