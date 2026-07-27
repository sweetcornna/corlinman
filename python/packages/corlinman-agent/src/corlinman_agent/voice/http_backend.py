"""Templated HTTP driver — one request in, audio bytes out.

Every non-realtime TTS provider we support (OpenAI ``/audio/speech``,
Fish Audio, ElevenLabs, Gemini, MiniMax, and any user-defined backend)
is a single HTTP call whose only real differences are the URL, the auth
header, the JSON body shape, and where the audio lives in the response.
:class:`~corlinman_agent.voice.catalog.HttpSynthSpec` captures exactly
those four things, so this module is the whole implementation.

Substitution rules
------------------
A template value that is *exactly* one placeholder is replaced with the
typed value — ``"speed": "{speed}"`` yields the float ``1.0``, not the
string ``"1.0"``. A value that merely *contains* placeholders is string
interpolated (``"/text-to-speech/{voice}"``). Keys whose value resolves
to ``None`` or an empty string are dropped, so an unset ``instructions``
never reaches a provider that would reject a blank one.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from corlinman_agent.voice.catalog import AudioFormat, BackendDef, HttpSynthSpec
from corlinman_agent.voice.errors import SynthesisError

__all__ = ["build_request", "extract_audio", "synthesize_http"]


def _placeholder_values(
    *,
    text: str,
    voice: str,
    fmt: AudioFormat,
    model: str,
    speed: float | None,
    instructions: str | None,
) -> dict[str, Any]:
    return {
        "{text}": text,
        "{voice}": voice,
        "{format}": fmt.id,
        "{model}": model,
        "{speed}": speed,
        "{instructions}": instructions,
    }


def _substitute(node: Any, values: Mapping[str, Any]) -> Any:
    """Recursively resolve placeholders in a template tree."""
    if isinstance(node, str):
        exact = values.get(node)
        if node in values:
            return exact
        out = node
        for token, value in values.items():
            if token in out:
                out = out.replace(token, "" if value is None else str(value))
        return out
    if isinstance(node, Mapping):
        resolved: dict[str, Any] = {}
        for key, value in node.items():
            sub = _substitute(value, values)
            if sub is None or sub == "":
                # Drop unset optionals rather than sending empty strings.
                continue
            if isinstance(sub, Mapping) and not sub:
                continue
            resolved[str(key)] = sub
        return resolved
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        return [_substitute(item, values) for item in node]
    return node


def _join_url(base: str, path: str) -> str:
    root = (base or "").rstrip("/")
    tail = path if path.startswith("/") else f"/{path}"
    return f"{root}{tail}"


def build_request(
    backend: BackendDef,
    spec: HttpSynthSpec,
    *,
    base_url: str,
    api_key: str | None,
    text: str,
    voice: str,
    fmt: AudioFormat,
    model: str,
    speed: float | None,
    instructions: str | None,
    extra_body: Mapping[str, Any] | None = None,
) -> tuple[str, str, dict[str, str], dict[str, Any], dict[str, str]]:
    """Resolve a spec into ``(method, url, headers, body, params)``."""
    values = _placeholder_values(
        text=text,
        voice=voice,
        fmt=fmt,
        model=model,
        speed=speed,
        instructions=instructions,
    )
    url = _join_url(base_url, str(_substitute(spec.path, values)))
    body = dict(_substitute(dict(spec.body), values))
    if extra_body:
        body.update(_substitute(dict(extra_body), values))
    headers: dict[str, str] = {"Content-Type": "application/json"}
    for key, value in spec.headers.items():
        resolved = _substitute(value, values)
        if resolved not in (None, ""):
            headers[key] = str(resolved)
    params: dict[str, str] = {}

    if spec.auth != "none":
        if not api_key:
            raise SynthesisError(
                "tts_unavailable",
                f"{backend.label} 未配置凭据 — provider 无 api_key，"
                f"环境变量 {backend.api_key_env or '<未指定>'} 也为空",
            )
        if spec.auth == "bearer":
            headers[spec.auth_header] = f"Bearer {api_key}"
        elif spec.auth == "header":
            headers[spec.auth_header] = api_key
        elif spec.auth == "query":
            params[spec.auth_header] = api_key
    return spec.method, url, headers, body, params


def _dig(payload: Any, dotted: str) -> Any:
    """Walk a dotted path, supporting numeric list indices."""
    node = payload
    for part in dotted.split("."):
        if node is None:
            return None
        if part.isdigit() and isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            index = int(part)
            node = node[index] if index < len(node) else None
            continue
        if isinstance(node, Mapping):
            node = node.get(part)
            continue
        return None
    return node


def extract_audio(spec: HttpSynthSpec, response: httpx.Response) -> bytes:
    """Pull audio bytes out of a provider response per the spec."""
    if spec.response == "binary":
        return response.content
    try:
        payload = response.json()
    except ValueError as exc:
        raise SynthesisError(
            "tts_bad_response", f"响应不是合法 JSON: {exc}"
        ) from exc
    if not spec.audio_path:
        raise SynthesisError(
            "tts_bad_response", "backend 声明 json_b64 但未配置 audio_path"
        )
    raw = _dig(payload, spec.audio_path)
    if not isinstance(raw, str) or not raw:
        raise SynthesisError(
            "tts_bad_response",
            f"响应中 {spec.audio_path} 处没有音频数据",
        )
    try:
        return base64.b64decode(raw)
    except (ValueError, TypeError) as exc:
        raise SynthesisError(
            "tts_bad_response", f"音频字段不是合法 base64: {exc}"
        ) from exc


async def synthesize_http(
    backend: BackendDef,
    *,
    base_url: str,
    api_key: str | None,
    text: str,
    voice: str,
    fmt: AudioFormat,
    model: str,
    speed: float | None = None,
    instructions: str | None = None,
    extra_body: Mapping[str, Any] | None = None,
    timeout: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """Execute one templated TTS request and return the audio bytes."""
    spec = backend.http
    if spec is None:
        raise SynthesisError(
            "tts_unavailable", f"backend {backend.id} 没有 HTTP 请求定义"
        )
    method, url, headers, body, params = build_request(
        backend,
        spec,
        base_url=base_url,
        api_key=api_key,
        text=text,
        voice=voice,
        fmt=fmt,
        model=model,
        speed=speed,
        instructions=instructions,
        extra_body=extra_body,
    )
    client_kwargs: dict[str, Any] = {"timeout": timeout, "headers": headers}
    if transport is not None:
        client_kwargs["transport"] = transport
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.request(
            method, url, json=body, params=params or None
        )
    if response.status_code >= 400:
        raise SynthesisError(
            "tts_http_status",
            f"{backend.label} 返回 {response.status_code} — "
            f"{response.text[:300]}",
            status_code=response.status_code,
        )
    audio = extract_audio(spec, response)
    if not audio:
        raise SynthesisError("tts_empty", f"{backend.label} 返回了空音频")
    return audio
