# PLAN: Skills / MCP / Plugin Marketplaces (GitHub-backed, hot-plug) + China acceleration

Status: in progress (v1.16.0 target). Tracks the unified extension
marketplace for **skills**, **MCP servers**, and **plugins**.

## Why

corlinman already ships a clawhub.ai-backed *skills* hub. This work
generalizes that into one **source-agnostic marketplace** serving three
kinds, hosts the catalog + content in a **single curated GitHub registry
repo**, makes MCP/plugin installs **hot-pluggable** at runtime, and adds a
**GitHub reverse-proxy accelerator** so China-region hosts get fast,
reliable downloads.

## Design decisions

1. **Single curated registry repo** (`sweetcornna/corlinman-marketplace`):
   `index.json` + per-item content (tarballs for skills/plugins,
   `manifest.json` specs for MCP).
2. **GitHub is the default source**; the legacy clawhub skill hub stays
   behind `clawhub_enabled` (generalized, not deleted).
3. **China acceleration** = configurable mirror with presets + auto-detect
   (`off`/`auto`/`on`), applied centrally to every GitHub fetch.
4. **Staged install, explicit enable** for code-executing items (MCP
   stdio = arbitrary commands, plugins = code). Skills (no code exec)
   auto-activate via the agent's 30 s registry refresh.

## Architecture

```
GitHub registry repo  →  GithubAccelerator (off|auto|on, ghproxy/jsdelivr/mirror/custom)
   index.json + dist/*.tar.gz + mcp/<slug>/manifest.json
        │
   MarketplaceSource (Protocol)
   ├─ GitHubSource   (default; index.json + raw fetch + sha256 verify)
   └─ ClawHubSource  (optional toggle; wraps existing ClawHubClient — skills only)
        │
   Skills  → reuse skill_hub installer → profile skills dir  (auto-activate)
   MCP     → persist spec (disabled) → McpClientManager.add_server on ENABLE (hot)
   Plugins → reuse _safe_extract → <data_dir>/plugins/<slug> → PluginRegistry on ENABLE
```

Two install shapes:
- **Skills & Plugins** = tarball bundles → hardened extract pipeline
  (`skill_hub/installer.py` `_safe_extract`: traversal/symlink/size guards
  + atomic rename + `.openclaw-meta.json` sidecar).
- **MCP** = a connection spec/manifest → validate + persist → connect on
  enable.

## Code map

`corlinman_server.system.marketplace/`
- `source.py` — `MarketplaceSource` Protocol + `MarketplaceItem` /
  `MarketplaceDownload` DTOs + typed errors
  (`MarketplaceUnavailableError` / `MarketplaceRateLimitedError` /
  `MarketplaceIntegrityError`).
- `accel.py` — `GithubAccelerator` + `AccelSettings` (pure URL rewriter;
  presets ghproxy/jsdelivr/mirror/custom; `is_trusted_host` gates token
  leakage through public proxies).
- `github_source.py` — `GitHubSource` (index.json TTL cache, raw-URL
  resolution through the accelerator, **mandatory sha256** on downloads).
- `clawhub_source.py` — `ClawHubSource` adapter over the existing
  `ClawHubClient`.
- `config.py` / `factory.py` — `[marketplace]` parsing + source builder.
- `mcp_store.py` / `plugin_store.py` — SQLite persistence
  (`<data_dir>/mcp_servers.sqlite`, `<data_dir>/plugins.sqlite`).
- `plugin_installer.py` — tarball → `<data_dir>/plugins/<slug>` (reuses
  the skill extractor).

`gateway/routes_admin_b/`
- `skills.py` — retargeted to resolve a `MarketplaceSource` from config
  (keeps `/admin/skills/hub/*` shapes + SSE).
- `mcp_adapter.py` — `McpAdapter` wiring `state.extras["mcp_adapter"]`
  (lights up the already-coded `/admin/plugins/{name}/{enable,disable,restart}`).
- `mcp_market.py` — `/admin/mcp/*` browse/install/list/delete.
- `plugin_market.py` — `/admin/plugins/market/*` browse/install/enable/
  disable/uninstall.

`corlinman-mcp-server` `client_manager.py` — single-server hot-plug
primitives `add_server`/`remove_server`/`restart_one`/`enable_one`/
`disable_one` (reuse `_bring_up`).

`marketplace/` (repo root) — the registry scaffold (index.json + seed
items + `scripts/build-registry.py`) that publishes to the GitHub repo.

## Security

- Staged MCP/plugin installs: fetched code is inert until an explicit
  admin Enable. MCP `requires.env` secrets are prompted at enable, never
  committed to the registry.
- Mandatory sha256 on GitHub tarballs (a mirror could be hostile) + the
  existing 25 MiB / 10 MiB size caps + path-traversal/symlink guards.
- The accelerator only rewrites GitHub hosts and never sends an auth
  token through a third-party proxy (`is_trusted_host`).
- All routes behind the existing `require_admin` gate.

## Config

See the `[marketplace]` + `[marketplace.github_proxy]` blocks in
`docs/config.example.toml`.

## Verification

- Unit: accelerator rewrite table; `GitHubSource` against
  `httpx.MockTransport`; sha256 mismatch rejected; MCP manager hot-plug
  against a fake peer; plugin installer reuses the skill safety fixtures.
- Integration: boot against a fixture registry; install a skill
  (auto-activates), install + enable an MCP server (tools appear, then
  disappear on disable), all without restart.
- Regression: `pytest`, `ruff`, `mypy`, `import-linter` green; existing
  `/admin/skills/hub/*` tests pass under the generalized source.
