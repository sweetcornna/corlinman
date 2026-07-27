"""``text_to_speech`` builtin tool — synthesise speech audio from text.

Sibling of the image-generation tools in this package (they share the
workspace ``generated`` output dir so the same ``send_attachment``
resolver picks up either tool's output). The model calls
``text_to_speech`` to turn a string into an audio file, then passes the
returned ``path`` to ``send_attachment`` to deliver it.

This module is deliberately thin: it owns the **wire contract** (tool
name, schema, envelope) and nothing else. Backend selection, the voice
catalog, credentials and the actual synthesis live in
:mod:`corlinman_agent.voice`, which the admin preview route and the
channel layer share — so a voice previewed in the UI is byte-identical to
what a channel later sends.

Wire contract (matches ``image_generate``)
------------------------------------------
* :data:`TEXT_TO_SPEECH_TOOL` — wire-stable tool name.
* :func:`text_to_speech_tool_schema` — OpenAI-shaped descriptor.
* :func:`dispatch_text_to_speech` — async dispatcher,
  ``args_json -> str``, never raises.

Config read at runtime
----------------------
* ``provider_params.tts_backend`` — which backend to use
  (``gpt_live`` / ``openai`` / ``fish`` / ``elevenlabs`` / ``gemini`` /
  ``minimax`` / any user-defined id). Falls back to
  ``CORLINMAN_TTS_BACKEND`` then the built-in default.
* ``provider_params.voice`` / ``reference_id`` — bound voice.
* ``provider_params.api_key`` / ``base_url`` — per-persona credential pin.
* ``CORLINMAN_TTS_MODEL`` / ``CORLINMAN_TTS_VOICE`` /
  ``CORLINMAN_TTS_TIMEOUT_SECS`` — env overrides.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
import structlog

from corlinman_agent.voice import (
    AUDIO_FORMATS,
    SynthesisError,
    SynthesisRequest,
    get_backend,
    normalize_backend,
    synthesize,
)

logger = structlog.get_logger(__name__)


__all__ = [
    "TEXT_TO_SPEECH_TOOL",
    "dispatch_text_to_speech",
    "text_to_speech_tool_schema",
]


#: Wire-stable tool name. Imported by the agent servicer's
#: ``BUILTIN_TOOLS`` set + the ``_dispatch_builtin`` switch.
TEXT_TO_SPEECH_TOOL: str = "text_to_speech"


def text_to_speech_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped tool descriptor for ``text_to_speech``.

    ``voice`` is intentionally a free string rather than an ``enum``: the
    servicer snapshots this schema at import time, so an enum would go
    stale the moment an operator adds a custom backend or a vendor ships
    a new voice. Unknown ids are coerced to the backend default by
    :func:`corlinman_agent.voice.resolve_voice` instead.
    """
    return {
        "type": "function",
        "function": {
            "name": TEXT_TO_SPEECH_TOOL,
            "description": (
                "Synthesise spoken audio from text. Returns a path to an "
                "audio file in the agent workspace; pair this with "
                "`send_attachment` to deliver the audio to the user. Use "
                "this when the user asks for a voice message or audio reply. "
                "Omit `voice` to use the configured default."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "The text to speak. Keep it concise — long "
                            "passages are truncated."
                        ),
                    },
                    "voice": {
                        "type": "string",
                        "description": (
                            "Optional voice id. Leave unset to use the "
                            "voice configured for this agent/persona."
                        ),
                    },
                    "format": {
                        "type": "string",
                        "enum": list(AUDIO_FORMATS.keys()),
                        "description": (
                            "Audio container/codec. Defaults to 'mp3', "
                            "which every channel accepts."
                        ),
                    },
                    "instructions": {
                        "type": "string",
                        "description": (
                            "Optional delivery direction, e.g. 'speak "
                            "slowly and warmly'. Ignored by backends that "
                            "do not support steering."
                        ),
                    },
                },
                "required": ["text"],
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


def _str_arg(args: Mapping[str, Any], key: str) -> str:
    raw = args.get(key)
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


async def dispatch_text_to_speech(
    *,
    args_json: bytes | str,
    provider: Any = None,
    model_override: str | None = None,
    provider_params: Mapping[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Dispatch one ``text_to_speech`` tool call into a JSON envelope.

    Parameters
    ----------
    args_json
        Raw ``ToolCallEvent.args_json`` bytes.
    provider
        Active ``CorlinmanProvider`` — read for credentials when the
        selected backend shares the provider's vendor. ``None`` is
        tolerated (the env-var and params paths still work).
    model_override
        Persona ``voice`` model binding, when one is set.
    provider_params
        Persona/provider params — carries ``tts_backend``, ``voice``,
        ``base_url`` and any vendor body extras.
    transport
        Optional :mod:`httpx` test seam.

    Returns
    -------
    str
        JSON envelope for ``ToolResult.content``. Always returns; never
        raises — every failure path becomes an ``{"ok": False, ...}``
        envelope so the model can degrade gracefully.
    """
    args = _decode(args_json)
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return _err("invalid_args", "missing or empty 'text' field")

    request = SynthesisRequest(
        text=text,
        voice=_str_arg(args, "voice"),
        fmt=_str_arg(args, "format"),
        model=(model_override or "").strip(),
        instructions=_str_arg(args, "instructions") or None,
        provider=provider,
        params=dict(provider_params or {}),
        transport=transport,
    )

    try:
        result = await synthesize(request)
    except SynthesisError as exc:
        logger.info(
            "text_to_speech.failed",
            code=exc.code,
            reason=exc.message,
            status_code=exc.status_code,
        )
        return _err(exc.code, exc.message)
    except httpx.TimeoutException as exc:
        return _err("tts_timeout", str(exc))
    except httpx.HTTPError as exc:
        return _err("tts_http_error", str(exc))
    except Exception as exc:  # noqa: BLE001 — dispatcher must never raise
        logger.exception("text_to_speech.unexpected")
        return _err("tts_failed", str(exc))

    envelope = result.as_envelope()
    # Back-compat: Fish Audio callers keyed off ``reference_id``.
    backend = get_backend(normalize_backend(result.backend))
    if backend is not None and backend.free_form_voices and result.voice:
        envelope["reference_id"] = result.voice
    return json.dumps(envelope, ensure_ascii=False)
