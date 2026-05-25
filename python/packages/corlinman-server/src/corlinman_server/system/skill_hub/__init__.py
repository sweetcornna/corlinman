"""``corlinman_server.system.skill_hub`` — ClawHub client + installer.

W1.1 + W1.2 of ``docs/PLAN_SKILL_HUB.md``. Owns the async HTTP client
the gateway uses to proxy ClawHub's anonymous read API, and the
installer pipeline that materialises a downloaded skill tarball into a
profile's ``skills/<slug>/`` directory.

Wiring contract
---------------

* One :class:`ClawHubClient` per gateway process (it owns a reusable
  :class:`httpx.AsyncClient` + a small TTL cache). The admin routes
  resolve it off ``AdminState.skill_hub_client``; tests construct one
  ad-hoc against an httpx ``MockTransport``.
* :func:`install_skill` / :func:`uninstall_skill` are stateless helpers
  — they take an explicit ``profile_skills_dir`` (resolved by the route
  layer from the active profile) plus a ``ClawHubClient`` for the
  download side. Both write best-effort audit-log rows when an
  :class:`SystemAuditLog` is passed in.
"""

from __future__ import annotations

from corlinman_server.system.skill_hub.client import (
    ClawHubClient,
    HubDownload,
    HubRateLimitedError,
    HubSkillDetail,
    HubSkillSummary,
    HubUnavailableError,
)
from corlinman_server.system.skill_hub.installer import (
    InstallReport,
    SkillAlreadyInstalledError,
    SkillInstallError,
    UnsafeTarballError,
    install_skill,
    uninstall_skill,
)

__all__ = [
    "ClawHubClient",
    "HubDownload",
    "HubRateLimitedError",
    "HubSkillDetail",
    "HubSkillSummary",
    "HubUnavailableError",
    "InstallReport",
    "SkillAlreadyInstalledError",
    "SkillInstallError",
    "UnsafeTarballError",
    "install_skill",
    "uninstall_skill",
]
