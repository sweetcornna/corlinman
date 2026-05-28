"""Provider abstraction for OpenAI image generation.

Wraps the OpenAI Responses API (``gpt-image-1`` family) for two
sibling tools:

* :func:`generate_with_refs` — reference-conditioned generation used
  by ``image_with_refs``. The prompt is paired with one or more
  character reference images, sent as base64 ``data:`` URLs.
* :func:`generate_plain` — plain generation used by ``image_generate``.
  Text-only input; no reference image conditioning. Strictly isolated
  from any persona / asset-store wiring at the dispatcher layer.

Both functions share the same endpoint, credentials resolution,
aspect-ratio mapping, env config, and response-parsing path via the
shared :func:`_post_responses_image` helper.

Initial implementation reads the provider's ``api_key`` and (optional)
``base_url`` directly off the :class:`CorlinmanProvider` adapter
instance. Both attributes are private on ``OpenAIProvider``
(``_api_key`` / ``_base_url``); we ``getattr`` them defensively so a
future provider that exposes them via a different name (or stores the
key in an SDK client) still works as long as it surfaces something at
the public ``api_key`` / ``base_url`` names. When neither pair is
populated we fall back to the ``OPENAI_API_KEY`` env var to match the
OpenAI provider's own constructor behaviour.

Config read at runtime
----------------------
* ``provider._api_key`` / ``provider.api_key`` — credential.
* ``provider._base_url`` / ``provider.base_url`` — optional override.
* ``OPENAI_API_KEY`` — env var fallback when the provider didn't carry
  a key (e.g. unit-test fakes).
* ``CORLINMAN_IMAGE_MODEL`` — env override for the model name; defaults
  to ``gpt-image-1``.
* ``CORLINMAN_IMAGE_QUALITY`` — env override for the quality knob;
  defaults to ``medium`` (matches hermes-agent's setting).
* ``CORLINMAN_IMAGE_TIMEOUT_SECS`` — HTTP timeout for the OpenAI call;
  defaults to ``120`` seconds.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any, Literal

import httpx
import structlog

logger = structlog.get_logger(__name__)


__all__ = [
    "ImageGenerationError",
    "ImageProviderUnavailable",
    "generate_plain",
    "generate_with_refs",
    "resolve_image_provider_name",
    "resolve_image_provider",
]


#: Aspect ratio → OpenAI ``size`` mapping. ``gpt-image-1`` accepts
#: 1024x1024, 1024x1536 and 1536x1024 (portrait / landscape are the
#: 1536px-on-the-long-edge variants). Captured as a small dict so the
#: tool schema can advertise only the three friendly names while the
#: provider call sticks to the literal sizes the API understands.
_ASPECT_TO_SIZE: dict[str, str] = {
    "square": "1024x1024",
    "portrait": "1024x1536",
    "landscape": "1536x1024",
}


_DEFAULT_MODEL: str = "gpt-image-1"
_DEFAULT_QUALITY: str = "medium"
_DEFAULT_TIMEOUT_SECS: float = 120.0


def resolve_image_provider_name(
    providers_cfg: dict[str, Any] | None,
    *,
    image_provider_name: str | None = None,
    default_chat_provider: str | None = None,
) -> str | None:
    """Pick the provider slot best suited for image generation.

    Resolution order, evaluated against ``providers_cfg`` (a mapping
    keyed by slot name → ``[providers.<slot>]`` block):

    1. ``image_provider_name``, when supplied AND the named slot exists
       in ``providers_cfg``. The caller wins outright — they may have
       set this from a per-channel override or the first-run wizard.
    2. The first enabled slot whose block carries
       ``image_capable = True``. Stable insertion order is preserved
       (Python 3.7+ dict guarantees) so multi-image-capable configs
       remain deterministic across reloads.
    3. ``default_chat_provider``, when supplied AND present in the
       config. The historical fall-through — keeps existing single-
       provider deployments working without operator action.
    4. ``None`` — the caller must decide whether to error or fall back
       to ``OPENAI_API_KEY`` env-only credentials.

    Designed to be cheap + side-effect-free so the agent loop can call
    it on every tool dispatch.

    Parameters
    ----------
    providers_cfg
        Mapping of slot name to provider config dict. ``None`` / empty
        mappings short-circuit to ``default_chat_provider``.
    image_provider_name
        Caller's explicit pick, e.g. from
        ``CORLINMAN_IMAGE_PROVIDER`` or a per-channel binding.
    default_chat_provider
        The default chat provider's slot name. Used only when no
        ``image_capable`` slot is found.
    """
    cfg = providers_cfg or {}
    if image_provider_name and image_provider_name in cfg:
        return image_provider_name

    for name, entry in cfg.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled", True) is False:
            continue
        if entry.get("image_capable") is True:
            return str(name)

    if default_chat_provider and default_chat_provider in cfg:
        return default_chat_provider
    return None


def resolve_image_provider(
    *,
    providers_cfg: dict[str, Any] | None,
    provider_factory: Any,
    image_provider_name: str | None = None,
    default_chat_provider: str | None = None,
    fallback_provider: Any | None = None,
) -> Any | None:
    """Materialise a provider adapter for image generation.

    Thin wrapper around :func:`resolve_image_provider_name` that also
    builds (or pulls from a cache) the concrete adapter via
    ``provider_factory(name)``. ``fallback_provider`` is returned when
    resolution falls through (no config, no match, factory raises) —
    callers typically pass the active chat provider here so
    :func:`generate_with_refs` keeps working on legacy deployments that
    haven't migrated to per-capability slots yet.

    Returns ``None`` only when both resolution and ``fallback_provider``
    are exhausted; the caller should then raise
    :class:`ImageProviderUnavailable` itself (the existing dispatchers
    already do).
    """
    name = resolve_image_provider_name(
        providers_cfg,
        image_provider_name=image_provider_name,
        default_chat_provider=default_chat_provider,
    )
    if name is None:
        return fallback_provider

    try:
        built = provider_factory(name)
    except Exception as exc:  # noqa: BLE001 — log & fall back
        logger.warning(
            "image.resolve_image_provider.factory_failed",
            provider_name=name,
            error=str(exc),
        )
        return fallback_provider

    return built if built is not None else fallback_provider


class ImageGenerationError(RuntimeError):
    """Generic failure raised by :func:`generate_with_refs` on any
    non-success path. The tool dispatcher catches this + folds the
    message into its JSON envelope so the model gets a clean error
    string."""


class ImageProviderUnavailable(ImageGenerationError):
    """Raised when no API key / base URL is reachable from the supplied
    provider. Surfaced to the tool dispatcher as
    ``"image generation not configured"`` per the PLAN."""


def _provider_credentials(provider: Any) -> tuple[str, str | None]:
    """Pull ``(api_key, base_url)`` off the provider adapter.

    Tries the documented private attributes first (``_api_key`` /
    ``_base_url`` on :class:`OpenAIProvider`) then falls back to the
    public names a future provider might use, then to the env var.
    Raises :class:`ImageProviderUnavailable` when no key is reachable.
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
    if not api_key:
        raise ImageProviderUnavailable(
            "image generation not configured — provider carries no "
            "api_key and OPENAI_API_KEY is unset"
        )
    return str(api_key), str(base_url) if base_url else None


def _ref_to_data_url(path: Path) -> str:
    """Encode a local image file as a ``data:`` URL the OpenAI API will
    accept as an ``input_image``. Streams the bytes through base64 in
    one shot — reference packs are small (<8 MiB per asset by the
    PersonaAssetStore cap) so a single read is fine."""
    mime, _ = mimetypes.guess_type(path.name)
    if not mime:
        # Default to PNG — the asset store only accepts the four image
        # MIMEs so unknown is fine to coerce.
        mime = "image/png"
    data = path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _resolve_runtime_config(provider: Any = None) -> tuple[str, str, float]:
    """Return ``(model, quality, timeout_secs)`` from env or defaults.

    Shared by every entry point in this module so the two image tools
    obey the same ``CORLINMAN_IMAGE_*`` knobs.

    Resolution order for the model id (highest precedence first):

    1. ``CORLINMAN_IMAGE_MODEL`` env var — operator override, beats
       config so on-the-fly debugging stays cheap.
    2. ``provider.image_model`` — the operator-asserted preferred model
       on the picked image provider (Wave 2 first-run wizard). Read
       defensively (``getattr``) so older callers that pass a non-spec
       adapter still hit the default.
    3. The historical default ``"gpt-image-1"``.
    """
    env_model = os.environ.get("CORLINMAN_IMAGE_MODEL")
    if env_model:
        model = env_model
    else:
        spec_model = getattr(provider, "image_model", None) if provider is not None else None
        model = str(spec_model) if isinstance(spec_model, str) and spec_model else _DEFAULT_MODEL
    quality = os.environ.get("CORLINMAN_IMAGE_QUALITY") or _DEFAULT_QUALITY
    try:
        timeout = float(
            os.environ.get("CORLINMAN_IMAGE_TIMEOUT_SECS")
            or _DEFAULT_TIMEOUT_SECS
        )
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECS
    return model, quality, timeout


async def _post_responses_image(
    *,
    api_key: str,
    base_url: str | None,
    payload: dict[str, Any],
    timeout: float,
    transport: httpx.BaseTransport | None,
) -> bytes:
    """POST ``payload`` to the Responses endpoint and decode the image.

    Owns the HTTP call, status check, JSON parse, and the two-shape
    payload extraction (``output[].image_generation_call.result`` and
    ``data[0].b64_json``). Returns raw PNG bytes; raises
    :class:`ImageGenerationError` on every non-success path.
    """
    root = (base_url or "https://api.openai.com/v1").rstrip("/")
    endpoint = f"{root}/responses"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    client_kwargs: dict[str, Any] = {
        "timeout": timeout,
        "headers": headers,
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(endpoint, json=payload)
    except httpx.TimeoutException as exc:
        raise ImageGenerationError(
            f"image_generation_timeout: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ImageGenerationError(
            f"image_generation_http_error: {exc}"
        ) from exc

    if response.status_code >= 400:
        raise ImageGenerationError(
            f"image_generation_http_status: server returned "
            f"{response.status_code} — {response.text[:500]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ImageGenerationError(
            f"image_generation_invalid_json: {exc}"
        ) from exc

    # The Responses API surfaces image-generation results in two
    # shapes depending on the model: either as a top-level ``output``
    # array containing a ``type=image_generation_call`` entry with a
    # ``result`` (base64 string), or — older / fallback — as a
    # ``data[].b64_json`` body. Accept either.
    encoded: str | None = None
    output = body.get("output")
    if isinstance(output, list):
        for entry in output:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "image_generation_call":
                result = entry.get("result")
                if isinstance(result, str) and result:
                    encoded = result
                    break
            content_arr = entry.get("content")
            if isinstance(content_arr, list):
                for part in content_arr:
                    if (
                        isinstance(part, dict)
                        and part.get("type") in ("output_image", "image")
                    ):
                        b64 = part.get("b64_json") or part.get("image_data")
                        if isinstance(b64, str) and b64:
                            encoded = b64
                            break
            if encoded is not None:
                break
    if encoded is None:
        data = body.get("data")
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                b64 = first.get("b64_json")
                if isinstance(b64, str):
                    encoded = b64

    if not encoded:
        raise ImageGenerationError(
            "image_generation_no_image: response body carried no "
            "image_generation_call.result or data[0].b64_json"
        )
    try:
        return base64.b64decode(encoded)
    except (ValueError, TypeError) as exc:
        raise ImageGenerationError(
            f"image_generation_invalid_b64: {exc}"
        ) from exc


async def generate_with_refs(
    provider: Any,
    prompt: str,
    ref_paths: list[Path],
    aspect_ratio: Literal["square", "portrait", "landscape"] = "square",
    *,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    """Generate a PNG image from ``prompt`` + character ``ref_paths``.

    Parameters
    ----------
    provider
        A :class:`CorlinmanProvider` adapter — we read ``api_key`` and
        optional ``base_url`` off it. Typed ``Any`` so unit tests can
        pass a ``SimpleNamespace`` without satisfying the full
        ``CorlinmanProvider`` Protocol.
    prompt
        The text instruction. The bound persona's voice / style is the
        caller's responsibility — we don't prepend anything.
    ref_paths
        Local filesystem paths to character reference images. Each is
        encoded as a base64 ``data:`` URL and passed as an
        ``input_image`` on the Responses API call.
    aspect_ratio
        One of ``square`` (1024x1024), ``portrait`` (1024x1536),
        ``landscape`` (1536x1024). The OpenAI API supports these three
        sizes for ``gpt-image-1``.
    transport
        Optional :mod:`httpx` test seam — production callers leave it
        ``None``.

    Returns
    -------
    bytes
        The raw PNG bytes the OpenAI API returned (base64-decoded).

    Raises
    ------
    ImageProviderUnavailable
        Provider carries no api_key and ``OPENAI_API_KEY`` is unset.
    ImageGenerationError
        OpenAI returned a non-success status or a malformed payload.
    """
    if aspect_ratio not in _ASPECT_TO_SIZE:
        raise ImageGenerationError(
            f"aspect_ratio must be one of {sorted(_ASPECT_TO_SIZE)}, "
            f"got {aspect_ratio!r}"
        )
    api_key, base_url = _provider_credentials(provider)
    model, quality, timeout = _resolve_runtime_config(provider)

    # The input is a single user message whose content list mixes the
    # prompt text + one ``input_image`` per reference path. Matches the
    # multimodal Responses contract.
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for path in ref_paths:
        content.append(
            {
                "type": "input_image",
                "image_url": _ref_to_data_url(path),
            }
        )
    payload: dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "tools": [
            {
                "type": "image_generation",
                "size": _ASPECT_TO_SIZE[aspect_ratio],
                "quality": quality,
            }
        ],
    }

    return await _post_responses_image(
        api_key=api_key,
        base_url=base_url,
        payload=payload,
        timeout=timeout,
        transport=transport,
    )


async def generate_plain(
    provider: Any,
    prompt: str,
    aspect_ratio: Literal["square", "portrait", "landscape"] = "square",
    *,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    """Generate a PNG image from ``prompt`` alone — no reference images.

    Sibling of :func:`generate_with_refs`: same endpoint, same model,
    same env config, but the Responses ``input`` carries only a single
    ``input_text`` part. Intentionally has no ``ref_paths`` argument —
    callers that want reference conditioning use the other function.

    Parameters
    ----------
    provider
        A :class:`CorlinmanProvider` adapter — credentials are read off
        it (see :func:`_provider_credentials`).
    prompt
        The text instruction. Caller is responsible for style / voice.
    aspect_ratio
        One of ``square`` / ``portrait`` / ``landscape``. Maps to
        ``1024x1024`` / ``1024x1536`` / ``1536x1024`` respectively.
    transport
        Optional :mod:`httpx` test seam.

    Returns
    -------
    bytes
        Raw PNG bytes returned by the OpenAI API (base64-decoded).

    Raises
    ------
    ImageProviderUnavailable
        Provider carries no api_key and ``OPENAI_API_KEY`` is unset.
    ImageGenerationError
        OpenAI returned a non-success status or a malformed payload.
    """
    if aspect_ratio not in _ASPECT_TO_SIZE:
        raise ImageGenerationError(
            f"aspect_ratio must be one of {sorted(_ASPECT_TO_SIZE)}, "
            f"got {aspect_ratio!r}"
        )
    api_key, base_url = _provider_credentials(provider)
    model, quality, timeout = _resolve_runtime_config(provider)

    payload: dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "tools": [
            {
                "type": "image_generation",
                "size": _ASPECT_TO_SIZE[aspect_ratio],
                "quality": quality,
            }
        ],
    }

    return await _post_responses_image(
        api_key=api_key,
        base_url=base_url,
        payload=payload,
        timeout=timeout,
        transport=transport,
    )
