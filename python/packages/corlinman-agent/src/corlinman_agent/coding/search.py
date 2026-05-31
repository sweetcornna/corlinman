"""Builtin ``search_files`` tool — grep file contents / glob file names
inside the agent workspace.

Two modes:

* ``content`` (default) — regex search across file contents, returns
  ``path:line: text`` matches;
* ``name`` — glob the workspace tree for matching file paths.

The content mode reaches ripgrep parity: ``output_mode`` selects between
matched lines (``content``), bare file paths (``files_with_matches``) or
per-file counts (``count``); ``case_insensitive`` toggles ``re.IGNORECASE``;
``before``/``after``/``context`` capture surrounding lines (-B/-A/-C); a
``glob`` / ``type`` pre-filter scopes the file iterator before reading.

Pure stdlib (``re`` + ``pathlib`` + ``fnmatch``) — no ripgrep dependency.
JSON envelope; never raises.
"""

from __future__ import annotations

import fnmatch
import json
import os
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
#: Per matched line cap. Past this we append an explicit truncation marker
#: rather than silently dropping the tail — ripgrep keeps the whole line but
#: an LLM context can't afford a single multi-megabyte minified line.
_MAX_LINE_CHARS = 2_000
_LINE_TRUNC_MARKER = " …(line truncated)"
#: Cap on context lines (-B/-A/-C) requested per match, to bound output.
_MAX_CONTEXT = 50
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

#: Named ``type`` filters (ripgrep ``--type``) → glob suffixes. Small,
#: opinionated subset covering the languages an agent edits most.
_TYPE_GLOBS: dict[str, tuple[str, ...]] = {
    "py": ("*.py", "*.pyi"),
    "python": ("*.py", "*.pyi"),
    "js": ("*.js", "*.mjs", "*.cjs", "*.jsx"),
    "ts": ("*.ts", "*.tsx"),
    "rust": ("*.rs",),
    "go": ("*.go",),
    "java": ("*.java",),
    "c": ("*.c", "*.h"),
    "cpp": ("*.cpp", "*.cc", "*.cxx", "*.hpp", "*.hh"),
    "json": ("*.json",),
    "yaml": ("*.yaml", "*.yml"),
    "toml": ("*.toml",),
    "md": ("*.md", "*.markdown"),
    "markdown": ("*.md", "*.markdown"),
    "txt": ("*.txt",),
    "html": ("*.html", "*.htm"),
    "css": ("*.css", "*.scss", "*.sass"),
    "sh": ("*.sh", "*.bash", "*.zsh"),
}


def search_files_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": SEARCH_FILES_TOOL,
            "description": (
                "Search the agent workspace. mode='content' (default) regex-"
                "matches file contents and returns path:line matches; "
                "mode='name' globs for file paths matching a pattern. "
                "Content mode supports ripgrep-style output_mode "
                "('content'|'files_with_matches'|'count'), case_insensitive, "
                "before/after/context lines, and a glob/type pre-filter."
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
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "description": (
                            "Content-mode shape: 'content' (default) returns "
                            "matched lines; 'files_with_matches' returns only "
                            "the matching file paths; 'count' returns per-file "
                            "match counts."
                        ),
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": (
                            "Content mode: match case-insensitively "
                            "(re.IGNORECASE). Default false."
                        ),
                    },
                    "before": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Content mode: include N lines BEFORE each match "
                            "(ripgrep -B). Default 0."
                        ),
                    },
                    "after": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Content mode: include N lines AFTER each match "
                            "(ripgrep -A). Default 0."
                        ),
                    },
                    "context": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Content mode: include N lines BEFORE AND AFTER "
                            "each match (ripgrep -C); overrides before/after."
                        ),
                    },
                    "glob": {
                        "type": "string",
                        "description": (
                            "Content mode: pre-filter files by glob on the "
                            "workspace-relative path, e.g. '*.py' or "
                            "'src/**/*.ts'."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "description": (
                            "Content mode: pre-filter by file type, e.g. "
                            "'py', 'ts', 'rust', 'md'."
                        ),
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


def _clip_line(line: str) -> str:
    """Return ``line`` (newline already stripped by caller) verbatim, only
    appending an explicit marker past the per-line cap. Indentation is
    preserved — no ``.strip()``."""
    if len(line) > _MAX_LINE_CHARS:
        return line[:_MAX_LINE_CHARS] + _LINE_TRUNC_MARKER
    return line


def _matches_prefilter(
    rel_path: str, name: str, globs: tuple[str, ...] | None
) -> bool:
    """True when ``rel_path``/``name`` passes the optional glob/type filter.

    ``globs`` is ``None`` when no filter was supplied (match everything).
    A glob with a path separator is matched against the workspace-relative
    path; a bare glob is matched against the file's basename too — so
    ``'*.py'`` matches ``src/a.py`` as ripgrep's ``--glob`` would.
    """
    if not globs:
        return True
    # ``fnmatch``'s ``*`` already crosses ``/`` (unlike a shell), so a
    # recursive ``**`` segment is equivalent to a single ``*`` here. Collapse
    # it so a ripgrep-style ``src/**/*.py`` matches ``src/a.py`` and deeper.
    norm = rel_path.replace(os.sep, "/")
    for g in globs:
        pat = g.replace("**/", "*").replace("**", "*")
        if "/" in pat:
            if fnmatch.fnmatch(norm, pat):
                return True
        else:
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(norm, pat):
                return True
    return False


def _iter_files(
    root: Path, *, globs: tuple[str, ...] | None, ws: Path
) -> list[Path]:
    """Walk ``root``, skipping noise dirs + binary-ish suffixes, applying
    the optional glob/type pre-filter against the workspace-relative path."""
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
        if globs is not None and not _matches_prefilter(
            workspace_rel(ws, path), path.name, globs
        ):
            continue
        out.append(path)
    return out


def _coerce_nonneg_int(raw: Any, field: str) -> int:
    if raw is None:
        return 0
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise CodingArgsInvalidError(f"{field} must be a non-negative int")
    return raw


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
        return _run_name_mode(raw=raw, pattern=pattern, scope=scope, ws=ws)

    return _run_content_mode(
        raw=raw, pattern=pattern, scope=scope, ws=ws, offset=offset
    )


def _run_name_mode(
    *, raw: dict[str, Any], pattern: str, scope: Path, ws: Path
) -> str:
    """Glob the tree for file paths, newest-modified first, then cap."""
    candidates: list[Path] = []
    for path in scope.rglob(pattern):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            candidates.append(path)
        if len(candidates) >= _MAX_FILES_SCANNED:
            break

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    # Sort by mtime DESC (newest first) BEFORE applying the cap so the most
    # recently touched files survive truncation.
    candidates.sort(key=_mtime, reverse=True)
    truncated = len(candidates) > _MAX_MATCHES
    capped = candidates[:_MAX_MATCHES]
    matches = [workspace_rel(ws, p) for p in capped]
    return json.dumps(
        {
            "mode": "name",
            "pattern": pattern,
            "matches": matches,
            "truncated": truncated,
        },
        ensure_ascii=False,
    )


def _run_content_mode(
    *,
    raw: dict[str, Any],
    pattern: str,
    scope: Path,
    ws: Path,
    offset: int,
) -> str:
    output_mode = raw.get("output_mode") or "content"
    if output_mode not in ("content", "files_with_matches", "count"):
        return _err(
            {
                "error": (
                    "args_invalid: output_mode must be 'content', "
                    "'files_with_matches' or 'count'"
                )
            }
        )
    case_insensitive = raw.get("case_insensitive", False)
    if not isinstance(case_insensitive, bool):
        return _err({"error": "args_invalid: case_insensitive must be a bool"})

    # Context lines: -C overrides -B/-A.
    try:
        context = _coerce_nonneg_int(raw.get("context"), "context")
        before = _coerce_nonneg_int(raw.get("before"), "before")
        after = _coerce_nonneg_int(raw.get("after"), "after")
    except CodingArgsInvalidError as exc:
        return _err({"error": f"args_invalid: {exc.message}"})
    if context:
        before = after = context
    before = min(before, _MAX_CONTEXT)
    after = min(after, _MAX_CONTEXT)

    # glob/type pre-filter → glob tuple (None == match everything).
    globs = _resolve_prefilter(raw)
    if isinstance(globs, str):  # error sentinel
        return _err({"error": globs})

    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return _err({"error": f"args_invalid: bad regex: {exc}"})

    # Collect matches per file, grouped by file. Gather ALL first (bounded by
    # ``_iter_files``'s scan cap) so we can deterministically sort files by
    # mtime descending before paging.
    per_file: dict[Path, list[dict[str, Any]]] = {}
    per_file_counts: dict[Path, int] = {}
    for path in _iter_files(scope, globs=globs, ws=ws):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.splitlines()
        file_hits: list[dict[str, Any]] = []
        count = 0
        for idx, line in enumerate(lines):
            if not regex.search(line):
                continue
            count += 1
            lineno = idx + 1
            hit: dict[str, Any] = {
                "path": workspace_rel(ws, path),
                "line": lineno,
                "text": _clip_line(line),
            }
            if before or after:
                lo = max(0, idx - before)
                hi = min(len(lines), idx + after + 1)
                hit["before_context"] = [
                    _clip_line(lines[j]) for j in range(lo, idx)
                ]
                hit["after_context"] = [
                    _clip_line(lines[j]) for j in range(idx + 1, hi)
                ]
            file_hits.append(hit)
        if count:
            per_file_counts[path] = count
            if output_mode == "content":
                per_file[path] = file_hits

    # Sort files by mtime DESC (newest first); within a file keep
    # line-number ascending (insertion order is already ascending).
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    if output_mode == "files_with_matches":
        sorted_files = sorted(per_file_counts.keys(), key=_mtime, reverse=True)
        rels = [workspace_rel(ws, p) for p in sorted_files]
        truncated = len(rels) > _MAX_MATCHES
        return json.dumps(
            {
                "mode": "content",
                "output_mode": "files_with_matches",
                "pattern": pattern,
                "files": rels[:_MAX_MATCHES],
                "truncated": truncated,
            },
            ensure_ascii=False,
        )

    if output_mode == "count":
        sorted_files = sorted(per_file_counts.keys(), key=_mtime, reverse=True)
        counts = [
            {"path": workspace_rel(ws, p), "count": per_file_counts[p]}
            for p in sorted_files
        ]
        total_matches = sum(per_file_counts.values())
        truncated = len(counts) > _MAX_MATCHES
        return json.dumps(
            {
                "mode": "content",
                "output_mode": "count",
                "pattern": pattern,
                "counts": counts[:_MAX_MATCHES],
                "total_matches": total_matches,
                "truncated": truncated,
            },
            ensure_ascii=False,
        )

    # output_mode == "content"
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
            "output_mode": "content",
            "pattern": pattern,
            "matches": page,
            "truncated": next_offset is not None,
            "next_offset": next_offset,
        },
        ensure_ascii=False,
    )


def _resolve_prefilter(raw: dict[str, Any]) -> tuple[str, ...] | None | str:
    """Resolve ``glob`` + ``type`` args into a glob tuple.

    Returns ``None`` when no filter was given (match everything), a tuple of
    glob patterns otherwise, or an error string sentinel on bad args.
    """
    globs: list[str] = []
    glob_raw = raw.get("glob")
    if glob_raw is not None:
        if not isinstance(glob_raw, str) or not glob_raw.strip():
            return "args_invalid: glob must be a non-empty string"
        globs.append(glob_raw)

    type_raw = raw.get("type")
    if type_raw is not None:
        if not isinstance(type_raw, str) or not type_raw.strip():
            return "args_invalid: type must be a non-empty string"
        type_key = type_raw.strip().lower()
        type_globs = _TYPE_GLOBS.get(type_key)
        if type_globs is None:
            known = ", ".join(sorted(_TYPE_GLOBS))
            return f"args_invalid: unknown type {type_raw!r}; known: {known}"
        globs.extend(type_globs)

    if not globs:
        return None
    return tuple(globs)


__all__ = [
    "SEARCH_FILES_TOOL",
    "dispatch_search_files",
    "search_files_tool_schema",
]
