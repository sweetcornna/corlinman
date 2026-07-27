"""``render_document`` builtin tool — Markdown in, verified document out.

The tool-shaped successor to the v1.12.3 "messy PDF" fix. Historically the
reliable pipeline (``corlinman-md2pdf``) was only reachable through an
always-on skill that told the model, every turn, how to shell out to it and
what NOT to do (hand-roll PDF bytes, use reportlab, wrong Chrome flags).
This tool replaces that standing prose with an interface whose *shape*
prevents the failure modes:

* the model hands over **Markdown source only** — there is no way to pass
  raw PDF bytes, an engine choice, or renderer flags;
* the output path is confined to the agent workspace and always gets the
  format's extension;
* rendering runs the vetted CJK-correct :mod:`.doc_render` pipeline, which
  validates the ``%PDF`` magic bytes before reporting success;
* the success envelope returns the workspace-relative path, ready to be
  passed to ``send_attachment``.

Wire contract (identical to the other builtin tools):

* :data:`RENDER_DOCUMENT_TOOL` — the wire-stable tool name.
* :func:`render_document_tool_schema` — the OpenAI tool descriptor.
* :func:`dispatch_render_document` — async dispatcher, ``args_json -> str``,
  never raises.

Success envelope::

    {"path": "report.pdf", "format": "pdf", "bytes": 12345,
     "title": "...", "hint": "call send_attachment ..."}

Failure envelope (well-formed so the loop continues)::

    {"error": "args_invalid: ..."} / {"error": "render_failed: ..."}
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import structlog
from corlinman_agent.coding import resolve_in_workspace, resolve_workspace
from corlinman_agent.coding._common import (
    CodingArgsInvalidError,
    WorkspaceEscapeError,
    decode_args,
    workspace_rel,
)

from corlinman_server.tools.doc_render import render_markdown_text_to_pdf

logger = structlog.get_logger(__name__)

#: Wire-stable tool name.
RENDER_DOCUMENT_TOOL: str = "render_document"

#: Output formats the underlying pipeline supports. ``doc_render`` renders
#: Markdown → HTML → PDF; widening this tuple (plus a renderer branch in
#: ``_render``) is the way to expose a new format.
SUPPORTED_FORMATS: tuple[str, ...] = ("pdf",)

#: Largest Markdown source accepted in one call (bytes) — same cap as
#: ``write_file`` so a document the model could write to disk can also be
#: rendered directly.
MAX_CONTENT_BYTES: int = 1_000_000

#: Wall-clock budget for one render. The Chrome engine inside
#: ``doc_render`` already caps each subprocess at 120s; this outer bound
#: also covers the WeasyPrint path so a stuck render can never wedge the
#: dispatch.
RENDER_TIMEOUT_SECS: float = 180.0

_FILENAME_SAFE_RE = re.compile(r"[^\w一-鿿.-]+")


def render_document_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped tool descriptor for ``render_document``."""
    return {
        "type": "function",
        "function": {
            "name": RENDER_DOCUMENT_TOOL,
            "description": (
                "Render Markdown source into a polished, correctly typeset "
                "document file (currently PDF) in the agent workspace. "
                "Handles CJK fonts, tables, code blocks and page layout — "
                "ALWAYS use this to produce a PDF/report/document file; "
                "never construct PDF bytes yourself or shell out to a "
                "browser/PDF library. Write the document body as normal "
                "Markdown (headings, lists, tables, bold, code, quotes; "
                "Chinese text verbatim — no escaping or extra spaces). On "
                "success it returns the output file's workspace path — "
                "deliver it to the user with send_attachment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "The full document body as Markdown source "
                            "(NOT a file path, NOT PDF/base64 bytes)."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Optional document title (defaults to the "
                            "first `# heading` in the content)."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional output path relative to the agent "
                            "workspace (e.g. `report.pdf`). The format's "
                            "extension is appended when missing. Defaults "
                            "to a name derived from the title."
                        ),
                    },
                    "format": {
                        "type": "string",
                        "enum": list(SUPPORTED_FORMATS),
                        "description": "Output format. Default: pdf.",
                    },
                },
                "required": ["content"],
                "additionalProperties": False,
            },
        },
    }


def _err(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _default_filename(content: str, title: str, fmt: str) -> str:
    """Derive an output filename from the explicit title or the first H1."""
    name = title.strip()
    if not name:
        m = re.search(r"^#\s+(.+)$", content, flags=re.MULTILINE)
        name = m.group(1).strip() if m else ""
    name = _FILENAME_SAFE_RE.sub("_", name).strip("._-")
    # Keep filenames short and predictable; the title still renders in full
    # inside the document itself.
    if len(name) > 60:
        name = name[:60].rstrip("._-")
    return f"{name or 'document'}.{fmt}"


def _parse_args(args_json: bytes | str) -> tuple[str, str, str, str]:
    """Validate args → ``(content, title, rel_path, fmt)``.

    Raises :class:`CodingArgsInvalidError` on any shape violation. The
    checks encode the v1.12.3 lessons as *rejections* instead of prose:
    no raw PDF bytes, no base64 blobs, no foreign extensions.
    """
    raw = decode_args(args_json)

    content = raw.get("content")
    if not isinstance(content, str) or not content.strip():
        raise CodingArgsInvalidError("missing or empty 'content' (Markdown source)")
    if content.lstrip().startswith("%PDF"):
        raise CodingArgsInvalidError(
            "'content' looks like raw PDF bytes — pass the Markdown source; "
            "this tool renders the PDF for you"
        )
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise CodingArgsInvalidError(
            f"'content' too large: cap is {MAX_CONTENT_BYTES} bytes"
        )

    title = raw.get("title", "")
    if not isinstance(title, str):
        raise CodingArgsInvalidError("'title' must be a string")

    fmt = raw.get("format", SUPPORTED_FORMATS[0])
    if not isinstance(fmt, str) or fmt.strip().lower() not in SUPPORTED_FORMATS:
        raise CodingArgsInvalidError(
            f"'format' must be one of {list(SUPPORTED_FORMATS)}"
        )
    fmt = fmt.strip().lower()

    rel_path = raw.get("path")
    if rel_path is None or (isinstance(rel_path, str) and not rel_path.strip()):
        rel_path = _default_filename(content, title, fmt)
    if not isinstance(rel_path, str):
        raise CodingArgsInvalidError("'path' must be a string")
    rel_path = rel_path.strip()
    # Force the format's extension so the artifact is always what the
    # envelope claims (a ``.txt`` "PDF" is exactly the class of mislabeled
    # output this tool exists to prevent).
    if not rel_path.lower().endswith(f".{fmt}"):
        rel_path = f"{rel_path}.{fmt}"

    return content, title, rel_path, fmt


def _render(content: str, out_path: Path, *, title: str, fmt: str) -> Path:
    """Synchronous render worker — runs off-loop via ``asyncio.to_thread``."""
    if fmt == "pdf":
        return render_markdown_text_to_pdf(content, out_path, title=title)
    # Unreachable while SUPPORTED_FORMATS == ("pdf",) — _parse_args gates.
    raise RuntimeError(f"unsupported format: {fmt}")


async def dispatch_render_document(
    *,
    args_json: bytes | str,
    workspace: Path | None = None,
) -> str:
    """Render a Markdown document into the workspace. JSON envelope; never raises."""
    try:
        content, title, rel_path, fmt = _parse_args(args_json)
        ws = resolve_workspace(workspace)
        out_path = resolve_in_workspace(ws, rel_path, for_write=True)
    except CodingArgsInvalidError as exc:
        return _err({"error": f"args_invalid: {exc.message}"})
    except WorkspaceEscapeError as exc:
        return _err({"error": f"workspace_escape: {exc}"})

    try:
        await asyncio.wait_for(
            asyncio.to_thread(_render, content, out_path, title=title, fmt=fmt),
            timeout=RENDER_TIMEOUT_SECS,
        )
    except TimeoutError:
        return _err(
            {
                "path": rel_path,
                "error": (
                    f"render_failed: exceeded the {RENDER_TIMEOUT_SECS:.0f}s "
                    "render budget"
                ),
            }
        )
    except RuntimeError as exc:
        # The pipeline's own actionable errors (no engine / invalid output).
        return _err({"path": rel_path, "error": f"render_failed: {exc}"})
    except Exception as exc:  # noqa: BLE001 — tool dispatch must never raise
        logger.warning(
            "render_document.unexpected_failure", path=rel_path, error=str(exc)
        )
        return _err({"path": rel_path, "error": f"render_failed: {exc}"})

    try:
        size = out_path.stat().st_size
    except OSError as exc:  # pragma: no cover — defensive
        return _err({"path": rel_path, "error": f"render_failed: {exc}"})

    return json.dumps(
        {
            "path": workspace_rel(ws, out_path),
            "format": fmt,
            "bytes": size,
            "title": title or None,
            "hint": (
                "Document rendered and verified. Deliver it with "
                "send_attachment using this path."
            ),
        },
        ensure_ascii=False,
    )


__all__ = [
    "MAX_CONTENT_BYTES",
    "RENDER_DOCUMENT_TOOL",
    "SUPPORTED_FORMATS",
    "dispatch_render_document",
    "render_document_tool_schema",
]
