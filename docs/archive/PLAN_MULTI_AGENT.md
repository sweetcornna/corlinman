# PLAN — Multi-Agent Orchestration (Claude Code style)

**Status:** draft v1.0 · 2026-05-25
**Goal:** main model can auto-dispatch a topic-specific sub-agent OR an operator-configured pre-built agent, both via the existing `subagent.spawn` tool (with a new `subagent_type` arg). Operators get an admin UI to create/clone/edit agents, plus an agent picker in `/admin/playground`.

This is largely **plumbing, not a rewrite** — corlinman already ships ~65% of the machinery: supervisor with caps, BubbleEmitter for event fan-out, AgentCardRegistry loading `agents/*.yaml`, `subagent.spawn` tool registered as a builtin, persona/model binding per card. We're filling the Claude Code-style ergonomics gaps.

---

## 0. Diagnosis (audited 2026-05-25)

### 0.1 What's already in corlinman (re-use)

| Piece | Where | Status |
|---|---|---|
| `subagent.spawn` + `subagent.spawn_many` tools | `corlinman-agent/src/corlinman_agent/subagent/{runner,tool_wrapper}.py` | shipped — schema accepts `goal, tool_allowlist, max_wall_seconds, max_tool_calls, extra_context` |
| Supervisor caps | `corlinman-subagent/src/corlinman_subagent/supervisor.py:83-105` | shipped — `max_concurrent_per_parent=3, per_tenant=15, max_depth=2, max_wall=60s` |
| Event bubbling | `BubbleEmitter` in `gateway/observability/emitter.py`; events `SubagentSpawned/Event/Completed` | shipped (W3.2 of observability) |
| AgentCard registry | `corlinman-agent/src/corlinman_agent/agents/{card,registry}.py` | shipped — 4 YAML cards, fields `system_prompt, variables, tools_allowed, skill_refs, model?, provider?` |
| Tool allowlist enforcement | `runner.py:_filter_tools_for_child()` | shipped — child tools ⊆ parent, escalation rejected |
| Persona seeding per child | `runner.py:150+` | shipped — `persona_store` wired |
| Admin agents list + Monaco editor + model binding | `ui/app/(admin)/agents/page.tsx`, `/agents/detail/page.tsx` | shipped |
| Per-agent model binding API | `PATCH /admin/agents/{name}/binding` | shipped |

### 0.2 Reference patterns we're porting

**From Claude Code (`/src/tools/AgentTool/`)**:
- `subagent_type` enum dispatched off a registry (built-in + plugin + user `~/.claude/agents/*.md` + project `.claude/agents/*.md`)
- Frontmatter MD format: `description / prompt / model / tools (with '*' wildcard) / disallowedTools / maxTurns / background / skills / mcpServers`
- `run_in_background: true` → returns `async_launched` immediately, status in `appState.tasks[agentId]`
- Multiple `Agent` tool calls in one turn fire in parallel; UI shows "Running N agents…"
- Fork agent (implicit) — `subagent_type` omitted shares parent's full prompt + tools

**From hermes (`tools/delegate_tool.py:1-2796`)**:
- Batch mode `tasks: [{goal, context, toolsets, role}]` runs in `ThreadPoolExecutor(max_workers=3)`
- `role: leaf | orchestrator` — only orchestrators may re-delegate, depth-capped
- `DELEGATE_BLOCKED_TOOLS = {delegate_task, clarify, memory, send_message, execute_code}` — children never recursively self-spawn or write shared memory
- Progress callback relay with `_identity_kwargs(subagent_id, parent_id, depth, model, toolsets, task_index)`
- Result is JSON `{results: [{task_index, status, summary, api_calls, duration_seconds, ...}], total_duration_seconds}` — order preserved, individual failure doesn't abort peers

### 0.3 Gaps to close

| # | Gap | User-visible impact |
|---|---|---|
| 1 | `subagent.spawn` schema has no `subagent_type` field — child resolves to the same card as parent, no way for model to pick "researcher" vs "editor" | Main model can't pick a topic-specific persona on the fly |
| 2 | `AgentCardRegistry.load_from_dir()` only reads the repo's `agents/` — no user overlay | Operators can't add agents without committing to git |
| 3 | No "create agent" UI — Monaco only edits existing YAML | Adding a new agent is a manual file-system step |
| 4 | No agent picker in `/admin/playground` — operator can't manually scope a session to one agent | All sessions use the default routing |
| 5 | No `run_in_background` flag — every spawn blocks the parent's tool dispatch | Long-running sub-agent (e.g. "summarize 50 docs") freezes the main chat |
| 6 | No agent activity panel — `appState.tasks[]` analog doesn't exist; only `SubagentSpawned/Completed` events flow | Operator can't see "agent X is at 14s elapsed, 6 tool calls in" without reading the live timeline |
| 7 | No frontmatter MD format — only YAML | Authoring is verbose; can't write the agent prompt as prose with metadata at top |

---

## 1. Target architecture

### 1.1 Extend `subagent.spawn` schema

Current schema:
```python
{
  "name": "subagent.spawn",
  "parameters": {
    "goal": str,
    "tool_allowlist": list[str]?,
    "max_wall_seconds": int?,
    "max_tool_calls": int?,
    "extra_context": str?,
  }
}
```

New schema (Claude Code-shaped):
```python
{
  "name": "subagent.spawn",
  "parameters": {
    "goal": str,                          # was: required
    "subagent_type": str?,                # NEW — registry key, e.g. "researcher", "general-purpose"
    "description": str?,                  # NEW — 3-5 word task label for UI
    "tool_allowlist": list[str]?,         # unchanged; merged with card's tools_allowed (intersection)
    "max_wall_seconds": int?,             # unchanged
    "max_tool_calls": int?,               # unchanged
    "extra_context": str?,                # unchanged
    "run_in_background": bool?,           # NEW — default false; true → returns request_id immediately
    "model": str?,                        # NEW — override card's model binding
  }
}
```

When `subagent_type` is omitted: dispatch falls back to "general-purpose" (a new built-in card we'll ship). When `subagent_type` resolves to a card with `tools_allowed: ["*"]`: child inherits the parent's full tool set (Claude Code's wildcard semantics).

Tool dispatch helper merges `tool_allowlist` (caller-supplied narrow) ∩ `card.tools_allowed` (registry-side whitelist) ∩ parent's available tools (escalation check).

### 1.2 User-overlay agent registry

Stack registries by precedence (Claude Code pattern):

```
1. built-in YAMLs        — repo's agents/*.yaml (4 cards today)
2. user overlay          — $DATA_DIR/agents/*.{yaml,md}  (NEW writable dir)
3. project overlay       — $CORLINMAN_DATA_DIR/.corlinman/agents/*.{yaml,md} (optional, for multi-tenant deploys)
```

User overlay loads on demand (admin UI changes) + on startup. **No file-watcher**: a `POST /admin/agents/reload` endpoint flushes the registry; UI calls it after every create/edit. Operators on the prod box can `touch $DATA_DIR/agents/foo.yaml` then hit Reload.

Frontmatter MD format support (Claude Code):

```markdown
---
description: Researcher agent for deep documentation dives.
model: claude-sonnet-4-6
tools: ["read_file", "web_search", "grep"]
maxTurns: 50
background: false
---

You are a researcher agent. Your goal is to dig deep into documentation, cite
sources, and return a structured summary. Always:

1. Cite sources by URL + section anchor.
2. Cross-reference at least 2 sources for any factual claim.
3. Return a markdown report with frontmatter `confidence: high/medium/low`.
```

Body becomes `system_prompt`; frontmatter becomes the rest of the card. Existing `.yaml` files keep working — the loader just gains a second branch.

### 1.3 Background dispatch

Mirror the one-click upgrade pattern we just shipped:

- `subagent.spawn` with `run_in_background: true`:
  - `Supervisor.try_acquire()` + register a task in a new `SubagentTaskStore` (analogous to `UpgradeStateStore`)
  - Return `{status: "async_launched", request_id, child_session_key}` immediately
  - Background `asyncio.create_task()` runs the child via `run_child()`
  - Status pollable via `GET /admin/subagents/{request_id}/status`
  - SSE stream at `GET /admin/subagents/{request_id}/events` (re-uses `BubbleEmitter` per-child queue)
  - On terminal state: `enqueue_subagent_notification()` injects a synthetic `user` message into the parent session — Claude Code's pattern. The parent model sees a turn-notification on its next request

- `run_in_background: false` (default): synchronous, blocks the parent's tool dispatch — same as today

### 1.4 Admin UI

**`/admin/agents` page** (modify):
- Today: table of YAML files + model binding inline edit + click → Monaco editor
- Add: "Create agent" button → modal with frontmatter form (description, model, tools picklist, body textarea) + Save → POST `/admin/agents` → registry reload → toast
- Add: "Clone from" dropdown on create modal — deep-copies an existing card's body+tools as a starting point
- Add: "Source" badge per row (`built-in` / `user` / `project`)
- Built-in rows are read-only (delete + edit body disabled); user/project rows are full-CRUD

**`/admin/playground` page** (modify):
- Add: agent picker dropdown at the top — defaults to "auto-route" (existing message-peek behavior); operator can pin to a specific agent
- When pinned, the playground's chat requests carry an explicit `agent_id` hint that the message router respects

**New `/admin/subagents` page**:
- Live table of active background subagents (driven by SSE on `/admin/subagents/events/live`)
- Columns: parent session, subagent_type, description, elapsed, tool count, state, kill button
- Click row → detail panel with the live BubbleEmitter feed (re-uses `<EventTimeline>` from observability)
- Shows up in main sidebar under the existing "Agents" group (or as a sibling — TBD in W3.1)

### 1.5 Audit + observability

Audit lines in `$DATA_DIR/system-audit.log` (existing JSONL) for:
- `subagent.dispatched` — when a tool call resolves; actor (parent_agent_id, requesting user_id), subagent_type, depth, goal_preview
- `subagent.completed` — finish_reason, elapsed_ms, tool_calls_made
- `subagent.killed` — if operator clicks kill in the live panel

---

## 2. Tasks (4 waves, 9 background agents)

### Wave 1 — Backend tool surface + registry overlay (3 parallel)

#### W1.1 Extend `subagent.spawn` schema + dispatch logic

- **Owner:** Backend Architect
- **Files:**
  - `corlinman-agent/src/corlinman_agent/subagent/tool_wrapper.py` — extend schema, validation, dispatch handler
  - `corlinman-agent/src/corlinman_agent/subagent/runner.py:run_child` — accept resolved AgentCard from a new `subagent_type` parameter; thread it through (today the child agent_id is derived; new path lets caller pick)
  - `corlinman-agent/src/corlinman_agent/agents/registry.py` — add `get(name)` lookup that falls back to "general-purpose" when name is None
  - Build "general-purpose" built-in card (new `agents/general-purpose.yaml`) — generic prompt + `tools_allowed: ["*"]` semantics
  - Tests: `tests/subagent/test_spawn_with_subagent_type.py` — 6+ cases (type resolution, fallback, escalation rejection, wildcard tools, model override, etc.)
- **ETA:** 6h

#### W1.2 User-overlay registry + reload endpoint + MD frontmatter parser

- **Owner:** Backend Architect
- **Files:**
  - `corlinman-agent/src/corlinman_agent/agents/registry.py` — `load_from_dir_stack(dirs: list[Path])` with precedence; `parse_markdown_card(text) -> AgentCard` for frontmatter MD
  - `gateway/lifecycle/entrypoint.py` — build the registry from `[repo agents/, $DATA_DIR/agents/, project_overlay/]`; store on `AdminState.agent_registry`
  - `routes_admin_b/agents.py` — extend with:
    - `POST /admin/agents` — create new agent (body: frontmatter MD or YAML). Validates, writes to `$DATA_DIR/agents/{name}.md`, triggers registry reload
    - `DELETE /admin/agents/{name}` — only for user/project overlays (built-ins refused with 409)
    - `POST /admin/agents/reload` — re-scan the overlay dirs
    - `GET /admin/agents` — extend response with `source: "built-in" | "user" | "project"` per row
  - Tests: `tests/agents/test_registry_overlay.py` — 8+ cases (precedence, MD parsing, frontmatter validation, hot reload semantics)
- **ETA:** 7h

#### W1.3 Background subagent task store + SSE endpoints

- **Owner:** Backend Architect
- **Files:**
  - `corlinman_server/system/subagent/__init__.py` (new) — `SubagentTaskStore` (mirror of `UpgradeStateStore` shape)
  - `corlinman_server/system/subagent/dispatcher.py` (new) — `dispatch_async_subagent(parent_session, request) -> SubagentDispatchResponse` — spawns background task, registers in store
  - `routes_admin_b/subagents.py` (new):
    - `GET /admin/subagents` — list active background subagents
    - `GET /admin/subagents/{id}/status`
    - `GET /admin/subagents/{id}/events` (SSE, mirrors `/upgrade/{id}/events`)
    - `GET /admin/subagents/events/live` (global SSE — for the live activity panel)
    - `POST /admin/subagents/{id}/kill` — operator kill switch
  - `SubagentSpawned/Event/Completed` already in the emitter — these endpoints subscribe and re-emit per-id
  - Tests: `tests/subagent/test_dispatcher.py`, `tests/gateway/routes_admin_b/test_subagents.py` — 10+ cases
- **ETA:** 6h

### Wave 2 — Admin UI (3 parallel)

#### W2.1 `/admin/agents` create/clone/delete + source badge

- **Owner:** Frontend Developer
- **Files:**
  - `ui/app/(admin)/agents/page.tsx` — add Create button → modal; show Source badge; gate Delete on non-built-in
  - `ui/components/agents/create-agent-modal.tsx` (new) — frontmatter form (description, model picker reusing `<ModelPickerDialog>`, tools multiselect, body textarea) + Clone-from dropdown + Save
  - `ui/lib/api.ts` — `createAgent({name, source, body})`, `deleteAgent(name)`, `reloadAgents()`
  - i18n keys: `agents.create.*`, `agents.source.*`
  - Tests: 3 cases — create flow, clone flow, delete refused for built-in
- **ETA:** 6h

#### W2.2 `/admin/subagents` live activity panel

- **Owner:** Frontend Developer
- **Files:**
  - `ui/app/(admin)/subagents/page.tsx` (new) — table powered by `streamSubagentEvents()` SSE
  - `ui/components/subagents/subagent-row.tsx` (new) — per-row: parent session link, subagent_type badge, description, elapsed counter, tool count, state pill, Kill button
  - `ui/components/subagents/subagent-detail-drawer.tsx` (new) — opens on row click; reuses `<EventTimeline mode="live">` scoped to the subagent
  - `ui/components/layout/sidebar.tsx` — new entry `Sub-agents` (icon: `Network` or `GitFork`) in the operator group
  - i18n keys: `subagents.*`
  - Tests: 2 cases — row renders with stub event, kill button POSTs to backend
- **ETA:** 7h

#### W2.3 `/admin/playground` agent picker + i18n

- **Owner:** Frontend Developer
- **Files:**
  - `ui/app/(admin)/playground/protocol/page.tsx` (or wherever the chat UI lives) — add agent picker dropdown at top
  - `ui/components/playground/agent-picker.tsx` (new) — defaults to "auto-route", lists user + built-in agents alphabetically, shows description on hover
  - Thread chosen agent_id into the chat request payload (backend will route on it if present, else fall back to message-peek)
  - Backend wiring: `agent_servicer.py:_peek_agent_binding` — if request body carries `agent_id` hint, prefer that over message-peek
  - i18n keys: `playground.agentPicker.*`
- **ETA:** 4h

### Wave 3 — Polish + smoke (2 parallel after W2)

#### W3.1 E2E smoke + audit log integration

- **Owner:** API Tester
- **Files:**
  - `ui/tests/e2e/multi-agent.spec.ts` (new) — 3 stub Playwright tests:
    1. `/admin/agents` create flow → list shows new row with user source badge
    2. `/admin/subagents` live panel renders a fixture event + Kill button POSTs
    3. `/admin/playground` agent picker dropdown lists agents and threads the pick into the request
  - Wire `dispatch_async_subagent` to append audit lines (`subagent.dispatched`, `subagent.completed`, `subagent.killed`) — re-use the W1.3 audit log writer from one-click upgrade
- **ETA:** 4h

#### W3.2 Docs + CHANGELOG

- **Owner:** Technical Writer
- **Files:**
  - `docs/multi-agent.md` (new) — operator doc covering agent registry layout, MD frontmatter format, `subagent_type` dispatch, background spawn semantics, kill semantics, audit log surface
  - `docs/quickstart.md` — one-line addition pointing operators at `/admin/agents` + how to create their first agent
  - `CHANGELOG.md` — entry under `[Unreleased]` (target v1.4.0)
- **ETA:** 2h

---

## 3. Parallelization

```
Wave 1 (3 parallel):       W1.1   W1.2   W1.3
                              │      │      │
Wave 2 (3 parallel):       W2.1   W2.2   W2.3
                              │      │      │
Wave 3 (2 parallel):       W3.1   W3.2
```

Total wall-clock ~1.5 working days with 3 concurrent agents.

---

## 4. Explicitly out of scope

- **Multi-tenant agent quotas** beyond the existing `max_concurrent_per_tenant=15` cap — pricing/billing is a separate effort
- **Plugin-loaded agents** — Claude Code's `plugin agents` are agents shipped by an MCP plugin. corlinman doesn't have a plugin-loaded-agent surface yet; defer
- **Hot-reload via inotify/watchdog** — defer; explicit reload endpoint is enough
- **Tool RBAC per agent** — today `tools_allowed: []` whitelist is enforced; finer-grained per-tool permissions (e.g. read-only vs read-write file ops) defer
- **In-process teammates (Claude Code's `name + team_name` flow)** — separate processes with shared mailbox is a v1.5 feature
- **Per-agent MCP server set** — defer; today MCP wiring is gateway-wide
- **Markdown frontmatter validation via Zod-equivalent** — defer; basic field presence + tool-name existence is enough
- **`run_in_background` auto-trigger when child takes > 60s** — Claude Code has `tengu_auto_background_agents` gate; defer

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| User overlay agent collides with built-in name (`researcher.yaml`) | Reload sorts precedence: user > built-in. The shadow is logged; admin UI badges built-ins as "shadowed by user override" |
| Agent body markdown contains XSS payload | Body is fed to the LLM, not rendered as HTML in admin UI. Editor uses Monaco (plaintext); preview renders via `react-markdown + rehype-sanitize` if we add a preview pane |
| Background subagent leaks if parent disconnects mid-stream | Supervisor's `max_wall_seconds_ceiling=60` enforces a hard timeout; `kill` endpoint allows manual stop. Background dispatch checks if parent session is alive every 30s |
| `tools_allowed: ["*"]` wildcard escalation | Wildcard expands ONLY to tools the parent already has; can never grant access to tools the parent lacks. Tested in W1.1 |
| Background spawn pile-up when an operator hammers the picker | `max_concurrent_per_tenant=15` already caps; the picker's "Run" button gates on the live count (visible in `/admin/subagents` panel) |
| Parent model loops infinitely on background-spawn → notification → background-spawn pattern | The synthetic user-notification message has a structured prefix `[subagent.completed:<id>]` so the parent model's prompt can be tuned (default agents/general-purpose.yaml) to NOT auto-spawn in response |

---

## 6. Decision points before kickoff

- [ ] Plan accepted as-is, or trim subset?
- [ ] `~/.corlinman/agents/` vs `$DATA_DIR/agents/` for user overlay path — recommend `$DATA_DIR/agents/` (consistent with where config.toml + .update_check.json live, persists across docker container recreate)
- [ ] Default `subagent_type` fallback name — `general-purpose` (Claude Code) or `default` (corlinman's existing convention)?
- [ ] Target release tag — `v1.4.0` (minor bump, no breaking) OK?

---

**End of plan v1.0.**
