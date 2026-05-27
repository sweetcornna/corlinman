"""``image_generate`` builtin tool — plain image generation, no refs.

Sibling of :mod:`corlinman_agent.image.dispatch`. Identical wire shape
to ``image_with_refs`` (JSON envelope, workspace-relative PNG path),
but the dispatcher is intentionally **not** coupled to any persona /
asset_store wiring — no ``persona_id``, no ``characters``, no
reference lookups. The agent reaches for this tool when there is no
suitable persona reference pack to condition on.

Isolation from qzone
--------------------
This tool is **not** invoked by ``qzone_publish``. The qzone flow
keeps using :func:`dispatch_image_with_refs` exclusively, so a QQ-空间
post still requires a persona reference pack. Future revisions may
add a qzone-side ``plain`` switch, but doing so is intentionally out
of scope here — the two surfaces are kept independent so a regression
in one cannot leak into the other.

Wire contract
-------------
* :data:`IMAGE_GENERATE_TOOL` — wire-stable tool name.
* :func:`image_generate_tool_schema` — OpenAI-shaped function
  descriptor advertised on every chat turn.
* :func:`dispatch_image_generate` — async dispatcher,
  ``args_json -> str``, never raises.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import structlog

from corlinman_agent.image.generate import (
    ImageGenerationError,
    ImageProviderUnavailable,
    generate_plain,
)

logger = structlog.get_logger(__name__)


__all__ = [
    "IMAGE_GENERATE_TOOL",
    "dispatch_image_generate",
    "image_generate_tool_schema",
]


#: Wire-stable tool name. Imported by the agent servicer's
#: ``BUILTIN_TOOLS`` set + the ``_dispatch_builtin`` switch.
IMAGE_GENERATE_TOOL: str = "image_generate"


def image_generate_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped tool descriptor for ``image_generate``."""
    return {
        "type": "function",
        "function": {
            "name": IMAGE_GENERATE_TOOL,
            "description": (
                "Generate a plain image from a text prompt. No persona "
                "reference images are used — pair this with "
                "`send_attachment` to deliver the result. When you need "
                "the output to look like a specific persona's "
                "characters, call `image_with_refs` instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The image generation prompt. Be "
                            "descriptive — scene, lighting, mood, "
                            "style. No persona conditioning is applied."
                        ),
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": ["square", "portrait", "landscape"],
                        "description": (
                            "Output aspect — 1024x1024 / 1024x1536 / "
                            "1536x1024. Defaults to 'square'."
                        ),
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
    }


def _err(code: str, message: str) -> str:
    """Render a failure envelope in the canonical persona-tool shape."""
    return json.dumps(
        {"ok": False, "error": code, "message": message},
        ensure_ascii=False,
    )


def _decode(args_json: bytes | str) -> dict[str, Any]:
    raw: str
    if isinstance(args_json, (bytes, bytearray)):
        try:
            raw = bytes(args_json).decode("utf-8")
        except UnicodeDecodeError:
            return {}
    else:
        raw = args_json or ""
    try:
        obj = json.loads(raw or "{}")
    except (ValueError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _ulid_like() -> str:
    """Short ulid-ish id for filenames — matches the asset_store style."""
    return uuid.uuid4().hex[:26]


def _resolve_workspace_generated_dir() -> Path:
    """Resolve ``<DATA_DIR>/workspace/generated``, creating it on first
    use. Shared workspace with ``image_with_refs`` so the same
    ``send_attachment`` resolver picks up either tool's output."""
    raw = os.environ.get("CORLINMAN_DATA_DIR")
    base = Path(raw) if raw else Path.home() / ".corlinman"
    target = base / "workspace" / "generated"
    target.mkdir(parents=True, exist_ok=True)
    return target


async def dispatch_image_generate(
    *,
    args_json: bytes | str,
    provider: Any,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Dispatch one ``image_generate`` tool call into a JSON envelope.

    Parameters
    ----------
    args_json
        Raw ``ToolCallEvent.args_json`` bytes.
    provider
        Active :class:`CorlinmanProvider` — used only to read OpenAI
        credentials inside :func:`generate_plain`.
    transport
        Optional :mod:`httpx` test seam.

    Notes
    -----
    Signature intentionally omits ``persona_store`` / ``asset_store`` /
    ``bound_persona_id``. The isolation from ``image_with_refs`` is
    structural — there is no path from this dispatcher into the
    persona or asset surface.
    """
    args = _decode(args_json)
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _err(
            "invalid_args", "missing or empty 'prompt' field"
        )
    aspect = args.get("aspect_ratio") or "square"
    if aspect not in ("square", "portrait", "landscape"):
        return _err(
            "invalid_args",
            "aspect_ratio must be one of square|portrait|landscape",
        )

    try:
        png_bytes = await generate_plain(
            provider,
            prompt,
            aspect_ratio=aspect,  # type: ignore[arg-type]
            transport=transport,
        )
    except ImageProviderUnavailable as exc:
        return _err("provider_unavailable", str(exc))
    except ImageGenerationError as exc:
        return _err("image_generation_failed", str(exc))
    except Exception as exc:  # noqa: BLE001 — dispatcher must never raise
        logger.exception("image_generate.generate_unexpected")
        return _err("image_generation_failed", str(exc))

    out_dir = _resolve_workspace_generated_dir()
    out_path = out_dir / f"{_ulid_like()}.png"
    try:
        out_path.write_bytes(png_bytes)
    except OSError as exc:
        logger.exception("image_generate.write_failed", path=str(out_path))
        return _err("write_failed", str(exc))

    return json.dumps(
        {
            "ok": True,
            "path": str(out_path),
            "mime": "image/png",
            "aspect_ratio": aspect,
            "size_bytes": len(png_bytes),
            "generated_at_ms": int(time.time() * 1000),
        },
        ensure_ascii=False,
    )
