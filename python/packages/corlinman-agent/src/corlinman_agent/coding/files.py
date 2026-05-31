"""Builtin file tools — ``read_file`` / ``write_file`` / ``edit_file`` /
``list_files``.

Workspace-confined: every path is resolved through
:func:`corlinman_agent.coding._common.resolve_in_workspace`, so the agent
cannot read or write outside its workspace directory.

Each ``dispatch_*`` returns a JSON envelope string for
``ToolResult.content`` and never raises.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
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
from corlinman_agent.coding._filestate import FileState

# Optional O_NOFOLLOW — present on every POSIX target this project
# supports (Linux + macOS). Windows lacks the flag entirely; the
# per-component lstat scan in :func:`resolve_in_workspace` is the only
# protection on that platform.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _require_read_before_edit() -> bool:
    """Claude-Code-style read-before-edit guard toggle.

    When a :class:`FileState` is threaded (the production agent path),
    refuse to edit an existing file the model never read (or wrote) this
    turn — a blind edit to unseen bytes is the classic destructive
    footgun. Default on; set ``CORLINMAN_REQUIRE_READ_BEFORE_EDIT=0``
    (``false``/``no``) to disable, e.g. for tooling that edits files it
    produced out-of-band. Read at call time so tests/operators can flip
    it via the environment.
    """
    return os.environ.get(
        "CORLINMAN_REQUIRE_READ_BEFORE_EDIT", "1"
    ).strip().lower() not in {"0", "false", "no", ""}


logger = structlog.get_logger(__name__)

READ_FILE_TOOL: str = "read_file"
WRITE_FILE_TOOL: str = "write_file"
EDIT_FILE_TOOL: str = "edit_file"
LIST_FILES_TOOL: str = "list_files"

#: Directory entries never surfaced by ``list_files`` — noise / unsafe.
_LIST_SKIP = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache"}

#: File extensions treated as binary image files by ``read_file``.
#: Reading these as UTF-8 text would produce garbage; instead the tool
#: encodes them as base64 and returns a multimodal content-block list so
#: vision models can see the image inline in the tool-result turn.
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp"}
)

#: Explicit MIME overrides for the most common image formats; falls back
#: to :func:`mimetypes.guess_type` for anything not in this table.
_IMAGE_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def read_file_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": READ_FILE_TOOL,
            "description": (
                "Read a file from the agent workspace. Text files return "
                "content with 1-based line numbers; use offset/limit to page "
                "through large files. Image files (.png, .jpg, .jpeg, .gif, "
                ".webp) are returned as a base64-encoded image content block "
                "so vision models can inspect them directly."
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
    *,
    args_json: bytes | str,
    workspace: Path | None = None,
    state: FileState | None = None,
) -> str | list[dict[str, Any]]:
    """Read a workspace file. Returns a JSON envelope (str) for text files or a
    multimodal content-block list for image files; never raises.

    **Text files** — returns a JSON string with line-numbered content plus
    pagination metadata (same as before).

    **Image files** (.png, .jpg, .jpeg, .gif, .webp) — returns a
    ``list[dict]`` containing a single ``image_url`` content block with the
    image encoded as a ``data:`` URL.  Callers that only handle ``str`` results
    will receive the list unchanged; the reasoning-loop's
    :func:`_extend_with_tool_round` forwards it verbatim to the provider so
    vision models see the image inline.

    When a per-turn ``state`` is supplied and the file's mtime is
    unchanged since the previous read in this turn, the cached content
    is reused (no disk hit). Every real read records ``(mtime, text)``
    so a follow-up ``edit_file`` can detect staleness (T2.2).
    """
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

    # --- Image files: encode as base64 content-block list ---------------
    # Binary image data cannot be decoded as UTF-8 and is useless as text
    # to a vision model anyway. Return a multimodal content-block list so
    # the provider sees the image inline in the tool-result turn.
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            return _err({"path": raw.get("path"), "error": f"read_failed: {exc}"})
        suffix_lower = path.suffix.lower()
        mime = _IMAGE_MIME.get(suffix_lower)
        if mime is None:
            guessed, _ = mimetypes.guess_type(path.name)
            mime = guessed or "application/octet-stream"
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        return [
            {
                "type": "image_url",
                "image_url": {"url": data_url},
            }
        ]

    offset = raw.get("offset", 1)
    limit = raw.get("limit", 500)
    try:
        offset = max(1, int(offset))
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        return _err({"error": "args_invalid: offset/limit must be integers"})

    text: str | None = None
    if state is not None:
        text = state.cached_read(path)
    if text is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _err({"path": raw.get("path"), "error": f"read_failed: {exc}"})
        if state is not None:
            try:
                state.record_read(path, path.stat().st_mtime, text)
            except OSError:
                pass

    lines = text.splitlines()
    total = len(lines)
    chunk = lines[offset - 1 : offset - 1 + limit]
    numbered = "\n".join(
        f"{offset + i}\t{ln}" for i, ln in enumerate(chunk)
    )
    truncated = len(numbered) > MAX_READ_CHARS
    result: dict[str, Any] = {
        "path": workspace_rel(ws, path),
        "content": numbered,
        "lines": total,
        "shown": [offset, min(offset + limit - 1, total)],
        "truncated": truncated,
    }
    if truncated:
        # Don't silently hand back a head slice with no way forward —
        # the model would just re-read the same head. Cut on a line
        # boundary, report exactly which lines survived, and point at
        # the next offset so a follow-up read continues instead of
        # repeating. (`search_files` is the better tool for jumping to a
        # known section; the hint says so.)
        kept = numbered[:MAX_READ_CHARS]
        complete_lines = kept.count("\n")  # fully-terminated numbered lines
        next_offset = offset + complete_lines
        result["content"] = kept
        result["shown"] = [offset, max(offset, next_offset - 1)]
        result["next_offset"] = next_offset if next_offset <= total else None
        result["hint"] = (
            f"output truncated at {MAX_READ_CHARS} chars — continue from "
            "next_offset, narrow with offset/limit, or use search_files to "
            "jump to the relevant section"
        )
    return json.dumps(result, ensure_ascii=False)


def dispatch_write_file(
    *,
    args_json: bytes | str,
    workspace: Path | None = None,
    state: FileState | None = None,
) -> str:
    """Create or overwrite a workspace file. JSON envelope; never raises.

    A ``state`` write invalidates any cached read for the path so the
    next read re-fetches and re-pins the new mtime.
    """
    try:
        raw = decode_args(args_json)
        ws = resolve_workspace(workspace)
        # S3: ``for_write=True`` refuses symlinked ancestors *and* a
        # leaf that is itself a symlink, so a write through a symlink
        # planted by an earlier turn cannot escape the workspace.
        path = resolve_in_workspace(ws, raw.get("path"), for_write=True)
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
        # S3: open with ``O_NOFOLLOW`` so a leaf symlink that appeared
        # between :func:`resolve_in_workspace` and the open (TOCTOU)
        # is also refused at the syscall layer. The pre-check catches
        # the static case; ``O_NOFOLLOW`` catches the race.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o644)
        except OSError as exc:
            # ELOOP from O_NOFOLLOW surfaces as OSError; treat as
            # a workspace_escape so the model sees a consistent error.
            msg = str(exc)
            if "ELOOP" in msg or "Too many levels of symbolic links" in msg or getattr(exc, "errno", None) == 62:
                return _err(
                    {"path": raw.get("path"), "error": f"workspace_escape: O_NOFOLLOW refused: {exc}"}
                )
            return _err(
                {"path": raw.get("path"), "error": f"write_failed: {exc}"}
            )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            return _err(
                {"path": raw.get("path"), "error": f"write_failed: {exc}"}
            )
    except OSError as exc:
        return _err({"path": raw.get("path"), "error": f"write_failed: {exc}"})
    if state is not None:
        # Invalidate the stale read cache but mark the path seen — the
        # agent authored these bytes, so it may edit the file this turn
        # without a redundant read first.
        state.forget(path)
        state.mark_seen(path)
    return json.dumps(
        {
            "path": workspace_rel(ws, path),
            "bytes": len(content.encode("utf-8")),
            "action": "overwritten" if existed else "created",
        },
        ensure_ascii=False,
    )


def _line_span_offsets(text: str) -> list[tuple[int, int]]:
    """Return ``[(line_start, line_end_exclusive_of_newline)]`` per line.

    ``text[start:end]`` is the line content without its trailing newline;
    ``text[end:end+1]`` is the newline (or empty at EOF). Used by the
    multi-line fuzzy matcher to map line-aligned matches back to character
    offsets so the surrounding bytes (newlines, indentation outside the
    matched block) are preserved verbatim.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    n = len(text)
    while start <= n:
        nl = text.find("\n", start)
        if nl == -1:
            spans.append((start, n))
            break
        spans.append((start, nl))
        start = nl + 1
    return spans


def _fuzzy_line_matches(
    text: str, old: str, transform
) -> list[tuple[int, int]]:
    """Find line-aligned matches of ``old`` in ``text`` under ``transform``.

    ``transform`` is applied per-line on both sides before comparing.
    Returns a list of ``(char_start, char_end)`` spans in the ORIGINAL
    ``text`` whose included lines, after ``transform``, match ``old``'s
    lines after ``transform``. Multi-line ``old`` only; the caller falls
    back to substring matching for single-line edits.

    The end offset is the last matched line's end (exclusive of any
    trailing newline), so ``text[start:end]`` is the bytes we replace
    and the model's ``new_string`` is substituted in their place.
    """
    text_lines = text.split("\n")
    old_lines = old.split("\n")
    if len(old_lines) < 2:
        return []
    t_xform = [transform(ln) for ln in text_lines]
    o_xform = [transform(ln) for ln in old_lines]
    if not o_xform or not t_xform:
        return []

    line_spans = _line_span_offsets(text)
    out: list[tuple[int, int]] = []
    end_idx = len(t_xform) - len(o_xform) + 1
    for i in range(end_idx):
        if t_xform[i : i + len(o_xform)] == o_xform:
            start_char = line_spans[i][0]
            end_char = line_spans[i + len(o_xform) - 1][1]
            out.append((start_char, end_char))
    return out


def dispatch_edit_file(
    *,
    args_json: bytes | str,
    workspace: Path | None = None,
    state: FileState | None = None,
) -> str:
    """Replace an exact string in a workspace file. JSON envelope.

    Match cascade (T2.2):
    1. exact substring match (today's behaviour);
    2. line-aligned ``rstrip`` match — recovers from trailing-whitespace
       drift in the model's ``old_string``;
    3. line-aligned ``strip`` match — recovers from indentation drift.

    Each tier is consulted in order; the first tier with **any** matches
    wins. The uniqueness rule still applies: >1 match without
    ``replace_all`` is rejected. Fuzzy tiers only run for multi-line
    ``old_string`` — single-line edits stay on the exact path.

    When ``state`` is supplied and the file changed under the agent
    since its last recorded read, the edit is refused with
    ``file_changed_since_read``. A successful edit invalidates the
    cache so the next read re-pins the new mtime.
    """
    try:
        raw = decode_args(args_json)
        ws = resolve_workspace(workspace)
        # S3: edits are writes — refuse symlinked ancestors and leaves.
        path = resolve_in_workspace(ws, raw.get("path"), for_write=True)
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

    # Read-before-edit guard: an edit to an existing file the agent never
    # observed this turn is a blind mutation of unseen bytes. Only enforced
    # when a FileState is threaded (the production path); state=None callers
    # (most unit tests, ad-hoc tooling) are unaffected.
    if (
        state is not None
        and _require_read_before_edit()
        and not state.was_seen(path)
    ):
        return _err(
            {
                "path": raw.get("path"),
                "error": (
                    "file_not_read: read the file before editing it — call "
                    "read_file first so the edit matches the current bytes"
                ),
            }
        )

    if state is not None and state.is_stale(path):
        return _err(
            {"path": raw.get("path"), "error": "file_changed_since_read"}
        )

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _err({"path": raw.get("path"), "error": f"read_failed: {exc}"})

    replace_all = bool(raw.get("replace_all", False))

    # --- Tier 1: exact substring -----------------------------------------
    exact_count = text.count(old)
    if exact_count > 0:
        if exact_count > 1 and not replace_all:
            return _err(
                {
                    "path": raw.get("path"),
                    "error": (
                        f"old_string_not_unique: {exact_count} matches — add "
                        "context or set replace_all=true"
                    ),
                }
            )
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        replacements = exact_count if replace_all else 1
        tier = "exact"
    else:
        # --- Tier 2/3: line-aligned fuzzy for multi-line old_string -----
        updated = None
        tier = None
        replacements = 0
        for tier_name, transform in (
            ("rstrip", str.rstrip),
            ("strip", str.strip),
        ):
            spans = _fuzzy_line_matches(text, old, transform)
            if not spans:
                continue
            if len(spans) > 1 and not replace_all:
                return _err(
                    {
                        "path": raw.get("path"),
                        "error": (
                            f"old_string_not_unique: {len(spans)} fuzzy "
                            f"({tier_name}) matches — add context or set "
                            "replace_all=true"
                        ),
                    }
                )
            # Apply right-to-left so earlier spans' offsets stay valid.
            updated = text
            for start, end in sorted(spans, reverse=True):
                updated = updated[:start] + new + updated[end:]
            replacements = len(spans) if replace_all else 1
            if not replace_all:
                # We only consumed the first span — recompute as a
                # single-shot replace using the first matched range.
                start, end = spans[0]
                updated = text[:start] + new + text[end:]
            tier = tier_name
            break
    if updated is None:
        return _err({"path": raw.get("path"), "error": "old_string_not_found"})

    try:
        # S3: open with O_NOFOLLOW. The earlier resolve_in_workspace
        # pre-check refused if any ancestor was a symlink at that
        # point; O_NOFOLLOW catches a leaf symlink swapped in between
        # the check and the open (TOCTOU). The path already exists
        # here (we just read it above), so O_TRUNC + O_WRONLY is
        # safe.
        flags = os.O_WRONLY | os.O_TRUNC | _O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            msg = str(exc)
            if "ELOOP" in msg or "Too many levels of symbolic links" in msg or getattr(exc, "errno", None) == 62:
                return _err(
                    {"path": raw.get("path"), "error": f"workspace_escape: O_NOFOLLOW refused: {exc}"}
                )
            return _err({"path": raw.get("path"), "error": f"write_failed: {exc}"})
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(updated)
        except OSError as exc:
            return _err({"path": raw.get("path"), "error": f"write_failed: {exc}"})
    except OSError as exc:
        return _err({"path": raw.get("path"), "error": f"write_failed: {exc}"})
    if state is not None:
        # Drop the stale cache entry but keep the path "seen" — the agent
        # just edited it, so a follow-up edit this turn is legitimate.
        state.forget(path)
        state.mark_seen(path)
    payload: dict[str, Any] = {
        "path": workspace_rel(ws, path),
        "replacements": replacements,
    }
    if tier and tier != "exact":
        payload["match_tier"] = tier  # surface fuzzy matches for transparency
    return json.dumps(payload, ensure_ascii=False)


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
    "IMAGE_EXTENSIONS",
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
