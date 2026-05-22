"""Builtin file tools — ``read_file`` / ``write_file`` / ``edit_file`` /
``list_files``.

Workspace-confined: every path is resolved through
:func:`corlinman_agent.coding._common.resolve_in_workspace`, so the agent
cannot read or write outside its workspace directory.

Each ``dispatch_*`` returns a JSON envelope string for
``ToolResult.content`` and never raises.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from corlinman_agent.coding._common import (
    MAX_READ_CHARS,
    MAX_WRITE_BYTES,
    CodingArgsInvalidError,
    WorkspaceEscapeError,
    decode_args,
    resolve_in_workspace,
    resolve_workspace,
    workspace_rel,
)

logger = structlog.get_logger(__name__)

READ_FILE_TOOL: str = "read_file"
WRITE_FILE_TOOL: str = "write_file"
EDIT_FILE_TOOL: str = "edit_file"
LIST_FILES_TOOL: str = "list_files"

#: Directory entries never surfaced by ``list_files`` — noise / unsafe.
_LIST_SKIP = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def read_file_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": READ_FILE_TOOL,
            "description": (
                "Read a UTF-8 text file from the agent workspace. Returns "
                "the file content with 1-based line numbers. Use offset/limit "
                "to page through large files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-based first line to read (default 1).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to read (default 500).",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }


def write_file_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": WRITE_FILE_TOOL,
            "description": (
                "Create or overwrite a text file in the agent workspace. "
                "Parent directories are created automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    }


def edit_file_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": EDIT_FILE_TOOL,
            "description": (
                "Edit a file by replacing an exact string. 'old_string' must "
                "match exactly once (include enough surrounding context to be "
                "unique). Use replace_all to replace every occurrence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to replace.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence (default false).",
                    },
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
        },
    }


def list_files_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": LIST_FILES_TOOL,
            "description": (
                "List files and directories under a workspace path. Returns "
                "entries with type (file/dir) and size."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Workspace-relative directory (default '.')."
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _err(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def dispatch_read_file(
    *, args_json: bytes | str, workspace: Path | None = None
) -> str:
    """Read a workspace file. Returns a JSON envelope; never raises."""
    try:
        raw = decode_args(args_json)
        ws = resolve_workspace(workspace)
        path = resolve_in_workspace(ws, raw.get("path"))
    except CodingArgsInvalidError as exc:
        return _err({"error": f"args_invalid: {exc.message}"})
    except WorkspaceEscapeError as exc:
        return _err({"error": f"workspace_escape: {exc}"})

    if not path.exists():
        return _err({"path": raw.get("path"), "error": "file_not_found"})
    if not path.is_file():
        return _err({"path": raw.get("path"), "error": "not_a_file"})

    offset = raw.get("offset", 1)
    limit = raw.get("limit", 500)
    try:
        offset = max(1, int(offset))
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        return _err({"error": "args_invalid: offset/limit must be integers"})

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _err({"path": raw.get("path"), "error": f"read_failed: {exc}"})

    lines = text.splitlines()
    total = len(lines)
    chunk = lines[offset - 1 : offset - 1 + limit]
    numbered = "\n".join(
        f"{offset + i}\t{ln}" for i, ln in enumerate(chunk)
    )
    truncated = len(numbered) > MAX_READ_CHARS
    if truncated:
        numbered = numbered[:MAX_READ_CHARS]
    return json.dumps(
        {
            "path": workspace_rel(ws, path),
            "content": numbered,
            "lines": total,
            "shown": [offset, min(offset + limit - 1, total)],
            "truncated": truncated,
        },
        ensure_ascii=False,
    )


def dispatch_write_file(
    *, args_json: bytes | str, workspace: Path | None = None
) -> str:
    """Create or overwrite a workspace file. JSON envelope; never raises."""
    try:
        raw = decode_args(args_json)
        ws = resolve_workspace(workspace)
        path = resolve_in_workspace(ws, raw.get("path"))
    except CodingArgsInvalidError as exc:
        return _err({"error": f"args_invalid: {exc.message}"})
    except WorkspaceEscapeError as exc:
        return _err({"error": f"workspace_escape: {exc}"})

    content = raw.get("content")
    if not isinstance(content, str):
        return _err({"error": "args_invalid: 'content' must be a string"})
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return _err(
            {"error": f"content_too_large: cap is {MAX_WRITE_BYTES} bytes"}
        )

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return _err({"path": raw.get("path"), "error": f"write_failed: {exc}"})
    return json.dumps(
        {
            "path": workspace_rel(ws, path),
            "bytes": len(content.encode("utf-8")),
            "action": "overwritten" if existed else "created",
        },
        ensure_ascii=False,
    )


def dispatch_edit_file(
    *, args_json: bytes | str, workspace: Path | None = None
) -> str:
    """Replace an exact string in a workspace file. JSON envelope."""
    try:
        raw = decode_args(args_json)
        ws = resolve_workspace(workspace)
        path = resolve_in_workspace(ws, raw.get("path"))
    except CodingArgsInvalidError as exc:
        return _err({"error": f"args_invalid: {exc.message}"})
    except WorkspaceEscapeError as exc:
        return _err({"error": f"workspace_escape: {exc}"})

    old = raw.get("old_string")
    new = raw.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return _err(
            {"error": "args_invalid: old_string/new_string must be strings"}
        )
    if old == new:
        return _err({"error": "args_invalid: old_string equals new_string"})
    if not path.is_file():
        return _err({"path": raw.get("path"), "error": "file_not_found"})

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _err({"path": raw.get("path"), "error": f"read_failed: {exc}"})

    count = text.count(old)
    if count == 0:
        return _err({"path": raw.get("path"), "error": "old_string_not_found"})
    replace_all = bool(raw.get("replace_all", False))
    if count > 1 and not replace_all:
        return _err(
            {
                "path": raw.get("path"),
                "error": (
                    f"old_string_not_unique: {count} matches — add context "
                    "or set replace_all=true"
                ),
            }
        )

    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return _err({"path": raw.get("path"), "error": f"write_failed: {exc}"})
    return json.dumps(
        {
            "path": workspace_rel(ws, path),
            "replacements": count if replace_all else 1,
        },
        ensure_ascii=False,
    )


def dispatch_list_files(
    *, args_json: bytes | str, workspace: Path | None = None
) -> str:
    """List a workspace directory. JSON envelope; never raises."""
    try:
        raw = decode_args(args_json)
        ws = resolve_workspace(workspace)
        target = resolve_in_workspace(ws, raw.get("path") or ".")
    except CodingArgsInvalidError as exc:
        return _err({"error": f"args_invalid: {exc.message}"})
    except WorkspaceEscapeError as exc:
        return _err({"error": f"workspace_escape: {exc}"})

    if not target.exists():
        return _err({"path": raw.get("path") or ".", "error": "not_found"})
    if not target.is_dir():
        return _err({"path": raw.get("path") or ".", "error": "not_a_directory"})

    entries: list[dict[str, Any]] = []
    try:
        for child in sorted(target.iterdir()):
            if child.name in _LIST_SKIP:
                continue
            is_dir = child.is_dir()
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if is_dir else "file",
                    "size": 0 if is_dir else child.stat().st_size,
                }
            )
    except OSError as exc:
        return _err({"error": f"list_failed: {exc}"})
    return json.dumps(
        {"path": workspace_rel(ws, target), "entries": entries},
        ensure_ascii=False,
    )


__all__ = [
    "EDIT_FILE_TOOL",
    "LIST_FILES_TOOL",
    "READ_FILE_TOOL",
    "WRITE_FILE_TOOL",
    "dispatch_edit_file",
    "dispatch_list_files",
    "dispatch_read_file",
    "dispatch_write_file",
    "edit_file_tool_schema",
    "list_files_tool_schema",
    "read_file_tool_schema",
    "write_file_tool_schema",
]
