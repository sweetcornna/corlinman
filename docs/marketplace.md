# Marketplace — operator guide

corlinman ships a unified marketplace for three installable extension
kinds — **skills**, **MCP servers**, and **plugins** — served from a
single curated GitHub registry repo, with optional China-region
acceleration. This guide covers configuration, the admin API, and
publishing to the registry.

## Configuration

See the `[marketplace]` + `[marketplace.github_proxy]` blocks in
`docs/config.example.toml`. Key knobs:

| Key | Default | Meaning |
|---|---|---|
| `marketplace.registry_repo` | `sweetcornna/corlinman-marketplace` | The curated GitHub repo holding `index.json` + content |
| `marketplace.registry_ref` | `main` | Branch/tag/sha to read |
| `marketplace.default_source` | `github` | Skills browse source: `github` or `clawhub` |
| `marketplace.clawhub_enabled` | `false` | Allow the legacy clawhub.ai skill source |
| `marketplace.github_token` | — | Optional PAT (never sent through a public proxy) |
| `marketplace.github_proxy.mode` | `auto` | `off` / `auto` / `on` |
| `marketplace.github_proxy.preset` | `ghproxy` | `ghproxy` / `jsdelivr` / `mirror` / `custom` |
| `marketplace.github_proxy.base` | `https://ghproxy.com/` | Prefix base for ghproxy/custom |
| `marketplace.github_proxy.mirror_host` | — | Host for `preset = "mirror"` |
| `marketplace.github_proxy.assume_region` | — | Force the `auto` decision: `cn` / `global` |

**China acceleration.** With `mode = "auto"` the gateway enables the
mirror when it detects a China signal: `assume_region = "cn"`, the
`CORLINMAN_REGION=cn` env, or a `TZ` like `Asia/Shanghai`. `ghproxy`
prefixes every GitHub URL (`https://ghproxy.com/https://raw...`);
`jsdelivr` serves raw repo content via the jsDelivr CDN; `mirror`
host-swaps to a self-hosted reverse proxy. The GitHub token is **only**
attached when talking to GitHub directly or to a `mirror` host — never
through a public proxy.

## Admin API

All routes are behind the admin auth gate.

### Skills
The existing `/admin/skills` + `/admin/skills/hub/*` routes are unchanged;
under `default_source = "github"` they are now served from the GitHub
registry. Installs land in the active profile's `skills/` dir and
**auto-activate** within ~30 s (no restart).

### MCP servers (staged install → explicit enable)
| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/admin/mcp/market` | — | Browse `kind=mcp` (offline-collapse) |
| GET | `/admin/mcp/market/{slug}` | — | Detail incl. `requires_env` |
| POST | `/admin/mcp/install` | `{slug, version?, env?}` | Persists **disabled**; prompts required env |
| GET | `/admin/mcp/servers` | — | Installed + live status (`ready`/`error`/`pending`, tool count) |
| DELETE | `/admin/mcp/{name}` | — | Teardown + delete |
| POST | `/admin/plugins/{name}/enable` | — | **Hot-connects** the live peer |
| POST | `/admin/plugins/{name}/disable` | — | Tears the live peer down |
| POST | `/admin/plugins/{name}/restart` | — | Reconnect |

Enable/disable/restart are served by the existing `/admin/plugins/{name}/*`
seam (now wired to the `mcp_adapter`). Installed MCP servers persist in
`<data_dir>/mcp_servers.sqlite` and reconnect on boot if enabled.

### Plugins (staged install → enable)
| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/admin/plugins/market` | — | Browse `kind=plugin` |
| GET | `/admin/plugins/market/{slug}` | — | Detail |
| POST | `/admin/plugins/market/install` | `{slug, version?}` | Extracts to `<data_dir>/plugins/<slug>`, registers **disabled** |
| POST | `/admin/plugins/market/{slug}/enable` | — | `applies: "now"` if a reload hook is wired, else `"next_restart"` |
| POST | `/admin/plugins/market/{slug}/disable` | — | |
| DELETE | `/admin/plugins/market/{slug}` | — | Removes the bundle + index row |

Plugins persist in `<data_dir>/plugins.sqlite`. Installs run untrusted
code only after an explicit enable.

## Publishing to the registry

The registry scaffold lives in `marketplace/`:

```
marketplace/
  index.json                       # catalog
  skills/<slug>/SKILL.md
  plugins/<slug>/{manifest.json,...}
  mcp/<slug>/manifest.json         # an McpServerSpec-shaped doc + requires.env
  dist/skills/<slug>-<ver>.tar.gz  # built bundles (committed)
  dist/plugins/<slug>-<ver>.tar.gz
  scripts/build-registry.py        # packs dist/*.tar.gz + regenerates index.json
  scripts/validate-index.py        # re-checks every sha256
```

To publish: add/edit a source dir, run `python marketplace/scripts/build-registry.py`
to repack the tarballs + regenerate `index.json` (sha256 stays in sync),
then commit and push to the `corlinman-marketplace` GitHub repo. Skills
and plugins are tarballs; MCP items are a `manifest.json` spec only.

**Integrity:** every GitHub tarball carries a mandatory `sha256` the
gateway verifies after download — a mismatch (e.g. a tampered mirror)
aborts the install.
