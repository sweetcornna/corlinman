"""Module-level support for :mod:`...marketplace.mcp_market`.

Extracted verbatim from ``mcp_market.py`` (wire models + helpers) as a
pure behaviour-preserving internal split. This module holds the
module-level mass that ``mcp_market.router()`` and its handlers depend
on.

MUST NOT import the route module (``mcp_market``) — that would create an
import cycle. The re-import flows one way: ``mcp_market`` imports from
here.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import AdminState
from corlinman_server.system.marketplace.source import MarketplaceItem

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


class McpReconfigureBody(BaseModel):
    """Body for ``PUT /admin/mcp/{name}`` — edit an installed/config
    server's launch spec without a delete + reinstall.

    Every field is optional: an absent field leaves that part of the
    spec unchanged, while a present ``env`` / ``headers`` *replaces* the
    stored map wholesale (so an operator can drop a secret). The
    ``enabled`` flag is deliberately **not** editable here — toggling
    stays on the ``/admin/plugins/{name}/{enable,disable}`` seam.
    """

    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    version: str | None = None


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
