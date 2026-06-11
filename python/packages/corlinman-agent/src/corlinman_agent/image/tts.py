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
* ``provider_params.tts_backend = "fish"`` — switch the dispatcher to
  Fish Audio's native ``/v1/tts`` endpoint. ``reference_id`` selects the
  Fish voice/model clone; ``model_override`` selects the Fish engine
  (for example ``s2-pro``).
* ``FISH_AUDIO_API_KEY`` / ``CORLINMAN_TTS_REFERENCE_ID`` — env fallbacks
  for Fish Audio credentials and voice reference id.
* ``CORLINMAN_TTS_TIMEOUT_SECS`` — HTTP timeout; defaults to ``60``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Mapping
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
_DEFAULT_FISH_MODEL: str = "s2-pro"
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
_FISH_FORMATS: frozenset[str] = frozenset({"mp3", "opus", "wav"})

_FISH_BACKENDS: frozenset[str] = frozenset(
    {"fish", "fish_audio", "fish-audio"}
)
_OPENAI_BACKENDS: frozenset[str] = frozenset(
    {"openai", "openai_compatible", "openai-compatible"}
)
_FISH_BODY_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "chunk_length",
        "condition_on_previous_chunks",
        "early_stop_threshold",
        "latency",
        "max_new_tokens",
        "min_chunk_length",
        "normalize",
        "mp3_bitrate",
        "opus_bitrate",
        "prosody",
        "repetition_penalty",
        "sample_rate",
        "seed",
        "temperature",
        "top_p",
    }
)


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


def _resolve_runtime_config(
    *,
    model_override: str | None = None,
) -> tuple[str, float]:
    """Return ``(model, timeout_secs)`` from env or defaults."""
    model = (
        os.environ.get("CORLINMAN_TTS_MODEL")
        or model_override
        or _DEFAULT_MODEL
    )
    try:
        timeout = float(
            os.environ.get("CORLINMAN_TTS_TIMEOUT_SECS")
            or _DEFAULT_TIMEOUT_SECS
        )
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECS
    return model, timeout


def _param_str(params: Mapping[str, Any] | None, key: str) -> str | None:
    if not params:
        return None
    raw = params.get(key)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _resolve_tts_backend(params: Mapping[str, Any] | None) -> str:
    backend = (
        _param_str(params, "tts_backend")
        or _param_str(params, "backend")
        or os.environ.get("CORLINMAN_TTS_BACKEND")
        or "openai"
    )
    normalized = backend.strip().lower()
    if normalized in _FISH_BACKENDS:
        return "fish"
    if normalized in _OPENAI_BACKENDS:
        return "openai"
    return normalized


def _resolve_fish_runtime_config(
    *,
    model_override: str | None = None,
    provider_params: Mapping[str, Any] | None = None,
) -> tuple[str, float]:
    model = (
        (model_override.strip() if isinstance(model_override, str) else None)
        or _param_str(provider_params, "model")
        or os.environ.get("CORLINMAN_TTS_MODEL")
        or _DEFAULT_FISH_MODEL
    )
    try:
        timeout = float(
            os.environ.get("CORLINMAN_TTS_TIMEOUT_SECS")
            or _DEFAULT_TIMEOUT_SECS
        )
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECS
    return model, timeout


def _provider_credentials(
    provider: Any,
    *,
    env_key: str = "OPENAI_API_KEY",
) -> tuple[str | None, str | None]:
    """Pull ``(api_key, base_url)`` off the provider adapter.

    Mirrors :func:`corlinman_agent.image.generate._provider_credentials`
    — tries the documented private attrs first, then public, then an
    env var. Returns ``(None, ...)`` (rather than
    raising) so the dispatcher can emit a graceful ``tts_unavailable``
    envelope.
    """
    api_key: str | None = (
        getattr(provider, "_api_key", None)
        or getattr(provider, "api_key", None)
        or os.environ.get(env_key)
    )
    base_url: str | None = (
        getattr(provider, "_base_url", None)
        or getattr(provider, "base_url", None)
    )
    return (str(api_key) if api_key else None), (str(base_url) if base_url else None)


def _fish_provider_credentials(provider: Any) -> tuple[str | None, str | None]:
    """Resolve Fish credentials without leaking OpenAI fallback secrets."""
    fish_env_key = os.environ.get("FISH_AUDIO_API_KEY")
    provider_key = getattr(provider, "_api_key", None) or getattr(
        provider,
        "api_key",
        None,
    )
    openai_env_key = os.environ.get("OPENAI_API_KEY")
    api_key = fish_env_key or (
        provider_key
        if provider_key and str(provider_key) != str(openai_env_key or "")
        else None
    )
    base_url = (
        getattr(provider, "_base_url", None)
        or getattr(provider, "base_url", None)
    )
    return (str(api_key) if api_key else None), (str(base_url) if base_url else None)


def _fish_tts_endpoint(base_url: str | None) -> str:
    root = (base_url or "https://api.fish.audio").rstrip("/")
    if root.endswith("/v1"):
        return f"{root}/tts"
    return f"{root}/v1/tts"


async def _try_openai_speech(
    *,
    text: str,
    voice: str,
    fmt: str,
    provider: Any,
    model_override: str | None,
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
    model, timeout = _resolve_runtime_config(model_override=model_override)
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


async def _try_fish_speech(
    *,
    text: str,
    fmt: str,
    provider: Any,
    model_override: str | None,
    provider_params: Mapping[str, Any] | None,
    transport: httpx.BaseTransport | None,
) -> tuple[bytes, str]:
    """Call Fish Audio's native ``/v1/tts`` endpoint.

    Fish separates the generation engine (``model`` request header) from
    the speaker identity (``reference_id`` in the JSON body). The latter is
    intentionally kept in provider params so each persona can bind a
    different voice without changing the chat model.
    """
    api_key, base_url = _fish_provider_credentials(provider)
    if not api_key:
        raise RuntimeError(
            "Fish Audio text-to-speech not configured — provider carries "
            "no Fish api_key and FISH_AUDIO_API_KEY is unset"
        )

    reference_id = (
        _param_str(provider_params, "reference_id")
        or os.environ.get("CORLINMAN_TTS_REFERENCE_ID")
    )
    if not reference_id:
        raise RuntimeError(
            "Fish Audio text-to-speech not configured — "
            "provider params must include reference_id or "
            "CORLINMAN_TTS_REFERENCE_ID must be set"
        )

    model, timeout = _resolve_fish_runtime_config(
        model_override=model_override,
        provider_params=provider_params,
    )
    payload: dict[str, Any] = {
        "text": text,
        "reference_id": reference_id,
        "format": fmt,
    }
    for key in _FISH_BODY_PARAM_KEYS:
        value = provider_params.get(key) if provider_params else None
        if value is not None:
            payload[key] = value

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "model": model,
    }
    client_kwargs: dict[str, Any] = {"timeout": timeout, "headers": headers}
    if transport is not None:
        client_kwargs["transport"] = transport
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(_fish_tts_endpoint(base_url), json=payload)
    if response.status_code >= 400:
        raise RuntimeError(
            f"fish_tts_http_status: server returned {response.status_code} — "
            f"{response.text[:300]}"
        )
    return response.content, reference_id


async def dispatch_text_to_speech(
    *,
    args_json: bytes | str,
    provider: Any = None,
    model_override: str | None = None,
    provider_params: Mapping[str, Any] | None = None,
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
    fmt = args.get("format") or _param_str(provider_params, "format") or _DEFAULT_FORMAT
    if fmt not in _FORMATS:
        fmt = _DEFAULT_FORMAT

    audio_bytes: bytes | None = None
    backend = _resolve_tts_backend(provider_params)
    if backend == "fish" and fmt not in _FISH_FORMATS:
        fmt = _DEFAULT_FORMAT
    ext, mime = _FORMATS[fmt]
    reference_id: str | None = None

    try:
        if backend == "fish":
            audio_bytes, reference_id = await _try_fish_speech(
                text=text,
                fmt=fmt,
                provider=provider,
                model_override=model_override,
                provider_params=provider_params,
                transport=transport,
            )
        elif backend == "openai":
            audio_bytes = await _try_openai_speech(
                text=text,
                voice=voice,
                fmt=fmt,
                provider=provider,
                model_override=model_override,
                transport=transport,
            )
        else:
            return _err(
                "tts_unavailable",
                f"unsupported tts_backend: {backend}",
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

    payload: dict[str, Any] = {
            "ok": True,
            "path": str(out_path),
            "mime": mime,
            "kind": "audio",
            "voice": reference_id or voice,
            "backend": backend,
            "size_bytes": len(audio_bytes),
            "generated_at_ms": int(time.time() * 1000),
    }
    if reference_id is not None:
        payload["reference_id"] = reference_id
    return json.dumps(
        payload,
        ensure_ascii=False,
    )
