"""Provider protocol invocation + tool-call stream assembly.

Extracted verbatim from
:mod:`corlinman_server.gateway.evolution.background_review` as part of a
behaviour-preserving god-file split. This module owns the advertised
tool schema, the stream-chunk reassembler, and the provider adapter that
the orchestrator calls. It MUST NOT import the source module
(``background_review``) to avoid an import cycle — the source module
re-imports the public names from here.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# The schemas we advertise to the model. The model may invent tool calls
# outside this set; the dispatcher drops them. We list them here purely
# so providers that accept a tool schema can be told what we expect.
_TOOL_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "skill_manage",
            "description": (
                "Create, edit, patch, or delete a SKILL.md in the active "
                "profile's skills/ directory."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "name"],
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "edit", "patch", "delete"],
                    },
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_write",
            "description": (
                "Append or replace memory content scoped to the active profile."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target", "action", "content"],
                "properties": {
                    "target": {"type": "string", "enum": ["MEMORY", "USER"]},
                    "action": {"type": "string", "enum": ["append", "replace"]},
                    "content": {"type": "string"},
                },
            },
        },
    },
]


async def _collect_tool_calls_from_stream(
    stream: Any,
) -> tuple[list[dict[str, Any]], str | None]:
    """Drain an ``AsyncIterator[ProviderChunk]`` into ``(tool_calls, error)``.

    Re-assembles ``tool_call_start`` / ``tool_call_delta`` /
    ``tool_call_end`` chunks into discrete ``{"tool", "id",
    "arguments_json"}`` records, then parses the JSON.

    Returns ``([], "no_chunks")`` if the iterator yielded nothing — useful
    signal for tests that pass a degenerate provider. Provider errors
    (``finish_reason == "error"``) surface as a non-None error string.
    """
    in_progress: dict[str, dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    saw_anything = False
    error: str | None = None

    async for chunk in stream:
        saw_anything = True
        kind = getattr(chunk, "kind", None)
        if kind == "tool_call_start":
            tcid = chunk.tool_call_id or f"call_{len(in_progress)}"
            in_progress[tcid] = {
                "id": tcid,
                "name": chunk.tool_name or "",
                "arguments": chunk.arguments_delta or "",
            }
        elif kind == "tool_call_delta":
            tcid = chunk.tool_call_id or ""
            entry = in_progress.get(tcid)
            if entry is not None and chunk.arguments_delta:
                entry["arguments"] += chunk.arguments_delta
        elif kind == "tool_call_end":
            tcid = chunk.tool_call_id or ""
            entry = in_progress.pop(tcid, None)
            if entry is not None:
                completed.append(entry)
        elif kind == "done":
            if chunk.finish_reason == "error":
                error = "provider_finish_reason_error"
            # Flush anything still in_progress at done.
            for entry in in_progress.values():
                completed.append(entry)
            in_progress.clear()
        # ``token`` chunks are ignored — the model is supposed to emit
        # tool_calls only; any prose it produces is discarded.

    if not saw_anything:
        return [], "no_chunks"

    # Convert the OpenAI-style intermediate records into the dispatcher's
    # flat shape.
    flat: list[dict[str, Any]] = []
    for entry in completed:
        args_raw = entry.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except (TypeError, json.JSONDecodeError):
            # Surface the malformed tool call so the dispatcher records it.
            flat.append({"tool": entry.get("name") or "unknown", "_malformed": True})
            continue
        if not isinstance(args, dict):
            flat.append({"tool": entry.get("name") or "unknown", "_malformed": True})
            continue
        args = dict(args)
        args["tool"] = entry.get("name") or args.get("tool")
        flat.append(args)

    return flat, error


async def _invoke_provider(
    *,
    provider: Any,
    model: str,
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Adapter over the provider Protocol.

    Two call paths supported, in priority order:

    1. ``provider.chat(...)`` — a hypothetical non-streaming method that
       returns ``{"tool_calls": [...]}`` directly. Real adapters don't
       expose this today; tests use it because it's the cleanest way
       to feed scripted tool_calls into the dispatcher.
    2. ``provider.chat_stream(...)`` — the canonical Protocol method.
       We drain the iterator and assemble tool_calls from chunks.
    """
    chat = getattr(provider, "chat", None)
    if callable(chat):
        result = chat(model=model, messages=messages, tools=_TOOL_SCHEMA)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, dict):
            return list(result.get("tool_calls") or []), result.get("error")
        # Unknown shape — treat as no calls so the report stays sane.
        return [], None

    stream_fn = getattr(provider, "chat_stream", None)
    if not callable(stream_fn):
        return [], "provider_missing_chat_methods"

    stream = stream_fn(model=model, messages=messages, tools=_TOOL_SCHEMA)
    return await _collect_tool_calls_from_stream(stream)
