"""Interactive tool-approval for the console REPL (ABSORB_MATRIX Dim 3).

The permission gate's ``ask`` verdict escalates to an async resolver
``(tool, args, ctx) -> bool`` (``agent_servicer.set_approval_resolver``).
Nothing ever wired one, so every ``ask`` fail-closed to deny in every
deployment. The console is the first wiring: pause the live renderer, show the
tool + an args preview, read **y**es / **a**lways / **N**o — "always" caches the
tool name for the rest of the session so it is not asked again.

The resolver runs on the SHARED event loop while the REPL task is parked
awaiting stream events (servicer and REPL are one process), so it must own the
prompt itself — a fresh prompt_toolkit session — rather than delegate to the
render loop. Deny is the answer to everything unexpected: empty input, EOF, a
broken prompt surface (mirrors the gate's own fail-closed posture).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

#: Presents an approval request (a one-line description of the tool call) and
#: returns the user's raw answer. Injectable so tests never need a TTY.
Prompter = Callable[[str], Awaitable[str]]

_PREVIEW_CAP = 160


def _args_preview(args: Any) -> str:
    """Compact one-line JSON preview of the tool args (truncated)."""
    try:
        text = json.dumps(args or {}, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 — preview must never break approval
        text = str(args)
    return text if len(text) <= _PREVIEW_CAP else text[: _PREVIEW_CAP - 1] + "…"


class ConsoleApprovalResolver:
    """Session-scoped interactive approval resolver.

    Answers: ``y``/``yes`` → allow once; ``a``/``always`` → allow and stop
    asking for this tool for the rest of the session; anything else → deny.
    """

    def __init__(self, prompter: Prompter) -> None:
        self._prompter = prompter
        #: Tool names the user answered "always" for — session-scoped by
        #: design (a durable grant belongs in the permission rule list, not an
        #: interactive cache). Cleared by :meth:`reset` on session/mode
        #: boundaries so a grant can never outlive the context it was
        #: given in (Codex #104).
        self.always_allow: set[str] = set()
        #: One prompt at a time: concurrent tool calls (e.g. a subagent
        #: fan-out) each awaiting approval would otherwise spawn competing
        #: prompt_toolkit sessions on the same terminal (Codex #104).
        self._prompt_lock = asyncio.Lock()

    def reset(self) -> None:
        """Drop every cached "always" grant.

        Called when the session boundary moves (``/new``, ``/clear``) or
        the permission mode switches (``/permissions``, ``/plan``) — a
        grant given under one session/mode must not silently carry into
        the next (most sharply: a cached ``run_shell`` kept mutating the
        workspace after entering plan mode).
        """
        self.always_allow.clear()

    async def __call__(self, tool: str, args: Any, ctx: Any) -> bool:
        _ = ctx
        if tool in self.always_allow:
            return True
        async with self._prompt_lock:
            # Re-check inside the lock: a concurrent call for the same tool
            # may have just answered "always" while we waited.
            if tool in self.always_allow:
                return True
            desc = f"{tool} {_args_preview(args)}"
            try:
                answer = (await self._prompter(desc)).strip().lower()
            except Exception:  # noqa: BLE001 — prompt failure → deny (fail-closed)
                return False
            if answer in ("a", "always"):
                self.always_allow.add(tool)
                return True
            return answer in ("y", "yes")


def build_console_prompter(
    renderer: Any,
    *,
    reader: Callable[[str], Awaitable[str]] | None = None,
) -> Prompter:
    """Prompter for the live REPL: pause the spinner, print the request, read
    an answer on the shared loop.

    ``reader`` (given the prompt suffix, returns the raw line) is injectable
    for tests; the default reads via a fresh prompt_toolkit session, which is
    safe mid-turn because the main REPL prompt is not active inside a turn.
    """

    async def _default_reader(suffix: str) -> str:
        from prompt_toolkit import PromptSession  # noqa: PLC0415 — REPL only
        from prompt_toolkit.patch_stdout import patch_stdout  # noqa: PLC0415

        session: PromptSession[str] = PromptSession()
        with patch_stdout():
            return await session.prompt_async(suffix)

    read = reader if reader is not None else _default_reader

    async def _prompt(desc: str) -> str:
        # Stop any live spinner/markdown widget so the prompt owns the
        # terminal (the Renderer restarts its live surface on the next event).
        stop = getattr(renderer, "_stop_live", None)
        if callable(stop):
            stop()
        renderer.console.print(f"⚠ approval needed — {desc}", style="bold yellow", highlight=False)
        return await read("allow? [y]es / [a]lways this session / [N]o › ")

    return _prompt


__all__ = ["ConsoleApprovalResolver", "Prompter", "build_console_prompter"]
