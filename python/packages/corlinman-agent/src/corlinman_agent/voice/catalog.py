"""Data-driven catalog of TTS backends, voices and audio formats.

Everything a text-to-speech provider needs is expressed as **data** here,
not as code:

* :class:`BackendDef` — one provider (id, label, models, voices, formats,
  credential shape) plus, for plain-HTTP providers, a :class:`HttpSynthSpec`
  describing the request/response wire shape.
* :class:`VoiceDef` — one selectable voice.
* :class:`AudioFormat` — one deliverable container.

Because a backend is data, a user-defined provider is not a special case:
the admin UI writes a ``[voice.backends.<id>]`` block into config, boot
calls :func:`register_backend`, and it appears in the picker next to the
built-ins with the same preview + channel-send behaviour.

Two wire shapes cover essentially every vendor:

``kind="http"``
    One request, audio in the response — either raw bytes or a
    base64 field in a JSON body. Covers OpenAI ``/audio/speech``,
    Fish Audio, ElevenLabs, Azure, MiniMax, Gemini, Volcengine and any
    OpenAI-compatible relay.
``kind="webrtc_live"``
    The GPT-Live realtime path: an SDP offer/answer handshake against
    ``POST /v1/live``, then audio arrives on a media track. See
    :mod:`corlinman_agent.voice.gpt_live`.

Design notes
------------
*Voices are validated against the selected backend, never globally.* The
old ``_VOICES`` tuple in ``tts.py`` listed six ids and coerced anything
else to ``alloy``, which would have swallowed every voice shipped since
2024 (``marin``, ``cedar``, the GPT-Live set, and every custom clone).

*Timbre, not gender.* Voice blurbs describe tone and delivery. We do not
label synthetic voices with a gender.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal

__all__ = [
    "AUDIO_FORMATS",
    "BUILTIN_BACKEND_IDS",
    "DEFAULT_BACKEND",
    "DEFAULT_FORMAT",
    "AudioFormat",
    "BackendDef",
    "HttpSynthSpec",
    "VoiceDef",
    "all_backends",
    "get_backend",
    "list_backend_ids",
    "normalize_backend",
    "register_backend",
    "register_backends_from_config",
    "reset_custom_backends",
    "resolve_format",
    "resolve_voice",
]


# --------------------------------------------------------------------------
# Audio formats
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """One deliverable audio container.

    ``id`` is what a provider expects in its ``response_format``-ish
    field; ``ext``/``mime`` are what the workspace writer and the channel
    layer need to route the clip.
    """

    id: str
    ext: str
    mime: str
    #: ``True`` when at least one chat channel accepts this container as a
    #: native voice note without transcoding.
    voice_note_capable: bool = False


AUDIO_FORMATS: Mapping[str, AudioFormat] = MappingProxyType(
    {
        "mp3": AudioFormat("mp3", ".mp3", "audio/mpeg", voice_note_capable=True),
        "opus": AudioFormat("opus", ".opus", "audio/opus", voice_note_capable=True),
        "ogg": AudioFormat("ogg", ".ogg", "audio/ogg", voice_note_capable=True),
        "aac": AudioFormat("aac", ".aac", "audio/aac"),
        "m4a": AudioFormat("m4a", ".m4a", "audio/mp4"),
        "wav": AudioFormat("wav", ".wav", "audio/wav"),
        "flac": AudioFormat("flac", ".flac", "audio/flac"),
        "pcm": AudioFormat("pcm", ".pcm", "audio/L16"),
        "silk": AudioFormat("silk", ".silk", "audio/silk", voice_note_capable=True),
        "amr": AudioFormat("amr", ".amr", "audio/amr", voice_note_capable=True),
    }
)

#: Broadest channel support, so it stays the default everywhere.
DEFAULT_FORMAT: str = "mp3"


# --------------------------------------------------------------------------
# Voices
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VoiceDef:
    """A selectable voice, shown in the picker and validated on input."""

    id: str
    label: str
    #: Short blurb for the UI card. Describes delivery/timbre.
    description: str = ""
    #: Secondary tag rendered next to the label, e.g. ``"温暖"``.
    tone: str = ""
    #: ``True`` for vendor-flagged current-generation picks.
    recommended: bool = False
    #: Free-form extras a custom backend may need (e.g. Fish
    #: ``reference_id``, Azure ``style``). Merged into the request body.
    params: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "tone": self.tone,
            "recommended": self.recommended,
        }


def _v(
    voice_id: str,
    label: str,
    description: str,
    tone: str = "",
    *,
    recommended: bool = False,
) -> VoiceDef:
    return VoiceDef(
        id=voice_id,
        label=label,
        description=description,
        tone=tone,
        recommended=recommended,
    )


#: GPT-Live's nine remastered voices (2026-07-08 launch). These are the
#: ChatGPT voice names the realtime ``session.audio`` block expects — they
#: are NOT interchangeable with the ``/audio/speech`` set.
_GPT_LIVE_VOICES: tuple[VoiceDef, ...] = (
    _v("cove", "Cove", "沉稳清晰，适合播报与长段讲述", "沉稳", recommended=True),
    _v("juniper", "Juniper", "明亮利落，日常对话的稳妥选择", "明亮", recommended=True),
    _v("ember", "Ember", "温暖偏低，念长句时不易发飘", "温暖"),
    _v("breeze", "Breeze", "轻快通透，适合短提示与提醒", "轻快"),
    _v("arbor", "Arbor", "从容平缓，叙述型内容的默认音色", "从容"),
    _v("maple", "Maple", "亲和柔和，适合陪伴式回复", "柔和"),
    _v("sol", "Sol", "干净中性，信息密度高时最省力", "中性"),
    _v("spruce", "Spruce", "厚实沉着，适合正式播报", "厚实"),
    _v("vale", "Vale", "轻盈舒缓，适合睡前与慢节奏内容", "轻盈"),
)

#: ``/v1/audio/speech`` voices. ``marin``/``cedar`` are the
#: current-generation pair; the rest are the long-standing set.
_OPENAI_VOICES: tuple[VoiceDef, ...] = (
    _v("marin", "Marin", "官方推荐的新一代音色，自然度最高", "自然", recommended=True),
    _v("cedar", "Cedar", "官方推荐的新一代音色，偏沉稳", "沉稳", recommended=True),
    _v("alloy", "Alloy", "中性均衡，通用默认", "中性"),
    _v("ash", "Ash", "干脆利落，适合短句", "利落"),
    _v("ballad", "Ballad", "叙事感强，适合讲故事", "叙事"),
    _v("coral", "Coral", "明亮友好，适合客服口吻", "明亮"),
    _v("echo", "Echo", "低沉平稳，适合正式内容", "低沉"),
    _v("fable", "Fable", "戏剧化，适合角色扮演", "戏剧"),
    _v("nova", "Nova", "清亮活泼，适合轻松语境", "活泼"),
    _v("onyx", "Onyx", "浑厚有力，适合强调段落", "浑厚"),
    _v("sage", "Sage", "沉着从容，适合说明性内容", "从容"),
    _v("shimmer", "Shimmer", "柔和轻盈，适合温和提示", "柔和"),
    _v("verse", "Verse", "富有表现力，适合朗读", "表现力"),
)

_GEMINI_VOICES: tuple[VoiceDef, ...] = (
    _v("Zephyr", "Zephyr", "明亮轻快", "明亮", recommended=True),
    _v("Puck", "Puck", "俏皮上扬", "俏皮"),
    _v("Charon", "Charon", "低沉稳重", "低沉"),
    _v("Kore", "Kore", "干净中性", "中性", recommended=True),
    _v("Fenrir", "Fenrir", "有力度，适合强调", "有力"),
    _v("Aoede", "Aoede", "柔和舒缓", "柔和"),
)


# --------------------------------------------------------------------------
# HTTP wire shape
# --------------------------------------------------------------------------

#: Placeholder tokens usable in a :attr:`HttpSynthSpec.body` template. The
#: synthesiser substitutes them; anything else is sent verbatim, so a
#: custom backend can pin constants (``"encoding": "mp3"``) inline.
PLACEHOLDERS: tuple[str, ...] = (
    "{text}",
    "{voice}",
    "{format}",
    "{model}",
    "{speed}",
    "{instructions}",
)


@dataclass(frozen=True, slots=True)
class HttpSynthSpec:
    """Declarative request/response shape for a one-shot HTTP TTS call."""

    #: Appended to the resolved base URL, e.g. ``"/audio/speech"``.
    path: str
    method: str = "POST"
    #: ``bearer`` → ``Authorization: Bearer <key>``; ``header`` → the raw
    #: key in :attr:`auth_header`; ``query`` → ``?<auth_header>=<key>``;
    #: ``none`` → unauthenticated.
    auth: Literal["bearer", "header", "query", "none"] = "bearer"
    auth_header: str = "Authorization"
    #: JSON body template. Values containing a placeholder token are
    #: substituted; everything else is a literal.
    body: Mapping[str, Any] = field(default_factory=dict)
    #: Static headers merged into every request. Values may contain
    #: placeholders too (Fish Audio puts its engine in a ``model`` header).
    headers: Mapping[str, str] = field(default_factory=dict)
    #: ``binary`` → the response body *is* the audio; ``json_b64`` → pull
    #: a base64 string out of :attr:`audio_path`.
    response: Literal["binary", "json_b64"] = "binary"
    #: Dotted path into the JSON response, e.g.
    #: ``"candidates.0.content.parts.0.inline_data.data"``.
    audio_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "method": self.method,
            "auth": self.auth,
            "auth_header": self.auth_header,
            "body": dict(self.body),
            "headers": dict(self.headers),
            "response": self.response,
            "audio_path": self.audio_path,
        }


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackendDef:
    """One TTS provider, fully described as data."""

    id: str
    label: str
    kind: Literal["http", "webrtc_live"] = "http"
    description: str = ""
    #: Default API root. A configured provider/base-url override wins.
    base_url: str = ""
    #: Env var consulted when no provider credential is reachable.
    api_key_env: str = ""
    #: Selectable model ids; first entry is the default.
    models: tuple[str, ...] = ()
    voices: tuple[VoiceDef, ...] = ()
    #: Format ids this backend returns, in preference order.
    formats: tuple[str, ...] = (DEFAULT_FORMAT,)
    default_voice: str = ""
    #: ``True`` when voice ids are user-created handles (voice clones), so
    #: the picker shows a free-text field and skips catalog validation.
    free_form_voices: bool = False
    #: ``True`` when the provider honours a natural-language delivery
    #: instruction (OpenAI ``instructions``, GPT-Live session prompt).
    supports_instructions: bool = False
    supports_speed: bool = False
    http: HttpSynthSpec | None = None
    #: ``True`` for entries registered from user config rather than shipped.
    custom: bool = False

    @property
    def default_model(self) -> str:
        return self.models[0] if self.models else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "description": self.description,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "models": list(self.models),
            "default_model": self.default_model,
            "voices": [v.as_dict() for v in self.voices],
            "formats": list(self.formats),
            "default_voice": self.default_voice,
            "free_form_voices": self.free_form_voices,
            "supports_instructions": self.supports_instructions,
            "supports_speed": self.supports_speed,
            "custom": self.custom,
            "http": self.http.as_dict() if self.http else None,
        }


_BUILTIN_BACKENDS: tuple[BackendDef, ...] = (
    BackendDef(
        id="gpt_live",
        label="GPT-Live",
        kind="webrtc_live",
        description=(
            "OpenAI GPT-Live 实时语音模型，经 WebRTC 会话生成音频。"
            "需要网关暴露 POST /v1/live 且具备 Live attestation 能力。"
        ),
        base_url="",
        api_key_env="OPENAI_API_KEY",
        models=("gpt-live-1", "gpt-live-1-mini"),
        voices=_GPT_LIVE_VOICES,
        formats=("opus", "mp3", "wav", "pcm"),
        default_voice="cove",
        supports_instructions=True,
    ),
    BackendDef(
        id="openai",
        label="OpenAI 语音合成",
        description="标准 /v1/audio/speech 端点，兼容任何 OpenAI 形状的中转。",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        models=("gpt-4o-mini-tts", "tts-1-hd", "tts-1"),
        voices=_OPENAI_VOICES,
        formats=("mp3", "opus", "aac", "wav", "flac", "pcm"),
        # ``alloy`` rather than the recommended ``marin``: marin/cedar only
        # exist on gpt-4o-mini-tts, so defaulting to them would 400 for
        # anyone still pinned to tts-1/tts-1-hd. The picker flags the
        # newer pair as recommended instead.
        default_voice="alloy",
        supports_instructions=True,
        supports_speed=True,
        http=HttpSynthSpec(
            path="/audio/speech",
            body={
                "model": "{model}",
                "voice": "{voice}",
                "input": "{text}",
                "response_format": "{format}",
                "instructions": "{instructions}",
                "speed": "{speed}",
            },
        ),
    ),
    BackendDef(
        id="fish",
        label="Fish Audio",
        description="Fish Audio 原生 /v1/tts，音色由 reference_id（声音克隆句柄）指定。",
        base_url="https://api.fish.audio",
        api_key_env="FISH_AUDIO_API_KEY",
        models=("s2-pro", "s1"),
        formats=("mp3", "opus", "wav"),
        free_form_voices=True,
        supports_speed=True,
        http=HttpSynthSpec(
            path="/v1/tts",
            body={"text": "{text}", "reference_id": "{voice}", "format": "{format}"},
            headers={"model": "{model}"},
        ),
    ),
    BackendDef(
        id="elevenlabs",
        label="ElevenLabs",
        description="ElevenLabs text-to-speech，voice 为 voice_id。",
        base_url="https://api.elevenlabs.io/v1",
        api_key_env="ELEVENLABS_API_KEY",
        models=("eleven_multilingual_v2", "eleven_turbo_v2_5", "eleven_flash_v2_5"),
        formats=("mp3", "pcm"),
        free_form_voices=True,
        http=HttpSynthSpec(
            path="/text-to-speech/{voice}",
            auth="header",
            auth_header="xi-api-key",
            body={"text": "{text}", "model_id": "{model}"},
        ),
    ),
    BackendDef(
        id="gemini",
        label="Gemini 语音合成",
        description="Google Gemini TTS，音频以 base64 内联在 JSON 响应中。",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_env="GEMINI_API_KEY",
        models=("gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"),
        voices=_GEMINI_VOICES,
        formats=("pcm", "wav"),
        default_voice="Kore",
        http=HttpSynthSpec(
            path="/models/{model}:generateContent",
            auth="query",
            auth_header="key",
            body={
                "contents": [{"parts": [{"text": "{text}"}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "{voice}"}}
                    },
                },
            },
            response="json_b64",
            audio_path="candidates.0.content.parts.0.inlineData.data",
        ),
    ),
    BackendDef(
        id="minimax",
        label="MiniMax 语音",
        description="MiniMax T2A v2，中文表现好，voice 为 voice_id。",
        base_url="https://api.minimax.chat/v1",
        api_key_env="MINIMAX_API_KEY",
        models=("speech-02-hd", "speech-02-turbo"),
        formats=("mp3", "wav", "pcm"),
        free_form_voices=True,
        supports_speed=True,
        http=HttpSynthSpec(
            path="/t2a_v2",
            body={
                "model": "{model}",
                "text": "{text}",
                "stream": False,
                "voice_setting": {"voice_id": "{voice}", "speed": "{speed}"},
                "audio_setting": {"format": "{format}"},
            },
            response="json_b64",
            audio_path="data.audio",
        ),
    ),
)

BUILTIN_BACKEND_IDS: tuple[str, ...] = tuple(b.id for b in _BUILTIN_BACKENDS)

#: The backend used when nothing is configured.
DEFAULT_BACKEND: str = "openai"

#: Accepted spellings folded onto canonical ids. Keeps older configs
#: (``fish-audio``, ``openai_compatible``, ``gpt-live``) working.
_BACKEND_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "gpt-live": "gpt_live",
        "gptlive": "gpt_live",
        "live": "gpt_live",
        "realtime": "gpt_live",
        "openai_compatible": "openai",
        "openai-compatible": "openai",
        "openai_speech": "openai",
        "fish_audio": "fish",
        "fish-audio": "fish",
        "eleven_labs": "elevenlabs",
        "eleven-labs": "elevenlabs",
        "11labs": "elevenlabs",
        "google": "gemini",
    }
)

_REGISTRY: dict[str, BackendDef] = {b.id: b for b in _BUILTIN_BACKENDS}
_REGISTRY_LOCK = threading.RLock()


def normalize_backend(raw: str | None, *, default: str = DEFAULT_BACKEND) -> str:
    """Fold a configured backend spelling onto a canonical id.

    Unknown non-empty values are lowercased and returned as-is rather than
    coerced, so callers can emit a precise ``unsupported tts_backend: <x>``
    error instead of silently synthesising with the wrong engine.
    """
    if not isinstance(raw, str) or not raw.strip():
        return default
    key = raw.strip().lower().replace(" ", "_")
    return _BACKEND_ALIASES.get(key, key)


def get_backend(backend: str | None) -> BackendDef | None:
    """Look up a backend definition, or ``None`` when unregistered."""
    with _REGISTRY_LOCK:
        return _REGISTRY.get(normalize_backend(backend))


def all_backends() -> tuple[BackendDef, ...]:
    """Every registered backend — built-ins first, then custom entries."""
    with _REGISTRY_LOCK:
        rows = list(_REGISTRY.values())
    # Order by the shipped sequence first so an operator who merely pins a
    # base URL on a built-in does not see it jump to the bottom of the
    # picker; genuinely new backends follow, alphabetically.
    shipped = [b for b in rows if b.id in BUILTIN_BACKEND_IDS]
    added = [b for b in rows if b.id not in BUILTIN_BACKEND_IDS]
    shipped.sort(key=lambda b: BUILTIN_BACKEND_IDS.index(b.id))
    added.sort(key=lambda b: b.id)
    return tuple(shipped + added)


def list_backend_ids() -> tuple[str, ...]:
    return tuple(b.id for b in all_backends())


def register_backend(backend: BackendDef) -> None:
    """Add or replace a backend definition (idempotent)."""
    with _REGISTRY_LOCK:
        _REGISTRY[backend.id] = backend


def reset_custom_backends() -> None:
    """Restore the registry to exactly the shipped set.

    Called before re-applying config so a removed ``[voice.backends.*]``
    block actually disappears instead of lingering until restart.

    This *rebuilds* rather than deleting flagged rows: a config block may
    also **extend** a built-in (pinning a relay base URL), and such a row
    carries ``custom=True`` too. Deleting by flag would drop the built-in
    itself, leaving the deployment with no OpenAI backend at all until
    the next process restart.
    """
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
        _REGISTRY.update({b.id: b for b in _BUILTIN_BACKENDS})


def _voices_from_config(raw: Any) -> tuple[VoiceDef, ...]:
    """Parse a ``voices`` list from config into :class:`VoiceDef` rows.

    Accepts both the terse form (``voices = ["alloy", "nova"]``) and the
    rich form (``[[voice.backends.x.voices]] id = "..." label = "..."``).
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    out: list[VoiceDef] = []
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                out.append(VoiceDef(id=item.strip(), label=item.strip()))
            continue
        if not isinstance(item, Mapping):
            continue
        voice_id = str(item.get("id") or "").strip()
        if not voice_id:
            continue
        params = item.get("params")
        out.append(
            VoiceDef(
                id=voice_id,
                label=str(item.get("label") or voice_id),
                description=str(item.get("description") or ""),
                tone=str(item.get("tone") or ""),
                recommended=bool(item.get("recommended") or False),
                params=dict(params) if isinstance(params, Mapping) else {},
            )
        )
    return tuple(out)


def _http_from_config(raw: Any) -> HttpSynthSpec | None:
    if not isinstance(raw, Mapping):
        return None
    path = str(raw.get("path") or "").strip()
    if not path:
        return None
    auth = str(raw.get("auth") or "bearer").strip().lower()
    if auth not in ("bearer", "header", "query", "none"):
        auth = "bearer"
    response = str(raw.get("response") or "binary").strip().lower()
    if response not in ("binary", "json_b64"):
        response = "binary"
    body = raw.get("body")
    headers = raw.get("headers")
    return HttpSynthSpec(
        path=path,
        method=str(raw.get("method") or "POST").strip().upper() or "POST",
        auth=auth,  # type: ignore[arg-type]
        auth_header=str(raw.get("auth_header") or "Authorization"),
        body=copy.deepcopy(dict(body)) if isinstance(body, Mapping) else {},
        headers={str(k): str(v) for k, v in headers.items()}
        if isinstance(headers, Mapping)
        else {},
        response=response,  # type: ignore[arg-type]
        audio_path=str(raw.get("audio_path") or ""),
    )


def _str_tuple(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    if isinstance(raw, Sequence):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    return ()


def backend_from_config(backend_id: str, raw: Mapping[str, Any]) -> BackendDef | None:
    """Build a :class:`BackendDef` from one ``[voice.backends.<id>]`` block.

    A block may either describe a brand-new provider or *extend* a
    built-in (adding models/voices, pinning a base URL). Extending is the
    common case for OpenAI-compatible relays, so unspecified fields
    inherit from the built-in rather than resetting to empty.
    """
    canonical = normalize_backend(backend_id, default="")
    if not canonical:
        return None
    base = get_backend(canonical)
    formats = _str_tuple(raw.get("formats"))
    models = _str_tuple(raw.get("models"))
    voices = _voices_from_config(raw.get("voices"))
    http = _http_from_config(raw.get("http"))
    kind = str(raw.get("kind") or (base.kind if base else "http")).strip().lower()
    if kind not in ("http", "webrtc_live"):
        kind = "http"

    if base is not None:
        return replace(
            base,
            label=str(raw.get("label") or base.label),
            description=str(raw.get("description") or base.description),
            base_url=str(raw.get("base_url") or base.base_url),
            api_key_env=str(raw.get("api_key_env") or base.api_key_env),
            models=models or base.models,
            voices=voices or base.voices,
            formats=formats or base.formats,
            default_voice=str(raw.get("default_voice") or base.default_voice),
            free_form_voices=bool(
                raw.get("free_form_voices", base.free_form_voices)
            ),
            supports_instructions=bool(
                raw.get("supports_instructions", base.supports_instructions)
            ),
            supports_speed=bool(raw.get("supports_speed", base.supports_speed)),
            http=http or base.http,
            custom=True,
        )

    if kind == "http" and http is None:
        # A brand-new HTTP backend with no wire shape cannot synthesise.
        return None
    return BackendDef(
        id=canonical,
        label=str(raw.get("label") or canonical),
        kind=kind,  # type: ignore[arg-type]
        description=str(raw.get("description") or ""),
        base_url=str(raw.get("base_url") or ""),
        api_key_env=str(raw.get("api_key_env") or ""),
        models=models,
        voices=voices,
        formats=formats or (DEFAULT_FORMAT,),
        default_voice=str(raw.get("default_voice") or ""),
        free_form_voices=bool(raw.get("free_form_voices", not voices)),
        supports_instructions=bool(raw.get("supports_instructions", False)),
        supports_speed=bool(raw.get("supports_speed", False)),
        http=http,
        custom=True,
    )


def register_backends_from_config(section: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Apply a ``[voice.backends]`` config table; returns registered ids.

    Replaces the previous custom set wholesale so a deleted block is
    actually removed. Malformed blocks are skipped, never fatal — a bad
    custom provider must not take the agent down at boot.
    """
    reset_custom_backends()
    if not isinstance(section, Mapping):
        return ()
    registered: list[str] = []
    for backend_id, raw in section.items():
        if not isinstance(raw, Mapping):
            continue
        if raw.get("enabled") is False:
            continue
        built = backend_from_config(str(backend_id), raw)
        if built is None:
            continue
        register_backend(built)
        registered.append(built.id)
    return tuple(registered)


# --------------------------------------------------------------------------
# Resolution helpers
# --------------------------------------------------------------------------


def resolve_voice(backend: str, requested: str | None) -> str:
    """Validate ``requested`` against ``backend``'s catalog.

    Returns the backend default when ``requested`` is empty or unknown.
    Free-form backends (voice clones: Fish, ElevenLabs, MiniMax) pass any
    non-empty value straight through.
    """
    definition = get_backend(backend)
    wanted = requested.strip() if isinstance(requested, str) else ""
    if definition is None:
        return wanted
    if definition.free_form_voices or not definition.voices:
        return wanted or definition.default_voice
    if not wanted:
        return definition.default_voice
    lowered = wanted.lower()
    for voice in definition.voices:
        if voice.id.lower() == lowered:
            return voice.id
    return definition.default_voice


def resolve_format(backend: str, requested: str | None) -> AudioFormat:
    """Pick an :class:`AudioFormat` the backend can actually produce."""
    definition = get_backend(backend)
    supported_ids = definition.formats if definition else (DEFAULT_FORMAT,)
    supported = [AUDIO_FORMATS[i] for i in supported_ids if i in AUDIO_FORMATS]
    if not supported:
        supported = [AUDIO_FORMATS[DEFAULT_FORMAT]]
    wanted = requested.strip().lower() if isinstance(requested, str) else ""
    if wanted:
        for fmt in supported:
            if fmt.id == wanted:
                return fmt
    for fmt in supported:
        if fmt.id == DEFAULT_FORMAT:
            return fmt
    return supported[0]


def resolve_model(backend: str, requested: str | None) -> str:
    """Pick a model id, preferring an explicit override."""
    wanted = requested.strip() if isinstance(requested, str) else ""
    if wanted:
        return wanted
    definition = get_backend(backend)
    return definition.default_model if definition else ""


def voice_params(backend: str, voice_id: str) -> Mapping[str, Any]:
    """Extra body params attached to a catalogued voice (may be empty)."""
    definition = get_backend(backend)
    if definition is None:
        return {}
    for voice in definition.voices:
        if voice.id == voice_id:
            return voice.params
    return {}


def iter_voices(backend: str) -> Iterator[VoiceDef]:
    definition = get_backend(backend)
    if definition is None:
        return iter(())
    return iter(definition.voices)
