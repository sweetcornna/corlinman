"""``corlinman_server.system.marketplace.skill_adapter`` — skills facade.

Presents a :class:`MarketplaceSource` to the existing
``/admin/skills/hub/*`` routes + the skill installer with the **exact
method surface of** :class:`~corlinman_server.system.skill_hub.client.ClawHubClient`
(``search`` / ``list_skills`` / ``get_skill`` / ``download`` / ``aclose`` /
``base_url``). Wiring this object in place of a ``ClawHubClient`` makes the
skills marketplace GitHub-backed with **zero changes to skills.py or the
installer** — the route layer keeps proxying the same calls, and the
returned shapes are the same ``HubSkillSummary`` / ``HubSkillDetail`` /
``HubDownload`` dataclasses it already renders.

``Marketplace*`` errors are translated back into the ``Hub*`` family so
the route layer's offline / rate-limit envelopes keep working unchanged.
"""

from __future__ import annotations

from corlinman_server.system.marketplace.source import (
    MarketplaceItem,
    MarketplaceRateLimitedError,
    MarketplaceSource,
    MarketplaceUnavailableError,
)
from corlinman_server.system.skill_hub.client import (
    HubDownload,
    HubRateLimitedError,
    HubSkillDetail,
    HubSkillSummary,
    HubUnavailableError,
)

__all__ = ["SkillHubSourceAdapter"]


class SkillHubSourceAdapter:
    """Adapts a :class:`MarketplaceSource` to the ``ClawHubClient`` surface."""

    __slots__ = ("_source",)

    def __init__(self, source: MarketplaceSource) -> None:
        self._source = source

    @property
    def base_url(self) -> str:
        # The skills routes only surface this for diagnostics.
        return getattr(self._source, "name", "marketplace")

    async def aclose(self) -> None:
        await self._source.aclose()

    async def search(self, q: str, *, limit: int = 25) -> list[HubSkillSummary]:
        try:
            rows = await self._source.search("skill", q, limit=limit)
        except (MarketplaceUnavailableError, MarketplaceRateLimitedError) as exc:
            raise _translate(exc) from exc
        return [_to_summary(i) for i in rows]

    async def list_skills(
        self,
        *,
        sort: str = "trending",
        cursor: str | None = None,
        limit: int = 25,
    ) -> tuple[list[HubSkillSummary], str | None]:
        try:
            rows, next_cursor = await self._source.list_items(
                "skill", sort=sort, cursor=cursor, limit=limit
            )
        except (MarketplaceUnavailableError, MarketplaceRateLimitedError) as exc:
            raise _translate(exc) from exc
        return [_to_summary(i) for i in rows], next_cursor

    async def get_skill(self, slug: str) -> HubSkillDetail:
        try:
            item = await self._source.detail("skill", slug)
        except (MarketplaceUnavailableError, MarketplaceRateLimitedError) as exc:
            raise _translate(exc) from exc
        return _to_detail(item)

    async def download(self, slug: str, version: str = "latest") -> HubDownload:
        try:
            dl = await self._source.download("skill", slug, version=version)
        except (MarketplaceUnavailableError, MarketplaceRateLimitedError) as exc:
            raise _translate(exc) from exc
        return HubDownload(
            content=dl.content,
            content_hash=dl.sha256,
            slug=slug,
            version=dl.version or version,
        )


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, MarketplaceRateLimitedError):
        return HubRateLimitedError(exc.retry_after_seconds)
    return HubUnavailableError(str(exc))


def _to_summary(i: MarketplaceItem) -> HubSkillSummary:
    return HubSkillSummary(
        slug=i.slug,
        name=i.name,
        description=i.description,
        emoji=i.emoji,
        stars=i.stars,
        downloads=i.downloads,
        latest_version=i.latest_version,
        updated_at=i.updated_at,
    )


def _to_detail(i: MarketplaceItem) -> HubSkillDetail:
    return HubSkillDetail(
        slug=i.slug,
        name=i.name,
        description=i.description,
        emoji=i.emoji,
        stars=i.stars,
        downloads=i.downloads,
        latest_version=i.latest_version,
        updated_at=i.updated_at,
        homepage=i.homepage,
        versions=list(i.versions),
        scan_summary=i.scan_summary,
        readme_excerpt=i.readme_excerpt,
    )
