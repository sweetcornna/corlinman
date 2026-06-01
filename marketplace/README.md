# corlinman marketplace

The curated registry that powers the **Skills / MCP / Plugins** marketplace
inside corlinman. A running corlinman gateway reads this repo's `index.json`
and per-item content directly from GitHub (raw), so a merged PR here is live
for every user the moment they refresh the Marketplace tab.

```
raw.githubusercontent.com/sweetcornna/corlinman-marketplace/main/index.json
        │
        ├─ skills/<slug>/SKILL.md            → packed into dist/skills/<slug>-<ver>.tar.gz
        ├─ plugins/<slug>/...                → packed into dist/plugins/<slug>-<ver>.tar.gz
        └─ mcp/<slug>/manifest.json          → fetched verbatim (a launch spec)
```

- **Skills** install into the active profile and **auto-activate** (no restart).
- **MCP servers** and **plugins** install *staged* (inert) and require an
  explicit **Enable** before any code runs — see [security](#security--review).

---

## Repository layout

```
index.json                         # the catalog (generated — do not hand-edit)
skills/<slug>/SKILL.md             # a skill (YAML frontmatter + markdown body)
plugins/<slug>/manifest.json       # plugin index metadata (name/version/description)
plugins/<slug>/plugin-manifest.toml# plugin runtime manifest (discovered + hot-loaded)
plugins/<slug>/<entry>             # plugin entry script(s)
mcp/<slug>/manifest.json           # an McpServerSpec launch spec
dist/{skills,plugins}/*.tar.gz     # generated tarballs (committed)
scripts/build-registry.py          # repacks dist/* + regenerates index.json
scripts/validate-index.py          # re-checks every declared sha256 + manifests
scripts/seed.py                    # regenerates the curated seed content
ATTRIBUTION.md                     # upstream credits + licenses
```

`index.json`, `dist/*.tar.gz`, and the sha256 hashes are **generated**. Never
edit them by hand — run `python scripts/build-registry.py` and commit the
result.

---

## Contributing — submit a Skill, MCP server, or Plugin

> One item per PR. Use a lowercase, kebab-case `<slug>`. Keep it genuinely
> useful and self-contained.

### 1. Add a **Skill**

Create `skills/<slug>/SKILL.md`:

```markdown
---
name: my-skill
description: One clear sentence on WHAT it does and WHEN to use it (this is what triggers the skill — be specific and a little "pushy").
emoji: ✨            # optional
---

Your instructions in markdown. Keep it focused (< ~500 lines). Prefer
imperative voice, explain the "why", and give concrete examples.
```

- Bundle small text resources alongside `SKILL.md` if needed (no large binaries).
- If adapted from elsewhere, add a `Source: <url> (<license>)` footer and an
  entry in `ATTRIBUTION.md`. Only submit content you have the right to share.

### 2. Add an **MCP server**

Create `mcp/<slug>/manifest.json` — an `McpServerSpec`-shaped launch spec:

```json
{
  "name": "my-server",
  "display_name": "My Server",
  "description": "What it does, one line.",
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@scope/my-mcp-server", "<REQUIRED_ARG>"],
  "env": {},
  "requires": { "env": ["MY_API_KEY"] },
  "homepage": "https://github.com/you/my-mcp-server",
  "license": "MIT",
  "category": "dev",
  "setup": ["Requires Node.js.", "Provide MY_API_KEY at enable time."]
}
```

- `transport`: `stdio` (a `command` + `args` child process) or `ws`/`http`
  (set `url` + optional `headers` instead of `command`).
- Put **secret names** (API keys, tokens) in `requires.env` — the install UI
  prompts the operator for them at enable time; never hardcode secrets here.
- Mark required positional args with a clear `<PLACEHOLDER>`.
- **Verify** the package name actually resolves (`npx -y <pkg>` / `uvx <pkg>`).

### 3. Add a **Plugin**

A corlinman plugin is a directory bundle. Create three files under
`plugins/<slug>/`:

`manifest.json` (index metadata):
```json
{ "name": "my-plugin", "version": "0.1.0", "description": "...", "entry": "main.py", "requires": { "env": [] } }
```

`plugin-manifest.toml` (the runtime manifest the gateway discovers + hot-loads):
```toml
manifest_version = 1
name = "my-plugin"
version = "0.1.0"
description = "..."
plugin_type = "sync"          # sync | async | service | mcp

[entry_point]
command = "python3"
args = ["main.py"]

[communication]
timeout_ms = 5000

[[capabilities.tools]]
name = "my_tool"
description = "What the tool does."
parameters = { type = "object", properties = { text = { type = "string" } } }
```

Plus your entry script(s) (e.g. `main.py`). Keep dependencies minimal —
prefer the language's standard library so the plugin runs anywhere.

### 4. Build, validate, open the PR

```bash
python scripts/build-registry.py            # repacks dist/* + regenerates index.json
python scripts/build-registry.py --check    # confirms sha256 reproducibility
python scripts/validate-index.py            # re-checks every hash + manifest
```

Then commit **your source files + the regenerated `index.json` + the new
`dist/*.tar.gz`** and open a PR. PR checklist:

- [ ] One item, kebab-case slug, clear `description`.
- [ ] `build-registry.py` run; `index.json` + `dist/` committed and in sync.
- [ ] `validate-index.py` passes.
- [ ] MCP: package name verified; secrets only in `requires.env`.
- [ ] Plugin: `plugin-manifest.toml` present; minimal deps; entry script included.
- [ ] License/attribution noted if adapted from elsewhere.
- [ ] No secrets, no obfuscated code, no network calls at import time.

---

## Security & review

corlinman installs MCP servers and plugins **staged** — fetched content is
inert until an operator explicitly **Enables** it. Still, this is a curated
registry and PRs are reviewed for: declared-vs-actual behavior (an MCP/plugin
that reads an undeclared secret is rejected), obvious data-exfiltration or
destructive operations, and obfuscation. Skill/MCP/plugin downloads are
sha256-pinned in `index.json` and verified on install, so a tampered mirror
cannot swap content. Report a problematic listing by opening an issue.

See `ATTRIBUTION.md` for upstream credits and licenses.
