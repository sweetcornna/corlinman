"""Provider-capability probes — non-destructive feature detection.

Counterpart to the operator-asserted ``image_capable`` /
``image_model`` fields on :class:`~corlinman_providers.specs.
ProviderSpec` (Wave 2.3 first-run wizard, contract §C1).

The single public entry point is :func:`probe_image_capability`,
called by the ``POST /admin/providers/{name}/probe-image`` admin
route during the easy-setup wizard's "reuse current API" step. It
**must never** invoke real image generation — generation is paid
per-request on most providers; the operator hasn't even finished
onboarding yet. Instead the probe walks two cheap signals:

1. ``GET <base_url>/v1/models`` — most OpenAI-shaped providers expose
   a free model catalog. We scan the returned ids for known image-
   model name patterns (``gpt-image-1``, ``dall-e-*``, ``flux-*``,
   ``imagen-*``, ``stable-diffusion-*``). Any hit → confirmed.

2. ``HEAD <base_url>/v1/images/generations`` — fallback when ``/models``
   doesn't list image models (or doesn't exist). A 405 / 404 means
   the endpoint isn't there → unsupported. A 200 / 2xx / 401 means the
   route exists (401 = "your auth is wrong but the route is here") →
   supported.

Both probes use a fresh :class:`httpx.AsyncClient` with a tight
10-second timeout so a hung upstream can never wedge the wizard's
finalize call. Any unhandled exception is caught and surfaced as
``supported=False`` with the exception message in ``evidence`` —
the wizard UI shows this verbatim so operators can debug.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


__all__ = ["probe_image_capability"]


# Known image-generation model id substrings / prefixes, drawn from the
# canonical OpenAI / Anthropic / Google / Replicate / Together /
# Stability AI catalogs as of 2026-Q1. We match case-insensitively and
# allow either a substring or a prefix hit — most providers either echo
# the canonical id verbatim or scope it under a vendor namespace
# (``black-forest-labs/flux-1.1-pro``).
_IMAGE_MODEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgpt-image-1\b", re.IGNORECASE),
    re.compile(r"\bdall-?e(-[23])?\b", re.IGNORECASE),
    re.compile(r"\bflux(?:[-./_].*)?\b", re.IGNORECASE),
    re.compile(r"\bimagen(?:[-./_].*)?\b", re.IGNORECASE),
    re.compile(r"\bstable-?diffusion(?:[-./_].*)?\b", re.IGNORECASE),
    re.compile(r"\bsdxl(?:[-./_].*)?\b", re.IGNORECASE),
    re.compile(r"\bmidjourney(?:[-./_].*)?\b", re.IGNORECASE),
    re.compile(r"\bplayground[-_]v\d", re.IGNORECASE),
    re.compile(r"\bkolors(?:[-./_].*)?\b", re.IGNORECASE),
    re.compile(r"\bwanx(?:[-./_].*)?\b", re.IGNORECASE),
    re.compile(r"\bcogview(?:[-./_].*)?\b", re.IGNORECASE),
)


# Probe timeout — kept short so a wedged upstream can't stall the
# first-run wizard. 10 seconds matches the existing
# ``_query_provider_models`` budget in routes_admin_b/providers.py.
_PROBE_TIMEOUT_SECS: float = 10.0


def _id_looks_like_image_model(model_id: str) -> bool:
    """Return whether ``model_id`` matches a known image-model pattern."""
    if not isinstance(model_id, str) or not model_id:
        return False
    return any(p.search(model_id) for p in _IMAGE_MODEL_PATTERNS)


def _resolve_provider_credentials(provider: Any) -> tuple[str, str | None]:
    """Read ``(api_key, base_url)`` off a provider adapter or spec.

    Tolerant of three shapes:

    * :class:`~corlinman_providers.specs.ProviderSpec` — reads
      ``api_key`` / ``base_url`` directly.
    * :class:`~corlinman_providers.base.CorlinmanProvider` adapter —
      reads ``_api_key`` / ``_base_url`` (OpenAI shape) then falls back
      to the public attributes.
    * any object with ``api_key`` / ``base_url`` attributes.

    Returns ``(api_key_or_empty_string, base_url_or_None)``. An empty
    api_key is fine — some OpenAI-shape gateways accept anonymous
    ``/models`` calls — but a missing ``base_url`` triggers the OpenAI
    default of ``https://api.openai.com``.
    """
    api_key_attr = getattr(provider, "_api_key", None) or getattr(
        provider, "api_key", None
    )
    # ``ProviderSpec.api_key`` is plain ``str | None``; provider adapters
    # may have already resolved env-ref shapes. Either way coerce to a
    # string, defaulting to "" for the empty/None case so httpx doesn't
    # synthesise a ``Bearer None`` header.
    if api_key_attr is None:
        api_key = ""
    elif isinstance(api_key_attr, str):
        api_key = api_key_attr
    elif isinstance(api_key_attr, dict):
        # Support the {"value": "..."} / {"env": "..."} shape used by
        # the admin config-file dialect. Lazily resolve env refs so the
        # caller can pass a raw ``ProviderSpec.api_key=None`` and the
        # probe still tries OPENAI_API_KEY.
        if "value" in api_key_attr:
            api_key = str(api_key_attr.get("value") or "")
        elif "env" in api_key_attr:
            env_name = str(api_key_attr.get("env") or "")
            api_key = os.environ.get(env_name, "") if env_name else ""
        else:
            api_key = ""
    else:
        api_key = str(api_key_attr)

    base_url_attr = getattr(provider, "_base_url", None) or getattr(
        provider, "base_url", None
    )
    base_url: str | None = None
    if isinstance(base_url_attr, str) and base_url_attr.strip():
        base_url = base_url_attr.strip()

    return api_key, base_url


async def _scan_models_endpoint(
    *, base_url: str, api_key: str
) -> tuple[bool, list[str], str]:
    """Probe ``GET {base_url}/v1/models`` for image-model ids.

    Returns ``(supported, matched_models, evidence)``:

    * ``supported`` — true iff at least one returned id matches an
      :data:`_IMAGE_MODEL_PATTERNS` entry.
    * ``matched_models`` — the matching ids (deduped, sorted).
    * ``evidence`` — a human-readable one-liner the admin route surfaces
      to the wizard UI verbatim.
    """
    url = base_url.rstrip("/") + "/v1/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECS) as client:
            resp = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return False, [], "models_endpoint_timeout"
    except httpx.HTTPError as exc:
        return False, [], f"models_endpoint_error: {type(exc).__name__}"

    if resp.status_code >= 400:
        return False, [], f"models_http_{resp.status_code}"

    try:
        body = resp.json()
    except ValueError:
        return False, [], "models_invalid_json"

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return False, [], "models_unexpected_shape"

    matched: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        if isinstance(mid, str) and _id_looks_like_image_model(mid):
            matched.add(mid)

    if not matched:
        return (
            False,
            [],
            f"models_endpoint_returned_{len(data)}_models_none_image_capable",
        )
    return (
        True,
        sorted(matched),
        f"matched_{len(matched)}_image_model(s)_via_/v1/models",
    )


async def _head_images_endpoint(
    *, base_url: str, api_key: str
) -> tuple[bool, str]:
    """Probe ``HEAD {base_url}/v1/images/generations`` as a fallback.

    Some OpenAI-shape proxies (and some self-hosted gateways) do not
    surface image models on ``/v1/models``. The presence of the
    ``/v1/images/generations`` route itself is the cheapest non-
    destructive signal. We treat:

    * 405 / 404                            → unsupported (route missing).
    * 200 / 2xx                            → supported.
    * 401 / 403                            → supported (route exists,
      our key is just wrong / scoped out).
    * anything else                        → unsupported with diagnostic.

    Some servers respond 405 to HEAD even when the route exists; we
    treat 405 as a soft-fail rather than a definite negative so the
    caller can decide based on the other probe.
    """
    url = base_url.rstrip("/") + "/v1/images/generations"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECS) as client:
            resp = await client.head(url, headers=headers)
    except httpx.TimeoutException:
        return False, "images_endpoint_timeout"
    except httpx.HTTPError as exc:
        return False, f"images_endpoint_error: {type(exc).__name__}"

    status = resp.status_code
    if status in (401, 403):
        return True, f"images_endpoint_authn_required_({status})"
    if 200 <= status < 300:
        return True, f"images_endpoint_ok_({status})"
    if status in (404, 405):
        return False, f"images_endpoint_not_present_({status})"
    return False, f"images_endpoint_unexpected_status_({status})"


async def probe_image_capability(provider: Any) -> dict[str, Any]:
    """Return ``{supported, evidence, models}`` for ``provider``.

    Parameters
    ----------
    provider
        Either a :class:`~corlinman_providers.specs.ProviderSpec`,
        a built :class:`~corlinman_providers.base.CorlinmanProvider`
        adapter, or any object that exposes ``api_key`` / ``base_url``
        (or the private ``_api_key`` / ``_base_url`` variants used by
        the OpenAI / OpenAI-compatible adapters).

    Returns
    -------
    dict
        ``{"supported": bool, "evidence": str, "models": list[str]}``.
        ``models`` is the list of image-capable model ids the probe
        identified (empty when probing fell back to the HEAD strategy
        or when the provider was already known to be image-capable
        without an enumerable catalog).

    Notes
    -----
    Never raises. Network / parse / auth failures are folded into a
    ``supported=False`` result with diagnostic ``evidence``.
    """
    api_key, base_url = _resolve_provider_credentials(provider)
    # Default to the OpenAI API origin so the probe still works for a
    # first-party OpenAI provider (which legitimately omits ``base_url``
    # — the adapter pulls the SDK default). Anything else MUST carry an
    # explicit ``base_url``; we don't synthesise per-vendor defaults.
    effective_base = base_url or "https://api.openai.com"

    logger.debug(
        "capabilities.probe_image.start",
        provider_name=getattr(provider, "name", None),
        base_url=effective_base,
        has_api_key=bool(api_key),
    )

    # Step 1 — scan /v1/models for known image-model ids.
    supported, matched, evidence = await _scan_models_endpoint(
        base_url=effective_base, api_key=api_key
    )
    if supported:
        logger.info(
            "capabilities.probe_image.matched_via_models",
            provider_name=getattr(provider, "name", None),
            count=len(matched),
        )
        return {
            "supported": True,
            "evidence": evidence,
            "models": list(matched),
        }

    # Step 2 — fall back to the HEAD probe on /v1/images/generations.
    fallback_supported, fallback_evidence = await _head_images_endpoint(
        base_url=effective_base, api_key=api_key
    )
    combined_evidence = f"{evidence}; {fallback_evidence}"
    if fallback_supported:
        logger.info(
            "capabilities.probe_image.matched_via_head",
            provider_name=getattr(provider, "name", None),
            evidence=fallback_evidence,
        )
        return {
            "supported": True,
            "evidence": combined_evidence,
            # No catalog enumeration when only the HEAD probe matched;
            # the caller can still default to the operator-asserted
            # image_model or the env knob.
            "models": [],
        }

    logger.info(
        "capabilities.probe_image.unsupported",
        provider_name=getattr(provider, "name", None),
        evidence=combined_evidence,
    )
    return {
        "supported": False,
        "evidence": combined_evidence,
        "models": [],
    }
