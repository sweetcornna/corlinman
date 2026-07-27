"""Interactive tool-approval for the console REPL (ABSORB_MATRIX Dim 3).

The permission gate's ``ask`` verdict escalates to an async resolver
``(tool, args, ctx) -> bool`` (``agent_servicer.set_approval_resolver``).
The console wiring pauses the live renderer, shows the tool + an args
preview, and reads an answer keyed to the unified memory vocabulary
(W3-1 / decision C2 — ``once`` / ``session`` / ``always``):

* ``y``/``yes`` — allow this one call (``once``); asked again next time.
* ``a`` (``s``/``session``) — allow and record a **session** grant in the
  shared :class:`~corlinman_agent.authz.grants.GrantStore`. The grant key
  includes the argument digest, so approving ``run_shell ls`` does NOT
  silence the prompt for ``run_shell rm -rf /`` (the old
  ``always_allow: set[str]`` cache did exactly that — agent-gate §8.7).
* ``p`` (``always``) — allow and record a durable **always** grant
  (GrantStore SQLite, survives the process). This REPLACES the old
  ``persist`` answer that appended a global unconditional allow rule to
  ``settings.json`` — the rules file is for operator-written policy, not
  flattened one-time approvals.
* anything else (empty, EOF, ``N``, garbage) — deny (fail-closed).

The resolver runs on the SHARED event loop while the REPL task is parked
awaiting stream events (servicer and REPL are one process), so it must own
the prompt itself — a fresh prompt_toolkit session — rather than delegate
to the render loop. Deny is the answer to everything unexpected: empty
input, EOF, a broken prompt surface (mirrors the gate's own fail-closed
posture).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from corlinman_agent.authz.grants import GrantStore, get_grant_store
from corlinman_agent.authz.model import Memory

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
    """Interactive approval resolver backed by the shared GrantStore.

    Answers map to the unified memory vocabulary: ``y``/``yes`` → once,
    ``a``/``s``/``session`` → session grant, ``p``/``always`` → durable
    always grant, anything else → deny. Console ``always`` grants
    deliberately do NOT scope to surface/user — the console is the
    operator's own trusted terminal (design plan §3.5); channel prompts
    will default the other way (W3-3).
    """

    def __init__(
        self,
        prompter: Prompter,
        *,
        grant_store: GrantStore | None = None,
    ) -> None:
        self._prompter = prompter
        #: The SAME store instance the servicer's AuthzGate consults, or the
        #: record/check key spaces would silently diverge.
        self._grants = grant_store if grant_store is not None else get_grant_store()
        #: One prompt at a time: concurrent tool calls (e.g. a subagent
        #: fan-out) each awaiting approval would otherwise spawn competing
        #: prompt_toolkit sessions on the same terminal (Codex #104).
        self._prompt_lock = asyncio.Lock()

    @property
    def grant_store(self) -> GrantStore:
        return self._grants

    def session_grant_tools(self) -> set[str]:
        """Tool names with a live session grant (the /permissions listing)."""
        return self._grants.session_grant_tools()

    def reset(self) -> None:
        """Drop every session grant.

        Called when the session boundary moves (``/new``, ``/clear``) or
        the permission mode switches (``/permissions``, ``/plan``) — a
        grant given under one session/mode must not silently carry into
        the next (most sharply: a cached ``run_shell`` kept mutating the
        workspace after entering plan mode, Codex #104).
        """
        self._grants.clear_session_grants()

    async def __call__(self, tool: str, args: Any, ctx: Any) -> bool:
        args_dict = args if isinstance(args, dict) else {}
        if self._grants.is_granted(ctx, tool, args_dict):
            return True
        async with self._prompt_lock:
            # Re-check inside the lock: a concurrent identical call may have
            # just been granted while we waited.
            if self._grants.is_granted(ctx, tool, args_dict):
                return True
            desc = f"{tool} {_args_preview(args)}"
            try:
                answer = (await self._prompter(desc)).strip().lower()
            except Exception:  # noqa: BLE001 — prompt failure → deny (fail-closed)
                return False
            if answer in ("a", "s", "session"):
                self._grants.record(ctx, tool, args_dict, Memory.SESSION)
                return True
            if answer in ("p", "always"):
                # Durable grant — GrantStore SQLite. A persistence failure
                # inside the store degrades to a memory-held grant rather
                # than a deny (the operator did answer "allow").
                self._grants.record(ctx, tool, args_dict, Memory.ALWAYS)
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
        return await read(
            "allow? [y]es once / [a] this session / [p] always (durable) / [N]o › "
        )

    return _prompt


__all__ = [
    "ConsoleApprovalResolver",
    "Prompter",
    "build_console_prompter",
]
