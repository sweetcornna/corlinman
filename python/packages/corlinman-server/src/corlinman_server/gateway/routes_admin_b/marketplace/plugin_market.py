"""``/admin/plugins/market*`` — plugin marketplace browse + install surface.

Wires the admin UI's "Browse plugins" tab to the unified marketplace:

* :class:`~corlinman_server.system.marketplace.source.MarketplaceSource`
  (``list_items`` / ``detail`` / ``download`` with ``kind="plugin"``) for
  the catalog + the install payload.
* :func:`~corlinman_server.system.marketplace.plugin_installer.install_plugin`
  / :func:`~corlinman_server.system.marketplace.plugin_installer.uninstall_plugin`
  to materialise / remove the on-disk bundle under ``<data_dir>/plugins``.
* :class:`~corlinman_server.system.marketplace.plugin_store.PluginStore`
  (the SQLite index) so the admin UI can render an installed-plugins list
  with an enabled toggle + provenance.

Surfaces, all gated by :func:`require_admin` via the router-level
``dependencies=[Depends(require_admin)]`` guard (mirrors ``skills.py`` /
``plugins.py``):

* ``GET    /admin/plugins/market``            — catalog list (offline-collapse).
* ``GET    /admin/plugins/market/{slug}``     — full detail for one plugin.
* ``POST   /admin/plugins/market/install``    — download + extract + index a
  plugin **staged DISABLED** (we do *not* hot-load it into the running
  :class:`PluginRegistry` here — staging keeps an untrusted bundle inert
  until an operator explicitly enables it).
* ``POST   /admin/plugins/market/{slug}/enable``  — flip the index row on +
  best-effort reload IF a reload hook is wired on ``AdminState.extras``.
* ``POST   /admin/plugins/market/{slug}/disable`` — flip the index row off.
* ``DELETE /admin/plugins/market/{slug}``     — rm the bundle + index row.

Offline-collapse contract (mirrors ``skills.py``): a
:class:`MarketplaceUnavailableError` from the source on the *list* route
collapses to a typed ``{rows: [], offline: true, error: ...}`` envelope at
HTTP 200 so the page still renders; a
:class:`MarketplaceRateLimitedError` surfaces ``retry_after`` so the UI can
render a countdown. The detail / install routes return a 502 / 503 envelope
(the page is already up; only the modal fails).

Runtime handles are resolved off :attr:`AdminState.extras` so tests swap in
fakes without monkey-patching modules:

* ``extras["marketplace_source"]`` — the :class:`MarketplaceSource`.
* ``extras["plugin_store"]``       — the :class:`PluginStore` index.
* ``extras["data_dir"]``           — the gateway data dir (``Path``); the
  plugin install dir is ``<data_dir>/plugins``.

A missing handle collapses to a typed 503 (``marketplace_disabled`` /
``plugin_store_disabled`` / ``data_dir_unset``) so a degraded boot still
serves the rest of the admin tree cleanly.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)

__all__ = ["router"]


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


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(
        dependencies=[Depends(require_admin)],
        tags=["admin", "plugins", "marketplace"],
    )

    # ------------------------------------------------------------------
    # GET /admin/plugins/market
    # ------------------------------------------------------------------

    @r.get(
        "/admin/plugins/market",
        response_model=MarketPluginListResponse,
    )
    async def list_market(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        sort: Annotated[str, Query()] = "trending",
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> MarketPluginListResponse:
        """List the marketplace plugin catalog.

        Collapses any source failure to the typed offline envelope at
        HTTP 200 so the page still renders (banner + Retry).
        """
        source = _resolve_source(admin_state)
        if source is None:
            return MarketPluginListResponse(
                rows=[],
                offline=True,
                error="marketplace_disabled",
            )
        try:
            result = await source.list_items(
                "plugin", sort=sort, cursor=cursor, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            err, retry_after = _offline_from_exc(exc)
            return MarketPluginListResponse(
                rows=[],
                offline=True,
                error=err,
                retry_after=retry_after,
            )
        # Normalise to (rows, next_cursor) regardless of bare-list vs tuple.
        if isinstance(result, tuple):
            items, next_cursor = (
                result[0],
                result[1] if len(result) > 1 else None,
            )
        else:
            items, next_cursor = result, None
        return MarketPluginListResponse(
            rows=[_item_to_row(it) for it in items],
            next_cursor=next_cursor,
        )

    # ------------------------------------------------------------------
    # GET /admin/plugins/market/installed
    #
    # Declared *before* the ``/{slug}`` route so FastAPI matches the
    # literal "installed" path first rather than treating it as a slug.
    # ------------------------------------------------------------------

    @r.get(
        "/admin/plugins/market/installed",
        response_model=list[InstalledPluginOut],
    )
    async def list_installed(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> list[InstalledPluginOut]:
        """List marketplace-installed plugins (the persisted index).

        A missing store is the empty-catalogue case (returns ``[]``),
        not a degradation — keeps the Installed tab renderable on a
        degraded boot.
        """
        store = _resolve_store(admin_state)
        if store is None:
            return []
        try:
            rows = list(store.list())
        except Exception:  # noqa: BLE001 — never 500 the list
            return []
        return [_row_to_out(row) for row in rows]

    # ------------------------------------------------------------------
    # GET /admin/plugins/market/{slug}
    # ------------------------------------------------------------------

    @r.get(
        "/admin/plugins/market/{slug}",
        response_model=MarketPluginDetailOut,
    )
    async def detail_market(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        slug: str = PathParam(..., description="Marketplace plugin slug."),
    ) -> MarketPluginDetailOut | JSONResponse:
        """Full detail for one marketplace plugin."""
        source = _resolve_source(admin_state)
        if source is None:
            return _error(
                503,
                "marketplace_disabled",
                "no marketplace source is wired on this gateway",
            )
        try:
            item = await source.detail("plugin", slug)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "not found" in msg or getattr(exc, "status_code", None) == 404:
                return _error(
                    404,
                    "plugin_not_found",
                    f"no marketplace plugin with slug {slug!r}",
                    slug=slug,
                )
            err, retry_after = _offline_from_exc(exc)
            return _error(
                502,
                err,
                f"marketplace detail fetch failed: {exc}",
                slug=slug,
                retry_after=retry_after,
            )
        return _item_to_detail(item)

    # ------------------------------------------------------------------
    # POST /admin/plugins/market/install
    # ------------------------------------------------------------------

    @r.post(
        "/admin/plugins/market/install",
        response_model=InstalledPluginOut,
        status_code=status.HTTP_201_CREATED,
    )
    async def install_market(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        body: MarketInstallBody,
    ) -> InstalledPluginOut | JSONResponse:
        """Download + extract + index a marketplace plugin, STAGED DISABLED.

        The bundle is materialised under ``<data_dir>/plugins/<slug>`` and
        an index row is written with ``enabled=False``. We deliberately do
        *not* hot-load it into the running :class:`PluginRegistry` — the
        plugin stays inert until an operator hits the enable route.
        """
        source = _resolve_source(admin_state)
        if source is None:
            return _error(
                503,
                "marketplace_disabled",
                "no marketplace source is wired on this gateway",
            )
        store = _resolve_store(admin_state)
        if store is None:
            return _error(
                503,
                "plugin_store_disabled",
                "no plugin index store is wired on this gateway",
            )
        plugins_dir = _plugins_dir(admin_state)
        if plugins_dir is None:
            return _error(
                503,
                "data_dir_unset",
                "gateway booted without a data dir; cannot resolve the "
                "plugin install directory",
            )

        version = body.version or "latest"

        # 1. Download the tarball bytes from the source.
        try:
            download = await source.download("plugin", body.slug, version)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "not found" in msg or getattr(exc, "status_code", None) == 404:
                return _error(
                    404,
                    "plugin_not_found",
                    f"no marketplace plugin with slug {body.slug!r}",
                    slug=body.slug,
                )
            err, retry_after = _offline_from_exc(exc)
            if err == "marketplace_integrity_failed":
                return _error(
                    422,
                    err,
                    f"download integrity check failed: {exc}",
                    slug=body.slug,
                )
            return _error(
                502,
                err,
                f"marketplace download failed: {exc}",
                slug=body.slug,
                retry_after=retry_after,
            )

        # The resolved version may differ from the requested "latest".
        resolved_version = str(
            getattr(download, "version", None) or version
        )
        content = getattr(download, "content", None)
        if not isinstance(content, (bytes, bytearray)):
            return _error(
                502,
                "marketplace_bad_download",
                "marketplace source returned a non-tarball payload",
                slug=body.slug,
            )

        # 2. Extract the bundle into <data_dir>/plugins/<slug>.
        audit_log = getattr(admin_state, "audit_log", None)
        try:
            from corlinman_server.system.marketplace.plugin_installer import (
                PluginAlreadyInstalledError,
                UnsafeTarballError,
                install_plugin,
            )
        except ImportError as exc:
            return _error(
                503,
                "installer_missing",
                f"the plugin installer is not available: {exc}",
            )
        try:
            await install_plugin(
                plugins_dir=plugins_dir,
                content=bytes(content),
                slug=body.slug,
                version=resolved_version,
                source="github",
                force=False,
                audit_log=audit_log,
            )
        except PluginAlreadyInstalledError as exc:
            return _error(
                409,
                "plugin_already_installed",
                str(exc),
                slug=body.slug,
            )
        except UnsafeTarballError as exc:
            return _error(
                400,
                "unsafe_tarball",
                str(exc),
                slug=body.slug,
            )
        except Exception as exc:  # noqa: BLE001
            return _error(
                500,
                "install_failed",
                str(exc),
                slug=body.slug,
            )

        # 3. Index the install DISABLED (staged until an operator enables).
        try:
            from corlinman_server.system.marketplace.plugin_store import (
                PluginInvalid,
            )
        except ImportError:
            PluginInvalid = ()  # type: ignore[assignment,misc]
        try:
            row = store.upsert(
                body.slug,
                version=resolved_version,
                source="github",
                enabled=False,
            )
        except PluginInvalid as exc:  # type: ignore[misc]
            return _error(
                422,
                "invalid_slug",
                str(exc),
                slug=body.slug,
            )
        except Exception as exc:  # noqa: BLE001
            return _error(
                500,
                "index_failed",
                str(exc),
                slug=body.slug,
            )

        return _row_to_out(row)

    # ------------------------------------------------------------------
    # POST /admin/plugins/market/{slug}/enable
    # ------------------------------------------------------------------

    @r.post(
        "/admin/plugins/market/{slug}/enable",
        response_model=EnableResultOut,
    )
    async def enable_market(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        slug: str = PathParam(..., description="Installed plugin slug."),
    ) -> EnableResultOut | JSONResponse:
        """Flip the index row on + best-effort live reload.

        The running :class:`PluginRegistry` has no hot-reload today, so the
        enable applies only after a restart unless the boot path wires a
        ``plugin_registry_reload`` callable into ``AdminState.extras``. When
        it does we call it best-effort and report ``applies="now"``;
        otherwise we report ``applies="next_restart"`` so the UI tells the
        operator a restart is needed.
        """
        store = _resolve_store(admin_state)
        if store is None:
            return _error(
                503,
                "plugin_store_disabled",
                "no plugin index store is wired on this gateway",
            )
        try:
            from corlinman_server.system.marketplace.plugin_store import (
                PluginNotFound,
            )
        except ImportError:
            PluginNotFound = ()  # type: ignore[assignment,misc]
        try:
            row = store.set_enabled(slug, True)
        except PluginNotFound as exc:  # type: ignore[misc]
            return _error(
                404,
                "plugin_not_installed",
                str(exc),
                slug=slug,
            )
        except Exception as exc:  # noqa: BLE001
            return _error(
                500,
                "enable_failed",
                str(exc),
                slug=slug,
            )

        applies = "next_restart"
        hook = _resolve_reload_hook(admin_state)
        if hook is not None:
            try:
                res = hook()
                if hasattr(res, "__await__"):
                    await res
                applies = "now"
            except Exception:  # noqa: BLE001 — reload is best-effort.
                applies = "next_restart"

        return EnableResultOut(
            slug=slug,
            enabled=True,
            applies=applies,
            row=_row_to_out(row),
        )

    # ------------------------------------------------------------------
    # POST /admin/plugins/market/{slug}/disable
    # ------------------------------------------------------------------

    @r.post(
        "/admin/plugins/market/{slug}/disable",
        response_model=InstalledPluginOut,
    )
    async def disable_market(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        slug: str = PathParam(..., description="Installed plugin slug."),
    ) -> InstalledPluginOut | JSONResponse:
        """Flip the index row off. The bundle stays on disk."""
        store = _resolve_store(admin_state)
        if store is None:
            return _error(
                503,
                "plugin_store_disabled",
                "no plugin index store is wired on this gateway",
            )
        try:
            from corlinman_server.system.marketplace.plugin_store import (
                PluginNotFound,
            )
        except ImportError:
            PluginNotFound = ()  # type: ignore[assignment,misc]
        try:
            row = store.set_enabled(slug, False)
        except PluginNotFound as exc:  # type: ignore[misc]
            return _error(
                404,
                "plugin_not_installed",
                str(exc),
                slug=slug,
            )
        except Exception as exc:  # noqa: BLE001
            return _error(
                500,
                "disable_failed",
                str(exc),
                slug=slug,
            )
        # Best-effort: drop the plugin from the live registry now so its
        # tools stop resolving without a restart.
        hook = _resolve_reload_hook(admin_state)
        if hook is not None:
            try:
                res = hook()
                if hasattr(res, "__await__"):
                    await res
            except Exception:  # noqa: BLE001 — reload is best-effort.
                pass
        return _row_to_out(row)

    # ------------------------------------------------------------------
    # DELETE /admin/plugins/market/{slug}
    # ------------------------------------------------------------------

    @r.delete("/admin/plugins/market/{slug}")
    async def uninstall_market(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        slug: str = PathParam(..., description="Installed plugin slug."),
    ) -> JSONResponse:
        """Remove the on-disk bundle + the index row.

        The installer refuses to ``rm -rf`` a bundle that lacks the
        ``.openclaw-meta.json`` sidecar (a bundled / hand-placed plugin),
        which surfaces here as a 409. The index row is always dropped after
        a successful (or already-absent) bundle removal so the listing
        stays consistent.
        """
        store = _resolve_store(admin_state)
        if store is None:
            return _error(
                503,
                "plugin_store_disabled",
                "no plugin index store is wired on this gateway",
            )
        plugins_dir = _plugins_dir(admin_state)
        if plugins_dir is None:
            return _error(
                503,
                "data_dir_unset",
                "gateway booted without a data dir; cannot resolve the "
                "plugin install directory",
            )

        audit_log = getattr(admin_state, "audit_log", None)
        try:
            from corlinman_server.system.marketplace.plugin_installer import (
                PluginInstallError,
                UnsafeTarballError,
                uninstall_plugin,
            )
        except ImportError as exc:
            return _error(
                503,
                "installer_missing",
                f"the plugin installer is not available: {exc}",
            )

        bundle_removed = True
        try:
            await uninstall_plugin(
                plugins_dir=plugins_dir,
                slug=slug,
                audit_log=audit_log,
            )
        except UnsafeTarballError as exc:
            return _error(
                400,
                "unsafe_slug",
                str(exc),
                slug=slug,
            )
        except PluginInstallError as exc:
            msg = str(exc).lower()
            # A missing target is the already-removed case — drop the index
            # row anyway so a dangling row left by an out-of-band ``rm`` is
            # reconciled. A sidecar-missing refusal is a hard 409.
            if "not installed" in msg:
                bundle_removed = False
            elif "refusing to uninstall" in msg or "sidecar" in msg:
                return _error(
                    409,
                    "bundled_protected",
                    str(exc),
                    slug=slug,
                )
            else:
                return _error(
                    500,
                    "uninstall_failed",
                    str(exc),
                    slug=slug,
                )
        except Exception as exc:  # noqa: BLE001
            return _error(
                500,
                "uninstall_failed",
                str(exc),
                slug=slug,
            )

        # Drop the index row regardless of whether the bundle existed.
        deleted = False
        with contextlib.suppress(Exception):
            deleted = bool(store.delete(slug))

        if not bundle_removed and not deleted:
            return _error(
                404,
                "plugin_not_installed",
                f"plugin {slug!r} is not installed",
                slug=slug,
            )

        # Best-effort: re-sync the live registry so the removed plugin's
        # tools stop resolving immediately (no restart).
        hook = _resolve_reload_hook(admin_state)
        if hook is not None:
            try:
                res = hook()
                if hasattr(res, "__await__"):
                    await res
            except Exception:  # noqa: BLE001 — reload is best-effort.
                pass

        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "slug": slug,
                "bundle_removed": bundle_removed,
                "index_removed": deleted,
            },
        )

    return r
