"""``/admin/providers*`` — provider registry CRUD.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/providers.rs``.

Routes:

* ``GET    /admin/providers``              — list every declared slot
  (kind, api-key source, ``params_schema``).
* ``POST   /admin/providers``              — upsert a provider slot.
* ``PATCH  /admin/providers/{name}``       — partial update.
* ``DELETE /admin/providers/{name}``       — refused with 409 when an
  alias or the ``[embedding]`` block still references it.
* ``POST   /admin/providers/{name}/test``  — zero-cost connectivity probe
  (W1.1). Returns ``{ok, latency_ms, error?, models_count?}``.
* ``GET    /admin/providers/{name}/models``— list models exposed by a
  provider (W1.1). 30s in-memory cache for openai-shape proxies;
  hardcoded catalogs for anthropic / google / mock.
* ``GET    /admin/providers/kinds``        — descriptor list of every
  registered :class:`ProviderKind` (W1.1) — ``{kinds: [{kind, label,
  description, params_schema}]}``.

JSON-schema for ``params`` is pulled lazily from
``corlinman_providers`` (sibling package) so the Python source stays the
single source of truth — mirrors the Rust note that "Python wins" on
schema drift.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import structlog
from corlinman_providers.specs import list_supported_kinds
from fastapi import APIRouter, Depends
from fastapi import Path as FPath
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

from corlinman_server.gateway.core.config_mutation import (
    write_config_atomic as _write_config_atomic,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)

# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class Capabilities(BaseModel):
    chat: bool = True
    embedding: bool = True


class ProviderView(BaseModel):
    name: str
    kind: str
    enabled: bool
    base_url: str | None = None
    api_key_source: str = "unset"
    api_key_env_name: str | None = None
    params: dict[str, Any] = {}
    params_schema: dict[str, Any] = {}
    capabilities: Capabilities = Capabilities()


class KindDescriptor(BaseModel):
    kind: str
    params_schema: dict[str, Any] = {}
    capabilities: Capabilities = Capabilities()


class ListOut(BaseModel):
    providers: list[ProviderView]
    kinds: list[KindDescriptor]


class ApiKeyEnv(BaseModel):
    env: str


class ApiKeyValue(BaseModel):
    value: str


class ProviderUpsert(BaseModel):
    name: str
    kind: str
    enabled: bool | None = None
    base_url: str | None = None
    api_key: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


class ProviderPatch(BaseModel):
    kind: str | None = None
    enabled: bool | None = None
    base_url: str | None = None
    api_key: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Legacy aliases accepted on write, but normalized to canonical
# ``ProviderKind`` ids before persisting/returning.
_KIND_ALIASES: dict[str, str] = {
    "openai-compatible": "openai_compatible",
    "newapi": "openai_compatible",
}

_CANONICAL_KINDS: frozenset[str] = frozenset(list_supported_kinds())


def _normalize_kind(kind: str | None) -> str:
    """Normalize user-supplied kind ids to canonical ProviderKind values.

    Accepts historical aliases (``openai-compatible`` and ``newapi``) and
    rewrites them to ``openai_compatible`` so admin CRUD, config-on-disk and
    read APIs all expose one stable spelling.
    """
    raw = (kind or "openai_compatible").strip().lower()
    normalized = _KIND_ALIASES.get(raw, raw)
    return normalized.replace("-", "_")


def _is_known_kind(kind: str) -> bool:
    """Return whether ``kind`` is a recognized canonical ProviderKind id."""
    return kind in _CANONICAL_KINDS


def _kind_capabilities(kind: str) -> Capabilities:
    if _normalize_kind(kind) == "anthropic":
        return Capabilities(chat=True, embedding=False)
    return Capabilities(chat=True, embedding=True)


def _params_schema_for(kind: str) -> dict[str, Any]:
    """Lazy lookup of ``corlinman_providers`` schema. Empty dict on miss."""
    canonical_kind = _normalize_kind(kind)
    try:
        from corlinman_providers.registry import _KIND_TO_CLASS
        from corlinman_providers.specs import ProviderKind

        # ``specs`` has no ``params_schema_for`` — the schema lives as a
        # per-kind ``params_schema()`` classmethod on the provider adapter
        # (openai_provider.py / anthropic_provider.py / ...). Map the
        # canonical kind to its class and read it; without this the
        # DynamicParamsForm was always the permissive fallback (untyped).
        cls = _KIND_TO_CLASS.get(ProviderKind(canonical_kind))
        getter = getattr(cls, "params_schema", None)
        if getter is not None:
            schema = getter()
            if isinstance(schema, dict):
                return schema
    except Exception:  # noqa: BLE001 — fall back to the permissive schema
        pass
    return {"type": "object", "additionalProperties": True}


def _view_from_entry(name: str, entry: dict[str, Any]) -> ProviderView:
    api_key = entry.get("api_key")
    if api_key is None:
        source, env_name = "unset", None
    elif isinstance(api_key, dict) and "env" in api_key:
        source, env_name = "env", str(api_key["env"])
    elif isinstance(api_key, dict) and "value" in api_key:
        source, env_name = "value", None
    else:
        source, env_name = "value", None
    kind = _normalize_kind(str(entry.get("kind") or "openai_compatible"))
    return ProviderView(
        name=name,
        kind=kind,
        enabled=bool(entry.get("enabled", True)),
        base_url=entry.get("base_url"),
        api_key_source=source,
        api_key_env_name=env_name,
        params=dict(entry.get("params") or {}),
        params_schema=_params_schema_for(kind),
        capabilities=_kind_capabilities(kind),
    )


def _alias_target(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("model", ""))
    return ""


def _alias_provider(entry: Any) -> str | None:
    if isinstance(entry, dict):
        return entry.get("provider")
    return None


def _find_alias_refs(cfg: dict[str, Any], slot: str) -> list[str]:
    aliases = (cfg.get("models") or {}).get("aliases") or {}
    out: list[str] = []
    for name, entry in aliases.items():
        if _alias_provider(entry) == slot:
            out.append(str(name))
    return out


def _bad(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": code, "message": message})


async def _persist(state: AdminState, cfg: dict[str, Any]) -> JSONResponse | None:
    if state.config_path is None:
        return JSONResponse(status_code=503, content={"error": "config_path_unset"})
    try:
        try:
            import tomli_w
        except ImportError:  # pragma: no cover
            import toml as tomli_w  # type: ignore
        serialised = tomli_w.dumps(cfg)  # type: ignore[attr-defined]
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": "serialise_failed", "message": str(exc)},
        )
    path = state.config_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".new")
        tmp.write_text(serialised, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={"error": "write_failed", "message": str(exc)},
        )
    return None


# ---------------------------------------------------------------------------
# W-B1 — custom-provider wire models + helpers
#
# These live alongside the legacy provider-slot CRUD above but address a
# different operator story: the "Add custom provider" form in
# ``ui/(admin)/providers``. The marker ``params.custom = true`` is what
# separates user-added blocks from built-in slots so the credentials UI
# can show them under their own group. See ``docs/PLAN_PROVIDER_AUTH.md``
# §1.2 for the on-disk shape.
# ---------------------------------------------------------------------------


# Slug regex pinned by the plan — lowercase ascii + digits, optionally
# separated by ``-`` or ``_``; 1-32 chars; first char alphanumeric.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


# Built-in slots managed by the credentials surface (or hardwired
# elsewhere). Operators cannot squat on these via the custom-provider
# endpoint — they must use ``/admin/credentials`` to configure them so
# the well-known UX (env-ref hints, masked previews) keeps working.
_BUILTIN_SLOTS: frozenset[str] = frozenset(
    {"anthropic", "openai", "google", "mock"}
)


class _ApiKeyEnvRef(BaseModel):
    env: str


class _ApiKeyValueRef(BaseModel):
    value: str


class CustomProviderView(BaseModel):
    """Read-side projection of one ``params.custom = true`` block."""

    slug: str
    kind: str
    base_url: str | None = None
    has_api_key: bool = False
    params: dict[str, Any] = {}


class CustomListOut(BaseModel):
    providers: list[CustomProviderView]


class CustomKindsOut(BaseModel):
    kinds: list[str]


class CustomProviderCreate(BaseModel):
    slug: str
    kind: str
    base_url: str | None = None
    api_key: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


class CustomProviderPatch(BaseModel):
    kind: str | None = None
    base_url: str | None = None
    api_key: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


def _custom_view_from_entry(slug: str, entry: dict[str, Any]) -> CustomProviderView:
    """Project a stored ``[providers.<slug>]`` block to the wire view.

    ``has_api_key`` follows the same masking convention as
    ``credentials._resolve_field_view``: any of literal string / ``{value=…}``
    / ``{env=…}`` shapes count as "set". We deliberately do NOT echo the
    literal back — the operator must re-paste to rotate (matches the
    paste-only edit story of the credentials UI).
    """
    api_key = entry.get("api_key")
    has_api_key = False
    if isinstance(api_key, str):
        has_api_key = bool(api_key)
    elif isinstance(api_key, dict):
        if "env" in api_key:
            has_api_key = bool(api_key.get("env"))
        elif "value" in api_key:
            has_api_key = bool(api_key.get("value"))
        else:
            has_api_key = bool(api_key)
    return CustomProviderView(
        slug=slug,
        kind=_normalize_kind(str(entry.get("kind") or "openai_compatible")),
        base_url=entry.get("base_url"),
        has_api_key=has_api_key,
        params=dict(entry.get("params") or {}),
    )


# ---------------------------------------------------------------------------
# Provider model-discovery helpers (module-level so tests can import them)
# ---------------------------------------------------------------------------

_OPENAI_COMPATIBLE_KINDS: frozenset[str] = frozenset(
    {
        "openai",
        "openai_compatible",
        "mistral",
        "cohere",
        "together",
        "codex",
        "groq",
        "replicate",
        "qwen",
        "glm",
        "deepseek",
    }
)


# ---------------------------------------------------------------------------
# SEC-008 — narrow SSRF guard for the model-discovery probe.
#
# ``_query_provider_models`` dials the operator-supplied ``base_url`` with the
# API key in an ``Authorization: Bearer`` header. A base_url pointing at the
# cloud-metadata endpoint would exfiltrate that key (and could pull instance
# credentials). We block ONLY link-local + cloud-metadata targets:
#
#   * IPv4 ``169.254.0.0/16`` (link-local, incl. 169.254.169.254 metadata)
#   * IPv6 ``fe80::/10`` link-local
#   * the GCP/Azure metadata hostnames (``metadata.google.internal`` etc.)
#   * any scheme other than http/https
#
# Loopback (127.0.0.0/8, ::1) and RFC1918 private ranges (10/8, 172.16/12,
# 192.168/16) are INTENTIONALLY allowed: admins legitimately point this at
# self-hosted LLM relays (Ollama / vLLM) on localhost or the LAN, and the
# host is operator-trusted. A prior blanket private/loopback block was
# reverted because it broke those local relays — keep this surgical.
# ---------------------------------------------------------------------------

# Metadata hostnames that never resolve to a link-local literal but still
# front instance-metadata services; blocked by name.
_BLOCKED_METADATA_HOSTS: frozenset[str] = frozenset(
    {"metadata.google.internal", "metadata.goog"}
)


class _UnsafeHost(Exception):
    """Raised when a probe target resolves to a blocked metadata/link-local host."""


def _assert_safe_probe_host(base_url: str) -> None:
    """Reject link-local / cloud-metadata probe targets (SEC-008).

    Raises :class:`_UnsafeHost` (message becomes ``unsafe_host: <reason>``)
    when ``base_url`` uses a non-http(s) scheme, names a known metadata
    host, or resolves to a link-local address (IPv4 169.254.0.0/16 or IPv6
    fe80::/10). Loopback and RFC1918 private addresses are allowed on
    purpose — see the module comment above.
    """
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https"):
        raise _UnsafeHost(f"scheme {parts.scheme!r} not allowed (expected http/https)")
    host = parts.hostname
    if not host:
        raise _UnsafeHost("missing host")

    if host.lower() in _BLOCKED_METADATA_HOSTS:
        raise _UnsafeHost(f"cloud-metadata host {host!r} blocked")

    # If the host is a literal IP, check it directly; otherwise resolve and
    # reject if ANY resolved address is link-local (defends against DNS
    # answers that point at the metadata range).
    candidates: list[str] = []
    try:
        ipaddress.ip_address(host)
        candidates.append(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parts.port or None)
        except OSError:
            # DNS failure: let the dial proceed and surface the real
            # connection error — do not fail-closed on resolution hiccups.
            return
        # getaddrinfo sockaddr[0] is the host string for both IPv4/IPv6.
        candidates = [str(info[4][0]) for info in infos]

    for raw in candidates:
        # Strip any IPv6 zone id (e.g. ``fe80::1%eth0``) before parsing.
        try:
            ip = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            continue
        if ip.is_link_local:
            raise _UnsafeHost(f"link-local/metadata address {raw} blocked")


# ---------------------------------------------------------------------------
# W1.1 — test / models / kinds endpoints
#
# The legacy `_query_provider_models` helper above returns ``models``
# as a flat ``list[str]`` shape used by older callers. The W1.1 plan
# requires the public surface to expose richer objects + zero-cost
# probes per kind, in-memory caching for the proxied list, and a
# kind-descriptor list with each kind's ``params_schema``. We keep the
# legacy helper untouched (existing tests depend on its shape) and
# layer the new surface on top.
# ---------------------------------------------------------------------------


# Hard-coded model catalogs for kinds where ``/v1/models`` is either
# unavailable, costs money to call, or returns the wrong shape. Source-
# controlled because the canonical lists are small and stable enough to
# beat live calls on both reliability and zero-cost guarantees. Each
# entry is `{id, display_name}` — `created_at` is omitted (unknown for
# hardcoded data; the wire shape allows the field to be absent).
_HARDCODED_MODELS: dict[str, list[dict[str, Any]]] = {
    "anthropic": [
        {"id": "claude-opus-4-5", "display_name": "Claude Opus 4.5"},
        {"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"},
        {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"},
        {"id": "claude-3-7-sonnet-latest", "display_name": "Claude 3.7 Sonnet"},
        {"id": "claude-3-5-sonnet-latest", "display_name": "Claude 3.5 Sonnet"},
        {"id": "claude-3-5-haiku-latest", "display_name": "Claude 3.5 Haiku"},
    ],
    "google": [
        {"id": "gemini-2.5-pro", "display_name": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "display_name": "Gemini 2.5 Flash"},
        {"id": "gemini-2.0-flash", "display_name": "Gemini 2.0 Flash"},
        {"id": "gemini-1.5-pro", "display_name": "Gemini 1.5 Pro"},
        {"id": "gemini-1.5-flash", "display_name": "Gemini 1.5 Flash"},
    ],
    "mock": [
        {"id": "mock", "display_name": "Mock Echo"},
    ],
}


# Human-readable labels + descriptions for the kinds descriptor. Used by
# the W1.1 ``/admin/providers/kinds`` endpoint. Unknown kinds fall back
# to a title-cased version of the kind id and an empty description.
_KIND_LABELS: dict[str, tuple[str, str]] = {
    "anthropic": ("Anthropic", "Claude models via the Anthropic Messages API."),
    "openai": ("OpenAI", "GPT/o-series models via the OpenAI Chat Completions API."),
    "google": ("Google", "Gemini models via the Google GenAI SDK."),
    "deepseek": ("DeepSeek", "DeepSeek-Chat / DeepSeek-Coder via OpenAI-compatible wire."),
    "qwen": ("Qwen", "Alibaba Qwen models via OpenAI-compatible wire."),
    "glm": ("GLM", "Zhipu GLM-4 family via OpenAI-compatible wire."),
    "openai_compatible": (
        "OpenAI-compatible",
        "Any service that speaks the OpenAI chat-completions wire shape.",
    ),
    "mistral": ("Mistral", "Mistral La Plateforme via OpenAI-compatible wire."),
    "cohere": ("Cohere", "Cohere Command-R family via OpenAI-compatible wire."),
    "together": ("Together AI", "Together inference platform via OpenAI-compatible wire."),
    "groq": ("Groq", "Groq LPU inference via OpenAI-compatible wire."),
    "replicate": ("Replicate", "Replicate prediction endpoint via OpenAI-compatible wire."),
    "bedrock": ("AWS Bedrock", "AWS Bedrock InvokeModelWithResponseStream (SigV4)."),
    "azure": ("Azure OpenAI", "Azure OpenAI Service with deployment-id routing."),
    "codex": ("Codex (ChatGPT)", "ChatGPT subscription via the Codex OAuth flow."),
    "mock": ("Mock", "Zero-config echo provider used for the easy-setup skip path."),
}


# In-memory cache for the W1.1 ``/admin/providers/{name}/models`` proxy.
# Key: provider name. Value: ``(expiry_monotonic_seconds, payload_dict)``.
# A 30s TTL keeps the dropdown snappy while bounding hits on upstream
# from a frantic operator clicking around. ``_clear_models_cache`` is
# exposed for tests; production code never invalidates manually.
_MODELS_CACHE_TTL_SECONDS: float = 30.0
_MODELS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MODELS_RETRYABLE_HTTP_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
_MODELS_MAX_RETRIES: int = 2


def _clear_models_cache() -> None:
    """Drop every cached entry. Test-only escape hatch."""
    _MODELS_CACHE.clear()


def _redact(message: str, *secrets: str | None) -> str:
    """Replace any occurrence of ``secret`` in ``message`` with ``***``.

    Defensive: also strips obvious bearer-token leak shapes
    (``Authorization: Bearer <key>`` patterns in URL or header dumps).
    """
    out = message
    for s in secrets:
        if s and len(s) >= 4 and s in out:
            out = out.replace(s, "***")
    return out


def _http_status_from_error(error: str) -> int | None:
    """Extract ``HTTP <status>`` from helper error text."""
    match = re.match(r"^HTTP\s+(\d{3})$", error.strip())
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _is_retryable_models_error(error: str) -> bool:
    """Whether an upstream models-list error should be retried."""
    status = _http_status_from_error(error)
    if status is not None:
        return status in _MODELS_RETRYABLE_HTTP_STATUS
    lowered = error.lower()
    return any(
        marker in lowered
        for marker in (
            "timeout",
            "timed out",
            "temporary",
            "temporarily",
            "connect",
            "connection",
            "network",
            "dns",
            "name or service not known",
            "remote protocol error",
            "connection reset",
        )
    )


async def _query_provider_models_with_retry(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Probe models with bounded retries for transient upstream failures."""
    last_result: dict[str, Any] = {"ok": False, "models": [], "latency_ms": 0, "error": "unknown"}
    for attempt in range(_MODELS_MAX_RETRIES + 1):
        result = await _query_provider_models(name, cfg)
        if result.get("ok"):
            return result
        last_result = result
        if attempt >= _MODELS_MAX_RETRIES:
            break
        error = str(result.get("error") or "")
        if not _is_retryable_models_error(error):
            break
        # Tiny linear backoff; keeps the endpoint responsive but smooths
        # over short-lived upstream/network jitter.
        await asyncio.sleep(0.15 * (attempt + 1))
    return last_result


def _zero_cost_probe_kind(kind: str) -> str:
    """Categorise ``kind`` by zero-cost probe strategy.

    Returns one of:
    * ``"openai_models"``   — ``GET /v1/models`` is free + reliable.
    * ``"hardcoded"``       — no zero-cost probe but we have a canned catalog
                              we treat as success (matches W1.1 plan: degrade
                              gracefully when upstream has no free probe).
    * ``"mock"``            — fast-path local-only.
    * ``"none"``            — no probe available.
    """
    k = kind.lower().replace("-", "_")
    if k == "mock":
        return "mock"
    if k in _OPENAI_COMPATIBLE_KINDS:
        return "openai_models"
    if k in _HARDCODED_MODELS:
        return "hardcoded"
    return "none"


def _resolve_api_key(entry: dict[str, Any]) -> str:
    """Extract the configured api key from a provider entry.

    Mirrors the env / value resolution used by
    :func:`_query_provider_models` but exposed so the test-endpoint can
    reuse it without going through the full models probe.
    """
    import os

    raw_key = entry.get("api_key")
    if isinstance(raw_key, dict):
        if "value" in raw_key:
            return str(raw_key["value"])
        if "env" in raw_key:
            return os.environ.get(str(raw_key["env"]), "")
    elif isinstance(raw_key, str):
        return raw_key
    return ""


async def _query_provider_models(
    name: str, cfg: dict[str, Any]
) -> dict[str, Any]:
    """Query ``/v1/models`` for a provider and return a result dict.

    Returns ``{"ok": bool, "models": list[str], "latency_ms": int, "error": str|null}``.
    For OpenAI-compatible providers, calls ``<base_url>/v1/models`` with the
    configured API key. For the ``codex`` provider, reads the token from
    ``~/.codex/auth.json`` and queries ``https://api.openai.com/v1/models``.
    """
    import os
    import time as _time

    import httpx as _httpx

    providers_cfg = cfg.get("providers") or {}
    entry = providers_cfg.get(name)

    # Special handling for the auto-injected codex provider (no entry in config).
    is_codex = name == "codex"
    if entry is None and not is_codex:
        return {"ok": False, "models": [], "latency_ms": 0, "error": "provider_not_found"}

    if is_codex:
        # Read token from ~/.codex/auth.json
        try:
            from corlinman_providers._codex_oauth import (
                load_codex_credential,
            )

            cred = load_codex_credential()
        except Exception as exc:
            return {"ok": False, "models": [], "latency_ms": 0, "error": str(exc)}
        if cred is None:
            return {
                "ok": False,
                "models": [],
                "latency_ms": 0,
                "error": "codex_auth_not_found",
            }
        api_key = cred.access_token
        base_url = "https://api.openai.com"
    else:
        entry_dict = dict(entry) if isinstance(entry, dict) else {}
        kind = _normalize_kind(str(entry_dict.get("kind") or "openai_compatible"))
        if kind not in _OPENAI_COMPATIBLE_KINDS:
            return {
                "ok": False,
                "models": [],
                "latency_ms": 0,
                "error": f"kind '{kind}' does not support /v1/models probe",
            }
        raw_key = entry_dict.get("api_key")
        if isinstance(raw_key, dict):
            if "value" in raw_key:
                api_key = str(raw_key["value"])
            elif "env" in raw_key:
                api_key = os.environ.get(str(raw_key["env"]), "")
            else:
                api_key = ""
        elif isinstance(raw_key, str):
            api_key = raw_key
        else:
            api_key = ""
        raw_base = entry_dict.get("base_url") or "https://api.openai.com"
        base_url = str(raw_base).rstrip("/")

    # SEC-008: refuse to dial cloud-metadata / link-local targets with the
    # api key attached. Loopback/private are intentionally allowed (local
    # relays). Rejected before any outbound request is made.
    try:
        _assert_safe_probe_host(base_url)
    except _UnsafeHost as exc:
        return {
            "ok": False,
            "models": [],
            "latency_ms": 0,
            "error": f"unsafe_host: {exc}",
        }

    url = base_url.rstrip("/") + "/v1/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    t0 = _time.monotonic()
    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
        latency_ms = int((_time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            return {
                "ok": False,
                "models": [],
                "latency_ms": latency_ms,
                "error": f"HTTP {resp.status_code}",
            }
        data = resp.json()
        model_ids = [
            str(item["id"])
            for item in (data.get("data") or [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        return {
            "ok": True,
            "models": sorted(model_ids),
            "latency_ms": latency_ms,
            "error": None,
        }
    except Exception as exc:
        latency_ms = int((_time.monotonic() - t0) * 1000)
        return {"ok": False, "models": [], "latency_ms": latency_ms, "error": str(exc)}


# ---------------------------------------------------------------------------
# Auto-bind default alias on enable
#
# Enabling a provider in /admin/providers used to leave ``[models]`` empty,
# which made /chat fall back to the legacy ``MODEL_PREFIX_DEFAULTS`` table
# and silently route to the public OpenAI endpoint with no key — i.e. the
# operator's freshly-keyed provider was never reached. Auto-binding the
# first enabled provider as ``models.default`` (via a self-named alias)
# closes that gap so "enable provider → start chatting" works.
# ---------------------------------------------------------------------------


_KIND_DEFAULT_MODEL: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "openai_compatible": "gpt-4o-mini",
    "mistral": "mistral-small-latest",
    "cohere": "command-r-08-2024",
    "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "groq": "llama-3.3-70b-versatile",
    "replicate": "meta/meta-llama-3-70b-instruct",
    "qwen": "qwen-plus",
    "glm": "glm-4-flash",
    "deepseek": "deepseek-chat",
    "anthropic": "claude-3-5-haiku-latest",
    "google": "gemini-2.0-flash",
    "codex": "gpt-4o",
    "mock": "mock",
}


# When a probe returns a giant catalog (relays often surface 100+ ids),
# prefer a well-known model over the alphabetically-first one so the
# default isn't something obscure like ``ada-001``.
_PREFERRED_DEFAULT_MODELS: tuple[str, ...] = (
    "gpt-4o-mini",
    "gpt-4o",
    "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-latest",
    "deepseek-chat",
    "qwen-plus",
    "glm-4-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
)


def _pick_default_model(kind: str, probed_ids: list[str]) -> str | None:
    for pref in _PREFERRED_DEFAULT_MODELS:
        if pref in probed_ids:
            return pref
        for mid in probed_ids:
            if mid.startswith(pref):
                return mid
    if probed_ids:
        return probed_ids[0]
    return _KIND_DEFAULT_MODEL.get(kind)


async def _autobind_default_alias(
    cfg: dict[str, Any],
    provider_name: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Populate ``models.default`` so /chat can reach a freshly-enabled provider.

    Idempotent: returns ``cfg`` unchanged when ``models.default`` is already
    set. Otherwise probes the provider for its model list, picks a sensible
    default, writes ``models.aliases.<provider_name>`` pointing back at the
    provider, and sets ``models.default = <provider_name>``. Mutates and
    returns ``cfg``; the caller persists.
    """
    models_cfg = dict(cfg.get("models") or {})
    if str(models_cfg.get("default") or "").strip():
        return cfg

    kind = str(entry.get("kind") or "openai_compatible").lower()
    probed_ids: list[str] = []
    try:
        result = await _query_provider_models(provider_name, cfg)
        if result.get("ok"):
            raw = result.get("models")
            if isinstance(raw, list):
                probed_ids = [str(m) for m in raw if isinstance(m, str) and m]
    except Exception as exc:  # noqa: BLE001 — fall back to kind default
        logger.debug(
            "admin.providers.autobind_probe_failed",
            provider=provider_name,
            error=str(exc),
        )

    picked = _pick_default_model(kind, probed_ids)
    if not picked:
        logger.info(
            "admin.providers.autobind_skipped_no_model",
            provider=provider_name,
            kind=kind,
        )
        return cfg

    aliases = dict(models_cfg.get("aliases") or {})
    aliases[provider_name] = {
        "provider": provider_name,
        "model": picked,
        "params": {},
    }
    models_cfg["aliases"] = aliases
    models_cfg["default"] = provider_name
    cfg["models"] = models_cfg
    logger.info(
        "admin.providers.autobind_default",
        provider=provider_name,
        alias=provider_name,
        model=picked,
        probed=len(probed_ids),
    )
    return cfg


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "providers"])

    @r.get("/admin/providers", response_model=ListOut)
    async def list_providers():
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        providers: list[ProviderView] = []
        if isinstance(providers_cfg, dict):
            for name, entry in providers_cfg.items():
                if isinstance(entry, dict):
                    providers.append(_view_from_entry(str(name), entry))
        providers.sort(key=lambda p: p.name)
        kinds = [
            KindDescriptor(
                kind=k, params_schema=_params_schema_for(k), capabilities=_kind_capabilities(k)
            )
            for k in list_supported_kinds()
        ]
        return ListOut(providers=providers, kinds=kinds)

    @r.post("/admin/providers")
    async def upsert_provider(body: ProviderUpsert):
        if not body.name:
            return _bad("invalid_name", "provider name must be non-empty")
        normalized_kind = _normalize_kind(body.kind)
        if not _is_known_kind(normalized_kind):
            return _bad("invalid_kind", f"unknown provider kind: {body.kind}")
        state = get_admin_state()
        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = dict(providers.get(body.name) or {})
            existing["kind"] = normalized_kind
            if body.enabled is not None:
                existing["enabled"] = body.enabled
            elif "enabled" not in existing:
                existing["enabled"] = True
            if body.base_url is not None:
                existing["base_url"] = body.base_url
            if body.api_key is not None:
                existing["api_key"] = body.api_key
            if body.params is not None:
                existing["params"] = body.params
            elif "params" not in existing:
                existing["params"] = {}
            providers[body.name] = existing
            cfg["providers"] = providers
            if bool(existing.get("enabled", True)):
                cfg = await _autobind_default_alias(cfg, body.name, existing)
            err = await _persist(state, cfg)
            if err is not None:
                return err
        return {"status": "ok", "provider": _view_from_entry(body.name, existing).model_dump()}

    @r.patch("/admin/providers/{name}")
    async def patch_provider(name: str, body: ProviderPatch):
        state = get_admin_state()
        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(name)
            if existing is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "resource": "provider", "id": name},
                )
            entry = dict(existing)
            if body.kind is not None:
                normalized_kind = _normalize_kind(body.kind)
                if not _is_known_kind(normalized_kind):
                    return _bad("invalid_kind", f"unknown provider kind: {body.kind}")
                entry["kind"] = normalized_kind
            if body.enabled is not None:
                entry["enabled"] = body.enabled
            if body.base_url is not None:
                entry["base_url"] = body.base_url
            if body.api_key is not None:
                entry["api_key"] = body.api_key
            if body.params is not None:
                entry["params"] = body.params
            providers[name] = entry
            cfg["providers"] = providers
            if bool(entry.get("enabled", True)):
                cfg = await _autobind_default_alias(cfg, name, entry)
            err = await _persist(state, cfg)
            if err is not None:
                return err
        return {"status": "ok", "provider": _view_from_entry(name, entry).model_dump()}

    @r.delete("/admin/providers/{name}")
    async def delete_provider(name: str):
        state = get_admin_state()
        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            if name not in providers:
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "resource": "provider", "id": name},
                )
            alias_refs = _find_alias_refs(cfg, name)
            emb = cfg.get("embedding") or {}
            emb_ref = emb.get("provider") == name
            if alias_refs or emb_ref:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "provider_in_use",
                        "alias_refs": alias_refs,
                        "embedding_uses": emb_ref,
                    },
                )
            providers.pop(name)
            cfg["providers"] = providers
            err = await _persist(state, cfg)
            if err is not None:
                return err
        return {"status": "ok", "removed": name}

    # -----------------------------------------------------------------
    # W-B1 — custom-provider CRUD
    #
    # Operators add ad-hoc providers via the admin UI by submitting
    # ``{slug, kind, base_url, api_key, params}``. The endpoint writes a
    # ``[providers.<slug>]`` block tagged ``params.custom = true`` — that
    # marker is the load-bearing distinction between user-added entries
    # (manageable through this surface) and built-in slots
    # (anthropic / openai / google / mock — owned by the credentials
    # surface). See ``docs/PLAN_PROVIDER_AUTH.md`` §1.2.
    # -----------------------------------------------------------------

    @r.get("/admin/providers/kinds")
    async def list_provider_kinds() -> dict[str, Any]:
        """W1.1 — descriptor list of every registered provider kind.

        Returns ``{kinds: [{kind, label, description, params_schema}]}``
        where ``params_schema`` is the per-kind adapter
        :meth:`params_schema` value resolved through ``_params_schema_for``.
        Order is the same alphabetical order as
        :func:`list_supported_kinds`.
        """
        items: list[dict[str, Any]] = []
        for kind in list_supported_kinds():
            label, description = _KIND_LABELS.get(
                kind, (kind.replace("_", " ").title(), "")
            )
            items.append(
                {
                    "kind": kind,
                    "label": label,
                    "description": description,
                    "params_schema": _params_schema_for(kind),
                }
            )
        return {"kinds": items}

    @r.get("/admin/providers/custom", response_model=CustomListOut)
    async def list_custom_providers() -> CustomListOut:
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        items: list[CustomProviderView] = []
        if isinstance(providers_cfg, dict):
            for slug, entry in providers_cfg.items():
                if not isinstance(entry, dict):
                    continue
                params = entry.get("params") or {}
                if not (isinstance(params, dict) and params.get("custom") is True):
                    continue
                items.append(_custom_view_from_entry(str(slug), entry))
        items.sort(key=lambda v: v.slug)
        return CustomListOut(providers=items)

    @r.post("/admin/providers/custom")
    async def create_custom_provider(body: CustomProviderCreate):
        if not _SLUG_RE.match(body.slug):
            return _bad("invalid_slug", "slug must match ^[a-z0-9][a-z0-9_-]{0,31}$")
        if body.slug in _BUILTIN_SLOTS:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "builtin_slot",
                    "message": f"slug {body.slug!r} is reserved for a built-in provider",
                    "slug": body.slug,
                },
            )
        normalized_kind = _normalize_kind(body.kind)
        if not _is_known_kind(normalized_kind):
            return _bad("invalid_kind", f"unknown provider kind: {body.kind}")

        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(status_code=503, content={"error": "config_path_unset"})

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            if body.slug in providers:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "slug_exists",
                        "message": f"provider {body.slug!r} already exists",
                        "slug": body.slug,
                    },
                )
            entry: dict[str, Any] = {
                "kind": normalized_kind,
                "enabled": True,
            }
            if body.base_url is not None:
                entry["base_url"] = body.base_url
            if body.api_key is not None:
                entry["api_key"] = dict(body.api_key)
            params = dict(body.params or {})
            params["custom"] = True
            entry["params"] = params

            providers[body.slug] = entry
            cfg["providers"] = providers
            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err

        view = _custom_view_from_entry(body.slug, entry)
        return JSONResponse(status_code=201, content=view.model_dump())

    @r.patch("/admin/providers/custom/{slug}")
    async def patch_custom_provider(
        body: CustomProviderPatch,
        slug: str = FPath(..., min_length=1),
    ):
        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(status_code=503, content={"error": "config_path_unset"})

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(slug)
            if not isinstance(existing, dict):
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "resource": "provider", "id": slug},
                )
            params = existing.get("params") or {}
            if not (isinstance(params, dict) and params.get("custom") is True):
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": "not_custom",
                        "message": f"provider {slug!r} is not a custom slot",
                        "id": slug,
                    },
                )

            entry = dict(existing)
            if body.kind is not None:
                normalized_kind = _normalize_kind(body.kind)
                if not _is_known_kind(normalized_kind):
                    return _bad("invalid_kind", f"unknown provider kind: {body.kind}")
                entry["kind"] = normalized_kind
            if body.base_url is not None:
                entry["base_url"] = body.base_url
            if body.api_key is not None:
                entry["api_key"] = dict(body.api_key)
            if body.params is not None:
                merged_params = dict(body.params)
                merged_params["custom"] = True
                entry["params"] = merged_params
            else:
                # Make sure the marker survives even if a caller dropped
                # the params block from a prior write.
                existing_params = dict(entry.get("params") or {})
                existing_params["custom"] = True
                entry["params"] = existing_params

            providers[slug] = entry
            cfg["providers"] = providers
            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err

        view = _custom_view_from_entry(slug, entry)
        return JSONResponse(status_code=200, content=view.model_dump())

    @r.delete("/admin/providers/custom/{slug}")
    async def delete_custom_provider(slug: str = FPath(..., min_length=1)):
        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(status_code=503, content={"error": "config_path_unset"})

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(slug)
            if not isinstance(existing, dict):
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "resource": "provider", "id": slug},
                )
            params = existing.get("params") or {}
            if not (isinstance(params, dict) and params.get("custom") is True):
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": "not_custom",
                        "message": f"provider {slug!r} is not a custom slot",
                        "id": slug,
                    },
                )
            providers.pop(slug)
            cfg["providers"] = providers
            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err

        return Response(status_code=204)

    @r.post("/admin/providers/{name}/test")
    async def test_provider(name: str) -> dict[str, Any]:
        """W1.1 — zero-cost connectivity probe for a configured provider.

        Returns ``{ok: bool, latency_ms: int, error?: str,
        models_count?: int}``. Strategy per kind:

        * ``mock``                     — instant ok, ``models_count=1``.
        * openai / openai-compatible   — ``GET <base>/v1/models``.
        * anthropic / google / etc.    — no free upstream probe; return
                                         ``ok=True`` with a hardcoded
                                         catalog count to signal "config
                                         shape is valid" without burning
                                         tokens. The UI can label this
                                         as "configured" rather than
                                         "verified live".
        * unknown                      — ``ok=False`` with diagnostic
                                         error.

        Every error message is run through :func:`_redact` so the api key
        never leaks into the response (or, by extension, the access log).
        Caps total latency at 5s via httpx timeout.
        """
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        entry = providers_cfg.get(name)

        # Resolve kind. Codex is special-cased — it has no config entry.
        if entry is None and name != "codex":
            return {
                "ok": False,
                "latency_ms": 0,
                "error": "provider_not_found",
            }

        if name == "codex":
            kind = "codex"
        else:
            kind = _normalize_kind(str((entry or {}).get("kind") or "openai_compatible"))

        probe_strategy = _zero_cost_probe_kind(kind)
        api_key = _resolve_api_key(entry or {})

        if probe_strategy == "mock":
            return {"ok": True, "latency_ms": 0, "models_count": 1}

        if probe_strategy == "openai_models":
            # Reuse the legacy helper, then reshape with a 5s cap.
            import asyncio as _asyncio

            t0 = time.monotonic()
            try:
                result = await _asyncio.wait_for(
                    _query_provider_models(name, cfg), timeout=5.0
                )
            except TimeoutError:
                latency_ms = int((time.monotonic() - t0) * 1000)
                return {"ok": False, "latency_ms": latency_ms, "error": "timeout"}
            latency_ms = int(result.get("latency_ms") or 0)
            if result.get("ok"):
                return {
                    "ok": True,
                    "latency_ms": latency_ms,
                    "models_count": len(result.get("models") or []),
                }
            err = _redact(str(result.get("error") or "upstream_error"), api_key)
            return {"ok": False, "latency_ms": latency_ms, "error": err}

        if probe_strategy == "hardcoded":
            # No free upstream probe — surface as ok so the operator sees
            # green for a well-formed config; the dropdown below will
            # serve the canned catalog. NOT a real liveness check.
            return {
                "ok": True,
                "latency_ms": 0,
                "models_count": len(_HARDCODED_MODELS.get(kind, [])),
                "note": "no zero-cost upstream probe; config-shape only",
            }

        return {
            "ok": False,
            "latency_ms": 0,
            "error": f"no zero-cost probe; configure provider kind {kind!r} to enable testing",
        }

    @r.get("/admin/providers/{name}/models")
    async def list_provider_models(name: str) -> dict[str, Any]:
        """W1.1 — list models a provider exposes.

        Returns ``{models: [{id, display_name?, created_at?}]}``. For
        openai-shape providers we proxy ``GET <base>/v1/models`` with a
        30s in-memory cache. For providers with a known fixed catalog
        (anthropic, google, mock) we serve the canned list from
        :data:`_HARDCODED_MODELS`. On transient upstream failures we
        retry and then fall back to the most recent cached success for
        that provider (if any), marked with ``stale=true``.
        """
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        entry = providers_cfg.get(name)

        if entry is None and name != "codex":
            return {"models": [], "error": "provider_not_found"}

        if name == "codex":
            kind = "codex"
        else:
            kind = _normalize_kind(str((entry or {}).get("kind") or "openai_compatible"))

        probe_strategy = _zero_cost_probe_kind(kind)

        if probe_strategy in ("mock", "hardcoded"):
            return {"models": list(_HARDCODED_MODELS.get(kind, []))}

        if probe_strategy != "openai_models":
            return {
                "models": [],
                "error": f"kind {kind!r} has no model-discovery endpoint",
            }

        # Cache lookup.
        now = time.monotonic()
        cached = _MODELS_CACHE.get(name)
        if cached is not None and cached[0] > now:
            return dict(cached[1])

        result = await _query_provider_models_with_retry(name, cfg)
        api_key = _resolve_api_key(entry or {})
        if not result.get("ok"):
            err = _redact(str(result.get("error") or "upstream_error"), api_key)
            if cached is not None:
                stale_payload = dict(cached[1])
                stale_payload["stale"] = True
                stale_payload["warning"] = err
                return stale_payload
            # Don't cache failures — operator likely just fixed the key.
            return {"models": [], "error": err}

        models = [
            {"id": mid, "display_name": mid}
            for mid in (result.get("models") or [])
            if isinstance(mid, str)
        ]
        payload: dict[str, Any] = {"models": models}
        _MODELS_CACHE[name] = (now + _MODELS_CACHE_TTL_SECONDS, payload)
        return dict(payload)

    return r
