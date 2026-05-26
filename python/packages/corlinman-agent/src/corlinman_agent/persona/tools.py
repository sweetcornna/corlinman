"""Tool name constants + OpenAI-shaped schemas for the ``persona.*`` family.

Mirrors the wire contract documented in ``docs/PLAN_PERSONA_STUDIO.md``
W3: seven builtin tools that let the agent read + mutate the persona
registry mid-conversation (typical use: the ``/persona`` wizard skill
walks the user through ``persona_create`` → ``persona_attach_asset_from_url``).

Each schema is a plain ``{"type": "function", "function": {...}}`` dict
ready to drop into ``ChatStart.tools`` via
``_inject_builtin_tools`` in the agent servicer.

These are *schema* and *name* declarations only — actual dispatch lives
in :mod:`corlinman_agent.persona.dispatch` so test code can import the
shape without dragging in the (lazy) corlinman-server store deps.
"""

from __future__ import annotations

from typing import Any

#: Wire-stable tool names. Imported by the agent servicer's
#: ``BUILTIN_TOOLS`` frozenset and any agent card that exposes the tools.
PERSONA_LIST_TOOL: str = "persona_list"
PERSONA_GET_TOOL: str = "persona_get"
PERSONA_CREATE_TOOL: str = "persona_create"
PERSONA_UPDATE_TOOL: str = "persona_update"
PERSONA_DELETE_TOOL: str = "persona_delete"
PERSONA_LIST_ASSETS_TOOL: str = "persona_list_assets"
PERSONA_ATTACH_ASSET_FROM_URL_TOOL: str = "persona_attach_asset_from_url"

#: Convenience set so the servicer can do ``BUILTIN_TOOLS | PERSONA_TOOLS``.
PERSONA_TOOLS: frozenset[str] = frozenset(
    {
        PERSONA_LIST_TOOL,
        PERSONA_GET_TOOL,
        PERSONA_CREATE_TOOL,
        PERSONA_UPDATE_TOOL,
        PERSONA_DELETE_TOOL,
        PERSONA_LIST_ASSETS_TOOL,
        PERSONA_ATTACH_ASSET_FROM_URL_TOOL,
    }
)


# Slug pattern — matches the admin route validator at
# ``routes_admin_a/personas.py::CreateBody``. Kept as a description-only
# hint here so a confused model can self-correct without having to call
# the create endpoint twice.
_SLUG_HINT: str = (
    "lowercase, 1-64 chars, only [a-z0-9_-] (e.g. ``grantley`` or "
    "``cyber_oracle``)"
)


def persona_list_tool_schema() -> dict[str, Any]:
    """``persona_list`` — return the full registry (no asset bytes)."""
    return {
        "type": "function",
        "function": {
            "name": PERSONA_LIST_TOOL,
            "description": (
                "List every persona registered in this corlinman "
                "deployment. Returns a short summary view per persona "
                "(id, display_name, short_summary, is_builtin) — call "
                "persona_get for the full system_prompt body."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }


def persona_get_tool_schema() -> dict[str, Any]:
    """``persona_get`` — fetch one persona by id."""
    return {
        "type": "function",
        "function": {
            "name": PERSONA_GET_TOOL,
            "description": (
                "Fetch one persona by id. Returns the full row including "
                "the system_prompt body. Long bodies (>2000 chars) are "
                "clipped with a ``…truncated`` marker; use the admin UI "
                "to see the full text when you need it verbatim."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": f"Persona id ({_SLUG_HINT}).",
                    },
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    }


def persona_create_tool_schema() -> dict[str, Any]:
    """``persona_create`` — insert a new persona row."""
    return {
        "type": "function",
        "function": {
            "name": PERSONA_CREATE_TOOL,
            "description": (
                "Create a new persona. Confirm the system_prompt body "
                "with the user BEFORE calling — operators cannot rewind "
                "a malformed seed without going through the admin UI. "
                "Returns the created Persona row on success; returns an "
                "``error: persona_exists`` envelope if the id is taken."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": f"Persona slug ({_SLUG_HINT}).",
                    },
                    "display_name": {
                        "type": "string",
                        "description": (
                            "Human-readable name shown in the admin UI "
                            "and chat-channel pickers. 1-200 chars."
                        ),
                    },
                    "short_summary": {
                        "type": "string",
                        "description": (
                            "One-sentence description of the persona's "
                            "vibe / role. Optional, max 500 chars."
                        ),
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": (
                            "Full system_prompt body the channel "
                            "prepends to chat requests when this "
                            "persona is bound. 1-200_000 chars."
                        ),
                    },
                },
                "required": ["id", "display_name", "system_prompt"],
                "additionalProperties": False,
            },
        },
    }


def persona_update_tool_schema() -> dict[str, Any]:
    """``persona_update`` — patch fields on an existing persona."""
    return {
        "type": "function",
        "function": {
            "name": PERSONA_UPDATE_TOOL,
            "description": (
                "Patch one or more fields on an existing persona. "
                "Omitted fields are preserved verbatim. The builtin "
                "flag is read-only — bodies of builtin personas can be "
                "rewritten via this tool but cannot be cleared."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": f"Persona id ({_SLUG_HINT}).",
                    },
                    "display_name": {
                        "type": "string",
                        "description": "Optional new display_name.",
                    },
                    "short_summary": {
                        "type": "string",
                        "description": "Optional new short summary.",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Optional new system_prompt body.",
                    },
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    }


def persona_delete_tool_schema() -> dict[str, Any]:
    """``persona_delete`` — remove one custom persona row."""
    return {
        "type": "function",
        "function": {
            "name": PERSONA_DELETE_TOOL,
            "description": (
                "Delete one persona by id. Refuses to remove builtin "
                "personas (returns ``error: persona_protected``). "
                "Custom personas are removed alongside their asset "
                "pack — the call is irreversible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": f"Persona id ({_SLUG_HINT}).",
                    },
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    }


def persona_list_assets_tool_schema() -> dict[str, Any]:
    """``persona_list_assets`` — list emoji + reference assets."""
    return {
        "type": "function",
        "function": {
            "name": PERSONA_LIST_ASSETS_TOOL,
            "description": (
                "List the emoji + reference assets attached to a "
                "persona. Returns metadata only — asset bytes never "
                "round-trip through the model. Use the listed labels "
                "with image_with_refs (for ``reference`` assets) or "
                "send_attachment (for ``emoji`` assets)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": f"Persona id ({_SLUG_HINT}).",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["emoji", "reference"],
                        "description": (
                            "Optional filter — limit the result to one "
                            "asset kind. Omit to receive both buckets."
                        ),
                    },
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    }


def persona_attach_asset_from_url_tool_schema() -> dict[str, Any]:
    """``persona_attach_asset_from_url`` — download + store an asset."""
    return {
        "type": "function",
        "function": {
            "name": PERSONA_ATTACH_ASSET_FROM_URL_TOOL,
            "description": (
                "Download an image from a URL and attach it to a "
                "persona as an emoji (``kind=emoji``) or reference "
                "image (``kind=reference``). The server fetches with a "
                "30s timeout, validates MIME (png/jpeg/webp/gif), and "
                "caps the download at 10 MiB. Returns the stored "
                "AssetRecord on success."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "persona_id": {
                        "type": "string",
                        "description": (
                            f"Target persona id ({_SLUG_HINT}). Must "
                            "already exist."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["emoji", "reference"],
                        "description": (
                            "Asset bucket — emoji ride via "
                            "send_attachment, reference images feed "
                            "image_with_refs."
                        ),
                    },
                    "label": {
                        "type": "string",
                        "description": (
                            "Slot label within the bucket "
                            "(e.g. ``happy`` / ``front``). Lowercase, "
                            "1-64 chars, only [a-z0-9_-]."
                        ),
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "Absolute http(s) URL of the source image."
                        ),
                    },
                    "file_name": {
                        "type": "string",
                        "description": (
                            "Optional original file name; defaults to "
                            "the URL path basename."
                        ),
                    },
                },
                "required": ["persona_id", "kind", "label", "url"],
                "additionalProperties": False,
            },
        },
    }


def persona_tool_schemas() -> list[dict[str, Any]]:
    """Return every persona.* tool schema as a list — kept callable so
    callers can re-derive at runtime and the agent_servicer's cached
    snapshot logic stays uniform with web / coding tool families."""
    return [
        persona_list_tool_schema(),
        persona_get_tool_schema(),
        persona_create_tool_schema(),
        persona_update_tool_schema(),
        persona_delete_tool_schema(),
        persona_list_assets_tool_schema(),
        persona_attach_asset_from_url_tool_schema(),
    ]


__all__ = [
    "PERSONA_ATTACH_ASSET_FROM_URL_TOOL",
    "PERSONA_CREATE_TOOL",
    "PERSONA_DELETE_TOOL",
    "PERSONA_GET_TOOL",
    "PERSONA_LIST_ASSETS_TOOL",
    "PERSONA_LIST_TOOL",
    "PERSONA_TOOLS",
    "PERSONA_UPDATE_TOOL",
    "persona_attach_asset_from_url_tool_schema",
    "persona_create_tool_schema",
    "persona_delete_tool_schema",
    "persona_get_tool_schema",
    "persona_list_assets_tool_schema",
    "persona_list_tool_schema",
    "persona_tool_schemas",
    "persona_update_tool_schema",
]
