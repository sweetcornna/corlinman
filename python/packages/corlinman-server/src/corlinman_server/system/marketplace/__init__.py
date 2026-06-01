"""``corlinman_server.system.marketplace`` — unified extension marketplace.

A source-agnostic marketplace for three installable extension kinds —
**skills**, **MCP servers**, and **plugins** — served from a single
curated GitHub registry repo by default (with the legacy clawhub.ai skill
hub available behind a toggle), plus a configurable GitHub-URL
accelerator for China-region hosts.

Public surface
--------------

* :class:`MarketplaceSource` — the backend Protocol.
* :class:`GitHubSource` / :class:`ClawHubSource` — the two backends.
* :class:`GithubAccelerator` / :class:`AccelSettings` — URL acceleration.
* :class:`MarketplaceConfig` + :func:`load_marketplace_config` — config.
* :func:`build_source` / :func:`source_from_config` — construction.
* The DTO + error family (:class:`MarketplaceItem`,
  :class:`MarketplaceDownload`, :class:`MarketplaceUnavailableError`, …).
"""

from __future__ import annotations

from corlinman_server.system.marketplace.accel import (
    AccelSettings,
    GithubAccelerator,
)
from corlinman_server.system.marketplace.clawhub_source import ClawHubSource
from corlinman_server.system.marketplace.config import (
    MarketplaceConfig,
    load_marketplace_config,
)
from corlinman_server.system.marketplace.factory import (
    build_source,
    source_from_config,
)
from corlinman_server.system.marketplace.github_source import GitHubSource
from corlinman_server.system.marketplace.source import (
    MarketplaceDownload,
    MarketplaceError,
    MarketplaceIntegrityError,
    MarketplaceItem,
    MarketplaceKind,
    MarketplaceRateLimitedError,
    MarketplaceSource,
    MarketplaceUnavailableError,
)

__all__ = [
    "AccelSettings",
    "ClawHubSource",
    "GitHubSource",
    "GithubAccelerator",
    "MarketplaceConfig",
    "MarketplaceDownload",
    "MarketplaceError",
    "MarketplaceIntegrityError",
    "MarketplaceItem",
    "MarketplaceKind",
    "MarketplaceRateLimitedError",
    "MarketplaceSource",
    "MarketplaceUnavailableError",
    "build_source",
    "load_marketplace_config",
    "source_from_config",
]
