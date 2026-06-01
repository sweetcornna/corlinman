# corlinman-marketplace

The curated **registry repo** the corlinman gateway reads to list and install
extensions. It holds one `index.json` catalog plus the per-item content:
gzip **tarballs** for skills/plugins and `manifest.json` **specs** for MCP
servers.

This repo is content-only. The consuming code lives in the gateway at
`corlinman_server.system.marketplace.github_source.GitHubSource`.

## How the gateway consumes it

`GitHubSource` is configured with a `repo` (`<owner>/<name>`) and a `ref`
(branch/tag/sha, default `main`) and fetches everything from
`raw.githubusercontent.com`:

```
index.json  →  https://raw.githubusercontent.com/<repo>/<ref>/index.json
tarball     →  https://raw.githubusercontent.com/<repo>/<ref>/<tarball>
manifest    →  https://raw.githubusercontent.com/<repo>/<ref>/<manifest>
```

Flow:

1. **Catalog** — `GET index.json`, parse `items[]`, filter by `kind`
   (`skill` / `mcp` / `plugin`). Cached with a short TTL.
2. **Install a skill/plugin** — `GET <tarball>`, then verify the bytes against
   the item's declared `sha256`. A mismatch raises `MarketplaceIntegrityError`
   and the install is refused (defence against a hostile mirror on the
   China-region accelerated path). The verified tarball is handed to the
   hardened extractor, which expects the archive to wrap the source dir as
   `<slug>/...` (so `<slug>/SKILL.md` / `<slug>/manifest.json` land at the
   top level after extraction).
3. **Install an MCP server** — `GET <manifest>`; the manifest is validated as
   JSON and persisted as a server spec. **No `sha256` is required** for MCP
   items (the manifest is the payload, not a tarball).

Every URL passes through the `GithubAccelerator` first, so a China-region
host transparently pulls from a mirror — which is exactly why tarball
`sha256` verification is mandatory.

## Repository layout

```
index.json                         # the catalog (generated; do not hand-edit)
skills/<slug>/SKILL.md             # skill source (yaml frontmatter + body)
plugins/<slug>/manifest.json       # plugin source (+ entry script / README)
mcp/<slug>/manifest.json           # McpServerSpec-shaped doc (no tarball)
dist/skills/<slug>-<version>.tar.gz   # packed skill tarball
dist/plugins/<slug>-<version>.tar.gz  # packed plugin tarball
scripts/build-registry.py          # packs dist/* + regenerates index.json
scripts/validate-index.py          # independent sha256 / manifest re-check
```

Seed items: one skill (`hello-skill`), one plugin (`echo-plugin`), one MCP
server (`echo-mcp`).

## index.json schema

A JSON object: `schema: 1`, `generated_at: <iso>`, `items: [...]`. Each item:

| field            | skill | plugin | mcp | notes                                   |
| ---------------- | :---: | :----: | :-: | --------------------------------------- |
| `kind`           |   ✓   |   ✓    |  ✓  | `"skill"` / `"plugin"` / `"mcp"`        |
| `slug`           |   ✓   |   ✓    |  ✓  | stable id; also the tarball dir prefix  |
| `name`           |   ✓   |   ✓    |  ✓  |                                         |
| `description`    |   ✓   |   ✓    |  ✓  |                                         |
| `emoji`          |  opt  |        |     | short string                            |
| `latest_version` |   ✓   |   ✓    |  ✓  |                                         |
| `versions`       |   ✓   |   ✓    |  ✓  | array                                   |
| `tarball`        |   ✓   |   ✓    |     | repo-relative path under `dist/`        |
| `sha256`         |   ✓   |   ✓    |     | hex digest of the tarball bytes         |
| `manifest`       |       |        |  ✓  | repo-relative path to the MCP spec      |
| `transport`      |       |        |  ✓  | `"stdio"` here                          |
| `requires`       |       |        |  ✓  | `{ "env": [...] }` operator secrets     |
| `updated_at`     |   ✓   |   ✓    |  ✓  | ISO-8601                                |

## Publishing

1. Add or edit content under `skills/`, `plugins/`, or `mcp/`.
   - Skills: a `skills/<slug>/SKILL.md` with YAML frontmatter (`name`,
     `description`, optional `emoji`).
   - Plugins: a `plugins/<slug>/manifest.json` (with a `version`) plus any
     entry script / README.
   - MCP: an `mcp/<slug>/manifest.json` shaped like an `McpServerSpec`
     (`name`, `transport`, `command`, `args`, `env`, `requires.env`).
2. Repack + regenerate the catalog:

   ```sh
   python scripts/build-registry.py
   ```

   This packs `dist/*.tar.gz` **deterministically** (members sorted;
   `tarinfo.mtime=0`, `uid=gid=0`; gzip header `mtime=0`) so re-running
   produces byte-identical tarballs, computes each tarball's `sha256`, and
   rewrites `index.json` with matching hashes.
3. Verify before committing:

   ```sh
   python scripts/build-registry.py --check   # tarballs reproduce + hashes match
   python scripts/validate-index.py           # independent sha256 / manifest check
   ```
4. Commit `index.json`, the source dirs, and the regenerated `dist/` tarballs,
   then push. The gateway picks up changes on its next `index.json` fetch
   (subject to the cache TTL).

> The `dist/` tarballs are committed on purpose: `GitHubSource` downloads them
> directly from `raw.githubusercontent.com`, so they must be present at the
> published `ref`.
