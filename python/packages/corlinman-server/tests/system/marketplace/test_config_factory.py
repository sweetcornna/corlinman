"""Tests for the marketplace ``config`` + ``factory`` modules.

* :func:`load_marketplace_config` defaults (no ``[marketplace]`` section)
  and parsing of the ``[marketplace]`` + ``[marketplace.github_proxy]``
  sections.
* :func:`build_source` returns a :class:`GitHubSource` by default and a
  :class:`ClawHubSource` when ``default_source="clawhub"`` **and**
  ``clawhub_enabled=True``.
"""

from __future__ import annotations

import pytest
from corlinman_server.system.marketplace.clawhub_source import ClawHubSource
from corlinman_server.system.marketplace.config import (
    MarketplaceConfig,
    load_marketplace_config,
)
from corlinman_server.system.marketplace.factory import build_source
from corlinman_server.system.marketplace.github_source import GitHubSource

_DEFAULT_REPO = "sweetcornna/corlinman-marketplace"


# ---------------------------------------------------------------------------
# load_marketplace_config — defaults
# ---------------------------------------------------------------------------


def test_defaults_when_no_marketplace_section() -> None:
    cfg = load_marketplace_config({})
    assert cfg.registry_repo == _DEFAULT_REPO
    assert cfg.registry_ref == "main"
    assert cfg.default_source == "github"
    assert cfg.clawhub_enabled is False
    assert cfg.github_token is None
    # accel defaults: off / ghproxy.
    assert cfg.accel.mode == "off"
    assert cfg.accel.preset == "ghproxy"
    assert cfg.accel.base == "https://ghproxy.com/"


def test_defaults_when_section_is_not_a_dict() -> None:
    # A malformed section degrades silently to defaults (never raises).
    cfg = load_marketplace_config({"marketplace": "nonsense"})
    assert cfg.registry_repo == _DEFAULT_REPO
    assert cfg.default_source == "github"


def test_returns_marketplace_config_type() -> None:
    cfg = load_marketplace_config({})
    assert isinstance(cfg, MarketplaceConfig)


# ---------------------------------------------------------------------------
# load_marketplace_config — parsing [marketplace]
# ---------------------------------------------------------------------------


def test_parses_marketplace_section(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep env from leaking a real token into the parse.
    monkeypatch.delenv("CORLINMAN_GITHUB_TOKEN", raising=False)
    cfg = load_marketplace_config(
        {
            "marketplace": {
                "registry_repo": "acme/registry",
                "registry_ref": "stable",
                "default_source": "CLAWHUB",  # case-folded
                "clawhub_enabled": True,
                "github_token": "  tok-123  ",
            }
        }
    )
    assert cfg.registry_repo == "acme/registry"
    assert cfg.registry_ref == "stable"
    assert cfg.default_source == "clawhub"
    assert cfg.clawhub_enabled is True
    assert cfg.github_token == "tok-123"


def test_clawhub_enabled_accepts_string_truthy() -> None:
    cfg = load_marketplace_config(
        {"marketplace": {"clawhub_enabled": "yes"}}
    )
    assert cfg.clawhub_enabled is True


def test_github_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_GITHUB_TOKEN", "env-token")
    cfg = load_marketplace_config({"marketplace": {}})
    assert cfg.github_token == "env-token"


def test_github_token_falls_back_to_update_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_GITHUB_TOKEN", raising=False)
    cfg = load_marketplace_config(
        {
            "marketplace": {},
            "system": {"update_check": {"github_token": "update-tok"}},
        }
    )
    assert cfg.github_token == "update-tok"


# ---------------------------------------------------------------------------
# load_marketplace_config — parsing [marketplace.github_proxy]
# ---------------------------------------------------------------------------


def test_parses_github_proxy_section() -> None:
    cfg = load_marketplace_config(
        {
            "marketplace": {
                "github_proxy": {
                    "mode": "AUTO",
                    "preset": "JSDELIVR",
                    "base": "https://my.proxy/",
                    "mirror_host": "gh.mycorp.cn",
                    "assume_region": "CN",
                }
            }
        }
    )
    assert cfg.accel.mode == "auto"
    assert cfg.accel.preset == "jsdelivr"
    assert cfg.accel.base == "https://my.proxy/"
    assert cfg.accel.mirror_host == "gh.mycorp.cn"
    assert cfg.accel.assume_region == "cn"


def test_github_proxy_missing_keys_use_defaults() -> None:
    cfg = load_marketplace_config(
        {"marketplace": {"github_proxy": {"mode": "on"}}}
    )
    assert cfg.accel.mode == "on"
    assert cfg.accel.preset == "ghproxy"
    assert cfg.accel.base == "https://ghproxy.com/"


# ---------------------------------------------------------------------------
# build_source
# ---------------------------------------------------------------------------


async def test_build_source_defaults_to_github() -> None:
    src = build_source(MarketplaceConfig())
    try:
        assert isinstance(src, GitHubSource)
        assert src.name == "github"
    finally:
        await src.aclose()


async def test_build_source_clawhub_when_enabled() -> None:
    cfg = MarketplaceConfig(default_source="clawhub", clawhub_enabled=True)
    src = build_source(cfg)
    try:
        assert isinstance(src, ClawHubSource)
        assert src.name == "clawhub"
    finally:
        await src.aclose()


async def test_build_source_clawhub_requires_enabled_flag() -> None:
    # default_source=clawhub but clawhub_enabled=False → still GitHub.
    cfg = MarketplaceConfig(default_source="clawhub", clawhub_enabled=False)
    src = build_source(cfg)
    try:
        assert isinstance(src, GitHubSource)
    finally:
        await src.aclose()
