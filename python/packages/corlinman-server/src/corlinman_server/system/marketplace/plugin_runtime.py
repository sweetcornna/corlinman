"""``corlinman_server.system.marketplace.plugin_runtime`` — live hot-load.

Bridges the marketplace plugin **store** (the persisted enabled flag) to
the live :class:`~corlinman_providers.plugins.PluginRegistry` so enabling
or disabling a plugin takes effect **without a restart**.

The registry's tool invoker resolves ``registry.get(name)`` on every call,
so an ``upsert``/``remove`` is picked up immediately. We keep the live
registry in lock-step with the enabled set:

* enabled marketplace plugin whose ``plugin-manifest.toml`` discovers
  cleanly  → ``upsert`` its :class:`PluginEntry`,
* disabled (or removed) plugin → ``remove`` it by manifest name.

Discovery only finds the canonical ``plugin-manifest.toml`` (the format
the registry already understands); a marketplace bundle that ships only a
``manifest.json`` installs fine but simply won't hot-load until it carries
a ``plugin-manifest.toml`` (logged, never fatal).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

import structlog
from corlinman_providers.plugins import PluginRegistry
from corlinman_providers.plugins.discovery import Origin, SearchRoot

logger = structlog.get_logger(__name__)

__all__ = ["make_reload_hook", "sync_registry"]


def _discover_by_slug(plugins_dir: Path) -> dict[str, Any]:
    """Map install-dir name (slug) → :class:`PluginEntry` for ``plugins_dir``.

    Uses a throwaway :class:`PluginRegistry` to run the same discovery the
    boot path uses, then re-keys the entries by the directory name (the
    marketplace slug) since the store keys on slug, not on ``manifest.name``.
    """
    temp = PluginRegistry.from_roots(
        [SearchRoot(path=plugins_dir, origin=Origin.CONFIG)]
    )
    by_slug: dict[str, Any] = {}
    for entry in temp.list():
        slug = entry.plugin_dir().name
        by_slug[slug] = entry
    return by_slug


async def sync_registry(
    registry: Any,
    plugins_dir: Path,
    enabled_slugs: set[str],
) -> dict[str, list[str]]:
    """Reconcile ``registry`` so exactly the enabled marketplace plugins
    under ``plugins_dir`` are live — without disturbing env-root plugins.

    The desired live set is the plugins that are both **enabled** (in
    ``enabled_slugs``) and **present** on disk (discoverable). We then:

    * remove every *marketplace-managed* registry entry (one whose manifest
      lives under ``plugins_dir``) that is no longer desired — this covers
      disable (still on disk, not enabled) AND uninstall (bundle already
      deleted from disk, so it can't be rediscovered), while leaving
      env-configured plugins untouched;
    * upsert each desired entry.

    Returns ``{"loaded": [...], "removed": [...]}`` (manifest names). Never
    raises — a bad manifest is skipped via discovery diagnostics.
    """
    plugins_root = Path(plugins_dir).resolve()
    by_slug = _discover_by_slug(plugins_root)
    desired = {
        slug: entry for slug, entry in by_slug.items() if slug in enabled_slugs
    }
    desired_names = {entry.manifest.name for entry in desired.values()}

    loaded: list[str] = []
    removed: list[str] = []

    # Drop marketplace-managed entries that are no longer desired (disabled
    # or uninstalled). Identify "marketplace-managed" by manifest path so
    # env-root plugins are never removed.
    for entry in registry.list():
        try:
            managed = entry.manifest_path.resolve().is_relative_to(plugins_root)
        except (ValueError, OSError):  # pragma: no cover — exotic paths
            managed = False
        if managed and entry.manifest.name not in desired_names:
            gone = await registry.remove(entry.manifest.name)
            if gone is not None:
                removed.append(entry.manifest.name)

    for entry in desired.values():
        await registry.upsert(entry)
        loaded.append(entry.manifest.name)

    # Warn about enabled slugs that did not discover (e.g. no
    # plugin-manifest.toml) so the operator knows why a plugin isn't live.
    missing = sorted(enabled_slugs - set(by_slug))
    if missing:
        logger.warning(
            "marketplace.plugin_runtime.enabled_not_discovered",
            slugs=missing,
            hint="plugin needs a plugin-manifest.toml to hot-load",
        )
    logger.info(
        "marketplace.plugin_runtime.synced",
        loaded=loaded,
        removed=removed,
    )
    return {"loaded": loaded, "removed": removed}


def make_reload_hook(
    registry: Any,
    plugins_dir: Path,
    enabled_provider: Callable[[], Iterable[str]],
) -> Callable[[], Awaitable[dict[str, list[str]]]]:
    """Build the ``extras["plugin_registry_reload"]`` callable.

    ``enabled_provider`` returns the current set of enabled slugs (read
    from the plugin store) each time the hook fires, so enable/disable/
    uninstall all converge the live registry to the persisted truth.
    """

    async def _reload() -> dict[str, list[str]]:
        enabled = set(enabled_provider())
        return await sync_registry(registry, plugins_dir, enabled)

    return _reload
