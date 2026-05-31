"""``vision_analyze`` builtin tool — inline image inspection.

Reads an image from the agent workspace (by ``path``) or a public URL
(by ``url``) and returns it as a multimodal content-block list so the
vision model can reason about the image inline in the tool-result turn.
An optional ``question`` annotation is prepended as a ``text`` part.

Wire contract
-------------
* :data:`VISION_ANALYZE_TOOL` — wire-stable tool name.
* :func:`vision_analyze_tool_schema` — OpenAI-shaped function descriptor.
* :func:`dispatch_vision_analyze` — sync dispatcher,
  ``args_json -> str | list[dict]``, never raises.

Return value
------------
On success the dispatcher returns a ``list[dict]`` containing:

1. (optional) ``{"type": "text", "text": "<question>"}`` when a question
   was provided — so the model has the question in the same tool-result
   turn as the image;
2. ``{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<b64>"}}``
   for workspace paths, or ``{"type": "image_url", "image_url": {"url": "<url>"}}``
   for public URLs.

On error, a plain JSON ``str`` envelope is returned (same ``{"ok": False, ...}``
shape as the other image tools) so errors stay consistent across the
tool family.
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
    CodingArgsInvalidError,
    WorkspaceEscapeError,
    decode_args,
    resolve_in_workspace,
    resolve_workspace,
)
from corlinman_agent.web._common import (
    WebFetchUnsafeHostError,
    is_safe_host,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "VISION_ANALYZE_TOOL",
    "dispatch_vision_analyze",
    "vision_analyze_tool_schema",
]

#: Wire-stable tool name.
VISION_ANALYZE_TOOL: str = "vision_analyze"

#: Image MIME types resolved by suffix.
_IMAGE_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}


def vision_analyze_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped tool descriptor for ``vision_analyze``."""
    return {
        "type": "function",
        "function": {
            "name": VISION_ANALYZE_TOOL,
            "description": (
                "Analyze or describe an image from the agent workspace or a "
                "public URL. Supply either `path` (workspace-relative file "
                "path) or `url` (https:// image URL). The image is returned "
                "inline so the model can reason about it directly. Use the "
                "optional `question` field to focus the analysis on a "
                "specific aspect of the image."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Workspace-relative path to the image file "
                            "(.png, .jpg, .jpeg, .gif, .webp, etc.). "
                            "Mutually exclusive with `url`."
                        ),
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "Public HTTPS URL of the image to analyze. "
                            "Mutually exclusive with `path`."
                        ),
                    },
                    "question": {
                        "type": "string",
                        "description": (
                            "Optional question or instruction about what to "
                            "look for in the image, e.g. 'What objects are "
                            "visible?' or 'Does this screenshot show an error?'"
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


def _err(code: str, message: str) -> str:
    """Return a canonical error envelope string."""
    return json.dumps(
        {"ok": False, "error": code, "message": message},
        ensure_ascii=False,
    )


def dispatch_vision_analyze(
    *,
    args_json: bytes | str,
    workspace: Path | None = None,
) -> str | list[dict[str, Any]]:
    """Dispatch one ``vision_analyze`` tool call.

    Returns a multimodal content-block list on success or a JSON error
    string on failure. Never raises.
    """
    try:
        raw = decode_args(args_json)
    except CodingArgsInvalidError as exc:
        return _err("invalid_args", f"args_invalid: {exc.message}")

    path_arg: str | None = raw.get("path")
    url_arg: str | None = raw.get("url")
    question: str | None = raw.get("question")

    if path_arg and url_arg:
        return _err("invalid_args", "supply either 'path' or 'url', not both")
    if not path_arg and not url_arg:
        return _err("invalid_args", "one of 'path' or 'url' is required")

    parts: list[dict[str, Any]] = []

    # Prepend the question as a text part when provided.
    if isinstance(question, str) and question.strip():
        parts.append({"type": "text", "text": question.strip()})

    if url_arg:
        # URL path — the provider downloads it SERVER-SIDE, so an
        # unvalidated url is a textbook SSRF (e.g. http://169.254.169.254
        # cloud-metadata exfiltration). The schema promises https-only, so
        # reject http:// outright, then run the same SSRF guard web_fetch
        # uses before forwarding (SEC-08).
        if not isinstance(url_arg, str) or not url_arg.startswith("https://"):
            return _err("invalid_args", "'url' must be an https:// URL")
        try:
            is_safe_host(url_arg)
        except WebFetchUnsafeHostError as exc:
            logger.warning("vision_analyze.unsafe_host", url=url_arg, reason=str(exc))
            return _err("unsafe_host", str(exc))
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": url_arg},
            }
        )
        return parts

    # Workspace path — resolve, read, base64-encode.
    try:
        ws = resolve_workspace(workspace)
        path = resolve_in_workspace(ws, path_arg)
    except CodingArgsInvalidError as exc:
        return _err("invalid_args", f"args_invalid: {exc.message}")
    except WorkspaceEscapeError as exc:
        return _err("workspace_escape", str(exc))

    if not path.exists():
        return _err("file_not_found", f"no such file: {path_arg!r}")
    if not path.is_file():
        return _err("not_a_file", f"path is not a regular file: {path_arg!r}")

    suffix_lower = path.suffix.lower()
    mime = _IMAGE_MIME.get(suffix_lower)
    if mime is None:
        guessed, _ = mimetypes.guess_type(path.name)
        mime = guessed or "application/octet-stream"

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        return _err("read_failed", str(exc))

    b64 = base64.b64encode(raw_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    parts.append(
        {
            "type": "image_url",
            "image_url": {"url": data_url},
        }
    )
    return parts


def _resolve_workspace_default() -> Path:
    """Return the default workspace path (``CORLINMAN_AGENT_WORKSPACE`` or
    a fallback temp path). Used only for manual invocations without an
    explicit workspace kwarg."""
    raw = os.environ.get("CORLINMAN_AGENT_WORKSPACE")
    if raw:
        return Path(raw)
    return Path.home() / ".corlinman" / "workspace"
