"""Builtin ``apply_patch`` tool — apply a multi-file textual patch.

Implements the Codex / opencode patch envelope. Codex models are
natively trained to emit it, so giving the agent a tool that consumes it
lets the model make many edits across files in one call::

    *** Begin Patch
    *** Add File: path/to/new.py
    +line one
    +line two
    *** Update File: path/to/existing.py
    *** Move to: path/to/renamed.py
    @@ optional context anchor
     unchanged line
    -removed line
    +added line
    *** Delete File: path/to/old.py
    *** End Patch

All paths are workspace-confined. The patch is fully parsed and every
update hunk is located before any file is written, so a malformed hunk
fails the whole patch instead of leaving a half-applied mess.

Line matching for update hunks is multi-pass (exact → right-stripped →
stripped) so minor whitespace drift in the model's context lines does
not break an otherwise-valid patch.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
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

APPLY_PATCH_TOOL: str = "apply_patch"

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_ADD = "*** Add File: "
_UPDATE = "*** Update File: "
_DELETE = "*** Delete File: "
_MOVE = "*** Move to: "


class PatchParseError(Exception):
    """Raised when the patch envelope is malformed."""


@dataclass
class _Hunk:
    """One contiguous edit block of an Update File section."""

    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)


@dataclass
class _AddFile:
    path: str
    content: str


@dataclass
class _DeleteFile:
    path: str


@dataclass
class _UpdateFile:
    path: str
    move_to: str | None
    hunks: list[_Hunk]


_Op = _AddFile | _DeleteFile | _UpdateFile


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_patch(text: str) -> list[_Op]:
    """Parse a patch envelope into a list of file operations."""
    lines = text.splitlines()
    # Tolerate a heredoc / fenced wrapper around the envelope.
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == _BEGIN)
    except StopIteration:
        raise PatchParseError("missing '*** Begin Patch'") from None
    try:
        end = next(
            i for i in range(start + 1, len(lines)) if lines[i].strip() == _END
        )
    except StopIteration:
        raise PatchParseError("missing '*** End Patch'") from None

    body = lines[start + 1 : end]
    ops: list[_Op] = []
    i = 0
    while i < len(body):
        line = body[i]
        if line.startswith(_ADD):
            path = line[len(_ADD):].strip()
            content_lines: list[str] = []
            i += 1
            while i < len(body) and not body[i].startswith("*** "):
                row = body[i]
                if row.startswith("+"):
                    content_lines.append(row[1:])
                elif row.strip() == "":
                    content_lines.append("")
                else:
                    raise PatchParseError(
                        f"Add File '{path}': content lines must start with '+'"
                    )
                i += 1
            ops.append(_AddFile(path, "\n".join(content_lines)))
        elif line.startswith(_DELETE):
            ops.append(_DeleteFile(line[len(_DELETE):].strip()))
            i += 1
        elif line.startswith(_UPDATE):
            path = line[len(_UPDATE):].strip()
            i += 1
            move_to: str | None = None
            if i < len(body) and body[i].startswith(_MOVE):
                move_to = body[i][len(_MOVE):].strip()
                i += 1
            hunks: list[_Hunk] = []
            current = _Hunk()
            while i < len(body) and not body[i].startswith("*** "):
                row = body[i]
                if row.startswith("@@"):
                    # Section anchor — start a fresh hunk if the current
                    # one already has content.
                    if current.old or current.new:
                        hunks.append(current)
                        current = _Hunk()
                elif row.startswith("-"):
                    current.old.append(row[1:])
                elif row.startswith("+"):
                    current.new.append(row[1:])
                elif row.startswith(" "):
                    current.old.append(row[1:])
                    current.new.append(row[1:])
                elif row == "":
                    current.old.append("")
                    current.new.append("")
                else:
                    raise PatchParseError(
                        f"Update File '{path}': bad hunk line {row!r}"
                    )
                i += 1
            if current.old or current.new:
                hunks.append(current)
            if not hunks:
                raise PatchParseError(f"Update File '{path}': no hunks")
            ops.append(_UpdateFile(path, move_to, hunks))
        elif line.strip() == "":
            i += 1
        else:
            raise PatchParseError(f"unexpected patch line: {line!r}")
    if not ops:
        raise PatchParseError("patch envelope is empty")
    return ops


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _locate(haystack: list[str], needle: list[str]) -> int:
    """Find ``needle`` as a contiguous block in ``haystack``.

    Multi-pass: exact, then right-stripped, then fully stripped — so a
    little whitespace drift in the model's context lines is tolerated.
    Returns the start index, or ``-1`` if not found.
    """
    if not needle:
        return -1
    for transform in (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip()):
        h = [transform(x) for x in haystack]
        n = [transform(x) for x in needle]
        for start in range(0, len(h) - len(n) + 1):
            if h[start : start + len(n)] == n:
                return start
    return -1


def _apply_update(text: str, upd: _UpdateFile) -> str:
    """Apply every hunk of an Update File to ``text``. Raises
    :class:`PatchParseError` if a hunk cannot be located."""
    lines = text.split("\n")
    for idx, hunk in enumerate(upd.hunks):
        if not hunk.old:
            # Pure insertion with no anchor — append at end of file.
            lines.extend(hunk.new)
            continue
        at = _locate(lines, hunk.old)
        if at < 0:
            preview = " / ".join(hunk.old[:3])
            raise PatchParseError(
                f"Update File '{upd.path}': hunk {idx + 1} context not "
                f"found (looking for: {preview!r})"
            )
        lines[at : at + len(hunk.old)] = hunk.new
    return "\n".join(lines)


def apply_patch_tool_schema() -> dict[str, Any]:
    """OpenAI tool descriptor for ``apply_patch``."""
    return {
        "type": "function",
        "function": {
            "name": APPLY_PATCH_TOOL,
            "description": (
                "Apply a multi-file patch in the standard patch envelope "
                "(*** Begin Patch / *** Add File / *** Update File with @@ "
                "hunks and +/- lines / *** Delete File / *** End Patch). "
                "Use this to make several edits across files in one call; "
                "for a single small change edit_file is simpler. All paths "
                "are workspace-relative."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "The full patch envelope text.",
                    }
                },
                "required": ["patch"],
                "additionalProperties": False,
            },
        },
    }


def dispatch_apply_patch(
    *, args_json: bytes | str, workspace: Path | None = None
) -> str:
    """Apply a patch envelope. JSON envelope; never raises."""
    try:
        raw = decode_args(args_json)
    except CodingArgsInvalidError as exc:
        return json.dumps({"error": f"args_invalid: {exc.message}"})
    patch_text = raw.get("patch")
    if not isinstance(patch_text, str) or not patch_text.strip():
        return json.dumps({"error": "args_invalid: missing or empty 'patch'"})

    ws = resolve_workspace(workspace)

    # --- parse + resolve + stage every op before writing anything -------
    try:
        ops = _parse_patch(patch_text)
    except PatchParseError as exc:
        return json.dumps({"error": f"patch_parse_error: {exc}"})

    # S3: every patch op writes — refuse symlinked ancestors and leaves.
    staged: list[tuple[str, Path, str | None]] = []  # (action, path, content)
    try:
        for op in ops:
            if isinstance(op, _AddFile):
                p = resolve_in_workspace(ws, op.path, for_write=True)
                staged.append(("add", p, op.content))
            elif isinstance(op, _DeleteFile):
                p = resolve_in_workspace(ws, op.path, for_write=True)
                staged.append(("delete", p, None))
            else:  # _UpdateFile
                p = resolve_in_workspace(ws, op.path, for_write=True)
                if not p.is_file():
                    return json.dumps(
                        {"error": f"update_target_missing: {op.path}"}
                    )
                new_text = _apply_update(p.read_text(encoding="utf-8"), op)
                dest = (
                    resolve_in_workspace(ws, op.move_to, for_write=True)
                    if op.move_to
                    else p
                )
                staged.append(("update", dest, new_text))
                if op.move_to:
                    staged.append(("delete", p, None))
    except WorkspaceEscapeError as exc:
        return json.dumps({"error": f"workspace_escape: {exc}"})
    except PatchParseError as exc:
        return json.dumps({"error": f"patch_apply_error: {exc}"})
    except OSError as exc:
        return json.dumps({"error": f"patch_io_error: {exc}"})

    # --- commit ---------------------------------------------------------
    # S3: use O_NOFOLLOW on every write so a leaf symlink swapped in
    # between the resolve_in_workspace pre-check and the open (TOCTOU)
    # is refused at the syscall layer.
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    changed: list[str] = []
    try:
        for action, path, content in staged:
            if action == "delete":
                if path.exists():
                    path.unlink()
                    changed.append(f"deleted {workspace_rel(ws, path)}")
            else:  # add / update
                path.parent.mkdir(parents=True, exist_ok=True)
                assert content is not None
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | o_nofollow
                fd = os.open(path, flags, 0o644)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(content)
                except OSError as exc:
                    return json.dumps(
                        {
                            "error": f"patch_write_failed: {exc}",
                            "partial": changed,
                        }
                    )
                verb = "added" if action == "add" else "updated"
                changed.append(f"{verb} {workspace_rel(ws, path)}")
    except OSError as exc:
        msg = str(exc)
        if "ELOOP" in msg or "Too many levels of symbolic links" in msg or getattr(exc, "errno", None) == 62:
            return json.dumps(
                {"error": f"workspace_escape: O_NOFOLLOW refused: {exc}", "partial": changed}
            )
        return json.dumps(
            {"error": f"patch_write_failed: {exc}", "partial": changed}
        )

    logger.info("agent.apply_patch.applied", files=len(changed))
    return json.dumps({"applied": True, "changes": changed}, ensure_ascii=False)


__all__ = [
    "APPLY_PATCH_TOOL",
    "PatchParseError",
    "apply_patch_tool_schema",
    "dispatch_apply_patch",
]
