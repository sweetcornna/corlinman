"""``text_to_speech`` builtin tool — synthesise speech audio from text.

Sibling of the image-generation tools in this package (they share the
workspace ``generated`` output dir so the same ``send_attachment``
resolver picks up either tool's output). The model calls
``text_to_speech`` to turn a string into an audio file, then passes the
returned ``path`` to ``send_attachment`` to deliver it.

Backend resolution is OPTIONAL, never a hard dependency on the VPS:

1. The OpenAI ``/audio/speech`` endpoint, reached with the active
   provider's credentials (same ``getattr`` dance as
   :mod:`corlinman_agent.image.generate`). Used when a key is reachable.
2. When unavailable → a graceful ``{"ok": False, "error":
   "tts_unavailable", ...}`` envelope so the model can keep reasoning
   instead of crashing.

Wire contract (matches ``image_generate``):

* :data:`TEXT_TO_SPEECH_TOOL` — wire-stable tool name.
* :func:`text_to_speech_tool_schema` — OpenAI-shaped descriptor.
* :func:`dispatch_text_to_speech` — async dispatcher,
  ``args_json -> str``, never raises.

Config read at runtime
----------------------
* ``provider._api_key`` / ``provider.api_key`` / ``OPENAI_API_KEY`` —
  credential for the OpenAI fallback.
* ``provider._base_url`` / ``provider.base_url`` — optional base override.
* ``CORLINMAN_TTS_MODEL`` — env override for the model id; defaults to
  ``gpt-4o-mini-tts``.
* ``CORLINMAN_TTS_VOICE`` — env override for the default voice; defaults
  to ``alloy``.
* ``CORLINMAN_TTS_TIMEOUT_SECS`` — HTTP timeout; defaults to ``60``.
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

logger = structlog.get_logger(__name__)


__all__ = [
    "TEXT_TO_SPEECH_TOOL",
    "dispatch_text_to_speech",
    "text_to_speech_tool_schema",
]


#: Wire-stable tool name. Imported by the agent servicer's
#: ``BUILTIN_TOOLS`` set + the ``_dispatch_builtin`` switch.
TEXT_TO_SPEECH_TOOL: str = "text_to_speech"

#: Hard cap on the input text — keeps a single synth call bounded and
#: avoids surprising the provider with a megabyte of prose.
_MAX_INPUT_CHARS: int = 8_000

_DEFAULT_MODEL: str = "gpt-4o-mini-tts"
_DEFAULT_VOICE: str = "alloy"
_DEFAULT_TIMEOUT_SECS: float = 60.0

#: OpenAI ``/audio/speech`` accepts these voices. Advertised in the
#: schema so the model picks a valid one; an unknown value is coerced
#: to the default before the call.
_VOICES: tuple[str, ...] = (
    "alloy",
    "echo",
    "fable",
    "onyx",
    "nova",
    "shimmer",
)

#: ``response_format`` -> (file extension, mime) for the formats we
#: surface. ``mp3`` is the safe default (broadest channel support).
_FORMATS: dict[str, tuple[str, str]] = {
    "mp3": (".mp3", "audio/mpeg"),
    "opus": (".opus", "audio/opus"),
    "aac": (".aac", "audio/aac"),
    "wav": (".wav", "audio/wav"),
}
_DEFAULT_FORMAT: str = "mp3"


def text_to_speech_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped tool descriptor for ``text_to_speech``."""
    return {
        "type": "function",
        "function": {
            "name": TEXT_TO_SPEECH_TOOL,
            "description": (
                "Synthesise spoken-audio from text. Returns a path to an "
                "audio file in the agent workspace; pair this with "
                "`send_attachment` to deliver the audio to the user. Use "
                "this when the user asks for a voice message or audio reply."
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
                        "enum": list(_VOICES),
                        "description": (
                            "Voice timbre. Defaults to 'alloy'."
                        ),
                    },
                    "format": {
                        "type": "string",
                        "enum": list(_FORMATS.keys()),
                        "description": (
                            "Audio container/codec. Defaults to 'mp3'."
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


def _ulid_like() -> str:
    """Short ulid-ish id for filenames — matches the asset_store style."""
    return uuid.uuid4().hex[:26]


def _resolve_workspace_generated_dir() -> Path:
    """Resolve ``<DATA_DIR>/workspace/generated``, creating it on first
    use. Shared workspace with the image tools so the same
    ``send_attachment`` resolver picks up the output."""
    raw = os.environ.get("CORLINMAN_DATA_DIR")
    base = Path(raw) if raw else Path.home() / ".corlinman"
    target = base / "workspace" / "generated"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _resolve_runtime_config() -> tuple[str, float]:
    """Return ``(model, timeout_secs)`` from env or defaults."""
    model = os.environ.get("CORLINMAN_TTS_MODEL") or _DEFAULT_MODEL
    try:
        timeout = float(
            os.environ.get("CORLINMAN_TTS_TIMEOUT_SECS")
            or _DEFAULT_TIMEOUT_SECS
        )
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECS
    return model, timeout


def _provider_credentials(provider: Any) -> tuple[str | None, str | None]:
    """Pull ``(api_key, base_url)`` off the provider adapter.

    Mirrors :func:`corlinman_agent.image.generate._provider_credentials`
    — tries the documented private attrs first, then public, then the
    ``OPENAI_API_KEY`` env var. Returns ``(None, ...)`` (rather than
    raising) so the dispatcher can emit a graceful ``tts_unavailable``
    envelope.
    """
    api_key: str | None = (
        getattr(provider, "_api_key", None)
        or getattr(provider, "api_key", None)
        or os.environ.get("OPENAI_API_KEY")
    )
    base_url: str | None = (
        getattr(provider, "_base_url", None)
        or getattr(provider, "base_url", None)
    )
    return (str(api_key) if api_key else None), (str(base_url) if base_url else None)


async def _try_openai_speech(
    *,
    text: str,
    voice: str,
    fmt: str,
    provider: Any,
    transport: httpx.BaseTransport | None,
) -> bytes:
    """Call the OpenAI ``/audio/speech`` endpoint. Raises on every
    non-success path so the dispatcher folds it into an envelope."""
    api_key, base_url = _provider_credentials(provider)
    if not api_key:
        raise RuntimeError(
            "text-to-speech not configured — provider carries no api_key "
            "and OPENAI_API_KEY is unset"
        )
    model, timeout = _resolve_runtime_config()
    root = (base_url or "https://api.openai.com/v1").rstrip("/")
    endpoint = f"{root}/audio/speech"
    payload: dict[str, Any] = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": fmt,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    client_kwargs: dict[str, Any] = {"timeout": timeout, "headers": headers}
    if transport is not None:
        client_kwargs["transport"] = transport
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(endpoint, json=payload)
    if response.status_code >= 400:
        raise RuntimeError(
            f"tts_http_status: server returned {response.status_code} — "
            f"{response.text[:300]}"
        )
    return response.content


async def dispatch_text_to_speech(
    *,
    args_json: bytes | str,
    provider: Any = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Dispatch one ``text_to_speech`` tool call into a JSON envelope.

    Parameters
    ----------
    args_json
        Raw ``ToolCallEvent.args_json`` bytes.
    provider
        Active :class:`CorlinmanProvider` — read only for OpenAI
        credentials in the fallback path. ``None`` is tolerated (the
        env-var credential path still works).
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
    text = text.strip()
    if len(text) > _MAX_INPUT_CHARS:
        text = text[:_MAX_INPUT_CHARS]

    voice = args.get("voice") or _DEFAULT_VOICE
    if voice not in _VOICES:
        voice = _DEFAULT_VOICE
    fmt = args.get("format") or _DEFAULT_FORMAT
    if fmt not in _FORMATS:
        fmt = _DEFAULT_FORMAT
    ext, mime = _FORMATS[fmt]

    audio_bytes: bytes | None = None
    backend = "openai"

    try:
        audio_bytes = await _try_openai_speech(
            text=text,
            voice=voice,
            fmt=fmt,
            provider=provider,
            transport=transport,
        )
    except RuntimeError as exc:
        # No credentials / HTTP error — graceful unavailable.
        logger.info("text_to_speech.unavailable", reason=str(exc))
        return _err("tts_unavailable", str(exc))
    except httpx.TimeoutException as exc:
        return _err("tts_timeout", str(exc))
    except httpx.HTTPError as exc:
        return _err("tts_http_error", str(exc))
    except Exception as exc:  # noqa: BLE001 — dispatcher must never raise
        logger.exception("text_to_speech.unexpected")
        return _err("tts_failed", str(exc))

    if not audio_bytes:
        return _err("tts_unavailable", "synthesis returned no audio")

    out_dir = _resolve_workspace_generated_dir()
    out_path = out_dir / f"{_ulid_like()}{ext}"
    try:
        out_path.write_bytes(audio_bytes)
    except OSError as exc:
        logger.exception("text_to_speech.write_failed", path=str(out_path))
        return _err("write_failed", str(exc))

    return json.dumps(
        {
            "ok": True,
            "path": str(out_path),
            "mime": mime,
            "kind": "audio",
            "voice": voice,
            "backend": backend,
            "size_bytes": len(audio_bytes),
            "generated_at_ms": int(time.time() * 1000),
        },
        ensure_ascii=False,
    )
