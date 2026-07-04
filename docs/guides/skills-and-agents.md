# Skills, Agents, and the Variable Cascade

This guide explains how the three authoring surfaces in corlinman fit
together, where each file lives on disk, and how a prompt gets expanded
before it reaches the model. For the authoritative config schema see
`docs/config.example.toml`; for internal specs see `docs/architecture.md`.

---

## 1. The three concepts, side by side

| Concept | File type | Default dir | What it is |
|---------|-----------|-------------|------------|
| **Skill** | `*.md` with YAML frontmatter | `skills/` | A reusable capability the agent can invoke — injected as an instruction block that describes when and how to use a tool. |
| **Agent card** | `*.yaml` | `agents/` | A `{{角色}}` persona — a named bundle of system prompt, allowed tools, and skill references. |
| **Cascade variable** | `*.txt` | `TVStxt/tar`, `TVStxt/var`, `TVStxt/sar`, `TVStxt/fixed` | A runtime-resolved string that expands `{{…}}` placeholders in prompts, including agent system prompts. |

Mental model: **variables** feed **skills** and **agent cards** feed the
**placeholder engine**. An agent card can reference a skill, which may
reference a variable — three layers, one pass.

---

## 2. Resolution order

When the agent assembles the final prompt for a turn, expansion happens
in this order. Each step's output feeds the next:

1. **Agent card expansion** — `{{角色}}` / bare-token references are
   resolved by `AgentCardRegistry`. The first card expanded in a prompt
   wins when `[agents] single_agent_gate = true`.
2. **Cascade variables** — every `{{X}}` is offered to the
   `VariableCascade`, which walks the four tiers (fixed → tar → sar →
   var) and substitutes the first hit.
3. **Skill injection** — any skill listed in the agent card's
   `skill_refs` has its `body_markdown` appended to the system prompt,
   along with a compact header line containing `name` + `emoji`.
4. **Placeholder engine final pass** — emits the fully-expanded prompt;
   any placeholder that resolved to `None` is left literal so the model
   can see an authoring bug rather than hide it.

> 💡 **Debug tip / 调试提示**: set `LOG_LEVEL=debug`
> to see each substitution and its source tier.

---

## 3. Where each file lives

Paths are relative to the `data_dir` in `[server]`:

```
<data_dir>/
├── skills/
│   ├── web_search.md
│   ├── code_review.md
│   └── memory.md
├── agents/
│   ├── mentor.yaml
│   ├── researcher.yaml
│   └── editor.yaml
└── TVStxt/
    ├── tar/
    │   ├── CurrentProject.txt     → {{TarCurrentProject}}
    │   └── HouseStyle.txt         → {{TarHouseStyle}}
    ├── var/                       → {{VarXYZ}} via env lookup
    ├── sar/
    │   └── SarPrompt1.txt         → {{SarPrompt1}} gated on env SarModel1
    └── fixed/
        └── README.md              (placeholder — tier not yet wired)
```

Each of those directory names is configurable; see `[skills]`,
`[variables]`, and `[agents]` in `docs/config.example.toml`.

---

## 4. Authoring a new skill

Minimal template (save as `skills/<name>.md`):

```markdown
---
name: my_skill
description: One sentence describing what this skill does.
metadata:
  openclaw:
    emoji: "✨"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      Steps an operator runs once, before enabling the skill.
allowed-tools:
  - some.tool
---
# My Skill

## When to use
- ...

## When NOT to use
- ...

## How to use
1. ...
```

Rules the loader enforces:

- `name` and `description` are required and must be non-empty.
- `allowed-tools` uses the kebab-cased frontmatter key (YAML native);
  internally it maps to `allowed_tools`.
- The file MUST start with a `---` fence on line 1.
- Unknown frontmatter keys are ignored, so you can add cross-harness
  metadata without breaking corlinman's parser.

---

## 5. Authoring a new agent card

Minimal template (save as `agents/<name>.yaml`):

```yaml
name: my_agent          # must match the filename stem
description: Short persona summary.
system_prompt: |
  You are a [role]. Current time: {{TimeVar}}.
  Project context: {{TarCurrentProject}}.
  [Concrete behavior rules here.]
variables:
  key: "value"          # stringified; referenced as {{agent.key}} inside the card
tools_allowed:
  - some.tool
skill_refs:
  - my_skill            # name of a file in skills/ (without extension)
```

Rules:

- The filename stem is authoritative: `mentor.yaml` must declare
  `name: mentor` (or omit `name:` entirely).
- `system_prompt` is required and must be non-empty.
- `variables.*` values are stringified at load time to keep the
  expander's substitution step type-safe.
- Duplicate names across files are rejected at load with the path of
  the offending duplicate, so you can find it fast.

---

## 6. Hot reload for TVStxt files

When `[variables] hot_reload = true`, a `HotReloadWatcher` polls
`tar/`, `var/`, `sar/`, and `fixed/` for changes. On a detected
mutation the corresponding `DirLoader` invalidates its cache for the
touched stem, so the next `resolve(...)` re-reads from disk.

Notes:

- Edits propagate within the watcher's polling window (default 1 s).
- Adding a brand-new file is also detected; deleting a file causes
  subsequent resolves to return `None` (i.e. the placeholder stays
  literal, which is almost always the desired "authoring error is
  visible" behavior).
- Agent cards (`agents/*.yaml`) and skills (`skills/*.md`) are **not**
  hot-reloaded today — they load once at startup. Restart the gateway
  or call the admin reload endpoint to pick up changes.

---

## 7. Placeholder reference

The placeholder engine understands these shapes:

| Placeholder | Tier | Resolves to |
|-------------|------|-------------|
| `{{TimeVar}}` | fixed | Current time, ISO 8601 with timezone. |
| `{{Date}}` | fixed | Current date, `YYYY-MM-DD`. |
| `{{TarX}}` | tar | Contents of `TVStxt/tar/X.txt`. |
| `{{VarX}}` | var | `os.environ["VarX"]`, or the contents of `TVStxt/var/<env_value>.txt` if the env value names a file stem. |
| `{{SarPromptN}}` | sar | Contents of `TVStxt/sar/SarPromptN.txt` **only if** the current model name matches the comma-separated list at `os.environ["SarModelN"]`; otherwise empty string. |
| `{{agent.key}}` | agent card | Value of `variables.key` inside the currently-expanding card. |
| `{{角色}}` / `{{RoleName}}` | agents registry | Expands to the named card's system prompt (subject to the single-agent gate). |

Unknown placeholders are left literal — that is not a bug, it is how
you detect an authoring typo without the model silently blurring over
it.

---

## 8. Quick verification loop

After editing any of these files:

```bash
# Load the sample agents/ + TVStxt/ and assert shape.
uv run pytest python/packages/corlinman-agent/tests/test_config_samples.py -v
```

Green means the sample tree still parses. Red surfaces the offending
file and message immediately.
