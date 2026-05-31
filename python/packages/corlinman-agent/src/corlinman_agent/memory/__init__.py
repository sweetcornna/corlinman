"""Builtin memory tools — agent-callable search over the memory store.

WP17: Wire the corlinman-memory-host FTS5 store as agent-callable tools
so skills that reference ``memory.search`` get a real dispatch path
instead of hitting the ``unsupported_action`` branch.

Two tools are exposed:

* :func:`dispatch_memory_search` — full-text / semantic search over the
  agent's long-term memory host (wraps ``MemoryHost.query``).
* :func:`dispatch_session_search` — search within the current session's
  in-progress conversation history (currently backed by the same host
  with a session-namespace filter; degrades to empty results when no
  host is wired).

Both dispatchers follow the standard contract:

* accept ``args_json: bytes`` (the raw JSON from the model's tool call);
* accept an optional ``memory_host`` keyword argument carrying a live
  :class:`corlinman_memory_host.MemoryHost` adapter (``None`` in bare
  deployments → graceful empty results);
* return a JSON string ready to feed as ``ToolResult.content``;
* never raise — every failure path folds into ``{"error": "..."}``.
"""

from __future__ import annotations

from corlinman_agent.memory.tools import (
    MEMORY_SEARCH_TOOL,
    SESSION_SEARCH_TOOL,
    dispatch_memory_search,
    dispatch_session_search,
    memory_search_tool_schema,
    memory_tool_schemas,
    session_search_tool_schema,
)

MEMORY_TOOLS: frozenset[str] = frozenset({MEMORY_SEARCH_TOOL, SESSION_SEARCH_TOOL})

__all__ = [
    "MEMORY_SEARCH_TOOL",
    "MEMORY_TOOLS",
    "SESSION_SEARCH_TOOL",
    "dispatch_memory_search",
    "dispatch_session_search",
    "memory_search_tool_schema",
    "memory_tool_schemas",
    "session_search_tool_schema",
]
