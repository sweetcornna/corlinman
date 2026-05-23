"""Builtin ``revert_changes`` tool — undo the last workspace snapshot.

The agent servicer snapshots the workspace at the start of every chat
turn (see :func:`corlinman_agent.coding._snapshot.snapshot`); this tool
lets the model (or, via a chat command, the user) roll that snapshot
back. Two modes:

* ``mode="last"`` (default) — revert the most recent snapshot. Returns
  ``{"reverted_to": "<sha>", "from": "<old_sha>", "label": "..."}`` on
  success, ``{"error": "no_snapshots"}`` when there is nothing to undo.
* ``mode="list"`` — return the recent snapshot log without changing the
  workspace. Useful when the model wants to confirm what would be
  undone before doing so.

The tool is workspace-confined — it never touches anything outside
the agent's workspace directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from corlinman_agent.coding._common import (
    CodingArgsInvalidError,
    decode_args,
    resolve_workspace,
)
from corlinman_agent.coding._snapshot import list_snapshots, revert_last

logger = structlog.get_logger(__name__)

REVERT_CHANGES_TOOL: str = "revert_changes"

_VALID_MODES = ("last", "list")


def revert_changes_tool_schema() -> dict[str, Any]:
    """OpenAI tool descriptor for ``revert_changes``."""
    return {
        "type": "function",
        "function": {
            "name": REVERT_CHANGES_TOOL,
            "description": (
                "Undo the last set of file changes the agent made in "
                "this turn or a prior turn. Reverts the agent workspace "
                "to the previous snapshot. Optional `mode`: 'last' "
                "(default) reverts the most recent snapshot; 'list' "
                "returns the snapshot log without reverting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": list(_VALID_MODES),
                        "description": (
                            "'last' to revert the most recent snapshot "
                            "(default); 'list' to inspect recent "
                            "snapshots without changing anything."
                        ),
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


def dispatch_revert_changes(
    *, args_json: bytes | str, workspace: Path | None = None
) -> str:
    """Dispatch a ``revert_changes`` call. JSON envelope; never raises."""
    try:
        raw = decode_args(args_json)
    except CodingArgsInvalidError as exc:
        return json.dumps({"error": f"args_invalid: {exc.message}"})

    mode = raw.get("mode", "last")
    if not isinstance(mode, str):
        return json.dumps({"error": "args_invalid: 'mode' must be a string"})
    mode = mode.strip() or "last"
    if mode not in _VALID_MODES:
        return json.dumps(
            {"error": f"args_invalid: mode must be one of {_VALID_MODES}"}
        )

    ws = resolve_workspace(workspace)

    if mode == "list":
        snaps = list_snapshots(ws)
        payload: dict[str, Any] = {"snapshots": snaps}
        logger.info("agent.revert.listed", count=len(snaps))
        return json.dumps(payload, ensure_ascii=False)

    # mode == "last"
    result = revert_last(ws)
    return json.dumps(result, ensure_ascii=False)


__all__ = [
    "REVERT_CHANGES_TOOL",
    "dispatch_revert_changes",
    "revert_changes_tool_schema",
]
