"""Backend-agnostic synthesis entrypoint.

Three callers share this module, and they must behave identically:

* the ``text_to_speech`` builtin tool;
* the admin preview route behind the UI's audition button;
* any channel-side flow that needs a clip without going through the model.

:func:`synthesize` resolves the backend, voice, format, model and
credentials, dispatches to the right driver (templated HTTP or the
GPT-Live WebRTC session), writes the audio into the agent workspace and
returns a :class:`SynthesisResult`.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import structlog

from corlinman_agent.voice.catalog import (
    AudioFormat,
    BackendDef,
    get_backend,
    normalize_backend,
    resolve_format,
    resolve_model,
    resolve_voice,
    voice_params,
)
from corlinman_agent.voice.defaults import get_voice_defaults
from corlinman_agent.voice.errors import SynthesisError
from corlinman_agent.voice.gpt_live import synthesize_gpt_live
from corlinman_agent.voice.http_backend import synthesize_http

logger = structlog.get_logger(__name__)

__all__ = [
    "MAX_INPUT_CHARS",
    "SynthesisRequest",
    "SynthesisResult",
    "resolve_credentials",
    "synthesize",
]

#: Hard cap on input text — keeps one call bounded and avoids handing a
#: provider a megabyte of prose.
MAX_INPUT_CHARS: int = 8_000

_DEFAULT_TIMEOUT_SECS: float = 60.0


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    """Everything one synthesis call needs."""

    text: str
    backend: str = ""
    voice: str = ""
    fmt: str = ""
    model: str = ""
    instructions: str | None = None
    speed: float | None = None
    #: Provider adapter to read credentials off (duck-typed, may be None).
    provider: Any = None
    #: Persona/provider params — carries ``tts_backend``, ``reference_id``,
    #: ``base_url`` overrides and any vendor-specific body extras.
    params: Mapping[str, Any] = field(default_factory=dict)
    #: ``True`` when :attr:`provider` is the adapter a persona bound to
    #: its ``voice`` capability rather than the generic chat fallback.
    #: Gates credential borrowing — see :func:`resolve_credentials`.
    provider_is_bound: bool = False
    #: Where to write. Defaults to the workspace ``generated`` dir.
    out_dir: Path | None = None
    timeout: float | None = None
    transport: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """A rendered clip on disk."""

    path: Path
    mime: str
    backend: str
    voice: str
    model: str
    fmt: str
    size_bytes: int
    generated_at_ms: int

    def as_envelope(self) -> dict[str, Any]:
        """The ``{"ok": true, ...}`` shape the tool layer returns."""
        return {
            "ok": True,
            "path": str(self.path),
            "mime": self.mime,
            "kind": "audio",
            "voice": self.voice,
            "backend": self.backend,
            "model": self.model,
            "format": self.fmt,
            "size_bytes": self.size_bytes,
            "generated_at_ms": self.generated_at_ms,
        }


def _param_str(params: Mapping[str, Any] | None, *keys: str) -> str | None:
    if not params:
        return None
    for key in keys:
        raw = params.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _ulid_like() -> str:
    """Short ulid-ish id for filenames — matches the asset_store style."""
    return uuid.uuid4().hex[:26]


def _resolve_out_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        explicit.mkdir(parents=True, exist_ok=True)
        return explicit
    # Imported lazily so the voice package stays importable in contexts
    # that have no workspace configured (unit tests, schema generation).
    from corlinman_runtime import resolve_agent_workspace

    # resolve_agent_workspace is untyped at this seam, so pin the type.
    target: Path = Path(resolve_agent_workspace()) / "generated"
    target.mkdir(parents=True, exist_ok=True)
    return target


def resolve_credentials(
    backend: BackendDef,
    provider: Any,
    params: Mapping[str, Any] | None,
    *,
    provider_is_bound: bool = False,
) -> tuple[str | None, str]:
    """Resolve ``(api_key, base_url)`` for ``backend``.

    Precedence runs **most specific first**, so a narrowly-scoped
    credential is never overridden by a broader one:

    1. explicit ``params`` (``api_key`` / ``base_url``) — how a persona or
       a custom UI-defined backend pins its own credentials;
    2. the provider adapter's key/base, but only when that adapter really
       belongs to this backend (see ``provider_is_bound`` below);
    3. the backend's declared env var (``api_key_env``) — a process-wide
       default;
    4. the backend's declared default ``base_url``.

    Ordering note: the env var deliberately sits *below* the adapter.
    Putting it above (as an earlier revision did) means an operator who
    exports ``FISH_AUDIO_API_KEY`` silently overrides every per-persona
    Fish binding, so a multi-account setup sends every request with one
    account's credential — and, because ``base_url`` is resolved
    separately, it pairs the env key with the adapter's URL.

    Parameters
    ----------
    provider_is_bound
        ``True`` when ``provider`` is the adapter a persona explicitly
        bound to its ``voice`` capability, rather than the generic chat
        provider the dispatcher falls back to. This distinction is a
        **credential boundary**: a Fish/ElevenLabs/MiniMax backend may
        legitimately read a key off its own bound adapter, but must never
        borrow one off the unrelated chat provider — that would ship the
        operator's OpenAI key to a third-party vendor's host. OpenAI-shaped
        backends are exempt because there the chat provider *is* the
        intended relay.
    """
    api_key = _param_str(params, "api_key", "tts_api_key")
    base_url = _param_str(params, "base_url", "tts_base_url")

    is_openai_backend = backend.id in ("openai", "gpt_live")
    may_borrow = is_openai_backend or provider_is_bound

    if provider is not None and may_borrow:
        candidate = getattr(provider, "_api_key", None) or getattr(
            provider, "api_key", None
        )
        if not api_key and candidate:
            # Belt-and-braces even inside a bound adapter: an operator can
            # bind voice to a provider that happens to carry the OpenAI
            # key, and that still must not reach a third-party host.
            openai_env = os.environ.get("OPENAI_API_KEY") or ""
            leaks_openai_key = bool(openai_env) and str(candidate) == openai_env
            if is_openai_backend or not leaks_openai_key:
                api_key = str(candidate)
        if not base_url:
            candidate_base = getattr(provider, "_base_url", None) or getattr(
                provider, "base_url", None
            )
            if candidate_base:
                base_url = str(candidate_base)

    if not api_key and backend.api_key_env:
        env_value = os.environ.get(backend.api_key_env)
        if env_value and env_value.strip():
            api_key = env_value.strip()

    if not base_url:
        base_url = backend.base_url

    return api_key, base_url


def _live_base_url(base_url: str) -> str:
    """Strip a trailing ``/v1`` so ``/v1/live`` is not doubled.

    Chat providers are configured with a ``.../v1`` base, but the Live
    handshake lives at the host root (``/v1/live`` and
    ``/backend-api/codex/realtime/calls``).
    """
    root = (base_url or "").rstrip("/")
    if root.endswith("/v1"):
        return root[: -len("/v1")]
    return root


def _timeout(request: SynthesisRequest) -> float:
    if request.timeout is not None:
        return request.timeout
    raw = os.environ.get("CORLINMAN_TTS_TIMEOUT_SECS")
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return _DEFAULT_TIMEOUT_SECS


def _extra_body(
    backend: BackendDef,
    params: Mapping[str, Any] | None,
    voice: str,
) -> dict[str, Any]:
    """Vendor-specific body extras: catalog voice params + config passthrough."""
    extra: dict[str, Any] = dict(voice_params(backend.id, voice))
    if not params:
        return extra
    passthrough = params.get("body")
    if isinstance(passthrough, Mapping):
        extra.update(passthrough)
    return extra


async def synthesize(request: SynthesisRequest) -> SynthesisResult:
    """Render ``request`` to an audio file. Raises :class:`SynthesisError`."""
    text = (request.text or "").strip()
    if not text:
        raise SynthesisError("invalid_args", "缺少要合成的文本")
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS]

    # Operator config sits between the caller's own arguments and the env
    # vars — see corlinman_agent.voice.defaults for why.
    cfg = get_voice_defaults()
    backend_id = normalize_backend(
        request.backend
        or _param_str(request.params, "tts_backend", "backend")
        or cfg.backend
        or os.environ.get("CORLINMAN_TTS_BACKEND")
        or ""
    )
    backend = get_backend(backend_id)
    if backend is None:
        raise SynthesisError(
            "tts_unavailable", f"未知的语音后端: {backend_id}"
        )

    voice = resolve_voice(
        backend.id,
        request.voice
        or _param_str(request.params, "voice", "reference_id")
        or cfg.voice
        or os.environ.get("CORLINMAN_TTS_VOICE")
        or "",
    )
    if not voice and backend.free_form_voices:
        # Kept as ``tts_unavailable`` (not a new code) so existing callers
        # that branch on "not configured" keep working; the message names
        # reference_id because that is the Fish Audio spelling operators
        # see in provider params.
        raise SynthesisError(
            "tts_unavailable",
            f"{backend.label} 需要显式音色（reference_id / voice_id），"
            "请在语音设置或 persona 参数中指定 reference_id",
        )

    fmt: AudioFormat = resolve_format(
        backend.id,
        request.fmt or _param_str(request.params, "format") or cfg.fmt or "",
    )
    model = resolve_model(
        backend.id,
        request.model
        or _param_str(request.params, "model", "tts_model")
        or cfg.model
        or os.environ.get("CORLINMAN_TTS_MODEL")
        or "",
    )
    instructions = (
        request.instructions
        or _param_str(request.params, "instructions")
        or (cfg.instructions or None)
    )
    if instructions and not backend.supports_instructions:
        instructions = None
    speed = request.speed
    if speed is None:
        raw_speed = (request.params or {}).get("speed")
        if isinstance(raw_speed, (int, float)):
            speed = float(raw_speed)
        elif cfg.speed is not None:
            speed = cfg.speed
    if speed is not None and not backend.supports_speed:
        speed = None

    api_key, base_url = resolve_credentials(
        backend,
        request.provider,
        request.params,
        provider_is_bound=request.provider_is_bound,
    )
    out_dir = _resolve_out_dir(request.out_dir)
    out_path = out_dir / f"{_ulid_like()}{fmt.ext}"
    timeout = _timeout(request)

    if backend.kind == "webrtc_live":
        size = await synthesize_gpt_live(
            backend,
            base_url=_live_base_url(base_url),
            api_key=api_key,
            text=text,
            voice=voice,
            fmt=fmt,
            model=model,
            out_path=out_path,
            instructions=instructions,
            timeout=timeout,
            transport=request.transport,
        )
    else:
        audio = await synthesize_http(
            backend,
            base_url=base_url,
            api_key=api_key,
            text=text,
            voice=voice,
            fmt=fmt,
            model=model,
            speed=speed,
            instructions=instructions,
            extra_body=_extra_body(backend, request.params, voice),
            timeout=timeout,
            transport=request.transport,
        )
        try:
            out_path.write_bytes(audio)
        except OSError as exc:
            raise SynthesisError("write_failed", str(exc)) from exc
        size = len(audio)

    logger.info(
        "voice.synthesized",
        backend=backend.id,
        model=model,
        voice=voice,
        fmt=fmt.id,
        size_bytes=size,
    )
    return SynthesisResult(
        path=out_path,
        mime=fmt.mime,
        backend=backend.id,
        voice=voice,
        model=model,
        fmt=fmt.id,
        size_bytes=size,
        generated_at_ms=int(time.time() * 1000),
    )
