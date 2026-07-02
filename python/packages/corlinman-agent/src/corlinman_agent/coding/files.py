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
import contextlib
import difflib
import json
import mimetypes
import os
import stat
import tempfile
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
NOTEBOOK_EDIT_TOOL: str = "notebook_edit"
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

#: Max PDF pages we extract text for in a single read (DoS guard — a
#: 10k-page PDF should not blow the read budget). Page selection via the
#: ``pages`` param can still target later pages within this window.
_PDF_MAX_PAGES: int = 200

#: Per-cell output cap (chars) when rendering a Jupyter notebook so a
#: single noisy cell (e.g. a dumped DataFrame) does not swamp the read.
_NOTEBOOK_OUTPUT_CHARS: int = 2_000

#: Curly/typographic quote -> ASCII straight quote map applied symmetrically
#: to the file text and ``old_string`` at edit match time. Models routinely
#: emit smart quotes that never appear in the on-disk source.
_QUOTE_NORMALIZE: dict[int, str] = {
    0x2018: "'",  # left single quotation mark
    0x2019: "'",  # right single quotation mark
    0x201A: "'",  # single low-9 quotation mark
    0x201B: "'",  # single high-reversed-9 quotation mark
    0x2032: "'",  # prime
    0x201C: '"',  # left double quotation mark
    0x201D: '"',  # right double quotation mark
    0x201E: '"',  # double low-9 quotation mark
    0x201F: '"',  # double high-reversed-9 quotation mark
    0x2033: '"',  # double prime
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
                "so vision models can inspect them directly. PDF files return "
                "per-page extracted text (use 'pages' like '1-5' to select a "
                "range); .ipynb notebooks return numbered cells with sources "
                "and truncated outputs."
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
                    "pages": {
                        "type": "string",
                        "description": (
                            "PDF only: 1-based page range like '1-5' or a "
                            "single page '3' (default: all, capped)."
                        ),
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
                "unique). Use replace_all to replace every occurrence. "
                "Matching tolerates curly-vs-straight quotes, CRLF-vs-LF, and "
                "leading BOM; the file's original line endings and encoding are "
                "preserved on write-back."
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


def _parse_pages(spec: Any, total: int) -> list[int]:
    """Resolve a ``pages`` spec to 0-based page indices within ``total``.

    Accepts ``"1-5"`` (inclusive range), a single page ``"3"``, or
    ``None``/empty (all pages). Out-of-range bounds are clamped; an
    unparsable spec returns all pages so a malformed hint never hard-fails
    the read. 1-based on the wire, 0-based in the returned list.
    """
    if spec is None or (isinstance(spec, str) and not spec.strip()):
        return list(range(total))
    text = str(spec).strip()
    try:
        if "-" in text:
            lo_s, hi_s = text.split("-", 1)
            lo = int(lo_s) if lo_s.strip() else 1
            hi = int(hi_s) if hi_s.strip() else total
        else:
            lo = hi = int(text)
    except ValueError:
        return list(range(total))
    lo = max(1, lo)
    hi = min(total, hi)
    if hi < lo:
        return []
    return list(range(lo - 1, hi))


def _read_pdf(
    path: Path, rel: str, pages_spec: Any
) -> str | list[dict[str, Any]]:
    """Extract per-page text from a PDF via an OPTIONAL parser.

    Tries ``pypdf`` then ``pdfminer.six``; honours a ``pages`` selection
    like ``"1-5"``. When neither library is installed the PDF bytes are
    returned as a base64 ``file`` content block (the same union the image
    branch uses) so a capable provider can still ingest the document,
    plus a note explaining the fallback.
    """
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        return _err({"path": rel, "error": f"read_failed: {exc}"})

    # --- Tier 1: pypdf -------------------------------------------------
    try:
        import pypdf  # type: ignore

        try:
            reader = pypdf.PdfReader(str(path))
            total = len(reader.pages)
            indices = _parse_pages(pages_spec, total)
            capped = indices[:_PDF_MAX_PAGES]
            sections: list[str] = []
            for idx in capped:
                try:
                    txt = reader.pages[idx].extract_text() or ""
                except Exception:  # noqa: BLE001 — page extraction is best-effort
                    txt = ""
                sections.append(f"--- page {idx + 1} ---\n{txt.strip()}")
            content = "\n\n".join(sections)
            truncated = len(content) > MAX_READ_CHARS
            return json.dumps(
                {
                    "path": rel,
                    "kind": "pdf",
                    "engine": "pypdf",
                    "pages_total": total,
                    "pages_shown": [i + 1 for i in capped],
                    "content": content[:MAX_READ_CHARS],
                    "truncated": truncated or len(indices) > len(capped),
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001 — fall through to next engine
            logger.debug("pdf_pypdf_failed", error=str(exc))
    except ImportError:
        pass

    # --- Tier 2: pdfminer.six -----------------------------------------
    try:
        from pdfminer.high_level import extract_text  # type: ignore

        try:
            indices_all: list[int] | None = None
            if pages_spec is not None and str(pages_spec).strip():
                # pdfminer page_numbers is 0-based; we cannot cheaply know
                # the total first, so pass the parsed selection and let it
                # clamp. A spec referencing only later pages still works.
                try:
                    # Probe total via a bounded parse is costly; instead
                    # accept whatever indices the spec yields up to the cap.
                    spec_indices = _parse_pages(pages_spec, _PDF_MAX_PAGES)
                except ValueError:
                    spec_indices = None
                indices_all = spec_indices
            txt = extract_text(
                str(path),
                page_numbers=indices_all if indices_all else None,
                maxpages=_PDF_MAX_PAGES,
            )
            content = (txt or "").strip()
            truncated = len(content) > MAX_READ_CHARS
            return json.dumps(
                {
                    "path": rel,
                    "kind": "pdf",
                    "engine": "pdfminer",
                    "content": content[:MAX_READ_CHARS],
                    "truncated": truncated,
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001 — fall through to base64
            logger.debug("pdf_pdfminer_failed", error=str(exc))
    except ImportError:
        pass

    # --- Fallback: no parser — return base64 file content block --------
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    data_url = f"data:application/pdf;base64,{b64}"
    return [
        {
            "type": "file",
            "file": {"filename": path.name, "file_data": data_url},
        },
        {
            "type": "text",
            "text": (
                "[note] No PDF text-extraction library (pypdf / pdfminer.six) "
                "is installed; returned the raw PDF as a base64 file block for "
                "providers that accept documents. Install pypdf for per-page "
                "text extraction."
            ),
        },
    ]


def _read_notebook(path: Path, rel: str) -> str:
    """Render a Jupyter ``.ipynb`` as numbered, readable cells.

    Each cell becomes ``[cell N] <type>`` followed by its source and, for
    code cells, truncated text representations of any stdout/result/error
    outputs. Returns the standard JSON read envelope (str). Falls back to
    a plain text read if the file is not valid notebook JSON.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _err({"path": rel, "error": f"read_failed: {exc}"})
    try:
        nb = json.loads(text)
    except json.JSONDecodeError:
        return _err({"path": rel, "error": "ipynb_invalid_json"})
    if not isinstance(nb, dict):
        return _err({"path": rel, "error": "ipynb_invalid_json"})

    cells = nb.get("cells")
    if not isinstance(cells, list):
        return _err({"path": rel, "error": "ipynb_no_cells"})

    def _join(src: Any) -> str:
        if isinstance(src, list):
            return "".join(str(s) for s in src)
        return str(src) if src is not None else ""

    blocks: list[str] = []
    for n, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            continue
        ctype = str(cell.get("cell_type", "unknown"))
        source = _join(cell.get("source", "")).rstrip("\n")
        header = f"[cell {n}] {ctype}"
        parts = [header, source] if source else [header]
        outputs = cell.get("outputs")
        if ctype == "code" and isinstance(outputs, list):
            for out in outputs:
                if not isinstance(out, dict):
                    continue
                otype = out.get("output_type")
                rendered = ""
                if otype == "stream":
                    rendered = _join(out.get("text", ""))
                elif otype in {"execute_result", "display_data"}:
                    data = out.get("data", {})
                    if isinstance(data, dict):
                        rendered = _join(data.get("text/plain", ""))
                elif otype == "error":
                    tb = out.get("traceback", [])
                    if isinstance(tb, list):
                        rendered = "\n".join(str(t) for t in tb)
                    rendered = (
                        f"{out.get('ename', 'Error')}: "
                        f"{out.get('evalue', '')}\n{rendered}"
                    )
                rendered = rendered.strip()
                if rendered:
                    if len(rendered) > _NOTEBOOK_OUTPUT_CHARS:
                        rendered = (
                            rendered[:_NOTEBOOK_OUTPUT_CHARS]
                            + "\n... [output truncated]"
                        )
                    parts.append(f"  [out] {rendered}")
        blocks.append("\n".join(parts))

    content = "\n\n".join(blocks)
    truncated = len(content) > MAX_READ_CHARS
    return json.dumps(
        {
            "path": rel,
            "kind": "notebook",
            "cells": len(blocks),
            "content": content[:MAX_READ_CHARS],
            "truncated": truncated,
        },
        ensure_ascii=False,
    )


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

    suffix = path.suffix.lower()

    # --- PDF files: per-page text extraction (optional libs) ------------
    if suffix == ".pdf":
        return _read_pdf(path, workspace_rel(ws, path), raw.get("pages"))

    # --- Jupyter notebooks: numbered cells with truncated outputs -------
    if suffix == ".ipynb":
        return _read_notebook(path, workspace_rel(ws, path))

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
        if complete_lines == 0:
            # A single line longer than MAX_READ_CHARS leaves no newline in
            # the slice, so ``offset + 0 == offset`` would point the model
            # back at the same head — an infinite paging loop (BUG-05).
            # Emit a clipped representation of just that one line with an
            # explicit marker (mirroring ``search._clip_line``) and still
            # advance the cursor past it so paging always progresses.
            clipped = numbered[:MAX_READ_CHARS] + " …(line truncated)"
            next_offset = offset + 1
            result["content"] = clipped
            result["shown"] = [offset, offset]
        else:
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
        # Capture prior content (best-effort) so the result can carry a
        # diff of what the overwrite changed. A read failure here is
        # non-fatal — we just skip the snippet.
        prior = ""
        if existed:
            try:
                prior = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                prior = ""
        # Atomic write (claude-code parity, ABSORB_MATRIX Dim 4): stage the
        # bytes into a unique sibling temp file, fsync, then ``os.replace`` onto
        # the target — a crash or partial write can never leave a truncated
        # file. ``os.replace`` never follows a symlink at ``path``; we also
        # refuse a symlinked target up front, preserving the O_NOFOLLOW
        # workspace-escape posture of the old O_TRUNC path (a symlinked *parent*
        # was already rejected by ``resolve_in_workspace`` above).
        if path.is_symlink():
            return _err(
                {
                    "path": raw.get("path"),
                    "error": "workspace_escape: refusing to write through a symlink",
                }
            )
        # Preserve an existing file's mode (e.g. an executable bit); a new file
        # gets 0644 like the old O_CREAT path.
        mode = 0o644
        if existed:
            with contextlib.suppress(OSError):
                mode = stat.S_IMODE(os.stat(path).st_mode)
        try:
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
            )
        except OSError as exc:
            return _err({"path": raw.get("path"), "error": f"write_failed: {exc}"})
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_path, mode)
            os.replace(tmp_path, path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            return _err({"path": raw.get("path"), "error": f"write_failed: {exc}"})
    except OSError as exc:
        return _err({"path": raw.get("path"), "error": f"write_failed: {exc}"})
    if state is not None:
        # Invalidate the stale read cache but mark the path seen — the
        # agent authored these bytes, so it may edit the file this turn
        # without a redundant read first.
        state.forget(path)
        state.mark_seen(path)
    payload: dict[str, Any] = {
        "path": workspace_rel(ws, path),
        "bytes": len(content.encode("utf-8")),
        "action": "overwritten" if existed else "created",
    }
    # Changed-region diff so the caller sees what the write did. For a
    # new file the diff is the full content as additions.
    snippet = _diff_snippet(
        prior.replace("\r\n", "\n").replace("\r", "\n"),
        content.replace("\r\n", "\n").replace("\r", "\n"),
        workspace_rel(ws, path),
    )
    if snippet:
        payload["diff"] = snippet
    return json.dumps(payload, ensure_ascii=False)


def _atomic_replace_write(
    path: Path, text: str, *, mode: int = 0o644, encoding: str = "utf-8"
) -> None:
    """Stage ``text`` into a unique temp sibling, fsync, then ``os.replace`` onto
    ``path`` (atomic). The caller must have refused symlinks + resolved the path
    for-write. Raises ``OSError`` on failure (temp cleaned up)."""
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def notebook_edit_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": NOTEBOOK_EDIT_TOOL,
            "description": (
                "Edit a Jupyter notebook (.ipynb): replace a cell's source, "
                "insert a new cell before an index, or delete a cell. Cells are "
                "addressed by 0-based index; the notebook is rewritten atomically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative .ipynb path.",
                    },
                    "cell_number": {
                        "type": "integer",
                        "description": "0-based cell index.",
                    },
                    "new_source": {
                        "type": "string",
                        "description": "New cell source (required for replace / insert).",
                    },
                    "edit_mode": {
                        "type": "string",
                        "enum": ["replace", "insert", "delete"],
                        "description": "replace (default) / insert before cell_number / delete.",
                    },
                    "cell_type": {
                        "type": "string",
                        "enum": ["code", "markdown"],
                        "description": "Cell type for insert (default code).",
                    },
                },
                "required": ["path", "cell_number"],
                "additionalProperties": False,
            },
        },
    }


def _nb_source_lines(text: str) -> list[str]:
    """Split ``text`` into the line-list nbformat stores as a cell ``source``."""
    return text.splitlines(keepends=True)


def dispatch_notebook_edit(
    *,
    args_json: bytes | str,
    workspace: Path | None = None,
    state: FileState | None = None,
) -> str:
    """Edit a ``.ipynb`` cell (replace / insert / delete). JSON envelope; never raises."""
    try:
        raw = decode_args(args_json)
        ws = resolve_workspace(workspace)
        path = resolve_in_workspace(ws, raw.get("path"), for_write=True)
    except CodingArgsInvalidError as exc:
        return _err({"error": f"args_invalid: {exc.message}"})
    except WorkspaceEscapeError as exc:
        return _err({"error": f"workspace_escape: {exc}"})

    edit_mode = str(raw.get("edit_mode") or "replace")
    if edit_mode not in ("replace", "insert", "delete"):
        return _err({"error": "args_invalid: edit_mode must be replace|insert|delete"})
    idx = raw.get("cell_number")
    if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
        return _err({"error": "args_invalid: 'cell_number' must be a non-negative integer"})
    if path.is_symlink():
        return _err({"error": "workspace_escape: refusing to write through a symlink"})
    if not path.exists():
        return _err({"error": f"not_found: {raw.get('path')}"})

    try:
        nb = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _err({"error": f"notebook_read_failed: {exc}"})
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        return _err({"error": "invalid_notebook: missing 'cells' array"})
    cells = nb["cells"]

    if edit_mode == "insert":
        if idx > len(cells):
            return _err({"error": f"cell_index_out_of_range: {idx} > {len(cells)}"})
        ct = str(raw.get("cell_type") or "code")
        if ct not in ("code", "markdown"):
            return _err({"error": "args_invalid: cell_type must be code|markdown"})
        src = raw.get("new_source")
        if not isinstance(src, str):
            return _err({"error": "args_invalid: 'new_source' must be a string for insert"})
        new_cell: dict[str, Any] = {
            "cell_type": ct,
            "metadata": {},
            "source": _nb_source_lines(src),
        }
        if ct == "code":
            new_cell["outputs"] = []
            new_cell["execution_count"] = None
        cells.insert(idx, new_cell)
        action = "inserted"
    else:
        if idx >= len(cells):
            return _err({"error": f"cell_index_out_of_range: {idx} >= {len(cells)}"})
        if edit_mode == "delete":
            cells.pop(idx)
            action = "deleted"
        else:  # replace
            src = raw.get("new_source")
            if not isinstance(src, str):
                return _err({"error": "args_invalid: 'new_source' must be a string for replace"})
            cell = cells[idx]
            if not isinstance(cell, dict):
                return _err({"error": "invalid_notebook: cell is not an object"})
            cell["source"] = _nb_source_lines(src)
            # A code cell whose source changed has stale outputs — clear them.
            if cell.get("cell_type") == "code":
                cell["outputs"] = []
                cell["execution_count"] = None
            action = "replaced"

    out_text = json.dumps(nb, indent=1, ensure_ascii=False) + "\n"
    if len(out_text.encode("utf-8")) > MAX_WRITE_BYTES:
        return _err({"error": f"content_too_large: cap is {MAX_WRITE_BYTES} bytes"})
    try:
        file_mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        file_mode = 0o644
    try:
        _atomic_replace_write(path, out_text, mode=file_mode)
    except OSError as exc:
        return _err({"error": f"write_failed: {exc}"})

    if state is not None:
        state.forget(path)
        state.mark_seen(path)
    return json.dumps(
        {
            "path": workspace_rel(ws, path),
            "action": action,
            "cell_number": idx,
            "cells_total": len(cells),
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


#: Max chars of unified-diff / changed-region snippet attached to an
#: edit/write result so the caller sees what changed without re-reading.
_DIFF_SNIPPET_CHARS: int = 4_000


def _normalize_for_match(s: str) -> str:
    """Canonicalize text for tolerant edit matching.

    Strips a leading BOM, folds CRLF/CR -> LF, and maps curly/typographic
    quotes to their ASCII equivalents. Applied SYMMETRICALLY to the file
    text and ``old_string`` so a smart-quote or CRLF mismatch in the
    model's argument still matches the on-disk source. Offsets in the
    normalized string are NOT 1:1 with the original (BOM/CRLF differ), so
    matching here is line-aligned via :func:`_fuzzy_line_matches` rather
    than substring char offsets.
    """
    if s.startswith("﻿"):
        s = s[1:]
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s.translate(_QUOTE_NORMALIZE)


def _detect_eol(text: str) -> str:
    """Return the dominant line ending in ``text`` (``\\r\\n`` or ``\\n``)."""
    crlf = text.count("\r\n")
    # bare LF = total LF minus those that are part of a CRLF pair
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _apply_eol_bom(text_lf: str, eol: str, bom: str) -> str:
    """Round-trip ``text_lf`` (LF-normalized) back to ``eol`` + leading ``bom``."""
    out = text_lf.replace("\n", eol) if eol != "\n" else text_lf
    return bom + out


def _diff_snippet(before: str, after: str, rel: str) -> str:
    """Compact unified diff of ``before`` -> ``after``, truncated for the result.

    Operates on LF-normalized text so a CRLF file does not produce a diff
    where every line looks changed. Returns ``""`` when nothing differs.
    """
    b_lines = before.split("\n")
    a_lines = after.split("\n")
    diff = difflib.unified_diff(
        b_lines, a_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="", n=2
    )
    text = "\n".join(diff)
    if len(text) > _DIFF_SNIPPET_CHARS:
        text = text[:_DIFF_SNIPPET_CHARS] + "\n... [diff truncated]"
    return text


def _block_anchor_matches(text: str, old: str) -> list[tuple[int, int]]:
    """Tier-4: match a multi-line block by its first+last line only.

    Anchors on the (stripped) first and last lines of ``old`` and matches
    any line-aligned span in ``text`` that begins/ends with those anchors,
    tolerating interior drift. Returns char-offset spans in ``text``.
    Single-line ``old`` is rejected (no interior to drift). The caller
    only accepts the result when exactly one span is found — a non-unique
    anchored span is too risky to auto-apply.
    """
    old_lines = old.split("\n")
    if len(old_lines) < 2:
        return []
    first = old_lines[0].strip()
    last = old_lines[-1].strip()
    if not first or not last:
        return []
    text_lines = text.split("\n")
    stripped = [ln.strip() for ln in text_lines]
    line_spans = _line_span_offsets(text)
    out: list[tuple[int, int]] = []
    n = len(text_lines)
    for i in range(n):
        if stripped[i] != first:
            continue
        # Find the nearest later line equal to the last anchor; the block
        # must be at least as long as old (>= 2 lines) so j > i.
        for j in range(i + 1, n):
            if stripped[j] == last:
                out.append((line_spans[i][0], line_spans[j][1]))
                break
    return out


def dispatch_edit_file(
    *,
    args_json: bytes | str,
    workspace: Path | None = None,
    state: FileState | None = None,
) -> str:
    """Replace an exact string in a workspace file. JSON envelope.

    Match cascade:
    1. exact substring match (today's behaviour);
    2. line-aligned ``rstrip`` match — recovers from trailing-whitespace
       drift in the model's ``old_string``;
    3. line-aligned ``strip`` match — recovers from indentation drift;
    4. block-anchor match — anchors the first+last line and tolerates
       interior drift, but only when the anchored span is UNIQUE.

    Before matching, the file text and ``old_string`` are normalized
    symmetrically: a leading BOM is stripped, CRLF/CR are folded to LF,
    and curly quotes are mapped to ASCII. The file's ORIGINAL encoding,
    line ending, and BOM are remembered and round-tripped on write-back,
    so a CRLF file stays CRLF.

    Each tier is consulted in order; the first tier with **any** matches
    wins. The uniqueness rule still applies: >1 match without
    ``replace_all`` is rejected. Fuzzy tiers only run for multi-line
    ``old_string`` — single-line edits stay on the exact path.

    The result envelope carries a compact unified-diff ``snippet`` of the
    changed region so the caller sees what changed without re-reading.

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
        raw_bytes = path.read_bytes()
    except OSError as exc:
        return _err({"path": raw.get("path"), "error": f"read_failed: {exc}"})

    # Detect encoding + BOM. UTF-16 files carry a 2-byte BOM; UTF-8 may
    # carry a 3-byte BOM. We decode losslessly, remember what we stripped,
    # and re-apply it on write so the file's on-disk shape is preserved.
    encoding = "utf-8"
    bom = ""
    if raw_bytes.startswith(b"\xff\xfe") or raw_bytes.startswith(b"\xfe\xff"):
        encoding = "utf-16"  # codec consumes the BOM and infers endianness
    try:
        decoded = raw_bytes.decode(encoding)
    except UnicodeDecodeError as exc:
        return _err({"path": raw.get("path"), "error": f"read_failed: {exc}"})
    if encoding == "utf-8" and decoded.startswith("﻿"):
        bom = "﻿"
        decoded = decoded[1:]

    # Original line ending, decided before we fold to LF.
    eol = _detect_eol(decoded)
    # ``text`` is the LF-normalized working copy we mutate; EOL/BOM are
    # re-applied at write time. We keep ``old``/``new`` LF-normalized too
    # so an exact match still behaves byte-identically on LF files.
    text = decoded.replace("\r\n", "\n").replace("\r", "\n")
    before_text = text
    old_lf = old.replace("\r\n", "\n").replace("\r", "\n")
    new_lf = new.replace("\r\n", "\n").replace("\r", "\n")

    replace_all = bool(raw.get("replace_all", False))

    # Normalized views for tolerant matching (quotes folded, BOM gone).
    match_text = _normalize_for_match(text)
    old_norm = _normalize_for_match(old_lf)

    updated: str | None = None
    tier: str | None = None
    replacements = 0

    # --- Tier 1: exact substring (on LF text) ----------------------------
    exact_count = text.count(old_lf)
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
        updated = (
            text.replace(old_lf, new_lf)
            if replace_all
            else text.replace(old_lf, new_lf, 1)
        )
        replacements = exact_count if replace_all else 1
        tier = "exact"

    # --- Tier 1b: normalized substring (quotes/BOM folded) ---------------
    # Only when the raw exact match failed but the normalized forms match —
    # recovers from curly-quote / BOM drift in the model's old_string while
    # still letting us splice into the ORIGINAL ``text`` via the normalized
    # offset (BOM already stripped from both, so offsets align 1:1 here).
    if updated is None and old_norm:
        norm_count = match_text.count(old_norm)
        if norm_count > 0:
            if norm_count > 1 and not replace_all:
                return _err(
                    {
                        "path": raw.get("path"),
                        "error": (
                            f"old_string_not_unique: {norm_count} matches "
                            "(after quote/BOM normalization) — add context or "
                            "set replace_all=true"
                        ),
                    }
                )
            # match_text and text are the same length (only char-for-char
            # quote substitutions + identical BOM-stripping), so offsets in
            # match_text map directly onto text.
            spans: list[tuple[int, int]] = []
            start = 0
            while True:
                idx = match_text.find(old_norm, start)
                if idx == -1:
                    break
                spans.append((idx, idx + len(old_norm)))
                start = idx + len(old_norm)
                if not replace_all:
                    break
            updated = text
            for s, e in sorted(spans, reverse=True):
                updated = updated[:s] + new_lf + updated[e:]
            replacements = len(spans)
            tier = "normalized"

    # --- Tier 2/3: line-aligned fuzzy for multi-line old_string ----------
    if updated is None:
        for tier_name, transform in (
            ("rstrip", str.rstrip),
            ("strip", str.strip),
        ):
            # Match against the normalized text so quote/BOM drift is also
            # tolerated here; offsets map 1:1 back onto ``text``.
            spans = _fuzzy_line_matches(match_text, old_norm, transform)
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
            if replace_all:
                updated = text
                for start, end in sorted(spans, reverse=True):
                    updated = updated[:start] + new_lf + updated[end:]
                replacements = len(spans)
            else:
                start, end = spans[0]
                updated = text[:start] + new_lf + text[end:]
                replacements = 1
            tier = tier_name
            break

    # --- Tier 4: block-anchor (first+last line, UNIQUE only) -------------
    if updated is None:
        anchored = _block_anchor_matches(match_text, old_norm)
        if len(anchored) == 1:
            start, end = anchored[0]
            updated = text[:start] + new_lf + text[end:]
            replacements = 1
            tier = "block-anchor"
        elif len(anchored) > 1:
            return _err(
                {
                    "path": raw.get("path"),
                    "error": (
                        f"old_string_not_unique: {len(anchored)} block-anchor "
                        "matches — the first/last line anchor is ambiguous, "
                        "add more distinctive context"
                    ),
                }
            )

    if updated is None:
        return _err({"path": raw.get("path"), "error": "old_string_not_found"})

    # Round-trip the original encoding, BOM, and line ending.
    out_text = _apply_eol_bom(updated, eol, bom)
    snippet = _diff_snippet(before_text, updated, workspace_rel(ws, path))

    # Atomic edit (ABSORB_MATRIX Dim 4): stage into a sibling temp file, fsync,
    # then ``os.replace`` so a crash mid-write can't corrupt the file the agent
    # just read. Symlink refusal + os.replace-not-following preserve the old
    # O_NOFOLLOW workspace-escape posture; the existing file's mode is kept.
    if path.is_symlink():
        return _err(
            {
                "path": raw.get("path"),
                "error": "workspace_escape: refusing to write through a symlink",
            }
        )
    try:
        edit_mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        edit_mode = 0o644
    try:
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
    except OSError as exc:
        return _err({"path": raw.get("path"), "error": f"write_failed: {exc}"})
    tmp_path = Path(tmp_name)
    try:
        # ``newline=""`` so Python does NOT translate our explicit EOL —
        # ``out_text`` already carries the file's original line endings.
        with os.fdopen(tmp_fd, "w", encoding=encoding, newline="") as fh:
            fh.write(out_text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, edit_mode)
        os.replace(tmp_path, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
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
    if snippet:
        payload["diff"] = snippet  # changed-region unified diff for the caller
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
    "NOTEBOOK_EDIT_TOOL",
    "READ_FILE_TOOL",
    "WRITE_FILE_TOOL",
    "dispatch_edit_file",
    "dispatch_list_files",
    "dispatch_notebook_edit",
    "dispatch_read_file",
    "dispatch_write_file",
    "edit_file_tool_schema",
    "list_files_tool_schema",
    "notebook_edit_tool_schema",
    "read_file_tool_schema",
    "write_file_tool_schema",
]
