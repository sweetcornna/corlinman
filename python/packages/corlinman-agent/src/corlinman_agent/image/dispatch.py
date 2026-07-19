"""``image_with_refs`` builtin tool — generate an image conditioned on a
persona's character reference pack.

Wire contract identical to the other agent-side dispatchers:

* :data:`IMAGE_WITH_REFS_TOOL` — wire-stable tool name.
* :func:`image_with_refs_tool_schema` — OpenAI-shaped function
  descriptor advertised on every chat turn.
* :func:`dispatch_image_with_refs` — async dispatcher,
  ``args_json -> str``, never raises.

Pipeline
--------
1. Resolve the persona (explicit ``persona_id`` arg → bound persona
   from the channel binding → error).
2. List the persona's ``reference`` assets; match them to the
   ``characters`` labels the model passed in. Unknown labels collapse
   to a clean error envelope so the model can retry with valid labels.
3. Encode each ref as a base64 ``data:`` URL and call
   :func:`generate_with_refs`.
4. Save the returned PNG bytes to
   ``<DATA_DIR>/workspace/generated/<ulid>.png`` so the existing
   ``send_attachment`` workspace resolver picks it up cleanly.
5. Return ``{"path": "...", "mime": "image/png", "chars_used": [...]}``.

The agent then passes the returned path to ``send_attachment`` to
deliver the image via the bound channel.
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
    generate_with_refs,
)

logger = structlog.get_logger(__name__)


__all__ = [
    "IMAGE_WITH_REFS_TOOL",
    "dispatch_image_with_refs",
    "image_with_refs_tool_schema",
]


#: Wire-stable tool name. Imported by the agent servicer's
#: ``BUILTIN_TOOLS`` set + the ``_dispatch_builtin`` switch.
IMAGE_WITH_REFS_TOOL: str = "image_with_refs"

#: Hard cap on the number of reference images we send to the provider.
#: Matches hermes-agent's practical limit — beyond ~6 refs the OpenAI
#: Responses API starts dropping conditioning fidelity, and the per-
#: request size grows fast (each ref is base64-encoded as a data URL).
_MAX_REFS: int = 8


#: Env switch (ported from hermes) — set ``CORLINMAN_IMAGE_REFS_INTRO=off``
#: to send the bare user prompt straight through with no composition
#: direction wrapper. Any other value (or unset) keeps the intro on.
_INTRO_ENV: str = "CORLINMAN_IMAGE_REFS_INTRO"


#: COMPOSITION DIRECTION prepended to every reference-conditioned prompt
#: (ported from hermes ``image_with_refs``). This is model-facing image
#: instruction text — never shown to the user. Its whole job is to steer
#: multi-character reference generations away from the default "everyone
#: lined up, facing the lens, smiling" group-photo failure mode and
#: toward a candid, lived-in slice-of-life snapshot.
#:
#: Deliberately persona-generic: the character-realism and art-style
#: sentences describe HOW to render, never WHICH style — the concrete
#: art direction always arrives upstream from the persona/prompt and the
#: reference images themselves, so no specific style is ever hardcoded
#: here.
_COMPOSITION_INTRO: str = (
    "COMPOSITION DIRECTION (read before rendering):\n"
    "Render this as a candid, slice-of-life snapshot — the kind of "
    "unplanned moment someone catches on a phone — NOT a posed group "
    "photo or a lined-up cast portrait. Nobody has stopped to arrange "
    "themselves for the camera.\n"
    "- Give EACH character a DIFFERENT action and let them face "
    "different directions; do not have everyone look at the camera or "
    "strike the same pose.\n"
    "- Use off-axis, off-center framing: place subjects away from the "
    "dead center and angle them to the lens, as if the shot were caught "
    "in passing rather than composed head-on.\n"
    "- Freeze everyone mid-action — reaching, turning, leaning, "
    "laughing, walking, glancing away — never a static row of figures "
    "standing still.\n"
    "- Fill the environment with lived-in clutter (scattered objects, "
    "everyday mess, incidental background props) so the scene reads as a "
    "real inhabited place, not a clean studio backdrop.\n"
    "- If an action is meant to be sneaky or casual — a quiet swipe, an "
    "offhand grab, a sidelong glance — stage it so that intent visibly "
    "reads as sneaky or casual, not blatant or posed.\n"
    "- Keep every character on-model and physically realistic in "
    "anatomy, proportion, and how they occupy the space.\n"
    "- Hold the art style, medium, and rendering already established by "
    "the reference images and the scene description below; do not shift "
    "or reinterpret the visual style."
)


def _intro_enabled() -> bool:
    """Whether the composition-direction wrapper is active this call.

    On by default; disabled only when ``CORLINMAN_IMAGE_REFS_INTRO`` is
    set to ``off`` (case-insensitive), matching hermes' opt-out knob.
    """
    return os.environ.get(_INTRO_ENV, "on").strip().lower() != "off"


def _compose_refs_prompt(
    prompt: str,
    chars_used: list[str],
    descriptions: list[str] | None = None,
) -> str:
    """Wrap the user ``prompt`` with the composition direction + a
    reference-image legend.

    Layout, top to bottom:

    1. :data:`_COMPOSITION_INTRO` — the candid-snapshot steering.
    2. A reference-order legend numbering each in-order label, e.g.
       ``Reference image 1 = front, Reference image 2 = side`` so the
       model can tie each conditioning image to the right character.
    3. The original ``prompt`` under a ``Scene:`` header, kept verbatim
       and last so the specifics remain the final word.

    ``chars_used`` is the in-sequence list of labels whose reference
    assets were actually sent (aligned 1:1 with the ``ref_paths`` order
    passed to :func:`generate_with_refs`). ``descriptions`` aligns 1:1
    with ``chars_used``; a non-empty entry is the operator-authored
    "what this image is / how to reference it" text and rides the
    legend so the model knows what each conditioning image carries.
    """
    if descriptions is None:
        descriptions = [""] * len(chars_used)
    parts: list[str] = []
    for i, (label, desc) in enumerate(
        zip(chars_used, descriptions, strict=False), start=1
    ):
        entry = f"Reference image {i} = {label}"
        if desc.strip():
            entry += f" ({desc.strip()})"
        parts.append(entry)
    legend = ", ".join(parts)
    return (
        f"{_COMPOSITION_INTRO}\n\n"
        f"Reference images, in order: {legend}.\n\n"
        f"Scene:\n{prompt}"
    )


def image_with_refs_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped tool descriptor for ``image_with_refs``."""
    return {
        "type": "function",
        "function": {
            "name": IMAGE_WITH_REFS_TOOL,
            "description": (
                "Generate an image conditioned on a prompt PLUS character "
                "reference images drawn from a persona's reference asset "
                "pack. Returns a path inside the agent workspace — pair "
                "with `send_attachment` to deliver the image to the "
                "user. `characters` are the label slots within the "
                "persona's reference bucket (call `persona_list_assets` "
                "to see what's available); unknown labels are silently "
                "skipped."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The image generation prompt. Be descriptive "
                            "— include scene, lighting, mood, and how the "
                            "referenced characters should appear."
                        ),
                    },
                    "characters": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Reference asset labels within the persona's "
                            "`reference` bucket (e.g. ['front', "
                            "'casual']). Limited to 8 entries."
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
                    "persona_id": {
                        "type": "string",
                        "description": (
                            "Optional persona id. When omitted the tool "
                            "resolves the persona bound to the current "
                            "channel; supply explicitly if you need to "
                            "use a non-bound persona's reference pack."
                        ),
                    },
                },
                "required": ["prompt", "characters"],
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
    """Generate a short ulid-ish id matching the asset_store style.
    26 hex chars — not a real ULID but unique enough for filenames."""
    return uuid.uuid4().hex[:26]


def _resolve_workspace_generated_dir() -> Path:
    """Resolve ``<DATA_DIR>/workspace/generated``, creating it on first
    use. The directory lives under the same workspace the channel
    handler's ``send_attachment`` resolver walks, so the returned path
    is picked up without any special wiring.

    Mirrors the agent_servicer's ``_resolve_data_dir`` env contract —
    ``CORLINMAN_DATA_DIR`` overrides; default is ``~/.corlinman``.
    """
    raw = os.environ.get("CORLINMAN_DATA_DIR")
    base = Path(raw) if raw else Path.home() / ".corlinman"
    target = base / "workspace" / "generated"
    target.mkdir(parents=True, exist_ok=True)
    return target


async def dispatch_image_with_refs(
    *,
    args_json: bytes | str,
    provider: Any,
    persona_store: Any,
    asset_store: Any,
    bound_persona_id: str | None = None,
    model_override: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Dispatch one ``image_with_refs`` tool call into a JSON envelope.

    Parameters
    ----------
    args_json
        Raw ``ToolCallEvent.args_json`` bytes.
    provider
        Active :class:`CorlinmanProvider` — used to read the OpenAI
        credentials in :func:`generate_with_refs`. Pass-through; never
        invoked here.
    persona_store
        :class:`PersonaStore` — for resolving the persona row and
        verifying the slug.
    asset_store
        :class:`PersonaAssetStore` — for listing reference assets and
        looking up their on-disk paths.
    bound_persona_id
        Persona id bound to the active channel turn (read off
        ``start.extra['persona_id']`` by the agent servicer if present).
        Falls back to ``None``; the tool then requires an explicit
        ``persona_id`` arg.
    transport
        Optional :mod:`httpx` test seam.
    """
    if persona_store is None:
        return _err(
            "persona_store_unavailable",
            "persona store is not wired in this deployment",
        )
    if asset_store is None:
        return _err(
            "persona_asset_store_unavailable",
            "persona asset store is not wired in this deployment",
        )

    args = _decode(args_json)
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _err(
            "invalid_args", "missing or empty 'prompt' field"
        )
    chars_raw = args.get("characters")
    if not isinstance(chars_raw, list) or not chars_raw:
        return _err(
            "invalid_args",
            "'characters' must be a non-empty list of reference labels",
        )
    characters: list[str] = []
    for c in chars_raw[:_MAX_REFS]:
        if isinstance(c, str) and c.strip():
            characters.append(c.strip())
    if not characters:
        return _err(
            "invalid_args",
            "'characters' carried no valid string labels",
        )

    aspect = args.get("aspect_ratio") or "square"
    if aspect not in ("square", "portrait", "landscape"):
        return _err(
            "invalid_args",
            "aspect_ratio must be one of square|portrait|landscape",
        )

    # Resolve persona — explicit > bound.
    explicit_id = args.get("persona_id")
    if isinstance(explicit_id, str) and explicit_id.strip():
        persona_id: str | None = explicit_id.strip()
    else:
        persona_id = bound_persona_id
    if not persona_id:
        return _err(
            "persona_unresolved",
            "no persona_id supplied and no persona bound to this turn — "
            "pass persona_id explicitly",
        )

    try:
        persona = await persona_store.get(persona_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "image_with_refs.persona_lookup_failed", persona_id=persona_id
        )
        return _err("persona_get_failed", str(exc))
    if persona is None:
        return _err(
            "persona_not_found", f"no persona with id {persona_id!r}"
        )

    # List reference assets and match labels.
    try:
        assets = await asset_store.list(persona_id, kind="reference")
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "image_with_refs.asset_list_failed", persona_id=persona_id
        )
        return _err("asset_list_failed", str(exc))
    by_label: dict[str, Any] = {a.label: a for a in assets}
    chars_used: list[str] = []
    chars_descs: list[str] = []
    chars_missing: list[str] = []
    ref_paths: list[Path] = []
    for label in characters:
        record = by_label.get(label)
        if record is None:
            chars_missing.append(label)
            continue
        try:
            ref_paths.append(asset_store.path_for(record))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "image_with_refs.path_for_failed",
                persona_id=persona_id,
                label=label,
                error=str(exc),
            )
            chars_missing.append(label)
            continue
        chars_used.append(label)
        chars_descs.append(str(getattr(record, "description", "") or ""))

    if not ref_paths:
        return _err(
            "no_refs_resolved",
            "none of the requested character labels matched a "
            "reference asset on this persona — available labels: "
            + ", ".join(sorted(by_label.keys())),
        )

    # Wrap the bare prompt with the composition direction + reference
    # legend (unless the operator opted out via CORLINMAN_IMAGE_REFS_INTRO).
    intro_enabled = _intro_enabled()
    gen_prompt = (
        _compose_refs_prompt(prompt, chars_used, chars_descs)
        if intro_enabled
        else prompt
    )

    # Generate the image.
    try:
        png_bytes = await generate_with_refs(
            provider,
            gen_prompt,
            ref_paths,
            aspect_ratio=aspect,  # type: ignore[arg-type]
            model_override=model_override,
            transport=transport,
        )
    except ImageProviderUnavailable as exc:
        return _err("provider_unavailable", str(exc))
    except ImageGenerationError as exc:
        return _err("image_generation_failed", str(exc))
    except Exception as exc:  # noqa: BLE001 — dispatcher must never raise
        logger.exception(
            "image_with_refs.generate_unexpected", persona_id=persona_id
        )
        return _err("image_generation_failed", str(exc))

    # Save under the workspace so send_attachment finds it.
    out_dir = _resolve_workspace_generated_dir()
    out_path = out_dir / f"{_ulid_like()}.png"
    try:
        out_path.write_bytes(png_bytes)
    except OSError as exc:
        logger.exception(
            "image_with_refs.write_failed", path=str(out_path)
        )
        return _err("write_failed", str(exc))

    return json.dumps(
        {
            "ok": True,
            "path": str(out_path),
            "mime": "image/png",
            "chars_used": chars_used,
            "chars_missing": chars_missing,
            "persona_id": persona_id,
            "aspect_ratio": aspect,
            "composition_intro": intro_enabled,
            "size_bytes": len(png_bytes),
            "generated_at_ms": int(time.time() * 1000),
        },
        ensure_ascii=False,
    )
