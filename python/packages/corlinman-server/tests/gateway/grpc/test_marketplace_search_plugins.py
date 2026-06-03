from __future__ import annotations

import json
from pathlib import Path

import pytest
from corlinman_providers.plugins.discovery import Origin
from corlinman_server.gateway.grpc.plugin_invoker import build_registry_invoker

MARKETPLACE_PLUGINS = (
    "any-search",
    "zhihu-search",
    "tinyfish-browser",
    "v-search",
)


def _copy_plugin_dir(src: Path, dst_root: Path) -> Path:
    dst = dst_root / src.name
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        if child.is_file():
            (dst / child.name).write_bytes(child.read_bytes())
    return dst


def _registry_from_marketplace_plugin(tmp_path: Path, slug: str):
    from corlinman_providers.plugins import PluginRegistry
    from corlinman_providers.plugins.discovery import SearchRoot

    src = Path("marketplace/plugins") / slug
    _copy_plugin_dir(src, tmp_path)
    return PluginRegistry.from_roots([SearchRoot(path=tmp_path, origin=Origin.CONFIG)])


def test_marketplace_search_plugins_register_tools(tmp_path: Path) -> None:
    expected = {
        "any-search": [
            "any_search_search",
            "any_search_list_domains",
            "any_search_batch_search",
            "any_search_extract",
        ],
        "zhihu-search": ["zhihu_site_search", "zhihu_global_search"],
        "tinyfish-browser": ["tinyfish_search", "tinyfish_fetch"],
        "v-search": ["v_search_research"],
    }

    for slug, tool_names in expected.items():
        registry = _registry_from_marketplace_plugin(tmp_path / slug, slug)
        entry = registry.get(slug)
        assert entry is not None, slug
        assert [tool.name for tool in entry.manifest.capabilities.tools] == tool_names


@pytest.mark.asyncio
async def test_v_search_missing_dependencies_returns_clear_error(tmp_path: Path) -> None:
    registry = _registry_from_marketplace_plugin(tmp_path, "v-search")
    invoker = build_registry_invoker(registry)
    result = await invoker(
        "v-search",
        "v_search_research",
        json.dumps({"SearchTopic": "AI", "Keywords": "agents"}).encode("utf-8"),
    )
    assert result.is_error is True
    body = json.loads(result.content)
    assert body["error"] == "plugin_error"
    assert "npm install" in body["message"]


@pytest.mark.asyncio
async def test_tinyfish_browser_requires_api_key(tmp_path: Path) -> None:
    registry = _registry_from_marketplace_plugin(tmp_path, "tinyfish-browser")
    invoker = build_registry_invoker(registry)
    result = await invoker(
        "tinyfish-browser",
        "tinyfish_search",
        json.dumps({"query": "AI agent tools"}).encode("utf-8"),
    )
    assert result.is_error is True
    body = json.loads(result.content)
    assert body["error"] == "plugin_error"
    assert "TINYFISH_API_KEY" in body["message"]


@pytest.mark.asyncio
async def test_zhihu_search_requires_secret(tmp_path: Path) -> None:
    registry = _registry_from_marketplace_plugin(tmp_path, "zhihu-search")
    invoker = build_registry_invoker(registry)
    result = await invoker(
        "zhihu-search",
        "zhihu_site_search",
        json.dumps({"query": "AI Agent 应用实践"}).encode("utf-8"),
    )
    assert result.is_error is True
    body = json.loads(result.content)
    assert body["error"] == "plugin_error"
    assert "ZHIHU_ACCESS_SECRET" in body["message"]
