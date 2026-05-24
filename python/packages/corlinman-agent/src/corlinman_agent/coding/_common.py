"""Shared internals for the builtin coding tools.

Kept private — the public surface is the ``dispatch_*`` callables in
:mod:`.files` / :mod:`.search` / :mod:`.shell`. This module holds the
``args_json`` decoder and the workspace-confinement helpers every coding
tool needs.

## Workspace confinement

Every file path a coding tool touches is resolved **inside** an agent
workspace directory. ``resolve_in_workspace`` rejects ``..`` escapes and
absolute paths that land outside the root, so ``read_file`` /
``write_file`` / ``edit_file`` cannot reach ``/etc/passwd`` or the
deployment's own source tree.

The workspace root is, in order:

1. ``$CORLINMAN_AGENT_WORKSPACE`` — explicit override;
2. ``$CORLINMAN_DATA_DIR/workspace``;
3. ``~/.corlinman/workspace``.

``run_shell`` (see :mod:`.shell`) runs *with the workspace as its cwd*
but is a real shell — it is not chrooted. That matches hermes-agent's
terminal model; the confinement guarantee is for the file tools.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

#: Largest file (chars) ``read_file`` returns in one call.
MAX_READ_CHARS: int = 60_000

#: Largest content ``write_file`` accepts in one call (bytes).
MAX_WRITE_BYTES: int = 1_000_000


class CodingArgsInvalidError(Exception):
    """Raised by per-tool arg parsers; the dispatcher folds the message
    into an ``{"error": "args_invalid: ..."}`` envelope. Same shape as
    the web tools' ``WebArgsInvalidError`` so the model sees a uniform
    failure surface."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class WorkspaceEscapeError(Exception):
    """Raised when a path argument resolves outside the workspace root."""


def decode_args(args_json: bytes | str) -> dict[str, Any]:
    """Decode a tool call's raw ``args_json`` into a dict.

    Accepts the ``ToolCallEvent.args_json`` bytes (utf-8 OpenAI
    ``function.arguments`` string) or an already-decoded string.
    """
    if isinstance(args_json, (bytes, bytearray)):
        try:
            decoded = bytes(args_json).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodingArgsInvalidError(f"args_json not utf-8: {exc}") from exc
    else:
        decoded = args_json
    try:
        raw = json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise CodingArgsInvalidError(f"args_json not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CodingArgsInvalidError(
            f"args_json must be a JSON object, got {type(raw).__name__}"
        )
    return raw


def resolve_workspace(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve (and create) the agent workspace root directory.

    ``explicit`` wins when given (the test seam). Otherwise the env
    chain ``CORLINMAN_AGENT_WORKSPACE`` → ``CORLINMAN_DATA_DIR/workspace``
    → ``~/.corlinman/workspace`` is used.
    """
    if explicit is not None:
        root = Path(explicit)
    else:
        env_ws = os.environ.get("CORLINMAN_AGENT_WORKSPACE")
        if env_ws:
            root = Path(env_ws)
        else:
            data_dir = os.environ.get("CORLINMAN_DATA_DIR")
            base = Path(data_dir) if data_dir else Path.home() / ".corlinman"
            root = base / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def resolve_in_workspace(
    workspace: Path,
    rel: str,
    *,
    for_write: bool = False,
) -> Path:
    """Resolve ``rel`` against ``workspace`` and confine it to the root.

    Raises :class:`WorkspaceEscapeError` if the resolved path escapes the
    workspace (via ``..`` or an absolute path outside it). The path need
    not exist — callers handle missing files themselves.

    **Symlink safety (S3)**: ``Path.resolve()`` follows every symlink in
    the path. For an *existing* path that targets outside the workspace
    the post-resolve ``relative_to`` check still rejects it, but for a
    *non-existing* leaf whose parent directory is a symlink pointing
    outside the workspace, ``resolve()`` would happily expand
    ``workspace/escape_dir/new_file`` to ``/tmp/attacker/new_file`` —
    inside the symlink's target, outside the workspace. The write would
    then escape.

    The fix walks each ancestor of the intended (un-resolved) path under
    the workspace root and ``lstat``-checks every component. If any
    ancestor is itself a symlink, the call is refused. Additionally,
    when ``for_write=True`` (writes / patches / edits), the immediate
    parent of the final leaf must NOT be a symlink either — too easy
    to misuse even when the symlink points back inside the workspace.

    Read-only callers may pass ``for_write=False`` (the default) to
    accept symlink-parented leafs when they exist; the existing
    ``relative_to`` post-resolve check still catches outright escapes.
    """
    if not isinstance(rel, str) or not rel.strip():
        raise CodingArgsInvalidError("missing or empty 'path'")
    candidate = Path(rel)
    # An absolute path is only allowed if it is already inside the
    # workspace; otherwise treat every path as workspace-relative.
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (workspace / candidate).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError:
        raise WorkspaceEscapeError(
            f"path {rel!r} escapes the agent workspace"
        ) from None

    # ---- S3: per-component lstat scan ---------------------------------
    # Walk the INTENDED (un-resolved) path under the workspace root and
    # lstat each component. If any ancestor is a symlink, we reject —
    # this catches the case where ``workspace/escape_dir`` is a symlink
    # to ``/tmp/attacker`` and the caller tries to write to
    # ``escape_dir/secret`` (a non-existing leaf whose ``resolve()``
    # would land inside the attacker's directory).
    if candidate.is_absolute():
        try:
            intended = candidate.relative_to(workspace)
        except ValueError:
            # Already rejected above; keep the safe fallback.
            intended = Path(candidate.name)
    else:
        intended = candidate

    cursor = workspace
    parts = [p for p in intended.parts if p not in ("", ".")]

    for idx, part in enumerate(parts):
        if part == "..":
            # ``..`` segments are already collapsed by ``resolve()``
            # above; nothing to lstat here.
            cursor = cursor.parent
            continue
        cursor = cursor / part
        try:
            st = os.lstat(cursor)
        except FileNotFoundError:
            # Past the existing prefix — every remaining component is
            # being newly created. No more symlinks to find.
            break
        except OSError:
            # Any other lstat error (permission, etc.) — fail closed.
            raise WorkspaceEscapeError(
                f"path {rel!r}: cannot stat component {part!r}"
            ) from None
        is_last = idx == len(parts) - 1
        if stat.S_ISLNK(st.st_mode):
            # The leaf itself being a symlink is fine for READS (the
            # post-resolve ``relative_to`` check above already
            # guaranteed the target lives in-workspace). For WRITES,
            # refuse — the caller almost certainly didn't intend to
            # follow the link and replace the target, and ``O_NOFOLLOW``
            # on the open would error out anyway.
            if for_write or not is_last:
                raise WorkspaceEscapeError(
                    f"path {rel!r}: ancestor / target component "
                    f"{part!r} is a symlink"
                )

    return resolved


def workspace_rel(workspace: Path, path: Path) -> str:
    """Render ``path`` as a workspace-relative string for tool output."""
    try:
        return str(path.relative_to(workspace)) or "."
    except ValueError:  # pragma: no cover — defensive
        return str(path)
