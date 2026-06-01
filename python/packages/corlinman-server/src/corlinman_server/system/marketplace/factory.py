"""``corlinman_server.system.marketplace.factory`` — source builder.

Resolves the configured :class:`MarketplaceConfig` into a live
:class:`MarketplaceSource` (one per gateway process). The default path is
GitHub; ``default_source = "clawhub"`` (with ``clawhub_enabled = true``)
returns the legacy clawhub adapter instead.
"""

from __future__ import annotations

from typing import Any

import httpx

from corlinman_server.system.marketplace.accel import GithubAccelerator
from corlinman_server.system.marketplace.clawhub_source import ClawHubSource
from corlinman_server.system.marketplace.config import (
    MarketplaceConfig,
    load_marketplace_config,
)
from corlinman_server.system.marketplace.github_source import GitHubSource
from corlinman_server.system.marketplace.source import MarketplaceSource

__all__ = ["build_source", "source_from_config"]


def build_source(
    cfg: MarketplaceConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> MarketplaceSource:
    """Construct the configured source. ``transport`` is the test seam."""
    if cfg.default_source == "clawhub" and cfg.clawhub_enabled:
        return ClawHubSource()
    accel = GithubAccelerator(cfg.accel)
    return GitHubSource(
        repo=cfg.registry_repo,
        ref=cfg.registry_ref,
        accel=accel,
        token=cfg.github_token,
        transport=transport,
    )


def source_from_config(
    config: Any,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> MarketplaceSource:
    """Convenience: parse a gateway config object then build the source."""
    return build_source(load_marketplace_config(config), transport=transport)
