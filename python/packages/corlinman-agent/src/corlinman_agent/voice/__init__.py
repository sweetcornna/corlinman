"""Text-to-speech: a data-driven backend registry plus one synthesiser.

The package is deliberately provider-agnostic. A TTS backend is a
:class:`~corlinman_agent.voice.catalog.BackendDef` — a row of data
describing its models, voices, formats, credentials and HTTP wire shape —
so shipping support for a new vendor means adding a row, and a user
adding their own means writing a ``[voice.backends.<id>]`` config block
from the admin UI. Both land in the same picker with the same preview and
channel-delivery behaviour.

Two transports cover every provider:

``kind="http"``
    One request, audio back (raw bytes or base64 in JSON). Built-ins:
    OpenAI ``/audio/speech``, Fish Audio, ElevenLabs, Gemini, MiniMax.
``kind="webrtc_live"``
    GPT-Live's realtime session — see :mod:`corlinman_agent.voice.gpt_live`.

Public surface
--------------
* :func:`synthesize` / :class:`SynthesisRequest` / :class:`SynthesisResult`
  — the one call every caller uses.
* :class:`SynthesisError` — uniform failure with a machine-readable code.
* :func:`all_backends`, :func:`get_backend`, :func:`resolve_voice`,
  :func:`resolve_format` — catalog reads for the admin/UI layer.
* :func:`register_backends_from_config` — boot-time hook that folds
  ``[voice.backends]`` into the registry.
"""

from __future__ import annotations

from corlinman_agent.voice.catalog import (
    AUDIO_FORMATS,
    BUILTIN_BACKEND_IDS,
    DEFAULT_BACKEND,
    DEFAULT_FORMAT,
    AudioFormat,
    BackendDef,
    HttpSynthSpec,
    VoiceDef,
    all_backends,
    backend_from_config,
    get_backend,
    list_backend_ids,
    normalize_backend,
    register_backend,
    register_backends_from_config,
    reset_custom_backends,
    resolve_format,
    resolve_model,
    resolve_voice,
)
from corlinman_agent.voice.defaults import (
    VoiceDefaults,
    apply_voice_config,
    get_voice_defaults,
    reset_voice_defaults,
    set_voice_defaults,
    voice_defaults_from_config,
)
from corlinman_agent.voice.errors import SynthesisError
from corlinman_agent.voice.synth import (
    MAX_INPUT_CHARS,
    SynthesisRequest,
    SynthesisResult,
    resolve_credentials,
    synthesize,
)

__all__ = [
    "AUDIO_FORMATS",
    "BUILTIN_BACKEND_IDS",
    "DEFAULT_BACKEND",
    "DEFAULT_FORMAT",
    "MAX_INPUT_CHARS",
    "AudioFormat",
    "BackendDef",
    "HttpSynthSpec",
    "SynthesisError",
    "SynthesisRequest",
    "SynthesisResult",
    "VoiceDef",
    "VoiceDefaults",
    "all_backends",
    "apply_voice_config",
    "backend_from_config",
    "get_backend",
    "get_voice_defaults",
    "list_backend_ids",
    "normalize_backend",
    "register_backend",
    "register_backends_from_config",
    "reset_custom_backends",
    "reset_voice_defaults",
    "resolve_credentials",
    "resolve_format",
    "resolve_model",
    "resolve_voice",
    "set_voice_defaults",
    "synthesize",
    "voice_defaults_from_config",
]
