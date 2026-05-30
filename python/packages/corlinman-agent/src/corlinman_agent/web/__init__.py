"""Builtin web tools — the network half of the agent's tool surface.

corlinman's builtin tool set started life as pure orchestration
(``subagent_spawn{,_many}`` + ``blackboard.{read,write}``). Real agents
also need to *look things up*: fetch a page, run a search. This package
ships those two tools (plus a cheap, self-contained ``calculator``)
following the exact same wire contract the subagent / blackboard tools
established:

* a wire-stable tool-name constant lives in one place and is imported by
  both the agent card layer and the gateway dispatcher;
* a ``dispatch_<tool>(args_json=..., ...) -> str`` async (or sync)
  callable takes one tool call's raw ``args_json`` bytes and returns the
  JSON string the reasoning loop feeds straight into
  ``ToolResult.content``;
* the dispatcher **never raises** — every failure path (bad args,
  timeout, non-200, oversized body, unavailable backend) folds into an
  ``{"error": "..."}`` envelope so the model's next round still reads
  something coherent.

Modules
-------
* :mod:`.fetch` — :func:`dispatch_web_fetch`, HTML → readable text.
* :mod:`.search` — :func:`dispatch_web_search`, keyless DuckDuckGo
  backend with an env-overridable key-based provider hook.
* :mod:`.calculator` — :func:`dispatch_calculator`, a safe arithmetic
  evaluator (no network, no API key — the "one more cheap tool").
"""

from __future__ import annotations

from corlinman_agent.web.calculator import (
    CALCULATOR_TOOL,
    calculator_tool_schema,
    dispatch_calculator,
)
from corlinman_agent.web.fetch import (
    WEB_FETCH_TOOL,
    dispatch_web_fetch,
    web_fetch_tool_schema,
)
from corlinman_agent.web.search import (
    WEB_SEARCH_TOOL,
    dispatch_web_search,
    web_search_tool_schema,
)

__all__ = [
    "CALCULATOR_TOOL",
    "WEB_FETCH_TOOL",
    "WEB_SEARCH_TOOL",
    "calculator_tool_schema",
    "dispatch_calculator",
    "dispatch_web_fetch",
    "dispatch_web_search",
    "web_fetch_tool_schema",
    "web_search_tool_schema",
]
