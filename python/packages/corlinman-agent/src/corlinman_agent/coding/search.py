"""Builtin ``search_files`` tool — grep file contents / glob file names
inside the agent workspace.

Two modes:

* ``content`` (default) — regex search across file contents, returns
  ``path:line: text`` matches;
* ``name`` — glob the workspace tree for matching file paths.

Pure stdlib (``re`` + ``pathlib``) — no ripgrep dependency. JSON
envelope; never raises.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import structlog

from corlinman_agent.coding._common import (
    CodingArgsInvalidError,
    WorkspaceEscapeError,
    decode_args,
    resolve_in_workspace,
    resolve_workspace,
    workspace_rel,
)

logger = structlog.get_logger(__name__)

SEARCH_FILES_TOOL: str = "search_files"

#: Hard caps so a broad pattern can't flood the model's context.
_MAX_MATCHES = 200
_MAX_FILES_SCANNED = 5_000
#: Skip these dir names + binary-ish suffixes when scanning content.
#: VCS metadata (`.git/.svn/.hg/.bzr`) is always skipped in BOTH content and
#: name modes — applied via ``_iter_files`` (content) and a ``parts`` check
#: in the ``name`` branch below.
_SKIP_DIRS = {
    ".git",
    ".svn",
    ".hg",
    ".bzr",
    "__pycache__",
    "node_modules",
    ".venv",
    ".mypy_cache",
}
_SKIP_SUFFIXES = {
    ".pyc", ".so", ".o", ".bin", ".png", ".jpg", ".jpeg", ".gif",
    ".pdf", ".zip", ".gz", ".tar", ".wav", ".mp3", ".mp4", ".ico",
}


def search_files_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": SEARCH_FILES_TOOL,
            "description": (
                "Search the agent workspace. mode='content' (default) regex-"
                "matches file contents and returns path:line matches; "
                "mode='name' globs for file paths matching a pattern."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Regex (content mode) or glob like '**/*.py' "
                            "(name mode)."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["content", "name"],
                        "description": "Search mode (default 'content').",
                    },
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative subdir to scope to.",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Content-mode paging: skip the first N matches "
                            "(after mtime-sorting files newest-first). "
                            "Default 0."
                        ),
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    }


def _err(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _iter_files(root: Path) -> list[Path]:
    """Walk ``root``, skipping noise dirs + binary-ish suffixes."""
    out: list[Path] = []
    for path in root.rglob("*"):
        if len(out) >= _MAX_FILES_SCANNED:
            break
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        out.append(path)
    return out


def dispatch_search_files(
    *, args_json: bytes | str, workspace: Path | None = None
) -> str:
    """Run a workspace search. JSON envelope; never raises."""
    try:
        raw = decode_args(args_json)
        ws = resolve_workspace(workspace)
        scope = resolve_in_workspace(ws, raw.get("path") or ".")
    except CodingArgsInvalidError as exc:
        return _err({"error": f"args_invalid: {exc.message}"})
    except WorkspaceEscapeError as exc:
        return _err({"error": f"workspace_escape: {exc}"})

    pattern = raw.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return _err({"error": "args_invalid: missing or empty 'pattern'"})
    mode = raw.get("mode") or "content"
    if mode not in ("content", "name"):
        return _err({"error": "args_invalid: mode must be 'content' or 'name'"})
    offset_raw = raw.get("offset", 0)
    if not isinstance(offset_raw, int) or isinstance(offset_raw, bool) or offset_raw < 0:
        return _err({"error": "args_invalid: offset must be a non-negative int"})
    offset = offset_raw
    if not scope.is_dir():
        return _err({"path": raw.get("path") or ".", "error": "not_a_directory"})

    if mode == "name":
        matches: list[str] = []
        for path in scope.rglob(pattern):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.is_file():
                matches.append(workspace_rel(ws, path))
            if len(matches) >= _MAX_MATCHES:
                break
        return json.dumps(
            {"mode": "name", "pattern": pattern, "matches": sorted(matches)},
            ensure_ascii=False,
        )

    # content mode
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return _err({"error": f"args_invalid: bad regex: {exc}"})

    # Collect (mtime, path_str, line, text) per file, grouped by file.
    # We gather ALL matches first (bounded by ``_iter_files``'s scan cap)
    # so we can deterministically sort files by mtime descending before
    # paging — otherwise ``offset`` would page through arbitrary scan
    # order and pagination would be non-deterministic.
    per_file: dict[Path, list[dict[str, Any]]] = {}
    for path in _iter_files(scope):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        file_hits: list[dict[str, Any]] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                file_hits.append(
                    {
                        "path": workspace_rel(ws, path),
                        "line": lineno,
                        "text": line.strip()[:300],
                    }
                )
        if file_hits:
            per_file[path] = file_hits

    # Sort files by mtime DESC (newest first); within a file keep
    # line-number ascending (insertion order from above is already ascending).
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    sorted_files = sorted(per_file.keys(), key=_mtime, reverse=True)
    ordered: list[dict[str, Any]] = []
    for path in sorted_files:
        ordered.extend(per_file[path])

    total = len(ordered)
    limit = _MAX_MATCHES
    page = ordered[offset : offset + limit]
    end = offset + len(page)
    next_offset: int | None = end if end < total else None
    return json.dumps(
        {
            "mode": "content",
            "pattern": pattern,
            "matches": page,
            "truncated": next_offset is not None,
            "next_offset": next_offset,
        },
        ensure_ascii=False,
    )


__all__ = [
    "SEARCH_FILES_TOOL",
    "dispatch_search_files",
    "search_files_tool_schema",
]
