"""``/admin/voice`` — TTS backend catalog, defaults, and audition preview.

Four routes back the voice settings page:

``GET  /admin/voice/backends``
    The live catalog: every built-in backend plus any the operator
    defined under ``[voice.backends.*]``, each with its models, voices,
    formats and credential status. This is what fills the picker.
``GET  /admin/voice/settings``
    The current ``[voice]`` defaults (secrets redacted).
``PUT  /admin/voice/settings``
    Write those defaults back. Blank secrets mean "keep what's stored",
    matching the ``***REDACTED***``-or-omit convention the rest of the
    admin tree uses.
``POST /admin/voice/preview``
    Synthesise a short sample and return a ``/v1/files/{id}`` url the
    browser can play. This runs the **same**
    :func:`corlinman_agent.voice.synthesize` the ``text_to_speech`` tool
    and the channel layer use, so what an operator auditions is exactly
    what a channel will later send.

Config is re-applied to the backend registry on every request
(:func:`_sync_registry`) rather than only at boot, so adding a custom
provider in the UI takes effect on the next poll instead of needing a
restart.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from corlinman_agent.voice import (
    AUDIO_FORMATS,
    DEFAULT_BACKEND,
    SynthesisError,
    SynthesisRequest,
    all_backends,
    get_backend,
    normalize_backend,
    register_backends_from_config,
    synthesize,
)
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.core.config_mutation import (
    publish_config_mutation as _publish_config_mutation,
)
from corlinman_server.gateway.core.config_mutation import (
    write_config_atomic as _write_config_atomic,
)
from corlinman_server.gateway.routes_admin_b.state import (
    config_snapshot,
    get_admin_state,
    require_admin,
)

logger = structlog.get_logger(__name__)

__all__ = ["build_router", "router"]

#: Sentinel the UI echoes back for a stored secret it never received.
_REDACTED = "***REDACTED***"

#: Preview is a UI affordance, not a synthesis endpoint — keep it short so
#: an accidental paste of a novel cannot run up a provider bill.
_PREVIEW_MAX_CHARS = 300

_DEFAULT_PREVIEW_TEXT = "你好，这是一段试听。我可以用这个音色给你发送语音消息。"


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class VoiceOut(BaseModel):
    id: str
    label: str
    description: str = ""
    tone: str = ""
    recommended: bool = False


class BackendOut(BaseModel):
    id: str
    label: str
    kind: str
    description: str = ""
    models: list[str] = Field(default_factory=list)
    default_model: str = ""
    voices: list[VoiceOut] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    default_voice: str = ""
    free_form_voices: bool = False
    supports_instructions: bool = False
    supports_speed: bool = False
    custom: bool = False
    #: ``True`` when a credential is reachable (config pin or env var).
    credential_set: bool = False
    api_key_env: str = ""
    base_url: str = ""


class BackendsOut(BaseModel):
    backends: list[BackendOut]
    formats: dict[str, str]
    default_backend: str


class VoiceSettingsOut(BaseModel):
    enabled: bool = True
    backend: str = DEFAULT_BACKEND
    voice: str = ""
    model: str = ""
    format: str = "mp3"
    instructions: str = ""
    speed: float | None = None
    #: Per-backend overrides, secrets redacted.
    backends: dict[str, dict[str, Any]] = Field(default_factory=dict)


class VoiceSettingsIn(BaseModel):
    enabled: bool | None = None
    backend: str | None = None
    voice: str | None = None
    model: str | None = None
    format: str | None = None
    instructions: str | None = None
    speed: float | None = None
    backends: dict[str, dict[str, Any]] | None = None


class PreviewIn(BaseModel):
    text: str = ""
    backend: str = ""
    voice: str = ""
    model: str = ""
    format: str = ""
    instructions: str = ""
    speed: float | None = None


class PreviewOut(BaseModel):
    ok: bool = True
    url: str
    mime: str
    backend: str
    voice: str
    model: str
    format: str
    size_bytes: int


class StatusOk(BaseModel):
    status: str = "ok"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _voice_section(cfg: Any | None = None) -> dict[str, Any]:
    snapshot = dict(cfg if cfg is not None else config_snapshot())
    section = snapshot.get("voice")
    return dict(section) if isinstance(section, dict) else {}


def _backends_section(voice_cfg: dict[str, Any]) -> dict[str, Any]:
    raw = voice_cfg.get("backends")
    return dict(raw) if isinstance(raw, dict) else {}


def _sync_registry(voice_cfg: dict[str, Any] | None = None) -> None:
    """Fold ``[voice.backends]`` into the process-wide backend registry.

    Called at the top of every route so a provider added through the UI is
    usable on the next request. :func:`register_backends_from_config`
    resets to the shipped set first, so removals apply too.
    """
    section = _backends_section(voice_cfg if voice_cfg is not None else _voice_section())
    try:
        register_backends_from_config(section)
    except Exception as exc:  # noqa: BLE001 — a bad block must not 500 the page
        logger.warning("admin.voice.registry_sync_failed", error=str(exc))


def _secret_of(block: Any) -> str:
    """Resolve an ``api_key`` that may be a literal or an ``{env=...}`` ref."""
    if not isinstance(block, dict):
        return ""
    raw = block.get("api_key")
    if isinstance(raw, dict):
        if "value" in raw:
            return str(raw.get("value") or "")
        env_name = str(raw.get("env") or "")
        return os.environ.get(env_name, "") if env_name else ""
    return str(raw or "")


def _credential_set(backend_id: str, voice_cfg: dict[str, Any]) -> bool:
    definition = get_backend(backend_id)
    block = _backends_section(voice_cfg).get(backend_id)
    if _secret_of(block):
        return True
    if definition is not None and definition.api_key_env:
        if (os.environ.get(definition.api_key_env) or "").strip():
            return True
    # OpenAI-shaped backends can ride the configured chat provider.
    if backend_id in ("openai", "gpt_live"):
        return bool(_chat_provider_key())
    return False


def _chat_provider_block() -> dict[str, Any]:
    """The first enabled OpenAI-ish ``[providers.*]`` block, if any."""
    providers = dict(config_snapshot()).get("providers")
    if not isinstance(providers, dict):
        return {}
    for entry in providers.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            continue
        kind = str(entry.get("kind") or "").lower()
        if kind in ("openai", "openai_compatible", "codex"):
            return dict(entry)
    return {}


def _chat_provider_key() -> str:
    return _secret_of(_chat_provider_block())


class _ProviderShim:
    """Attribute bag satisfying :func:`resolve_credentials`.

    Preview must resolve credentials the same way a live turn does — a
    turn hands the synthesiser the persona's bound provider adapter. There
    is no adapter in an admin request, so we reconstruct the two
    attributes the resolver actually reads off the configured chat
    provider. Same seam as ``config_admin/image_provider._ProviderShim``.
    """

    def __init__(self, api_key: str, base_url: str) -> None:
        self._api_key = api_key or None
        self._base_url = base_url or None


def _preview_provider(backend_id: str) -> Any:
    if backend_id not in ("openai", "gpt_live"):
        return None
    block = _chat_provider_block()
    if not block:
        return None
    return _ProviderShim(_secret_of(block), str(block.get("base_url") or ""))


def _params_for(backend_id: str, voice_cfg: dict[str, Any]) -> dict[str, Any]:
    """Build synthesis params from the ``[voice.backends.<id>]`` block."""
    block = _backends_section(voice_cfg).get(backend_id)
    params: dict[str, Any] = {}
    if isinstance(block, dict):
        for key in ("base_url", "voice", "reference_id", "model", "format", "speed"):
            value = block.get(key)
            if value not in (None, ""):
                params[key] = value
        body = block.get("body")
        if isinstance(body, dict):
            params["body"] = dict(body)
        secret = _secret_of(block)
        if secret:
            params["api_key"] = secret
    return params


def _redact_backends(voice_cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for backend_id, block in _backends_section(voice_cfg).items():
        if not isinstance(block, dict):
            continue
        view = {k: v for k, v in block.items() if k != "api_key"}
        if block.get("api_key"):
            view["api_key"] = _REDACTED
        out[str(backend_id)] = view
    return out


def _merge_backend_blocks(
    existing: dict[str, Any], incoming: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Apply an incoming backends map, preserving untouched secrets.

    A blank or ``***REDACTED***`` ``api_key`` means "keep the stored one",
    so the UI can round-trip a settings form without ever holding the
    plaintext credential.
    """
    merged = dict(existing)
    for backend_id, block in incoming.items():
        if not isinstance(block, dict):
            continue
        key = str(backend_id)
        current = dict(merged.get(key) or {}) if isinstance(merged.get(key), dict) else {}
        incoming_secret = block.get("api_key")
        for field_name, value in block.items():
            if field_name == "api_key":
                continue
            current[field_name] = value
        if isinstance(incoming_secret, str) and incoming_secret.strip():
            if incoming_secret != _REDACTED:
                current["api_key"] = incoming_secret
        elif incoming_secret is not None and not isinstance(incoming_secret, str):
            current["api_key"] = incoming_secret
        merged[key] = current
    return merged


def _backend_view(backend_id: str, voice_cfg: dict[str, Any]) -> BackendOut | None:
    definition = get_backend(backend_id)
    if definition is None:
        return None
    return BackendOut(
        id=definition.id,
        label=definition.label,
        kind=definition.kind,
        description=definition.description,
        models=list(definition.models),
        default_model=definition.default_model,
        voices=[VoiceOut(**v.as_dict()) for v in definition.voices],
        formats=list(definition.formats),
        default_voice=definition.default_voice,
        free_form_voices=definition.free_form_voices,
        supports_instructions=definition.supports_instructions,
        supports_speed=definition.supports_speed,
        custom=definition.custom,
        credential_set=_credential_set(definition.id, voice_cfg),
        api_key_env=definition.api_key_env,
        base_url=definition.base_url,
    )


async def _probe_live(
    definition: Any, provider: Any, params: dict[str, Any]
) -> SynthesisError | None:
    """Pre-flight the Realtime endpoint; returns the blocking error, if any."""
    from corlinman_agent.voice.gpt_live import probe_live_endpoint
    from corlinman_agent.voice.synth import _live_base_url, resolve_credentials

    api_key, base_url = resolve_credentials(definition, provider, params)
    try:
        return await probe_live_endpoint(
            base_url=_live_base_url(base_url), api_key=api_key
        )
    except Exception as exc:  # noqa: BLE001 — a probe must never break preview
        logger.warning("admin.voice.live_probe_failed", error=str(exc))
        return None


def _error(code: str, message: str, *, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "voice"])

    @r.get("/admin/voice/backends", response_model=BackendsOut)
    async def list_backends() -> BackendsOut:
        voice_cfg = _voice_section()
        _sync_registry(voice_cfg)
        views = [
            view
            for view in (_backend_view(b.id, voice_cfg) for b in all_backends())
            if view is not None
        ]
        return BackendsOut(
            backends=views,
            formats={fmt.id: fmt.mime for fmt in AUDIO_FORMATS.values()},
            default_backend=normalize_backend(voice_cfg.get("backend")),
        )

    @r.get("/admin/voice/settings", response_model=VoiceSettingsOut)
    async def get_settings() -> VoiceSettingsOut:
        voice_cfg = _voice_section()
        _sync_registry(voice_cfg)
        speed = voice_cfg.get("speed")
        return VoiceSettingsOut(
            enabled=bool(voice_cfg.get("enabled", True)),
            backend=normalize_backend(voice_cfg.get("backend")),
            voice=str(voice_cfg.get("voice") or ""),
            model=str(voice_cfg.get("model") or ""),
            format=str(voice_cfg.get("format") or "mp3"),
            instructions=str(voice_cfg.get("instructions") or ""),
            speed=float(speed) if isinstance(speed, (int, float)) else None,
            backends=_redact_backends(voice_cfg),
        )

    @r.put("/admin/voice/settings", response_model=None)
    async def put_settings(body: VoiceSettingsIn) -> JSONResponse | StatusOk:
        state = get_admin_state()
        if state.config_path is None:
            return _error("config_path_unset", "服务未以配置文件模式启动", status=503)

        if body.format is not None and body.format and body.format not in AUDIO_FORMATS:
            return _error("unknown_format", f"不支持的音频格式: {body.format}")

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            voice_cfg = _voice_section(cfg)

            if body.enabled is not None:
                voice_cfg["enabled"] = bool(body.enabled)
            if body.backend is not None:
                voice_cfg["backend"] = normalize_backend(body.backend)
            for field_name in ("voice", "model", "format", "instructions"):
                value = getattr(body, field_name)
                if value is not None:
                    voice_cfg[field_name] = value
            if body.speed is not None:
                voice_cfg["speed"] = float(body.speed)
            if body.backends is not None:
                voice_cfg["backends"] = _merge_backend_blocks(
                    _backends_section(voice_cfg), body.backends
                )

            cfg["voice"] = voice_cfg
            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err
            await _publish_config_mutation(state, cfg)

        _sync_registry(voice_cfg)
        return StatusOk()

    @r.post("/admin/voice/preview", response_model=None)
    async def preview(body: PreviewIn) -> JSONResponse | PreviewOut:
        voice_cfg = _voice_section()
        _sync_registry(voice_cfg)

        backend_id = normalize_backend(body.backend or voice_cfg.get("backend"))
        if get_backend(backend_id) is None:
            return _error("unknown_backend", f"未知的语音后端: {backend_id}", status=404)

        text = (body.text or "").strip() or _DEFAULT_PREVIEW_TEXT
        if len(text) > _PREVIEW_MAX_CHARS:
            text = text[:_PREVIEW_MAX_CHARS]

        params = _params_for(backend_id, voice_cfg)
        provider = _preview_provider(backend_id)

        definition = get_backend(backend_id)
        if definition is not None and definition.kind == "webrtc_live":
            # Report a gateway that cannot host Live sessions *before*
            # the local WebRTC stack gets a chance to fail — otherwise a
            # missing aiortc wheel masks the real blocker.
            blocked = await _probe_live(definition, provider, params)
            if blocked is not None:
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": blocked.code,
                        "message": blocked.message,
                        "backend": backend_id,
                        "upstream_status": blocked.status_code,
                    },
                )

        request = SynthesisRequest(
            text=text,
            backend=backend_id,
            voice=body.voice or str(voice_cfg.get("voice") or ""),
            fmt=body.format or str(voice_cfg.get("format") or ""),
            model=body.model or str(voice_cfg.get("model") or ""),
            instructions=body.instructions
            or str(voice_cfg.get("instructions") or "")
            or None,
            speed=body.speed,
            provider=provider,
            params=params,
        )

        try:
            result = await synthesize(request)
        except SynthesisError as exc:
            logger.info(
                "admin.voice.preview_failed",
                backend=backend_id,
                code=exc.code,
                status_code=exc.status_code,
            )
            return JSONResponse(
                status_code=502,
                content={
                    "error": exc.code,
                    "message": exc.message,
                    "backend": backend_id,
                    "upstream_status": exc.status_code,
                },
            )
        except Exception as exc:  # noqa: BLE001 — preview must never 500 blind
            logger.exception("admin.voice.preview_unexpected", backend=backend_id)
            return _error("preview_failed", str(exc), status=500)

        # Lazy import: routes.files owns the blob store and pulling it in at
        # module scope would drag the whole routes package into admin boot.
        from corlinman_server.gateway.routes.files import register_local_file

        descriptor = register_local_file(result.path)
        if not descriptor or not descriptor.get("url"):
            return _error(
                "preview_unavailable", "音频已生成但无法注册为可播放文件", status=500
            )

        return PreviewOut(
            url=str(descriptor["url"]),
            mime=result.mime,
            backend=result.backend,
            voice=result.voice,
            model=result.model,
            format=result.fmt,
            size_bytes=result.size_bytes,
        )

    return r


def build_router() -> APIRouter:
    return router()
