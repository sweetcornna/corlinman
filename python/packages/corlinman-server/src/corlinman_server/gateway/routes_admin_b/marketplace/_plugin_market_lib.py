"""Library internals for ``/admin/plugins/market*`` — wire models + helpers.

Extracted verbatim from :mod:`...marketplace.plugin_market` so that module
shrinks to the ``router()`` factory + its route handlers. This sibling holds
every module-level pydantic wire model and the helper functions the handlers
call. It is imported back into ``plugin_market.py``; it must NOT import
``plugin_market.py`` (no cycle). It imports the same siblings the original
module did (``...routes_admin_b.state``, ``...system.*`` lazily inside the
functions that need them).

Behaviour is byte-for-byte identical to the original module-level code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import AdminState

# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class MarketPluginRowOut(BaseModel):
    """One catalog row in ``GET /admin/plugins/market``.

    Projects :class:`MarketplaceItem` onto the browser-facing fields. The
    source-internal install pointers (``content_ref`` / ``sha256``) are
    intentionally *not* surfaced.
    """

    slug: str
    name: str
    description: str = ""
    latest_version: str = ""
    emoji: str | None = None
    stars: int = 0
    downloads: int = 0
    updated_at: str = ""
    homepage: str | None = None
    tags: list[str] = Field(default_factory=list)
    requires_env: list[str] = Field(default_factory=list)


class MarketPluginListResponse(BaseModel):
    """Envelope for the catalog list.

    ``offline`` flips ``True`` when the source surfaces a
    :class:`MarketplaceUnavailableError`; the UI renders a banner + Retry
    button off it. Even offline we keep HTTP 200 (so the fetch resolves),
    with an explicit ``error`` machine code. ``retry_after`` is populated
    only on a rate-limit collapse.
    """

    rows: list[MarketPluginRowOut] = Field(default_factory=list)
    next_cursor: str | None = None
    offline: bool = False
    error: str | None = None
    retry_after: int | None = None


class MarketPluginDetailOut(BaseModel):
    """Full detail returned by ``GET /admin/plugins/market/{slug}``."""

    slug: str
    name: str
    description: str = ""
    latest_version: str = ""
    versions: list[str] = Field(default_factory=list)
    emoji: str | None = None
    stars: int = 0
    downloads: int = 0
    updated_at: str = ""
    homepage: str | None = None
    tags: list[str] = Field(default_factory=list)
    requires_env: list[str] = Field(default_factory=list)
    readme_excerpt: str = ""
    scan_summary: str | None = None


class InstalledPluginOut(BaseModel):
    """One index row — mirrors :class:`InstalledPluginRow`."""

    slug: str
    version: str = ""
    source: str = ""
    enabled: bool = False
    installed_at: str = ""
    updated_at: str = ""


class MarketInstallBody(BaseModel):
    """Body for ``POST /admin/plugins/market/install``."""

    slug: str
    version: str | None = None


class EnableResultOut(BaseModel):
    """Result of the enable route.

    ``applies`` distinguishes a live reload (``"now"``) from the
    staged-until-restart case (``"next_restart"``) when no reload hook is
    wired on :attr:`AdminState.extras`.
    """

    slug: str
    enabled: bool
    applies: str
    row: InstalledPluginOut


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(
    status_code: int,
    error: str,
    message: str,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {"error": error, "message": message}
    body.update(extra)
    return JSONResponse(status_code=status_code, content=body)


def _resolve_source(state: AdminState) -> Any | None:
    """Read the :class:`MarketplaceSource` off ``AdminState.extras``.

    Duck-typed so tests can swap in a fake exposing ``list_items`` /
    ``detail`` / ``download``. Returns ``None`` when unwired so the
    handlers degrade to a typed 503 / offline envelope.
    """
    extras = getattr(state, "extras", None)
    if not isinstance(extras, dict):
        return None
    source = extras.get("marketplace_source")
    if source is None:
        return None
    if hasattr(source, "list_items") or hasattr(source, "download"):
        return source
    return None


def _resolve_store(state: AdminState) -> Any | None:
    """Read the :class:`PluginStore` index off ``AdminState.extras``."""
    extras = getattr(state, "extras", None)
    if not isinstance(extras, dict):
        return None
    store = extras.get("plugin_store")
    if store is None:
        return None
    if hasattr(store, "upsert") and hasattr(store, "set_enabled"):
        return store
    return None


def _resolve_data_dir(state: AdminState) -> Path | None:
    """Resolve the gateway data dir.

    Prefers ``extras["data_dir"]`` (the documented wiring key for this
    surface) and falls back to the dataclass slot so either bootstrap path
    works.
    """
    extras = getattr(state, "extras", None)
    if isinstance(extras, dict):
        raw = extras.get("data_dir")
        if raw is not None:
            return Path(raw)
    raw = getattr(state, "data_dir", None)
    if raw is not None:
        return Path(raw)
    return None


def _plugins_dir(state: AdminState) -> Path | None:
    data_dir = _resolve_data_dir(state)
    if data_dir is None:
        return None
    return data_dir / "plugins"


def _resolve_reload_hook(state: AdminState) -> Any | None:
    """Best-effort PluginRegistry reload hook off ``AdminState.extras``.

    The running :class:`~corlinman_providers.plugins.PluginRegistry` has no
    hot-reload method today, so enabling a freshly-installed plugin applies
    only after a restart *unless* the boot path wires a callable under
    ``extras["plugin_registry_reload"]``. When present we call it
    best-effort; any failure degrades to the ``next_restart`` answer.
    """
    extras = getattr(state, "extras", None)
    if not isinstance(extras, dict):
        return None
    hook = extras.get("plugin_registry_reload")
    return hook if callable(hook) else None


def _getter(obj: Any) -> Any:
    """Return a ``get(key, default)`` callable for a dict-or-attr object."""
    if isinstance(obj, dict):
        return obj.get
    return lambda k, d=None: getattr(obj, k, d)


def _item_to_row(item: Any) -> MarketPluginRowOut:
    g = _getter(item)
    tags = g("tags") or ()
    requires_env = g("requires_env") or ()
    return MarketPluginRowOut(
        slug=str(g("slug") or ""),
        name=str(g("name") or g("slug") or ""),
        description=str(g("description") or ""),
        latest_version=str(g("latest_version") or ""),
        emoji=g("emoji"),
        stars=int(g("stars") or 0),
        downloads=int(g("downloads") or 0),
        updated_at=str(g("updated_at") or ""),
        homepage=g("homepage"),
        tags=[str(t) for t in tags],
        requires_env=[str(e) for e in requires_env],
    )


def _item_to_detail(item: Any) -> MarketPluginDetailOut:
    g = _getter(item)
    tags = g("tags") or ()
    versions = g("versions") or ()
    requires_env = g("requires_env") or ()
    return MarketPluginDetailOut(
        slug=str(g("slug") or ""),
        name=str(g("name") or g("slug") or ""),
        description=str(g("description") or ""),
        latest_version=str(g("latest_version") or ""),
        versions=[str(v) for v in versions],
        emoji=g("emoji"),
        stars=int(g("stars") or 0),
        downloads=int(g("downloads") or 0),
        updated_at=str(g("updated_at") or ""),
        homepage=g("homepage"),
        tags=[str(t) for t in tags],
        requires_env=[str(e) for e in requires_env],
        readme_excerpt=str(g("readme_excerpt") or ""),
        scan_summary=g("scan_summary"),
    )


def _row_to_out(row: Any) -> InstalledPluginOut:
    g = _getter(row)
    return InstalledPluginOut(
        slug=str(g("slug") or ""),
        version=str(g("version") or ""),
        source=str(g("source") or ""),
        enabled=bool(g("enabled")),
        installed_at=str(g("installed_at") or ""),
        updated_at=str(g("updated_at") or ""),
    )


def _offline_from_exc(exc: Exception) -> tuple[str, int | None]:
    """Map a marketplace exception onto ``(error_code, retry_after)``.

    Lazy-imports the marketplace error family so this module stays
    importable when the marketplace package is excluded from a build.
    """
    retry_after: int | None = getattr(exc, "retry_after_seconds", None)
    try:
        from corlinman_server.system.marketplace.source import (
            MarketplaceIntegrityError,
            MarketplaceRateLimitedError,
            MarketplaceUnavailableError,
        )
    except ImportError:
        return "marketplace_unreachable", retry_after
    if isinstance(exc, MarketplaceRateLimitedError):
        return "marketplace_rate_limited", retry_after
    if isinstance(exc, MarketplaceIntegrityError):
        return "marketplace_integrity_failed", retry_after
    if isinstance(exc, MarketplaceUnavailableError):
        return "marketplace_unreachable", retry_after
    return "marketplace_unreachable", retry_after
