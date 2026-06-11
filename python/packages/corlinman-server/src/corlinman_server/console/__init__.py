"""``corlinman console`` — interactive CLI agent console.

A terminal REPL that hosts (or attaches to) the corlinman agent brain and
streams its reasoning, tool calls, and answers — the CLI counterpart of the
web ``/chat`` playground and the channel adapters.

Design notes live in ``docs/PLAN_CLI_CONSOLE.md``. The console is a *client*
of the same internal chat contract every other surface uses
(:class:`corlinman_server.gateway_api.InternalChatRequest` →
``AsyncIterator[InternalChatEvent]``); it adds no second agent loop.

Two brain backends:

* :class:`~corlinman_server.console.embedded.EmbeddedBrain` (default) —
  boots the full :class:`~corlinman_server.agent_servicer.CorlinmanAgentServicer`
  on a private per-process UDS inside the console process (hermes-agent
  style: the CLI *is* the agent).
* :class:`~corlinman_server.console.attach.AttachBrain` (``--attach URL``) —
  SSE client to a running gateway's ``/v1/chat/completions`` (opencode
  client/server style).
"""

from __future__ import annotations

from corlinman_server.console.app import run_console

__all__ = ["run_console"]
