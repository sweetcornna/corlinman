"""``/admin/mcp*`` — MCP-server marketplace browse + install surface.

The MCP twin of ``skills.py``. Wires the admin UI's "MCP servers" page to
the unified marketplace (:mod:`corlinman_server.system.marketplace`) and
the live-pool ↔ store bridge (:class:`~corlinman_server.gateway.
routes_admin_b.mcp_adapter.McpAdapter`).

Surfaces (prefix ``/admin/mcp``):

* **Browse** — ``GET /admin/mcp/market`` lists the marketplace's
  ``kind="mcp"`` rows; ``GET /admin/mcp/market/{slug}`` returns one
  fully-populated detail (including ``requires_env`` so the UI can prompt
  for secrets before install). Both collapse a
  :class:`MarketplaceUnavailableError` to a typed *offline envelope*
  (``{rows: [], offline: true, error: "marketplace_unreachable"}`` at
  HTTP 200) — exactly as ``skills.py`` does for the clawhub hub — so the
  page still renders when the source is unreachable. A
  :class:`MarketplaceRateLimitedError` surfaces ``retry_after_seconds``.

* **Install** — ``POST /admin/mcp/install`` downloads the manifest,
  parses it into a launch spec, merges any operator-supplied ``env``,
  then persists it **disabled** via the adapter (the operator enables it
  explicitly afterwards). Returns the registered (disabled) row.

* **Manage** — ``GET /admin/mcp/servers`` returns the adapter's merged
  installed-+-live view; ``DELETE /admin/mcp/{name}`` uninstalls.

Enable / disable / restart are *not* duplicated here — they're served by
the existing ``/admin/plugins/{name}/{enable,disable,restart}`` seam,
which drives the same :class:`McpAdapter`.

Auth: every route mounts behind :func:`require_admin` via the
``dependencies=[Depends(require_admin)]`` router-level guard, matching
``skills.py`` / ``subagents.py``. The :class:`MarketplaceSource` + the
:class:`McpAdapter` are resolved off :class:`AdminState.extras` (keys
``"marketplace_source"`` / ``"mcp_adapter"``) so tests swap fakes in
without monkey-patching modules.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)
from corlinman_server.system.marketplace.source import (
    MarketplaceItem,
    MarketplaceRateLimitedError,
    MarketplaceUnavailableError,
)

__all__ = ["build_router", "router"]


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class McpMarketRow(BaseModel):
    """One row in ``GET /admin/mcp/market`` (list / search)."""

    slug: str
    name: str
    description: str = ""
    latest_version: str = ""
    emoji: str | None = None
    transport: str | None = None
    stars: int = 0
    downloads: int = 0
    updated_at: str = ""
    tags: list[str] = Field(default_factory=list)
    requires_env: list[str] = Field(default_factory=list)


class McpMarketListResponse(BaseModel):
    """Envelope for the market list.

    ``offline`` flips to ``True`` on a
    :class:`MarketplaceUnavailableError`; the UI uses it to render the
    banner + Retry. Even offline we keep HTTP 200 (so the fetch promise
    resolves) with an explicit machine ``error`` code.
    """

    rows: list[McpMarketRow] = Field(default_factory=list)
    next_cursor: str | None = None
    offline: bool = False
    error: str | None = None


class McpMarketDetail(BaseModel):
    """Full detail for ``GET /admin/mcp/market/{slug}``."""

    slug: str
    name: str
    description: str = ""
    latest_version: str = ""
    versions: list[str] = Field(default_factory=list)
    emoji: str | None = None
    transport: str | None = None
    stars: int = 0
    downloads: int = 0
    updated_at: str = ""
    homepage: str | None = None
    tags: list[str] = Field(default_factory=list)
    requires_env: list[str] = Field(default_factory=list)
    readme_excerpt: str = ""
    scan_summary: str | None = None


class McpInstallBody(BaseModel):
    """Body for ``POST /admin/mcp/install``."""

    slug: str
    version: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class McpServerRow(BaseModel):
    """One row in ``GET /admin/mcp/servers`` (merged installed + live)."""

    name: str
    source: str | None = None
    version: str | None = None
    enabled: bool = False
    transport: str = ""
    status: str = "stopped"
    tools: int = 0
    error: str | None = None
    installed_at: str | None = None
    updated_at: str | None = None


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

    Mirrors ``skills.py``'s :func:`_resolve_hub_client`: duck-typed so
    tests swap a fake exposing ``list_items`` / ``detail`` / ``download``.
    Returns ``None`` so the browse handlers degrade to the offline
    envelope rather than 503.
    """
    extras = getattr(state, "extras", None)
    if isinstance(extras, dict):
        source = extras.get("marketplace_source")
        if source is not None and (
            hasattr(source, "list_items") or hasattr(source, "detail")
        ):
            return source
    return None


def _resolve_adapter(state: AdminState) -> Any | None:
    """Read the :class:`McpAdapter` off ``AdminState.extras``."""
    extras = getattr(state, "extras", None)
    if isinstance(extras, dict):
        adapter = extras.get("mcp_adapter")
        if adapter is not None:
            return adapter
    return None


def _item_to_row(item: MarketplaceItem) -> McpMarketRow:
    return McpMarketRow(
        slug=str(item.slug),
        name=str(item.name),
        description=str(item.description),
        latest_version=str(item.latest_version),
        emoji=item.emoji,
        transport=item.transport,
        stars=int(item.stars),
        downloads=int(item.downloads),
        updated_at=str(item.updated_at or ""),
        tags=[str(t) for t in item.tags],
        requires_env=[str(e) for e in item.requires_env],
    )


def _item_to_detail(item: MarketplaceItem) -> McpMarketDetail:
    return McpMarketDetail(
        slug=str(item.slug),
        name=str(item.name),
        description=str(item.description),
        latest_version=str(item.latest_version),
        versions=[str(v) for v in item.versions],
        emoji=item.emoji,
        transport=item.transport,
        stars=int(item.stars),
        downloads=int(item.downloads),
        updated_at=str(item.updated_at or ""),
        homepage=item.homepage,
        tags=[str(t) for t in item.tags],
        requires_env=[str(e) for e in item.requires_env],
        readme_excerpt=str(item.readme_excerpt or ""),
        scan_summary=item.scan_summary,
    )


def _row_from_installed(installed: Any) -> McpServerRow:
    """Project the adapter's installed-row dict (or
    :class:`InstalledMcpServer`) onto the wire shape."""
    getter = (
        installed.get  # type: ignore[union-attr]
        if isinstance(installed, dict)
        else lambda k, d=None: getattr(installed, k, d)
    )
    return McpServerRow(
        name=str(getter("name") or ""),
        source=getter("source"),
        version=getter("version"),
        enabled=bool(getter("enabled")),
        transport=str(getter("transport") or ""),
        status=str(getter("status") or "stopped"),
        tools=int(getter("tools") or 0),
        error=getter("error"),
        installed_at=_as_opt_str(getter("installed_at")),
        updated_at=_as_opt_str(getter("updated_at")),
    )


def _as_opt_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(value.isoformat())
    except AttributeError:
        return str(value)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(
        dependencies=[Depends(require_admin)], tags=["admin", "mcp"]
    )

    # ------------------------------------------------------------------
    # GET /admin/mcp/market
    # ------------------------------------------------------------------

    @r.get("/admin/mcp/market", response_model=McpMarketListResponse)
    async def market_list(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> McpMarketListResponse:
        """List the marketplace's ``kind="mcp"`` rows with offline-collapse."""
        source = _resolve_source(admin_state)
        if source is None:
            return McpMarketListResponse(
                rows=[], offline=True, error="marketplace_unreachable"
            )
        try:
            items, next_cursor = await source.list_items(kind="mcp")
        except MarketplaceRateLimitedError:
            return McpMarketListResponse(
                rows=[],
                offline=True,
                error="marketplace_rate_limited",
            )
        except MarketplaceUnavailableError:
            return McpMarketListResponse(
                rows=[], offline=True, error="marketplace_unreachable"
            )
        except Exception:  # noqa: BLE001
            return McpMarketListResponse(
                rows=[], offline=True, error="marketplace_unreachable"
            )
        return McpMarketListResponse(
            rows=[_item_to_row(i) for i in items],
            next_cursor=next_cursor,
        )

    # ------------------------------------------------------------------
    # GET /admin/mcp/market/{slug}
    # ------------------------------------------------------------------

    @r.get(
        "/admin/mcp/market/{slug}",
        response_model=McpMarketDetail,
    )
    async def market_detail(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        slug: str = PathParam(..., description="Marketplace MCP slug."),
    ) -> McpMarketDetail | JSONResponse:
        source = _resolve_source(admin_state)
        if source is None:
            return _error(
                503,
                "marketplace_unreachable",
                "the marketplace source is not wired on this gateway",
            )
        try:
            item = await source.detail(kind="mcp", slug=slug)
        except MarketplaceRateLimitedError as exc:
            return _error(
                429,
                "marketplace_rate_limited",
                "marketplace source is rate-limited",
                retry_after_seconds=exc.retry_after_seconds,
            )
        except MarketplaceUnavailableError as exc:
            msg = str(exc).lower()
            if "not found" in msg or "no such" in msg:
                return _error(
                    404,
                    "mcp_not_found",
                    f"no marketplace MCP server with slug {slug!r}",
                    slug=slug,
                )
            return _error(
                502,
                "marketplace_unreachable",
                f"marketplace detail fetch failed: {exc}",
                slug=slug,
            )
        except Exception as exc:  # noqa: BLE001
            return _error(
                502,
                "marketplace_unreachable",
                f"marketplace detail fetch failed: {exc}",
                slug=slug,
            )
        return _item_to_detail(item)

    # ------------------------------------------------------------------
    # POST /admin/mcp/install
    # ------------------------------------------------------------------

    @r.post("/admin/mcp/install", response_model=McpServerRow)
    async def market_install(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        body: McpInstallBody,
    ) -> McpServerRow | JSONResponse:
        """Download a manifest, merge env, persist the server **disabled**.

        Never connects the server — the operator enables it explicitly
        via the ``/admin/plugins/{name}/enable`` seam afterwards.
        """
        source = _resolve_source(admin_state)
        if source is None:
            return _error(
                503,
                "marketplace_unreachable",
                "the marketplace source is not wired on this gateway",
            )
        adapter = _resolve_adapter(admin_state)
        if adapter is None:
            return _error(
                503,
                "mcp_adapter_disabled",
                "no McpAdapter wired into this gateway",
            )

        version = body.version or "latest"
        try:
            download = await source.download(
                kind="mcp", slug=body.slug, version=version
            )
        except MarketplaceRateLimitedError as exc:
            return _error(
                429,
                "marketplace_rate_limited",
                "marketplace source is rate-limited",
                retry_after_seconds=exc.retry_after_seconds,
            )
        except MarketplaceUnavailableError as exc:
            return _error(
                502,
                "marketplace_unreachable",
                f"marketplace download failed: {exc}",
                slug=body.slug,
            )
        except Exception as exc:  # noqa: BLE001
            return _error(
                502,
                "marketplace_unreachable",
                f"marketplace download failed: {exc}",
                slug=body.slug,
            )

        # The MCP install payload is a JSON manifest = launch spec.
        raw = getattr(download, "content", b"") or b""
        try:
            text = (
                raw.decode("utf-8")
                if isinstance(raw, (bytes, bytearray))
                else str(raw)
            )
            spec = json.loads(text)
        except (ValueError, UnicodeDecodeError) as exc:
            return _error(
                502,
                "manifest_invalid",
                f"downloaded MCP manifest is not valid JSON: {exc}",
                slug=body.slug,
            )
        if not isinstance(spec, dict):
            return _error(
                502,
                "manifest_invalid",
                "downloaded MCP manifest must be a JSON object",
                slug=body.slug,
            )

        # Merge operator-supplied env over the manifest's declared env.
        if body.env:
            merged_env = dict(spec.get("env") or {})
            merged_env.update(
                {str(k): str(v) for k, v in body.env.items()}
            )
            spec["env"] = merged_env

        resolved_version = (
            getattr(download, "version", None) or body.version or version
        )
        try:
            row = await adapter.install(
                spec, source="github", version=resolved_version
            )
        except ValueError as exc:
            return _error(
                422,
                "manifest_invalid",
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
        if row is None:
            return _error(
                503,
                "mcp_store_disabled",
                "no MCP server store wired; install could not be persisted",
                slug=body.slug,
            )
        return _row_from_installed(row)

    # ------------------------------------------------------------------
    # GET /admin/mcp/servers
    # ------------------------------------------------------------------

    @r.get("/admin/mcp/servers", response_model=list[McpServerRow])
    async def list_servers(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> list[McpServerRow] | JSONResponse:
        adapter = _resolve_adapter(admin_state)
        if adapter is None:
            return _error(
                503,
                "mcp_adapter_disabled",
                "no McpAdapter wired into this gateway",
            )
        try:
            rows = adapter.servers()
        except Exception as exc:  # noqa: BLE001
            return _error(
                500,
                "mcp_servers_failed",
                str(exc),
            )
        return [_row_from_installed(row) for row in rows]

    # ------------------------------------------------------------------
    # DELETE /admin/mcp/{name}
    # ------------------------------------------------------------------

    @r.delete("/admin/mcp/{name}")
    async def remove_server(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        name: str = PathParam(..., description="Installed MCP server name."),
    ) -> Any:
        adapter = _resolve_adapter(admin_state)
        if adapter is None:
            return _error(
                503,
                "mcp_adapter_disabled",
                "no McpAdapter wired into this gateway",
            )
        try:
            deleted = await adapter.remove(name)
        except Exception as exc:  # noqa: BLE001
            return _error(
                500,
                "mcp_remove_failed",
                str(exc),
                name=name,
            )
        if not deleted:
            return _error(
                404,
                "mcp_not_found",
                f"no installed MCP server named {name!r}",
                name=name,
            )
        return JSONResponse(
            status_code=200,
            content={"ok": True, "name": name, "removed": True},
        )

    return r


def build_router() -> APIRouter:
    """Alias matching the ``build_router`` convention some bundles mount
    by. Returns the same router as :func:`router`."""
    return router()
