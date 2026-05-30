"""Builtin ``persona.*`` tool family — read + mutate the persona
registry mid-conversation.

Backs PLAN_PERSONA_STUDIO W3: gives the agent the same CRUD surface the
admin UI uses, plus a download-and-store helper so a chat user can paste
an image URL and have the agent attach it as an emoji / reference asset
without leaving the conversation.

Public surface
--------------
* Tool-name constants (``PERSONA_LIST_TOOL`` etc.) — imported by the
  agent servicer's ``BUILTIN_TOOLS`` set + the ``_dispatch_builtin``
  switch.
* OpenAI-shaped schemas (``persona_list_tool_schema()`` …) — picked up
  by ``_inject_builtin_tools`` and advertised to the model.
* Async dispatchers (``dispatch_persona_list`` …) — wire-stable
  contract: ``args_json: bytes -> str`` JSON envelope, never raises.

The dispatchers depend on a ``PersonaStore`` + ``PersonaAssetStore`` —
both live in the corlinman-server package and are passed in by keyword
arg so this module stays import-decoupled from the server package
(mirrors the subagent ``BlackboardStore`` pattern).
"""

from __future__ import annotations

from corlinman_agent.persona.dispatch import (
    dispatch_persona_attach_asset_from_url,
    dispatch_persona_create,
    dispatch_persona_delete,
    dispatch_persona_get,
    dispatch_persona_list,
    dispatch_persona_list_assets,
    dispatch_persona_update,
)
from corlinman_agent.persona.life import (
    PERSONA_LIFE_DIARY_ADD_TOOL,
    PERSONA_LIFE_EVENT_SEED_TOOL,
    PERSONA_LIFE_GET_SEEDS_TOOL,
    PERSONA_LIFE_GET_TOOL,
    PERSONA_LIFE_SET_SEEDS_TOOL,
    PERSONA_LIFE_SET_STATE_TOOL,
    PERSONA_LIFE_TOOLS,
    dispatch_persona_life_diary_add,
    dispatch_persona_life_event_seed,
    dispatch_persona_life_get,
    dispatch_persona_life_get_seeds,
    dispatch_persona_life_set_seeds,
    dispatch_persona_life_set_state,
    persona_life_tool_schemas,
)
from corlinman_agent.persona.tools import (
    PERSONA_ATTACH_ASSET_FROM_URL_TOOL,
    PERSONA_CREATE_TOOL,
    PERSONA_DELETE_TOOL,
    PERSONA_GET_TOOL,
    PERSONA_LIST_ASSETS_TOOL,
    PERSONA_LIST_TOOL,
    PERSONA_TOOLS,
    PERSONA_UPDATE_TOOL,
    persona_attach_asset_from_url_tool_schema,
    persona_create_tool_schema,
    persona_delete_tool_schema,
    persona_get_tool_schema,
    persona_list_assets_tool_schema,
    persona_list_tool_schema,
    persona_tool_schemas,
    persona_update_tool_schema,
)

__all__ = [
    "PERSONA_ATTACH_ASSET_FROM_URL_TOOL",
    "PERSONA_CREATE_TOOL",
    "PERSONA_DELETE_TOOL",
    "PERSONA_GET_TOOL",
    "PERSONA_LIFE_DIARY_ADD_TOOL",
    "PERSONA_LIFE_EVENT_SEED_TOOL",
    "PERSONA_LIFE_GET_SEEDS_TOOL",
    "PERSONA_LIFE_GET_TOOL",
    "PERSONA_LIFE_SET_SEEDS_TOOL",
    "PERSONA_LIFE_SET_STATE_TOOL",
    "PERSONA_LIFE_TOOLS",
    "PERSONA_LIST_ASSETS_TOOL",
    "PERSONA_LIST_TOOL",
    "PERSONA_TOOLS",
    "PERSONA_UPDATE_TOOL",
    "dispatch_persona_attach_asset_from_url",
    "dispatch_persona_create",
    "dispatch_persona_delete",
    "dispatch_persona_get",
    "dispatch_persona_life_diary_add",
    "dispatch_persona_life_event_seed",
    "dispatch_persona_life_get",
    "dispatch_persona_life_get_seeds",
    "dispatch_persona_life_set_seeds",
    "dispatch_persona_life_set_state",
    "dispatch_persona_list",
    "dispatch_persona_list_assets",
    "dispatch_persona_update",
    "persona_attach_asset_from_url_tool_schema",
    "persona_create_tool_schema",
    "persona_delete_tool_schema",
    "persona_get_tool_schema",
    "persona_life_tool_schemas",
    "persona_list_assets_tool_schema",
    "persona_list_tool_schema",
    "persona_tool_schemas",
    "persona_update_tool_schema",
]
