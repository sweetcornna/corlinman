"""Shell-command hook runner — blocking/discoverable hooks for corlinman-agent.

Mirrors the claude-code hooks feature: operators register shell commands for
specific events (``pre_tool``, ``post_tool``, ``notification``). When the
event fires, the command is executed with the event payload on stdin as JSON.
For blocking events (``pre_tool``), a non-zero exit code stops the tool call.

Configuration shape (from the agent config dict)::

    {
        "hooks": {
            "pre_tool": "path/to/hook.sh",
            "pre_read_file": "path/to/read-file-hook.sh",
            "post_tool": "path/to/after-hook.sh",
            "notification": "path/to/notify.sh"
        }
    }

Lookup order for ``pre_tool`` events:

1. Tool-specific key: ``pre_{tool_name}`` (e.g. ``pre_run_shell``).
2. Wildcard key: ``pre_tool``.

The first matching key wins. Missing keys are a silent no-op (allow-all is
the safe default).

Post-tool hooks (``post_{tool_name}`` / ``post_tool``) and notification hooks
(``notification``) are fire-and-forget — they run but their exit code does not
affect the agent.

Thread-safety: ``HookRunner`` is designed for use from a single asyncio
event loop. The subprocess is run with
:func:`asyncio.create_subprocess_shell` when called from an async context
(see :meth:`run_pre_tool_async`), or with :mod:`subprocess` in the sync
fallback used by tests and the agent_servicer's ``_emit_pre_tool_dispatch``
path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from typing import Any

__all__ = ["HookRunner"]

_log = logging.getLogger("corlinman.hooks.runner")

# Maximum time (seconds) a hook command is allowed to run before we
# forcibly kill it and treat the result as "allow" (so a broken hook
# never permanently bricks tool dispatch).
_HOOK_TIMEOUT: float = 5.0


class HookRunner:
    """Runs shell-command hooks keyed by event name.

    Parameters
    ----------
    config:
        The agent-level config dict (or any sub-dict). ``hooks`` is
        extracted from ``config.get("hooks", {})``.  An empty or
        missing mapping means all events pass through.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        raw = config.get("hooks", {})
        self._hooks: dict[str, str] = {k: str(v) for k, v in raw.items() if v} if isinstance(raw, dict) else {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @property
    def registered(self) -> dict[str, str]:
        """Return a copy of the registered hook commands keyed by event name.

        Useful for the ``GET /admin/hooks`` discovery endpoint.
        """
        return dict(self._hooks)

    def supported_events(self) -> list[str]:
        """Return the canonical event names this runner understands.

        The list is fixed and documents the hook protocol; registered
        commands are a subset.
        """
        return ["pre_tool", "post_tool", "notification"]

    # ------------------------------------------------------------------
    # Synchronous API (used inside ``_emit_pre_tool_dispatch`` + tests)
    # ------------------------------------------------------------------

    def run_pre_tool(self, tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
        """Run the ``pre_{tool_name}`` or ``pre_tool`` hook synchronously.

        Returns ``(should_proceed, message)`` where:

        * ``should_proceed=True`` means the tool call is allowed.
        * ``should_proceed=False`` means it should be blocked; ``message``
          carries the hook's stdout (first 500 chars) as a reason string.

        No matching hook → ``(True, "")`` (allow-all default).

        The hook process receives a JSON payload on stdin::

            {"tool": "<name>", "args": {...}}

        A zero exit code = allow. Non-zero = block.
        Timeout (>5 s) is treated as allow so a stuck hook never bricks
        tool dispatch.
        """
        cmd = self._hooks.get(f"pre_{tool_name}") or self._hooks.get("pre_tool")
        if not cmd:
            return True, ""
        payload = json.dumps({"tool": tool_name, "args": args}, ensure_ascii=False)
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                input=payload,
                capture_output=True,
                text=True,
                timeout=_HOOK_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            _log.warning("hook.pre_tool.timeout", extra={"tool": tool_name, "cmd": cmd})
            return True, ""  # timeout → allow
        except Exception as exc:  # noqa: BLE001
            _log.warning("hook.pre_tool.error", extra={"tool": tool_name, "error": str(exc)})
            return True, ""  # error → allow
        if result.returncode != 0:
            msg = (result.stdout or result.stderr or "").strip()[:500]
            _log.info(
                "hook.pre_tool.blocked",
                extra={"tool": tool_name, "returncode": result.returncode, "message": msg},
            )
            return False, msg
        return True, ""

    def run_post_tool(self, tool_name: str, args: dict[str, Any], result_json: str) -> None:
        """Run the ``post_{tool_name}`` or ``post_tool`` hook (fire-and-forget).

        The hook's exit code is ignored. Errors are logged and suppressed so
        a misbehaving post-hook never affects the agent.

        The hook process receives a JSON payload on stdin::

            {"tool": "<name>", "args": {...}, "result": "<json>"}
        """
        cmd = self._hooks.get(f"post_{tool_name}") or self._hooks.get("post_tool")
        if not cmd:
            return
        payload = json.dumps(
            {"tool": tool_name, "args": args, "result": result_json},
            ensure_ascii=False,
        )
        try:
            subprocess.run(
                cmd,
                shell=True,
                input=payload,
                capture_output=True,
                text=True,
                timeout=_HOOK_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("hook.post_tool.error", extra={"tool": tool_name, "error": str(exc)})

    def run_notification(self, payload: dict[str, Any]) -> None:
        """Run the ``notification`` hook (fire-and-forget).

        The hook receives the ``payload`` dict as JSON on stdin. Exit code
        and output are ignored.
        """
        cmd = self._hooks.get("notification")
        if not cmd:
            return
        try:
            subprocess.run(
                cmd,
                shell=True,
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=_HOOK_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("hook.notification.error", extra={"error": str(exc)})

    # ------------------------------------------------------------------
    # Async API (preferred inside async tool dispatch)
    # ------------------------------------------------------------------

    async def run_pre_tool_async(
        self, tool_name: str, args: dict[str, Any]
    ) -> tuple[bool, str]:
        """Async variant of :meth:`run_pre_tool`.

        Runs the hook subprocess via :func:`asyncio.create_subprocess_shell`
        so the event loop is not blocked. Falls back to the synchronous
        implementation when no running loop is available (shouldn't happen
        in production but keeps tests simple).
        """
        cmd = self._hooks.get(f"pre_{tool_name}") or self._hooks.get("pre_tool")
        if not cmd:
            return True, ""
        payload = json.dumps({"tool": tool_name, "args": args}, ensure_ascii=False)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(payload.encode()),
                    timeout=_HOOK_TIMEOUT,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                _log.warning("hook.pre_tool.timeout", extra={"tool": tool_name, "cmd": cmd})
                return True, ""
        except Exception as exc:  # noqa: BLE001
            _log.warning("hook.pre_tool.error", extra={"tool": tool_name, "error": str(exc)})
            return True, ""
        if proc.returncode != 0:
            msg = (stdout_b or stderr_b or b"").decode("utf-8", "replace").strip()[:500]
            _log.info(
                "hook.pre_tool.blocked",
                extra={"tool": tool_name, "returncode": proc.returncode, "message": msg},
            )
            return False, msg
        return True, ""
