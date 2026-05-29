# Multi-Agent Dispatch

corlinman ships a small set of topic-specific sub-agents (researcher,
editor, mentor, orchestrator, general-purpose) plus everything an
operator needs to author their own. The main model can pick one on the
fly through the `subagent.spawn` tool; an operator can pin a playground
session to a specific persona; long-running children can run detached
and notify the parent on completion.

This document is the operator-facing deep dive. The implementation plan
lives at [`docs/PLAN_MULTI_AGENT.md`](PLAN_MULTI_AGENT.md). For the
event-stream contract that backs the live activity panel, see
[Observability](observability.md).

---

## Overview

Two surfaces, both backed by the same registry + supervisor:

- **Auto-dispatch** — the main model calls `subagent.spawn` with a
  `subagent_type` argument (`"researcher"`, `"editor"`, …). The
  registry resolves the name to an AgentCard, the supervisor enforces
  caps + tool whitelists, and a child reasoning loop runs against the
  card's system prompt and tool set. If `subagent_type` is omitted,
  the registry falls through to the new `general-purpose` card.
- **Operator pre-selection** — `/admin/playground/protocol` carries an
  `<AgentPicker>` at the top of the chat panel. Default is "Auto-route"
  (the existing message-peek heuristic); flip it to a specific agent
  and every request from the playground is pinned to that persona.

The same registry feeds both — there is one canonical answer to "what
agents exist on this gateway?", and it lives in
[`/admin/agents`](#admin-agents-page).

---

## Agent registry

Three tiers, last-wins on name collisions, shadows logged at boot:

```
1. built-in   — repo's agents/*.yaml          (ships with the release)
2. user       — $DATA_DIR/agents/*.{yaml,md}  (operator overlay)
3. project    — ./.corlinman/agents/*.{yaml,md}  (per-deployment overlay)
```

Built-ins today (`agents/`):

```bash
$ ls agents/
editor.yaml          general-purpose.yaml  mentor.yaml
orchestrator.yaml    researcher.yaml
```

`AgentCardRegistry.load_from_dir_stack(dirs)` walks the list in order
and records the winning source per card name. The `/admin/agents`
endpoint exposes the source on every row as one of `built-in`, `user`,
or `project`. Shadowing is allowed (a user card named `researcher`
overrides the built-in) and the override is logged at INFO so it shows
up in `journalctl`.

To reload the registry without restarting the gateway, hit
`POST /admin/agents/reload`. The UI calls this after every create
or delete; on the host you can `touch $DATA_DIR/agents/foo.md` and
then `curl -X POST` the same path.

---

## Card formats

### YAML (legacy, still supported)

The four original built-ins are YAML. Example
(`agents/researcher.yaml`, abbreviated):

```yaml
name: researcher
description: Reads sources and produces cited summaries.
system_prompt: |
  You are a careful research assistant. ...
variables:
  citation_style: "inline-link"
tools_allowed:
  - web.search
  - web.fetch
  - file.read
skill_refs:
  - web_search
  - deep-research
```

### Markdown with frontmatter (recommended)

New cards should use the Markdown form — the body reads like prose, the
frontmatter holds the metadata:

```markdown
---
description: Researcher agent for deep documentation dives.
model: claude-sonnet-4-6
tools: ["web.search", "web.fetch", "file.read"]
skills: ["web_search", "deep-research"]
variables:
  citation_style: "inline-link"
---

You are a researcher agent. Your goal is to dig deep into documentation,
cite sources, and return a structured summary. Always:

1. Cite sources by URL + section anchor.
2. Cross-reference at least 2 sources for any factual claim.
3. Return a markdown report with confidence high/medium/low.
```

Field reference:

| Key            | Type       | Meaning                                                          |
|----------------|------------|------------------------------------------------------------------|
| `description`  | string     | One-line label shown in pickers and the activity panel.          |
| `model`        | string?    | Pins this card to a specific model alias. Optional.              |
| `tools`        | list[str]  | Whitelist of tool names. `"*"` means "inherit parent's tools".   |
| `skills`       | list[str]? | Skill names seeded into the child's `SOUL.md` skill section.     |
| `variables`    | dict?      | `{{TimeVar}}`-style template substitutions for `system_prompt`.  |
| `maxTurns`     | int?       | Claude Code compatibility — silently dropped, supervisor enforces. |
| `background`   | bool?      | Claude Code compatibility — silently dropped (caller-side flag). |

Unknown frontmatter keys are dropped quietly so cards copy-pasted from
upstream tools (Claude Code, opencode) load without edits.

---

## `subagent.spawn` tool

The tool is registered as a builtin and is the only way the main model
spawns children. Schema:

```python
{
  "name": "subagent.spawn",
  "parameters": {
    "goal": str,                  # required — what the child should do
    "subagent_type": str?,        # registry key; fallback "general-purpose"
    "description": str?,          # 3-5 word task label for UI
    "tool_allowlist": list[str]?, # caller-side narrow (intersected with card)
    "max_wall_seconds": int?,     # capped at 60s
    "max_tool_calls": int?,
    "extra_context": str?,        # appended to the child's first user message
    "run_in_background": bool?,   # default false
    "model": str?,                # override the card's model binding
  }
}
```

Semantics:

- **Type resolution**: `subagent_type` looked up in the registry. Miss
  → fall through to `general-purpose`. Hit a `general-purpose` card
  with `tools_allowed: ["*"]` → child inherits the parent's full tool
  set (wildcard expansion is card-side only).
- **Tool whitelist**: child tools = `card.tools_allowed` ∩
  `tool_allowlist` ∩ parent's tools. Escalation (asking for a tool the
  parent can't see) is rejected before the child boots.
- **Model override**: `model` wins over the card's `model` binding.
- **Background**: see next section.

---

## Background dispatch

> **Status (as of v1.9.x): NOT YET IMPLEMENTED.** Setting
> `run_in_background: true` currently returns a clean rejection envelope
> (`finish_reason=REJECTED`, `error="run_in_background_not_implemented"`)
> and the spawn does **not** run detached — use the default (synchronous,
> foreground) spawn instead. The gateway ships an `AsyncSubagentDispatcher`
> + persistent task store, but the dispatcher's `run_child_factory` is not
> wired to a real child runner, and `agent_servicer` does not yet thread
> the dispatcher into the spawn tool path. A complete implementation also
> needs: the parent-side tool snapshot + model + depth + `max_wall_seconds`
> persisted onto `SubagentRequest` (today only metadata is stored, so a
> background child cannot inherit the parent's tools and would be
> pure-LLM-only); the `start_turn_for_subagent_notification` journal helper
> (missing from every backend, so the "synthetic user msg" below is
> currently a no-op); routing through the Rust supervisor so all three
> R3-004 caps apply; and a boot-time sweep to reconcile orphaned
> `running` rows after a restart. Tracked in `audit/ARCH_DEBT.md`.

The intended design (once implemented): when `run_in_background: true`,
the tool returns the moment the supervisor has admitted the request — the
child runs detached and the parent can resume its turn:

```
main model                gateway                 child loop
    │                        │                        │
    ├─ subagent.spawn ──────►│                        │
    │  run_in_background     ├─ admit (cap check)     │
    │                        ├─ persist task ─┐       │
    │                        │                │       │
    │◄─ {request_id, …} ─────┤                │       │
    │                        ├─ create_task ──┴──────►│
    │  (continues turn)      │                        │  (runs)
    │                        │                        │
    │                        │◄── BubbleEmitter ──────┤
    │                        │   (events stream)      │
    │                        │                        │
    │                        │◄── terminal ───────────┤
    │                        ├─ audit log entry       │
    │                        ├─ synthetic user msg ──►(parent journal)
```

The synthetic message format the parent sees on its next turn:

```
[subagent.completed:8d2f3e1a-...] researcher

<the child's final summary>
```

State is persisted at `$DATA_DIR/.subagent-state.json` (atomic JSON
writes, same pattern as the one-click upgrade store). The dispatcher
caps at 15 in-flight per tenant; over-cap requests fail fast with a
clear sentinel rather than queueing.

---

## Live activity panel

`/admin/subagents` is a single-pane view of every active background
child. It is SSE-driven (`GET /admin/subagents/events/live`) so rows
update in place without polling.

| Column         | Source                                  | Updates                  |
|----------------|-----------------------------------------|--------------------------|
| Parent session | `parent_session_key`                    | static                   |
| Type           | `subagent_type`                         | static                   |
| Description    | tool args `description` or goal preview | static                   |
| State          | `pending / running / completed / failed / killed` | on state change |
| Elapsed        | `now() - dispatched_at`                 | ticks every second       |
| Tool calls     | counted off the BubbleEmitter feed      | per child event          |
| Kill           | button — wired to `POST /admin/subagents/{id}/kill` | on demand   |

Clicking a row opens a drawer with a per-child `<EventTimeline
mode="live">` — the same component the session detail page uses, just
seeded from the child's session key. Kill sends a cooperative cancel;
the child has up to `max_wall_seconds` before the supervisor force-
terminates the task.

Terminal rows can be filtered in (`?include_terminal=true`) so an
operator can scroll back through the day's dispatches.

---

## `/admin/agents` page

The agents page lists every card the registry has loaded:

- **Source badge** — coloured pill (`built-in`, `user`, `project`) so
  it's obvious which cards are part of the release vs. local additions.
- **Create button** — opens `<CreateAgentModal>`. Required: a name
  (regex `^[a-z][a-z0-9-]{1,39}$`); a format (`md` or `yaml`); a body.
  Optional: clone-from (deep-copies an existing card's body + tools as
  a starting template) and a force-override-built-in checkbox.
- **Delete button** — enabled on `user` / `project` rows; disabled on
  `built-in` (the backend returns 409 even if the UI is bypassed).

Inline edits still go through the Monaco editor on the detail page —
unchanged from the pre-multi-agent surface, just now aware of the
source badge.

---

## Playground picker

The protocol playground (`/admin/playground/protocol`) carries an
`<AgentPicker>` at the top of the chat panel. Two modes:

- **Auto-route** (default) — request goes through the existing message-
  peek heuristic. The router scans the user message for keywords and
  picks an agent.
- **Pinned** — the picker's selection is threaded into the chat request
  body as `agent_id`. The backend's `_peek_agent_binding` prefers
  `start.extra["agent_id"]` over the heuristic when set, so the
  operator's pick wins. Unknown ids log a warning and fall back to the
  heuristic — backwards compatible with older clients.

API callers (Python SDK, `curl`) can pin the same way by passing
`start.extra["agent_id"]` on the request envelope.

---

## Caps and safety

Supervisor caps live in
`corlinman-subagent/src/corlinman_subagent/supervisor.py`:

| Cap                            | Default | Why                                           |
|--------------------------------|---------|-----------------------------------------------|
| `max_concurrent_per_parent`    | 3       | Parent can't fork a dozen children at once.   |
| `max_concurrent_per_tenant`    | 15      | Whole gateway is bounded.                     |
| `max_depth`                    | 2       | No nested-grandchild delegation by default.   |
| `max_wall_seconds_ceiling`     | 60      | Hard wall on any child, even if caller asked for longer. |

Tool whitelist enforcement (`runner.py:_filter_tools_for_child`) is the
authoritative gate: child tools ⊆ parent tools, full stop. The wildcard
`"*"` is honoured **only** on a card's `tools_allowed` (the card author
opted in). Caller-side `tool_allowlist: ["*"]` is rejected literally —
there is no path to widen the parent's tool set through a tool call.

Every dispatch lands four lines in
`$DATA_DIR/system-audit.log`:

```
subagent.dispatched   parent_agent_id, requesting_user_id, subagent_type, depth, goal_preview
subagent.completed    request_id, finish_reason, elapsed_ms, tool_calls
subagent.failed       request_id, error, elapsed_ms
subagent.killed       request_id, killed_by (user_id), elapsed_ms
```

These join the existing one-click-upgrade audit lines in
`/admin/system`.

---

## API reference

### Subagent lifecycle

| Method | Path                                          | Behavior                                              |
|--------|-----------------------------------------------|-------------------------------------------------------|
| GET    | `/admin/subagents?include_terminal=…`         | List active (and optionally terminal) dispatches.     |
| GET    | `/admin/subagents/{id}/status`                | Read-once snapshot of one child's state.              |
| GET    | `/admin/subagents/{id}/events`                | Per-child SSE stream (re-uses BubbleEmitter).         |
| GET    | `/admin/subagents/events/live`                | Tenant-wide SSE overview (one event per state change).|
| POST   | `/admin/subagents/{id}/kill`                  | Cooperative cancel; 204 on accept.                    |

### Agent CRUD

| Method | Path                          | Behavior                                                                       |
|--------|-------------------------------|--------------------------------------------------------------------------------|
| POST   | `/admin/agents`               | Body `{name, format, body, force?}` → 201; writes to `$DATA_DIR/agents/`.      |
| DELETE | `/admin/agents/{name}`        | 204 on user/project; 409 on built-in (cannot delete shipped cards).            |
| POST   | `/admin/agents/reload`        | Re-scan all three tiers; respond with the new card list + shadow report.       |

---

## Authoring a custom agent — step by step

Goal: a `documentation-cleaner` agent that fixes stale code blocks in
your own repo. We'll do it from the host shell first so you can see the
file layout, then through the UI.

1. **Pick a name.** Lowercase, hyphens, ≤ 40 chars. We'll use
   `documentation-cleaner`.

2. **Write the card.** Create
   `$CORLINMAN_DATA_DIR/agents/documentation-cleaner.md`:

   ```markdown
   ---
   description: Audits markdown docs for stale code blocks and broken links.
   model: claude-sonnet-4-6
   tools: ["file.read", "file.write", "web.fetch", "grep"]
   ---

   You are a documentation cleaner. Your job is to take a path to a
   markdown file, read it, and:

   1. Re-run every fenced code block that claims to be shell/Python.
      If it errors, flag the block with a comment line above it.
   2. Resolve every link via `web.fetch`. If it 404s, flag the link.
   3. Return a summary: how many blocks ran clean, how many links
      resolved, and an action list for the human reviewer.

   Do not edit the file unless explicitly told to.
   ```

3. **Reload the registry.** Either click "Reload" on `/admin/agents`,
   or:

   ```bash
   curl -X POST -b admin-cookie http://localhost:6005/admin/agents/reload
   ```

4. **Verify.** Visit `/admin/agents`. You should see a new row with
   source badge `user` and a Delete button enabled (because it's not a
   built-in).

5. **Dispatch.** Either ask the main model to "use the
   documentation-cleaner agent to audit docs/quickstart.md", or pin
   the playground picker to `documentation-cleaner` and send the same
   prompt. The tool call goes out as
   `subagent.spawn(subagent_type="documentation-cleaner", goal=…)`,
   the supervisor admits it, and you can watch the run in
   `/admin/subagents`.

To remove the card later, click Delete in the UI or
`curl -X DELETE …/admin/agents/documentation-cleaner`. The file is
removed from `$DATA_DIR/agents/` and the registry reloaded.

---

## Limitations

- **No nested delegation by default.** `max_depth=2` means a child
  cannot spawn its own children. A future role-escalation pass
  (planned in [`PLAN_MULTI_AGENT.md`](PLAN_MULTI_AGENT.md) §3) will
  add an `orchestrator` role that can re-delegate.
- **60-second wall.** Even with `max_wall_seconds: 600` on the call,
  the supervisor ceiling is 60s. Long workflows belong in a plugin or
  in the (planned) batch-task surface, not a single subagent.
- **No plugin agents.** Cards must live on disk under one of the three
  tiers. Plugins cannot register cards at runtime yet.
- **No in-process teammates.** corlinman doesn't ship Claude Code's
  `name + team_name` shared-context flow. Children get a fresh
  `subagent_runtime` write origin and don't see the parent's
  `MEMORY.md`.

---

## See also

- [Observability](observability.md) — the BubbleEmitter, event types,
  and the timeline component the live panel reuses.
- [System updates](system-updates.md) — the one-click upgrade flow
  whose audit log shares the same surface as the subagent dispatch
  entries.
- [`docs/PLAN_MULTI_AGENT.md`](PLAN_MULTI_AGENT.md) — implementation
  plan with the full wave breakdown.
