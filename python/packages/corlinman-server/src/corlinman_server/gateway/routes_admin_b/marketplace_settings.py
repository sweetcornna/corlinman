"""``/admin/marketplace/settings`` — marketplace + accelerator surface.

Read-only view of the resolved ``[marketplace]`` config (the source
toggle + the GitHub accelerator state) plus a live "Test acceleration"
probe the UI's settings card calls to compare a direct GitHub fetch
against the accelerated one. Editing the values themselves goes through
the existing ``/admin/config`` TOML editor — this module never writes.
"""

from __future__ import annotations

import time

import httpx
import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.state import (
    config_snapshot,
    require_admin,
)
from corlinman_server.system.marketplace.accel import GithubAccelerator
from corlinman_server.system.marketplace.config import load_marketplace_config

logger = structlog.get_logger(__name__)

__all__ = ["build_router", "router"]

_PROBE_TIMEOUT_S = 6.0


class AccelView(BaseModel):
    mode: str
    preset: str
    base: str
    mirror_host: str
    assume_region: str
    enabled: bool


class MarketplaceSettingsOut(BaseModel):
    registry_repo: str
    registry_ref: str
    default_source: str
    clawhub_enabled: bool
    github_token_set: bool
    index_url: str
    accelerated_index_url: str
    accel: AccelView


class ProbeLeg(BaseModel):
    url: str
    ok: bool
    status: int | None = None
    ms: int | None = None
    error: str | None = None


class AccelTestOut(BaseModel):
    enabled: bool
    direct: ProbeLeg
    accelerated: ProbeLeg


def _raw_index_url(repo: str, ref: str) -> str:
    return f"https://raw.githubusercontent.com/{repo.strip('/')}/{ref}/index.json"


async def _probe(url: str) -> ProbeLeg:
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=_PROBE_TIMEOUT_S, follow_redirects=True
        ) as client:
            # GET (not HEAD) — some mirrors don't implement HEAD; we only
            # read the status, the small body is discarded.
            resp = await client.get(url)
        ms = int((time.monotonic() - started) * 1000)
        return ProbeLeg(url=url, ok=resp.is_success, status=resp.status_code, ms=ms)
    except httpx.HTTPError as exc:
        ms = int((time.monotonic() - started) * 1000)
        return ProbeLeg(url=url, ok=False, ms=ms, error=str(exc))


def router() -> APIRouter:
    r = APIRouter(
        dependencies=[Depends(require_admin)], tags=["admin", "marketplace"]
    )

    @r.get("/admin/marketplace/settings", response_model=MarketplaceSettingsOut)
    async def get_settings() -> MarketplaceSettingsOut:
        cfg = load_marketplace_config(config_snapshot())
        accel = GithubAccelerator(cfg.accel)
        index_url = _raw_index_url(cfg.registry_repo, cfg.registry_ref)
        return MarketplaceSettingsOut(
            registry_repo=cfg.registry_repo,
            registry_ref=cfg.registry_ref,
            default_source=cfg.default_source,
            clawhub_enabled=cfg.clawhub_enabled,
            github_token_set=bool(cfg.github_token),
            index_url=index_url,
            accelerated_index_url=accel.accelerate(index_url),
            accel=AccelView(
                mode=cfg.accel.mode,
                preset=cfg.accel.preset,
                base=cfg.accel.base,
                mirror_host=cfg.accel.mirror_host,
                assume_region=cfg.accel.assume_region,
                enabled=accel.enabled,
            ),
        )

    @r.post("/admin/marketplace/accel/test", response_model=AccelTestOut)
    async def test_accel() -> AccelTestOut:
        cfg = load_marketplace_config(config_snapshot())
        accel = GithubAccelerator(cfg.accel)
        index_url = _raw_index_url(cfg.registry_repo, cfg.registry_ref)
        accelerated = accel.accelerate(index_url)
        direct_leg = await _probe(index_url)
        # Skip a duplicate probe when acceleration is a no-op.
        if accelerated == index_url:
            accel_leg = ProbeLeg(
                url=accelerated,
                ok=direct_leg.ok,
                status=direct_leg.status,
                ms=direct_leg.ms,
                error="acceleration disabled (same as direct)",
            )
        else:
            accel_leg = await _probe(accelerated)
        return AccelTestOut(
            enabled=accel.enabled, direct=direct_leg, accelerated=accel_leg
        )

    return r


def build_router() -> APIRouter:
    """Alias matching the sibling modules' factory name."""
    return router()
