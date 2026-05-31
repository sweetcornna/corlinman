"""Tool schemas + dispatch handlers for memory search (WP17).

``memory_search`` and ``session_search`` are registered in
:data:`BUILTIN_TOOLS` in the agent servicer so the model can call them
directly. Dispatch degrades gracefully:

* no ``memory_host`` wired → returns ``{"results": [], "total": 0,
  "note": "memory_host_not_configured"}`` so the model can keep reasoning
  instead of crashing.
* ``MemoryHostError`` during query → same empty-results envelope with the
  error surfaced in ``"note"``.

The ``session_search`` tool narrows results to the current session via a
``namespace`` filter (the calling convention used by the
:class:`~corlinman_memory_host.LocalSqliteHost` FTS5 back-end).
"""

from __future__ import annotations

import json
import logging
from typing import Any

_logger = logging.getLogger("corlinman_agent.memory")

# ---------------------------------------------------------------------------
# Wire-stable tool name constants
# ---------------------------------------------------------------------------

MEMORY_SEARCH_TOOL: str = "memory_search"
SESSION_SEARCH_TOOL: str = "session_search"


# ---------------------------------------------------------------------------
# OpenAI tool schemas
# ---------------------------------------------------------------------------


def memory_search_tool_schema() -> dict[str, Any]:
    """OpenAI descriptor for ``memory_search``."""
    return {
        "type": "function",
        "function": {
            "name": MEMORY_SEARCH_TOOL,
            "description": (
                "Search the agent's long-term memory store for documents "
                "relevant to the query. Returns the top-k matching passages "
                "with relevance scores. Use this to recall facts, prior "
                "conversations, or knowledge the agent has accumulated."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": (
                            "Maximum number of results to return "
                            "(default 5, max 20)."
                        ),
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "namespace": {
                        "type": "string",
                        "description": (
                            "Optional namespace to restrict the search "
                            "(e.g. a persona id or topic tag)."
                        ),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def session_search_tool_schema() -> dict[str, Any]:
    """OpenAI descriptor for ``session_search``."""
    return {
        "type": "function",
        "function": {
            "name": SESSION_SEARCH_TOOL,
            "description": (
                "Search within the current session's conversation history "
                "for turns or tool results matching the query. Useful for "
                "recalling what was discussed or computed earlier in this "
                "chat without re-running expensive tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": (
                            "Maximum number of results to return "
                            "(default 5, max 20)."
                        ),
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def memory_tool_schemas() -> list[dict[str, Any]]:
    """Return both memory tool schemas as a list."""
    return [memory_search_tool_schema(), session_search_tool_schema()]


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------

_NOT_CONFIGURED = json.dumps(
    {
        "results": [],
        "total": 0,
        "note": "memory_host_not_configured",
    }
)


async def dispatch_memory_search(
    args_json: bytes,
    *,
    memory_host: Any = None,
    session_key: str | None = None,
) -> str:
    """Dispatch ``memory_search`` — wraps ``MemoryHost.query``.

    Parameters
    ----------
    args_json:
        Raw JSON bytes from the model's tool-call arguments.
    memory_host:
        A live ``MemoryHost`` adapter.  ``None`` → empty results with a
        ``memory_host_not_configured`` note so the model can handle
        unavailability gracefully.
    session_key:
        Ignored for ``memory_search`` (session-scoping is
        ``session_search``'s job); accepted here for call-site
        symmetry.
    """
    if memory_host is None:
        return _NOT_CONFIGURED

    try:
        args: dict[str, Any] = json.loads(args_json or b"{}")
    except (json.JSONDecodeError, ValueError):
        return json.dumps({"error": "invalid_args_json"})

    query_text = args.get("query", "")
    if not isinstance(query_text, str) or not query_text.strip():
        return json.dumps({"error": "query_required"})

    top_k = min(max(int(args.get("top_k") or 5), 1), 20)
    namespace = args.get("namespace") or None

    try:
        from corlinman_memory_host.types import MemoryQuery

        req = MemoryQuery(text=query_text.strip(), top_k=top_k, namespace=namespace)
        hits = await memory_host.query(req)
        results = [
            {
                "id": h.id,
                "content": h.content,
                "score": round(h.score, 4),
                "source": h.source,
            }
            for h in hits
        ]
        return json.dumps({"results": results, "total": len(results)})
    except Exception as exc:  # noqa: BLE001 — never raise from dispatcher
        _logger.warning("memory_search.query_failed", error=str(exc))
        return json.dumps(
            {"results": [], "total": 0, "note": f"query_failed: {exc}"}
        )


async def dispatch_session_search(
    args_json: bytes,
    *,
    memory_host: Any = None,
    session_key: str | None = None,
) -> str:
    """Dispatch ``session_search`` — namespace-filtered ``MemoryHost.query``.

    Uses the ``session_key`` as the namespace filter so only documents
    stored under the current session are returned.  Degrades to an empty
    result set when no host is wired or the session key is absent.
    """
    if memory_host is None:
        return _NOT_CONFIGURED

    try:
        args: dict[str, Any] = json.loads(args_json or b"{}")
    except (json.JSONDecodeError, ValueError):
        return json.dumps({"error": "invalid_args_json"})

    query_text = args.get("query", "")
    if not isinstance(query_text, str) or not query_text.strip():
        return json.dumps({"error": "query_required"})

    top_k = min(max(int(args.get("top_k") or 5), 1), 20)
    namespace = session_key or None

    try:
        from corlinman_memory_host.types import MemoryQuery

        req = MemoryQuery(
            text=query_text.strip(),
            top_k=top_k,
            namespace=namespace,
        )
        hits = await memory_host.query(req)
        results = [
            {
                "id": h.id,
                "content": h.content,
                "score": round(h.score, 4),
                "source": h.source,
            }
            for h in hits
        ]
        return json.dumps({"results": results, "total": len(results)})
    except Exception as exc:  # noqa: BLE001 — never raise from dispatcher
        _logger.warning("session_search.query_failed", error=str(exc))
        return json.dumps(
            {"results": [], "total": 0, "note": f"query_failed: {exc}"}
        )


__all__ = [
    "MEMORY_SEARCH_TOOL",
    "SESSION_SEARCH_TOOL",
    "dispatch_memory_search",
    "dispatch_session_search",
    "memory_search_tool_schema",
    "memory_tool_schemas",
    "session_search_tool_schema",
]
