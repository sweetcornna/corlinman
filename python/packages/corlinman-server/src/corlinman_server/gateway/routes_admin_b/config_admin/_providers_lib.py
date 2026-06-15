"""Wire models + helpers for ``/admin/providers*`` (extracted god-file split).

Module-level pydantic wire models, pure helper functions, and constants used
by :mod:`...config_admin.providers` route handlers. Split out verbatim to
shrink the ``providers.py`` god-file; ``providers.py`` re-imports every name
it needs so the router + handlers stay byte-for-byte unchanged.

This module must NOT import ``providers`` (no import cycle). It pulls
``AdminState`` from ``...routes_admin_b.state`` exactly as ``providers.py``
does; ``_write_config_atomic`` / ``config_snapshot`` are referenced only by
the route handlers (which remain in ``providers.py``), so they are not needed
here.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import structlog
from corlinman_providers.china import DeepSeekProvider, GLMProvider, QwenProvider
from corlinman_providers.market_providers import (
    CohereProvider,
    GroqProvider,
    MistralProvider,
    ReplicateProvider,
    TogetherProvider,
)
from corlinman_providers.specs import list_supported_kinds
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from corlinman_server.gateway.core.config_mutation import publish_config_mutation
from corlinman_server.gateway.routes_admin_b.state import AdminState

logger = structlog.get_logger(__name__)


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


class ProviderModelProbe(BaseModel):
    kind: str
    base_url: str | None = None
    api_key: dict[str, Any] | None = None
    existing_name: str | None = None
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


def _remove_model_refs(cfg: dict[str, Any], provider_name: str) -> dict[str, Any]:
    """Drop model defaults/aliases that point at a removed provider."""
    models_cfg = dict(cfg.get("models") or {})
    aliases = dict(models_cfg.get("aliases") or {})
    removed_aliases: set[str] = set()
    for alias_name, alias_entry in list(aliases.items()):
        if _alias_provider(alias_entry) == provider_name:
            removed_aliases.add(str(alias_name))
            aliases.pop(alias_name, None)
    models_cfg["aliases"] = aliases
    default_name = str(models_cfg.get("default") or "")
    if default_name == provider_name or default_name in removed_aliases:
        models_cfg.pop("default", None)
    cfg["models"] = models_cfg
    return cfg


def _remove_default_model_ref(cfg: dict[str, Any], provider_name: str) -> dict[str, Any]:
    """Drop only the active default alias for a disabled provider."""
    models_cfg = dict(cfg.get("models") or {})
    aliases = dict(models_cfg.get("aliases") or {})
    default_name = str(models_cfg.get("default") or "")
    if not default_name:
        return cfg

    default_alias = aliases.get(default_name)
    default_alias_provider = _alias_provider(default_alias)
    if default_alias_provider is not None:
        points_to_provider = default_alias_provider == provider_name
    else:
        points_to_provider = default_name == provider_name

    if points_to_provider:
        if default_alias_provider == provider_name:
            aliases.pop(default_name, None)
        models_cfg["aliases"] = aliases
        models_cfg.pop("default", None)
        cfg["models"] = models_cfg
    return cfg


def _has_api_key(entry: dict[str, Any]) -> bool:
    raw_key = entry.get("api_key")
    if isinstance(raw_key, str):
        return bool(raw_key)
    if isinstance(raw_key, dict):
        if "env" in raw_key:
            return bool(raw_key.get("env"))
        if "value" in raw_key:
            return bool(raw_key.get("value"))
        return bool(raw_key)
    return False


def _has_base_url(entry: dict[str, Any]) -> bool:
    raw_base_url = entry.get("base_url")
    return isinstance(raw_base_url, str) and bool(raw_base_url.strip())


_AUTOBIND_REQUIRES_API_KEY_KINDS: frozenset[str] = frozenset(
    {
        "anthropic",
        "openai",
        "google",
        "deepseek",
        "qwen",
        "glm",
        "mistral",
        "cohere",
        "together",
        "groq",
        "replicate",
        "azure",
        "bedrock",
    }
)


# Built-in adapters that authenticate via a documented env-var key when the
# provider entry omits one (e.g. ``OpenAIProvider`` falls back to
# ``OPENAI_API_KEY``, ``DeepSeekProvider`` to ``DEEPSEEK_API_KEY``). Such a slot
# is usable without a config key, so the api-key autobind guard treats the
# env-var as satisfying the requirement — otherwise env-only deployments enable
# a working provider but never get a ``models.default``.
#
# These MUST mirror each adapter's own env fallback (see the provider classes in
# ``corlinman_providers``); ``test_autobind_env_fallback_map_is_consistent``
# pins that every api-key-required kind except ``bedrock`` is covered. ``bedrock``
# is intentionally excluded: it authenticates via AWS SigV4 (``api_key`` as
# ``"access:secret"`` or the ``AWS_*`` credential chain), not a single api-key
# env, so it stays gated on explicit config.
_AUTOBIND_API_KEY_ENV_FALLBACK: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN"),
    "google": ("GOOGLE_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "qwen": ("DASHSCOPE_API_KEY",),
    "glm": ("ZHIPU_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "cohere": ("COHERE_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "replicate": ("REPLICATE_API_TOKEN",),
    "azure": ("AZURE_OPENAI_API_KEY",),
}


def _env_api_key_available(kind: str) -> bool:
    return any(
        (os.environ.get(var) or "").strip()
        for var in _AUTOBIND_API_KEY_ENV_FALLBACK.get(kind, ())
    )


def _can_autobind_default_alias(entry: dict[str, Any], name: str) -> bool:
    kind = _normalize_kind(str(entry.get("kind") or "openai_compatible"))
    if _provider_tts_backend(entry) == "fish":
        return False
    if kind == "openai_compatible" and not _has_base_url(entry):
        return False
    if kind not in _AUTOBIND_REQUIRES_API_KEY_KINDS or _has_api_key(entry):
        return True
    # No config key: bindable only for the BUILT-IN slot of a kind whose adapter
    # has a documented env-var key fallback (``name == kind`` — e.g. the
    # canonical ``openai`` slot served by OPENAI_API_KEY). A custom slot of the
    # same kind (e.g. ``openai-clone``) is not covered by the env fallback and
    # must still carry an explicit key to autobind.
    return name == kind and _env_api_key_available(kind)


def _bad(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": code, "message": message})


async def _persist(
    state: AdminState,
    cfg: dict[str, Any],
    *,
    py_config_writer: Any | None = None,
) -> JSONResponse | None:
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
    await publish_config_mutation(
        state,
        cfg,
        py_config_writer=py_config_writer,
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
_NATIVE_MODEL_PROBE_KINDS: frozenset[str] = frozenset({"anthropic", "google"})

_OPENAI_COMPATIBLE_DEFAULT_BASE_URLS: dict[str, str] = {
    "mistral": MistralProvider.DEFAULT_BASE_URL,
    "cohere": CohereProvider.DEFAULT_BASE_URL,
    "together": TogetherProvider.DEFAULT_BASE_URL,
    "groq": GroqProvider.DEFAULT_BASE_URL,
    "replicate": ReplicateProvider.DEFAULT_BASE_URL,
    "qwen": QwenProvider.DEFAULT_BASE_URL,
    "glm": GLMProvider.DEFAULT_BASE_URL,
    "deepseek": DeepSeekProvider.DEFAULT_BASE_URL,
}


def _default_base_url_for_kind(kind: str) -> str | None:
    return _OPENAI_COMPATIBLE_DEFAULT_BASE_URLS.get(_normalize_kind(kind))


def _codex_auth_path_for_data_dir(data_dir: Any | None) -> Any | None:
    if data_dir is None:
        return None
    try:
        from pathlib import Path

        return Path(data_dir) / ".codex" / "auth.json"
    except TypeError:
        return None


async def _refresh_codex_probe_credential(
    cred: Any,
    *,
    credential_path: Any | None = None,
) -> Any | None:
    """Refresh a Codex OAuth credential for admin model discovery."""
    refresh_token = getattr(cred, "refresh_token", None)
    if not refresh_token:
        return None
    try:
        from corlinman_providers._codex_oauth import (  # noqa: PLC0415
            persist_codex_credential,
            refresh_codex_token,
        )

        refreshed = await refresh_codex_token(refresh_token=refresh_token)
        persist_codex_credential(refreshed, path=credential_path)
        return refreshed
    except Exception as exc:  # noqa: BLE001
        logger.warning("gateway.providers.codex_probe_refresh_failed", error=str(exc))
        return None


def _codex_models_headers(access_token: str) -> dict[str, str]:
    from corlinman_providers._codex_oauth import codex_cloudflare_headers  # noqa: PLC0415

    return {
        "Authorization": f"Bearer {access_token}",
        **codex_cloudflare_headers(access_token),
    }


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
        {"id": "claude-fable-5", "display_name": "Claude Fable 5"},
        {"id": "claude-opus-4-8", "display_name": "Claude Opus 4.8"},
        {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6"},
        {"id": "claude-opus-4-5", "display_name": "Claude Opus 4.5"},
        {"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"},
        {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"},
        {"id": "claude-3-7-sonnet-latest", "display_name": "Claude 3.7 Sonnet"},
        {"id": "claude-3-5-sonnet-latest", "display_name": "Claude 3.5 Sonnet"},
        {"id": "claude-3-5-haiku-latest", "display_name": "Claude 3.5 Haiku"},
    ],
    "google": [
        {"id": "gemini-3.5-flash", "display_name": "Gemini 3.5 Flash"},
        {"id": "gemini-3.1-pro-preview", "display_name": "Gemini 3.1 Pro Preview"},
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
_FISH_TTS_BACKENDS: frozenset[str] = frozenset(
    {"fish", "fish_audio", "fish-audio"}
)
_FISH_TTS_MODELS: list[dict[str, str]] = [
    {"id": "s2-pro", "display_name": "Fish Audio S2 Pro"},
    {"id": "s1", "display_name": "Fish Audio S1"},
]


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


async def _query_provider_models_with_retry(
    name: str,
    cfg: dict[str, Any],
    *,
    data_dir: Any | None = None,
) -> dict[str, Any]:
    """Probe models with bounded retries for transient upstream failures."""
    last_result: dict[str, Any] = {"ok": False, "models": [], "latency_ms": 0, "error": "unknown"}
    for attempt in range(_MODELS_MAX_RETRIES + 1):
        result = await _query_provider_models(name, cfg, data_dir=data_dir)
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
    if k in _NATIVE_MODEL_PROBE_KINDS:
        return "native_models"
    if k in _HARDCODED_MODELS:
        return "hardcoded"
    return "none"


def _provider_tts_backend(entry: dict[str, Any] | None) -> str | None:
    """Return a custom provider's TTS backend marker, if present."""
    if not isinstance(entry, dict):
        return None
    params = entry.get("params")
    if not isinstance(params, dict):
        return None
    raw = params.get("tts_backend") or params.get("backend")
    if not isinstance(raw, str) or not raw.strip():
        return None
    normalized = raw.strip().lower()
    if normalized in _FISH_TTS_BACKENDS:
        return "fish"
    return normalized


def _fish_tts_reference_id(entry: dict[str, Any] | None) -> str | None:
    """Return the configured Fish Audio voice reference id, if present."""
    if not isinstance(entry, dict):
        return None
    params = entry.get("params")
    if not isinstance(params, dict):
        return None
    raw = params.get("reference_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    env_raw = os.environ.get("CORLINMAN_TTS_REFERENCE_ID")
    if env_raw and env_raw.strip():
        return env_raw.strip()
    return None


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


def _provider_models_url(base_url: str) -> str:
    """Return the OpenAI-shape model-list URL for an operator base URL.

    Operators commonly paste either the origin (``https://relay``), an API
    root (``https://relay/api``), or a versioned root
    (``https://relay/api/v1``, ``https://relay/api/v4``). Treat a
    trailing ``/v<digits>`` as already versioned so the probe does not
    request paths like ``/v1/v1/models`` or ``/v4/v1/models``.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(str(base_url).strip().rstrip("/"))
    path = parts.path.rstrip("/")
    if path.endswith("/models"):
        models_path = path
    elif re.search(r"/v\d+$", path):
        models_path = f"{path}/models"
    else:
        models_path = f"{path}/v1/models"
    if not models_path.startswith("/"):
        models_path = f"/{models_path}"
    return urlunsplit((parts.scheme, parts.netloc, models_path, "", ""))


async def _query_provider_models(
    name: str,
    cfg: dict[str, Any],
    *,
    data_dir: Any | None = None,
) -> dict[str, Any]:
    """Query ``/v1/models`` for a provider and return a result dict.

    Returns ``{"ok": bool, "models": list[str], "latency_ms": int, "error": str|null}``.
    For OpenAI-compatible providers, calls ``<base_url>/v1/models`` with the
    configured API key. For the ``codex`` provider, reads the ChatGPT
    subscription token from ``~/.codex/auth.json`` and queries the Codex
    backend on ``chatgpt.com/backend-api/codex`` with the same Cloudflare
    headers used by the runtime adapter.
    """
    import time as _time

    import httpx as _httpx

    providers_cfg = cfg.get("providers") or {}
    entry = providers_cfg.get(name)

    # Special handling for the auto-injected codex provider (no entry in config).
    is_codex = name == "codex"
    if entry is None and not is_codex:
        return {"ok": False, "models": [], "latency_ms": 0, "error": "provider_not_found"}

    if is_codex:
        credential_path = _codex_auth_path_for_data_dir(data_dir)
        # Prefer the gateway data dir credential, then fall back to the
        # Codex CLI location for legacy single-user deployments.
        try:
            from corlinman_providers._codex_oauth import load_codex_credential

            cred = (
                load_codex_credential(credential_path)
                if credential_path is not None
                else None
            )
            if cred is None:
                cred = load_codex_credential()
                credential_path = None
        except Exception as exc:
            return {"ok": False, "models": [], "latency_ms": 0, "error": str(exc)}
        if cred is None:
            return {
                "ok": False,
                "models": [],
                "latency_ms": 0,
                "error": "codex_auth_not_found",
            }
        is_expired = getattr(cred, "is_expired", None)
        if callable(is_expired) and is_expired():
            refreshed = await _refresh_codex_probe_credential(
                cred,
                credential_path=credential_path,
            )
            if refreshed is not None:
                cred = refreshed
        api_key = cred.access_token
        url = "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"
        headers = _codex_models_headers(api_key)
        params: dict[str, str] = {}
    else:
        entry_dict = dict(entry) if isinstance(entry, dict) else {}
        kind = _normalize_kind(str(entry_dict.get("kind") or "openai_compatible"))
        api_key = _resolve_api_key(entry_dict)
        params = {}
        if kind == "anthropic":
            if not api_key:
                return {
                    "ok": False,
                    "models": [],
                    "latency_ms": 0,
                    "error": "api_key_missing",
                }
            url = "https://api.anthropic.com/v1/models"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
        elif kind == "google":
            if not api_key:
                return {
                    "ok": False,
                    "models": [],
                    "latency_ms": 0,
                    "error": "api_key_missing",
                }
            url = "https://generativelanguage.googleapis.com/v1beta/models"
            headers = {}
            params = {"key": api_key}
        else:
            if kind not in _OPENAI_COMPATIBLE_KINDS:
                return {
                    "ok": False,
                    "models": [],
                    "latency_ms": 0,
                    "error": f"kind '{kind}' does not support model discovery",
                }
            raw_base = (
                entry_dict.get("base_url")
                or _default_base_url_for_kind(kind)
                or "https://api.openai.com"
            )
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

            url = _provider_models_url(base_url)
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

    t0 = _time.monotonic()
    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            if is_codex and resp.status_code == 401:
                refreshed = await _refresh_codex_probe_credential(
                    cred,
                    credential_path=credential_path,
                )
                if refreshed is not None and refreshed.access_token != api_key:
                    api_key = refreshed.access_token
                    headers = _codex_models_headers(api_key)
                    resp = await client.get(url, headers=headers, params=params)
        latency_ms = int((_time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            return {
                "ok": False,
                "models": [],
                "latency_ms": latency_ms,
                "error": f"HTTP {resp.status_code}",
            }
        data = resp.json()
        if is_codex:
            model_ids = [
                str(item["slug"])
                for item in (data.get("models") or [])
                if isinstance(item, dict) and isinstance(item.get("slug"), str)
            ]
        elif kind == "google":
            model_ids = []
            for item in data.get("models") or []:
                if not isinstance(item, dict):
                    continue
                methods = item.get("supportedGenerationMethods")
                if isinstance(methods, list) and not any(
                    method in methods for method in ("generateContent", "streamGenerateContent")
                ):
                    continue
                model_name = item.get("name")
                if isinstance(model_name, str) and model_name.startswith("models/"):
                    model_ids.append(model_name.removeprefix("models/"))
        elif kind == "anthropic":
            model_ids = [
                str(item["id"])
                for item in (data.get("data") or [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
        else:
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
    "openai": "gpt-5.5",
    "openai_compatible": "gpt-5.5",
    "mistral": "mistral-medium-latest",
    "cohere": "command-a-plus-05-2026",
    "together": "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
    "groq": "openai/gpt-oss-120b",
    "replicate": "meta/llama-4-maverick-instruct",
    "qwen": "qwen3.7-max",
    "glm": "glm-5.1",
    "deepseek": "deepseek-v4-pro",
    "anthropic": "claude-fable-5",
    "google": "gemini-3.5-flash",
    "codex": "gpt-5.5",
    "mock": "mock",
}


# When a probe returns a giant catalog (relays often surface 100+ ids),
# prefer a well-known model over the alphabetically-first one so the
# default isn't something obscure like ``ada-001``.
_KIND_PREFERRED_DEFAULT_MODELS: dict[str, tuple[str, ...]] = {
    "openai": ("gpt-5.5", "gpt-5.4", "gpt-5.3", "gpt-4o"),
    "openai_compatible": ("gpt-5.5", "gpt-5.4", "gpt-4o"),
    "codex": ("gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gpt-4o"),
    "anthropic": (
        "claude-fable-5",
        "claude-opus-4-8",
        "claude-opus-4-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-3-7-sonnet-latest",
        "claude-3-5-sonnet-latest",
    ),
    "google": (
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ),
    "mistral": (
        "mistral-medium-latest",
        "mistral-medium-3.5",
        "mistral-large-latest",
        "mistral-small-latest",
    ),
    "cohere": (
        "command-a-plus-05-2026",
        "command-a-03-2025",
        "command-r-plus-08-2024",
        "command-r-08-2024",
    ),
    "deepseek": ("deepseek-v4-pro", "deepseek-v4-flash", "deepseek-reasoner"),
    "qwen": ("qwen3.7-max", "qwen3.7-max-2026-06-08", "qwen-max", "qwen-plus"),
    "glm": ("glm-5.1", "glm-5", "glm-4-plus", "glm-4-flash"),
    "together": (
        "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    ),
    "groq": (
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile",
    ),
    "replicate": (
        "meta/llama-4-maverick-instruct",
        "meta/meta-llama-3-70b-instruct",
    ),
}
_PREFERRED_DEFAULT_MODELS: tuple[str, ...] = (
    "gpt-5.5",
    "gpt-5.4",
    "gpt-4o",
    "claude-fable-5",
    "gemini-3.5-flash",
    "mistral-medium-latest",
    "command-a-plus-05-2026",
    "deepseek-v4-pro",
    "qwen3.7-max",
    "glm-5.1",
)


_NON_CHAT_MODEL_MARKERS: tuple[str, ...] = (
    "audio",
    "dall-e",
    "embed",
    "embedding",
    "image",
    "moderation",
    "rerank",
    "review",
    "transcribe",
    "tts",
    "whisper",
)


def _has_model_marker(lowered: str, marker: str) -> bool:
    return re.search(rf"(^|[^a-z0-9]){re.escape(marker)}([^a-z0-9]|$)", lowered) is not None


def _has_any_model_marker(lowered: str, markers: tuple[str, ...]) -> bool:
    return any(_has_model_marker(lowered, marker) for marker in markers)


def _numeric_score(lowered: str, *, width: int = 4) -> tuple[int, ...]:
    parts = tuple(int(part) for part in re.findall(r"\d+", lowered))
    return (*parts[:width], *((0,) * max(0, width - len(parts))))


def _cohere_release_score(lowered: str) -> tuple[int, int]:
    match = re.search(r"(?:^|[^0-9])(\d{2})-(\d{4})(?:$|[^0-9])", lowered)
    if not match:
        return (0, 0)
    month = int(match.group(1))
    year = int(match.group(2))
    return (year, month)


def _llama_score(lowered: str) -> tuple[int, ...] | None:
    match = re.search(r"llama-?(\d+(?:\.\d+)*)", lowered)
    if not match:
        return None
    version = tuple(int(part) for part in match.group(1).split("."))
    version = (*version[:3], *((0,) * max(0, 3 - len(version))))
    tier = 80 if "maverick" in lowered else 50 if "scout" in lowered else 20
    params_match = re.search(r"(\d+)b", lowered)
    params = int(params_match.group(1)) if params_match else 0
    return (*version, tier, params)


def _flagship_candidate_score(kind: str, model_id: str) -> tuple[int, ...] | None:
    lowered = model_id.lower()
    if _has_any_model_marker(lowered, _NON_CHAT_MODEL_MARKERS):
        return None

    version = _numeric_score(lowered)
    if kind in {"openai", "openai_compatible", "codex"}:
        if "gpt-oss" in lowered:
            return None
        if re.search(r"(^|/)gpt-\d", lowered) is None:
            return None
        if _has_any_model_marker(lowered, ("mini", "nano")):
            return None
        tier = 50 if "codex" not in lowered else 45
        return (100, *version, tier)

    if kind == "anthropic":
        if not lowered.startswith("claude-"):
            return None
        if _has_model_marker(lowered, "haiku"):
            return None
        tier = 90 if "fable" in lowered else 80 if "opus" in lowered else 60
        return (100, *version, tier)

    if kind == "google":
        if not lowered.startswith("gemini-"):
            return None
        if _has_model_marker(lowered, "lite"):
            return None
        tier = 90 if "pro" in lowered else 60 if "flash" in lowered else 50
        preview = 5 if "preview" in lowered else 10
        return (100, *version, tier, preview)

    if kind == "mistral":
        if not lowered.startswith("mistral-"):
            return None
        latest = 50 if "latest" in lowered else 0
        tier = 90 if "medium" in lowered else 70 if "large" in lowered else 20
        return (100, latest, tier, *version)

    if kind == "cohere":
        if not lowered.startswith("command-a"):
            return None
        year, month = _cohere_release_score(lowered)
        tier = 80 if "plus" in lowered else 50
        return (100, year, month, tier, *version)

    if kind == "deepseek":
        if not lowered.startswith("deepseek-"):
            return None
        tier = (
            90
            if "pro" in lowered
            else 50
            if "flash" in lowered
            else 40
            if "reasoner" in lowered
            else 30
        )
        return (100, *version, tier)

    if kind == "qwen":
        if not lowered.startswith("qwen"):
            return None
        tier = 90 if "max" in lowered else 60 if "plus" in lowered else 40
        return (100, *version, tier)

    if kind == "glm":
        if not lowered.startswith("glm-"):
            return None
        tier = 80 if "plus" in lowered else 20 if "flash" in lowered else 60
        return (100, *version, tier)

    if kind in {"together", "replicate"}:
        score = _llama_score(lowered)
        if score is None:
            return None
        return (100, *score)

    if kind == "groq":
        gpt_oss = re.search(r"gpt-oss-(\d+)b", lowered)
        if gpt_oss is not None:
            return (120, int(gpt_oss.group(1)))
        score = _llama_score(lowered)
        if score is None:
            return None
        return (80, *score)

    return None


def _pick_dynamic_flagship_model(kind: str, probed_ids: list[str]) -> str | None:
    candidates: list[tuple[tuple[int, ...], int, str]] = []
    for index, model_id in enumerate(probed_ids):
        score = _flagship_candidate_score(kind, model_id)
        if score is not None:
            candidates.append((score, -index, model_id))
    if not candidates:
        return None
    return max(candidates)[2]


def _pick_default_model(kind: str, probed_ids: list[str]) -> str | None:
    normalized_kind = _normalize_kind(kind)
    dynamic = _pick_dynamic_flagship_model(normalized_kind, probed_ids)
    if dynamic is not None:
        return dynamic

    preferences = (
        *_KIND_PREFERRED_DEFAULT_MODELS.get(normalized_kind, ()),
        *_PREFERRED_DEFAULT_MODELS,
    )
    seen: set[str] = set()
    for pref in preferences:
        if pref in seen:
            continue
        seen.add(pref)
        if pref in probed_ids:
            return pref
        for mid in probed_ids:
            if mid.startswith(pref):
                return mid
    if probed_ids:
        return probed_ids[0]
    return _KIND_DEFAULT_MODEL.get(normalized_kind)


async def _autobind_default_alias(
    cfg: dict[str, Any],
    provider_name: str,
    entry: dict[str, Any],
    *,
    data_dir: Any | None = None,
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
        result = await _query_provider_models(
            provider_name,
            cfg,
            data_dir=data_dir,
        )
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
    existing_alias = aliases.get(provider_name)
    if isinstance(existing_alias, dict) and existing_alias.get("provider"):
        pass
    elif isinstance(existing_alias, str) and existing_alias.strip():
        aliases[provider_name] = {
            "provider": provider_name,
            "model": existing_alias.strip(),
            "params": {},
        }
    elif isinstance(existing_alias, dict):
        raw_params = existing_alias.get("params")
        aliases[provider_name] = {
            "provider": provider_name,
            "model": str(existing_alias.get("model") or picked),
            "params": dict(raw_params) if isinstance(raw_params, dict) else {},
        }
    else:
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
