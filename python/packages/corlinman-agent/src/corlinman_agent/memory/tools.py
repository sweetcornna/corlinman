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
MEMORY_WRITE_TOOL: str = "memory_write"
MEMORY_READ_TOOL: str = "memory_read"

#: Default namespace for agent-written notes when the caller does not
#: pass one. Kept distinct from the session-keyed conversational store
#: (which uses ``session_key`` as its namespace) so durable profile
#: facts/notes survive across sessions and don't collide with the
#: auto-recall turns the servicer upserts.
_NOTES_NAMESPACE: str = "agent_notes"

#: Cap on a single stored note — durable memory should be a fact, not a
#: dumping ground; keeps the FTS5 chunk bounded.
_MAX_NOTE_CHARS: int = 4_000


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


def memory_write_tool_schema() -> dict[str, Any]:
    """OpenAI descriptor for ``memory_write``."""
    return {
        "type": "function",
        "function": {
            "name": MEMORY_WRITE_TOOL,
            "description": (
                "Store a durable note or profile fact in the agent's "
                "long-term memory so it can be recalled in future "
                "conversations. Use this for stable facts the user shares "
                "(preferences, names, ongoing projects) — NOT for "
                "transient chatter. Returns the stored note's id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "The note or fact to remember. Phrase it as a "
                            "standalone statement (e.g. 'User prefers "
                            "metric units')."
                        ),
                    },
                    "tag": {
                        "type": "string",
                        "description": (
                            "Optional category tag for the note "
                            "(e.g. 'profile', 'project', 'preference'). "
                            "Stored as metadata for later filtering."
                        ),
                    },
                    "namespace": {
                        "type": "string",
                        "description": (
                            "Optional namespace to scope the note. Defaults "
                            "to the shared agent-notes namespace; pass a "
                            "persona id or topic to isolate it."
                        ),
                    },
                },
                "required": ["content"],
                "additionalProperties": False,
            },
        },
    }


def memory_read_tool_schema() -> dict[str, Any]:
    """OpenAI descriptor for ``memory_read`` (notes recall)."""
    return {
        "type": "function",
        "function": {
            "name": MEMORY_READ_TOOL,
            "description": (
                "Read back durable notes / profile facts the agent stored "
                "earlier with `memory_write`. Provide a query to surface "
                "the most relevant notes, or omit it to list the most "
                "recent ones. Use this to recall what you know about the "
                "user before answering."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional search query. When omitted the most "
                            "recent notes in the namespace are returned."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": (
                            "Maximum number of notes to return "
                            "(default 5, max 20)."
                        ),
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "namespace": {
                        "type": "string",
                        "description": (
                            "Optional namespace to read from. Defaults to "
                            "the shared agent-notes namespace."
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


def memory_tool_schemas() -> list[dict[str, Any]]:
    """Return every memory tool schema as a list."""
    return [
        memory_search_tool_schema(),
        session_search_tool_schema(),
        memory_write_tool_schema(),
        memory_read_tool_schema(),
    ]


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


async def dispatch_memory_write(
    args_json: bytes,
    *,
    memory_host: Any = None,
    session_key: str | None = None,
) -> str:
    """Dispatch ``memory_write`` — store a durable note via ``MemoryHost.upsert``.

    Wraps the host's ``upsert`` with a :class:`MemoryDoc`. The note's
    ``tag`` (if any) is folded into the doc metadata so a later
    ``memory_read`` / ``memory_search`` can surface it. Degrades to a
    ``not_configured`` envelope when no host is wired so the model can
    keep reasoning.

    Parameters
    ----------
    args_json:
        Raw JSON bytes from the model's tool-call arguments.
    memory_host:
        A live ``MemoryHost`` adapter. ``None`` → ``not_configured``.
    session_key:
        Accepted for call-site symmetry; notes are durable and namespaced
        by ``namespace`` (default :data:`_NOTES_NAMESPACE`), NOT by the
        ephemeral session key.
    """
    if memory_host is None:
        return json.dumps({"ok": False, "note": "memory_host_not_configured"})

    try:
        args: dict[str, Any] = json.loads(args_json or b"{}")
    except (json.JSONDecodeError, ValueError):
        return json.dumps({"ok": False, "error": "invalid_args_json"})

    content = args.get("content", "")
    if not isinstance(content, str) or not content.strip():
        return json.dumps({"ok": False, "error": "content_required"})
    content = content.strip()[:_MAX_NOTE_CHARS]

    namespace = args.get("namespace") or _NOTES_NAMESPACE
    tag = args.get("tag")
    metadata: dict[str, Any] = {"kind": "note"}
    if isinstance(tag, str) and tag.strip():
        metadata["tag"] = tag.strip()

    try:
        from corlinman_memory_host.types import MemoryDoc

        doc = MemoryDoc(content=content, metadata=metadata, namespace=namespace)
        note_id = await memory_host.upsert(doc)
        return json.dumps(
            {"ok": True, "id": str(note_id), "namespace": namespace}
        )
    except Exception as exc:  # noqa: BLE001 — never raise from dispatcher
        _logger.warning("memory_write.upsert_failed", error=str(exc))
        return json.dumps({"ok": False, "error": f"write_failed: {exc}"})


async def dispatch_memory_read(
    args_json: bytes,
    *,
    memory_host: Any = None,
    session_key: str | None = None,
) -> str:
    """Dispatch ``memory_read`` — recall durable notes.

    With a ``query`` it runs a BM25 search scoped to the notes namespace;
    without one it returns the most recent notes (via the host's
    ``recent`` helper when available, else an empty list). Degrades to
    empty results when no host is wired.
    """
    if memory_host is None:
        return _NOT_CONFIGURED

    try:
        args: dict[str, Any] = json.loads(args_json or b"{}")
    except (json.JSONDecodeError, ValueError):
        return json.dumps({"error": "invalid_args_json"})

    top_k = min(max(int(args.get("top_k") or 5), 1), 20)
    namespace = args.get("namespace") or _NOTES_NAMESPACE
    query_text = args.get("query")

    try:
        if isinstance(query_text, str) and query_text.strip():
            from corlinman_memory_host.types import MemoryQuery

            req = MemoryQuery(
                text=query_text.strip(), top_k=top_k, namespace=namespace
            )
            hits = await memory_host.query(req)
        else:
            # No query → most recent notes. ``recent`` is not part of the
            # MemoryHost ABC, so probe for it (LocalSqliteHost has it).
            recent_fn = getattr(memory_host, "recent", None)
            hits = await recent_fn(namespace, top_k) if recent_fn else []
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
        _logger.warning("memory_read.query_failed", error=str(exc))
        return json.dumps(
            {"results": [], "total": 0, "note": f"query_failed: {exc}"}
        )


__all__ = [
    "MEMORY_READ_TOOL",
    "MEMORY_SEARCH_TOOL",
    "MEMORY_WRITE_TOOL",
    "SESSION_SEARCH_TOOL",
    "dispatch_memory_read",
    "dispatch_memory_search",
    "dispatch_memory_write",
    "dispatch_session_search",
    "memory_read_tool_schema",
    "memory_search_tool_schema",
    "memory_tool_schemas",
    "memory_write_tool_schema",
    "session_search_tool_schema",
]
