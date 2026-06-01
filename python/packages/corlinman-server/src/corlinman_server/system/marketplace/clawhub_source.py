"""``corlinman_server.system.marketplace.clawhub_source`` — clawhub adapter.

Wraps the legacy :class:`~corlinman_server.system.skill_hub.client.ClawHubClient`
as a :class:`MarketplaceSource` so the existing clawhub.ai catalog stays
available behind the new abstraction when an operator toggles
``clawhub_enabled``. clawhub serves **skills only** — list/search/detail/
download for ``mcp``/``plugin`` return empty / raise, by contract.
"""

from __future__ import annotations

from corlinman_server.system.marketplace.source import (
    MarketplaceDownload,
    MarketplaceError,
    MarketplaceItem,
    MarketplaceKind,
    MarketplaceRateLimitedError,
    MarketplaceUnavailableError,
)
from corlinman_server.system.skill_hub.client import (
    ClawHubClient,
    HubRateLimitedError,
    HubSkillSummary,
    HubUnavailableError,
)

__all__ = ["ClawHubSource"]


class ClawHubSource:
    """Adapts :class:`ClawHubClient` to the ``MarketplaceSource`` Protocol."""

    __slots__ = ("_client",)

    def __init__(self, client: ClawHubClient | None = None) -> None:
        self._client = client or ClawHubClient()

    @property
    def name(self) -> str:
        return "clawhub"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_items(
        self,
        kind: MarketplaceKind,
        *,
        sort: str = "trending",
        cursor: str | None = None,
        limit: int = 25,
    ) -> tuple[list[MarketplaceItem], str | None]:
        if kind != "skill":
            return [], None
        try:
            rows, next_cursor = await self._client.list_skills(
                sort=sort, cursor=cursor, limit=limit
            )
        except HubRateLimitedError as exc:
            raise MarketplaceRateLimitedError(exc.retry_after_seconds) from exc
        except HubUnavailableError as exc:
            raise MarketplaceUnavailableError(str(exc)) from exc
        return [_summary_to_item(r) for r in rows], next_cursor

    async def search(
        self, kind: MarketplaceKind, q: str, *, limit: int = 25
    ) -> list[MarketplaceItem]:
        if kind != "skill":
            return []
        try:
            rows = await self._client.search(q, limit=limit)
        except HubRateLimitedError as exc:
            raise MarketplaceRateLimitedError(exc.retry_after_seconds) from exc
        except HubUnavailableError as exc:
            raise MarketplaceUnavailableError(str(exc)) from exc
        return [_summary_to_item(r) for r in rows]

    async def detail(self, kind: MarketplaceKind, slug: str) -> MarketplaceItem:
        if kind != "skill":
            raise MarketplaceError(f"clawhub does not serve {kind} items")
        try:
            d = await self._client.get_skill(slug)
        except HubRateLimitedError as exc:
            raise MarketplaceRateLimitedError(exc.retry_after_seconds) from exc
        except HubUnavailableError as exc:
            raise MarketplaceUnavailableError(str(exc)) from exc
        return MarketplaceItem(
            kind="skill",
            slug=d.slug,
            name=d.name,
            description=d.description,
            latest_version=d.latest_version,
            emoji=d.emoji,
            versions=tuple(d.versions),
            stars=d.stars,
            downloads=d.downloads,
            updated_at=d.updated_at,
            homepage=d.homepage,
            readme_excerpt=d.readme_excerpt,
            scan_summary=d.scan_summary,
            content_ref=d.slug,  # clawhub downloads by slug
        )

    async def download(
        self, kind: MarketplaceKind, slug: str, version: str = "latest"
    ) -> MarketplaceDownload:
        if kind != "skill":
            raise MarketplaceError(f"clawhub does not serve {kind} downloads")
        try:
            dl = await self._client.download(slug, version=version)
        except HubRateLimitedError as exc:
            raise MarketplaceRateLimitedError(exc.retry_after_seconds) from exc
        except HubUnavailableError as exc:
            raise MarketplaceUnavailableError(str(exc)) from exc
        return MarketplaceDownload(
            kind="skill",
            slug=slug,
            version=version,
            content=dl.content,
            media="tarball",
            sha256=dl.content_hash,
        )


def _summary_to_item(s: HubSkillSummary) -> MarketplaceItem:
    return MarketplaceItem(
        kind="skill",
        slug=s.slug,
        name=s.name,
        description=s.description,
        latest_version=s.latest_version,
        emoji=s.emoji,
        stars=s.stars,
        downloads=s.downloads,
        updated_at=s.updated_at,
        content_ref=s.slug,
    )
