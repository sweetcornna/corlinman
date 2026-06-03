"""Pure config-resolution helpers for the gateway entrypoint.

Side-effect-free, module-level functions that read the loaded gateway
config (a plain dict from the production loader, a dataclass-shaped
wrapper in tests, or ``None`` during a degraded boot) and resolve
individual settings — CORS origins, trusted proxies, forwarded-proto
trust, the admin session-cookie flag, and the status-link public-URL
gate.

Extracted verbatim from
:mod:`corlinman_server.gateway.lifecycle.entrypoint` (Modularization
Phase 8) to keep that module focused on boot orchestration. These
helpers call each other (the origins/proxies resolvers reach for
:func:`_extract_section` / :func:`_coerce_str_list`; the forwarded-proto
resolvers reach for :func:`_mapping_section`) but have no other coupling
to the entrypoint, so the whole set moves together. ``entrypoint``
re-imports every name so its existing call sites keep working unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def _resolve_cors_origins() -> list[str]:
    """Parse the opt-in browser UI CORS allowlist."""
    raw = os.environ.get("CORLINMAN_CORS_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _coerce_str_list(value: Any) -> list[str]:
    """Parse config/env list values into trimmed strings."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _resolve_allowed_public_origins(cfg: Any | None) -> list[str]:
    """Allowed origins for zero-config public-origin learning.

    Config and env values are additive. The middleware treats an empty resolved
    list as deny-all, so automatic learning never trusts arbitrary Host headers.
    """
    server_cfg = _extract_section(cfg, "server")
    configured = _coerce_str_list(
        _extract_section(server_cfg, "allowed_public_origins")
    )
    env = _coerce_str_list(os.environ.get("CORLINMAN_ALLOWED_PUBLIC_ORIGINS"))
    return configured + [item for item in env if item not in configured]


def _resolve_trusted_proxies(cfg: Any | None) -> list[str]:
    """Trusted reverse-proxy IP/CIDR ranges for X-Forwarded-* origin learning."""
    server_cfg = _extract_section(cfg, "server")
    configured = _coerce_str_list(_extract_section(server_cfg, "trusted_proxies"))
    env = _coerce_str_list(os.environ.get("CORLINMAN_TRUSTED_PROXIES"))
    return configured + [item for item in env if item not in configured]


def _status_links_explicitly_configured(cfg: Any | None) -> bool:
    """True when an operator set ``public_url`` via config or env.

    When explicit, the learned-origin auto-detection must stand down so a
    health-check / loopback request can't shadow the operator's choice.
    """
    server_cfg = _extract_section(cfg, "server")
    config_public_url = _extract_section(server_cfg, "public_url")
    if isinstance(config_public_url, str) and config_public_url.strip():
        return True
    return bool(os.environ.get("CORLINMAN_PUBLIC_URL", "").strip())


def _extract_section(obj: Any, key: str) -> Any:
    """Read ``obj[key]`` / ``obj.key`` tolerantly.

    Mirrors the ``_should_run_legacy_migration`` helper's discipline:
    the config may arrive as a plain dict (production loader), a
    dataclass-shaped wrapper (tests), or ``None`` (degraded boot). One
    helper keeps every caller's branch logic single-line.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _mapping_section(value: Any, key: str) -> Mapping[str, Any]:
    """Return a dict-like config section or an empty mapping."""
    if not isinstance(value, Mapping):
        return {}
    section = value.get(key)
    return section if isinstance(section, Mapping) else {}


def _admin_session_cookie_secure_from_config(config: Any) -> bool | None:
    """Resolve optional ``[admin].session_cookie_secure`` from config."""
    admin = _mapping_section(config, "admin")
    value = admin.get("session_cookie_secure")
    return value if isinstance(value, bool) else None


def _trusted_forwarded_proto_proxies_from_config(config: Any) -> tuple[str, ...]:
    """Resolve trusted proxy CIDRs from ``[server]`` config."""
    server = _mapping_section(config, "server")
    value = server.get("trusted_forwarded_proto_proxies")
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return ()


def _trust_forwarded_proto_from_config(config: Any) -> bool:
    """Resolve the ``[server].trust_forwarded_proto`` compatibility flag."""
    server = _mapping_section(config, "server")
    return bool(server.get("trust_forwarded_proto"))
