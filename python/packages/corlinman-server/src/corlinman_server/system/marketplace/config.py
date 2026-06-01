"""``corlinman_server.system.marketplace.config`` — config parsing.

Reads the ``[marketplace]`` + ``[marketplace.github_proxy]`` sections out
of the resolved gateway config dict (the shape produced by
:func:`corlinman_server.gateway.core.config.load_from_path`) into typed
:class:`MarketplaceConfig` / :class:`AccelSettings`. Every key has a safe
default so a config with no ``[marketplace]`` section yields the
GitHub-default, accel-off baseline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from corlinman_server.system.marketplace.accel import AccelSettings

__all__ = ["MarketplaceConfig", "load_marketplace_config"]

_DEFAULT_REGISTRY_REPO = "sweetcornna/corlinman-marketplace"
_GITHUB_TOKEN_ENV = "CORLINMAN_GITHUB_TOKEN"


@dataclass(frozen=True, slots=True)
class MarketplaceConfig:
    """Resolved ``[marketplace]`` configuration."""

    registry_repo: str = _DEFAULT_REGISTRY_REPO
    registry_ref: str = "main"
    default_source: str = "github"  # github | clawhub
    clawhub_enabled: bool = False
    github_token: str | None = None
    accel: AccelSettings = field(default_factory=AccelSettings)


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _as_str(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


def load_marketplace_config(config: Any) -> MarketplaceConfig:
    """Build a :class:`MarketplaceConfig` from a gateway config object.

    Never raises — a missing / malformed section degrades to defaults.
    """
    section = _get(config, "marketplace")
    if not isinstance(section, dict):
        section = {}

    proxy = section.get("github_proxy")
    if not isinstance(proxy, dict):
        proxy = {}
    accel = AccelSettings(
        mode=_as_str(proxy.get("mode"), "off").lower() or "off",
        preset=_as_str(proxy.get("preset"), "ghproxy").lower() or "ghproxy",
        base=_as_str(proxy.get("base"), "https://ghproxy.com/"),
        mirror_host=_as_str(proxy.get("mirror_host")),
        assume_region=_as_str(proxy.get("assume_region")).lower(),
    )

    token = section.get("github_token")
    if token is None:
        # Fall back to the same token the update checker uses.
        update = _get(_get(config, "system"), "update_check")
        if isinstance(update, dict):
            token = update.get("github_token")
    if token is None:
        token = os.environ.get(_GITHUB_TOKEN_ENV)
    token_str = _as_str(token).strip() or None

    return MarketplaceConfig(
        registry_repo=_as_str(section.get("registry_repo"), _DEFAULT_REGISTRY_REPO)
        or _DEFAULT_REGISTRY_REPO,
        registry_ref=_as_str(section.get("registry_ref"), "main") or "main",
        default_source=_as_str(section.get("default_source"), "github").lower()
        or "github",
        clawhub_enabled=_as_bool(section.get("clawhub_enabled")),
        github_token=token_str,
        accel=accel,
    )
