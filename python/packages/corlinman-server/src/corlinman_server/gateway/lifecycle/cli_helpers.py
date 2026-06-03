"""Pure module-level CLI / resolution helpers for the gateway entrypoint.

Extracted verbatim from
:mod:`corlinman_server.gateway.lifecycle.entrypoint` (Modularization
Phase 2 god-file reduction). These are the pure, side-effect-light
helpers the entrypoint's CLI driver and app factory call:

* :func:`_lazy_import` — swallow-ImportError sibling-module importer used
  heavily by ``build_app`` to boot in degraded mode.
* :func:`_resolve_config_path` — ``--config`` > ``$CORLINMAN_CONFIG``.
* :func:`_resolve_data_dir` — ``--data-dir`` > env > ``[server].data_dir``
  > ``~/.corlinman``.
* :func:`_should_run_legacy_migration` — the ``[tenants]`` migration gate.
* :func:`_tenant_scope_params` — derive the tenant-scope middleware params.
* :func:`_build_parser` — the ``argparse`` CLI parser.
* :func:`_resolve_bind` — resolve the ``(host, port)`` bind address.

The entrypoint re-imports every one of these back, so its public surface
and ``__all__`` are unchanged. This module never imports the entrypoint
module (no import cycle); ``_extract_section`` comes from the already-split
:mod:`corlinman_server.gateway.lifecycle.config_resolve`.
"""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
from typing import Any

import structlog

from corlinman_server.gateway.lifecycle.config_resolve import (
    _extract_section,
)

logger = structlog.get_logger(__name__)

#: Mirrors ``corlinman_gateway::main::resolve_addr`` — same defaults so a
#: deployment-script that sets ``PORT`` / ``BIND`` against the Rust
#: binary keeps working against the Python port.
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 6005


def _lazy_import(dotted: str) -> Any | None:
    """Import ``dotted`` and return the module; ``None`` on ImportError.

    The siblings populated by parallel agents may not exist when this
    file is imported. Swallowing ``ImportError`` lets ``build_app`` boot
    in degraded mode without leaking partial-port state into a startup
    crash.
    """
    try:
        return importlib.import_module(dotted)
    except ImportError as exc:
        logger.warning(
            "gateway.sibling_missing",
            module=dotted,
            error=str(exc),
            detail=(
                "sibling submodule not present; gateway will boot in "
                "degraded mode without it"
            ),
        )
        return None


def _resolve_config_path(cli_value: str | None) -> Path | None:
    """``--config`` > ``$CORLINMAN_CONFIG`` > ``None``. Mirrors the
    Rust ``main::load_config`` precedence."""
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("CORLINMAN_CONFIG")
    if env:
        return Path(env)
    return None


def _resolve_data_dir(cli_value: str | None, cfg: Any | None = None) -> Path:
    """``--data-dir`` > ``$CORLINMAN_DATA_DIR`` > ``[server].data_dir`` >
    ``~/.corlinman`` > ``./.corlinman``.

    Mirrors ``corlinman_gateway::server::resolve_data_dir`` but additionally
    honours ``[server].data_dir`` from the loaded config when no CLI / env
    override is present — so the value the admin config editor lists (and
    flags restart-required) actually takes effect on the next restart
    rather than being silently ignored. CLI / env still win so a launch
    flag can override a stale config.
    """
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env)
    config_data_dir = _extract_section(_extract_section(cfg, "server"), "data_dir")
    if isinstance(config_data_dir, str) and config_data_dir.strip():
        return Path(config_data_dir.strip()).expanduser()
    try:
        return Path.home() / ".corlinman"
    except (RuntimeError, OSError):
        return Path(".corlinman")


def _should_run_legacy_migration(cfg: Any | None) -> bool:
    """Mirror the Rust gate: ``[tenants].enabled && [tenants].migrate_legacy_paths``.

    Default off — pre-Phase-4 deployments keep their flat layout unless
    the operator opts in.
    """
    if cfg is None:
        return False
    tenants = getattr(cfg, "tenants", None)
    if tenants is None and isinstance(cfg, dict):
        tenants = cfg.get("tenants")
    if tenants is None:
        return False
    enabled = (
        getattr(tenants, "enabled", None)
        if not isinstance(tenants, dict)
        else tenants.get("enabled")
    )
    migrate = (
        getattr(tenants, "migrate_legacy_paths", None)
        if not isinstance(tenants, dict)
        else tenants.get("migrate_legacy_paths")
    )
    return bool(enabled) and bool(migrate)


def _tenant_scope_params(cfg: Any | None) -> tuple[bool, frozenset, Any]:
    """Derive ``(enabled, allowed, fallback)`` for the tenant-scope
    middleware from the loaded ``[tenants]`` config section.

    SEC-06b: the middleware is installed unconditionally so handlers
    always observe a resolved ``request.state.tenant``. The single-tenant
    happy path keeps ``enabled=False`` (the byte-for-byte legacy
    behaviour: every request transparently resolves to the ``"default"``
    tenant and nothing is ever rejected). A multi-tenant operator opts in
    via ``[tenants].enabled = true`` + ``[tenants].allowed`` and the
    middleware then validates ``?tenant=`` / ``X-Corlinman-Tenant``
    against the allow-set, still falling back to the default when no slug
    is supplied.

    Tolerant of dict / dataclass / ``None`` config shapes (mirrors
    :func:`_should_run_legacy_migration`). On any parse hiccup we return
    the safe disabled default rather than raising — boot must never fail
    on this.
    """
    from corlinman_server.tenancy import TenantId, default_tenant

    fallback = default_tenant()
    allowed: frozenset = frozenset({fallback})
    if cfg is None:
        return (False, allowed, fallback)

    tenants = _extract_section(cfg, "tenants")
    if tenants is None:
        return (False, allowed, fallback)

    enabled = bool(_extract_section(tenants, "enabled"))

    default_slug = _extract_section(tenants, "default")
    if isinstance(default_slug, str) and default_slug.strip():
        try:
            fallback = TenantId.new(default_slug.strip())
        except Exception:  # noqa: BLE001 — keep the legacy default
            fallback = default_tenant()

    allowed_set: set = {fallback}
    raw_allowed = _extract_section(tenants, "allowed")
    if isinstance(raw_allowed, (list, tuple)):
        for slug in raw_allowed:
            if not isinstance(slug, str) or not slug.strip():
                continue
            try:
                allowed_set.add(TenantId.new(slug.strip()))
            except Exception:  # noqa: BLE001 — skip a malformed entry
                continue

    return (enabled, frozenset(allowed_set), fallback)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="corlinman-gateway",
        description=(
            "Run the corlinman gateway (Python port of the Rust "
            "corlinman-gateway binary)."
        ),
    )
    p.add_argument(
        "--config",
        dest="config",
        default=None,
        help="Path to the gateway config TOML. Falls back to "
        "$CORLINMAN_CONFIG, then no config (defaults).",
    )
    p.add_argument(
        "--host",
        dest="host",
        default=None,
        help=f"Bind host. Default: $BIND or {DEFAULT_HOST}.",
    )
    p.add_argument(
        "--port",
        dest="port",
        type=int,
        default=None,
        help=f"Bind port. Default: $PORT or {DEFAULT_PORT}.",
    )
    p.add_argument(
        "--data-dir",
        dest="data_dir",
        default=None,
        help="Override the data directory (default: $CORLINMAN_DATA_DIR or ~/.corlinman).",
    )
    p.add_argument(
        "--log-level",
        dest="log_level",
        default=os.environ.get("LOG_LEVEL", "info"),
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="uvicorn log level. Default: $LOG_LEVEL or info.",
    )
    return p


def _resolve_bind(
    cli_host: str | None,
    cli_port: int | None,
    cfg: Any | None = None,
) -> tuple[str, int]:
    """Resolve ``(host, port)`` to bind on.

    Precedence (first present wins), per field:

    * host: ``--host`` > ``$BIND`` > ``[server].bind`` > :data:`DEFAULT_HOST`.
    * port: ``--port`` > ``$PORT`` > ``[server].port`` > :data:`DEFAULT_PORT`.

    The ``[server].bind`` / ``[server].port`` fallbacks make the values the
    admin config editor lists (and flags restart-required) actually honoured
    on the next restart — previously they were resolved from CLI/env only,
    so editing them in the UI did nothing even after a restart. CLI / env
    still win so a launch flag overrides a stale config.
    """
    server_cfg = _extract_section(cfg, "server")

    host = cli_host or os.environ.get("BIND")
    if not host:
        config_bind = _extract_section(server_cfg, "bind")
        if isinstance(config_bind, str) and config_bind.strip():
            host = config_bind.strip()
    if not host:
        host = DEFAULT_HOST

    if cli_port is not None:
        port = cli_port
    else:
        env_port = os.environ.get("PORT")
        if env_port and env_port.isdigit():
            port = int(env_port)
        else:
            config_port = _extract_section(server_cfg, "port")
            if isinstance(config_port, int) and not isinstance(config_port, bool):
                port = config_port
            elif isinstance(config_port, str) and config_port.strip().isdigit():
                port = int(config_port.strip())
            else:
                port = DEFAULT_PORT
    return host, port
