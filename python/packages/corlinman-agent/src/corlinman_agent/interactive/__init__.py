"""Interactive builtin tools — let the agent talk back to the user.

Sits next to :mod:`corlinman_agent.coding` / :mod:`corlinman_agent.web` as
a third builtin-tool family. Its first member is ``ask_user``: a
"pause-and-ask" tool that lets the model surface a clarification
question instead of guessing when the request is ambiguous.

The dispatch side is intentionally a stub — the real "ask the user"
action is on the channel side (Telegram inline keyboard, QQ-family
numbered list). The agent merely needs a JSON envelope to feed back to
the reasoning loop so the round finalises cleanly.
"""

from __future__ import annotations

from corlinman_agent.interactive.ask_user import (
    ASK_USER_TOOL,
    ask_user_tool_schema,
    dispatch_ask_user,
    parse_ask_user_args,
)

__all__ = [
    "ASK_USER_TOOL",
    "ask_user_tool_schema",
    "dispatch_ask_user",
    "parse_ask_user_args",
]
