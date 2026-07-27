# PLAN — Borrow Claude Code's dual-mode subagent dispatch into corlinman

Goal: the **main agent** can (1) **call an existing registered agent** by name, and
(2) **create a temporary, purpose-built agent on the fly** (ad-hoc, inline, not
persisted) — mirroring Claude Code's `Task`/Agent tool. Researched from CC's actual
binary source + corlinman's code + its 6 multi-agent design docs.

## What Claude Code does (the pattern we borrow)

CC's one `Task`/Agent tool has **two dispatch modes** off a single `subagent_type` param:

- **Named registered agent** — `subagent_type: "code-reviewer"` resolves a definition from
  `.claude/agents/<name>.md` (frontmatter `name`/`description`/`tools`/`model` + markdown
  body = system prompt). Pre-configured tools + model + prompt.
- **Ad-hoc general-purpose** — `subagent_type` omitted/`"general-purpose"` → a throwaway agent
  given only a freeform `prompt`, full tools (`"*"`), default model. **Ephemeral**: it exists
  only for that one call, never written to any registry.

Both run in an **isolated fresh context** (the `isSidechain` marker — zero parent history),
restricted to the agent's tool/model, and the subagent's **final message becomes the tool
result** to the parent. Multiple `Task` calls in one turn run concurrently; depth is bounded.

## What corlinman has today (verified in code)

corlinman already has a nested-subagent stack that is **structurally ready** for both modes:

- `subagent.spawn` / `subagent.spawn_many` tools → `dispatch_subagent_spawn` →
  `AgentCardRegistry.get_or_default(subagent_type)` → `run_child(parent_ctx, card, spec)`.
  The card's `system_prompt` / `tools_allowed` / `model` are all reused by the runner.
- **`run_child(agent_card: AgentCard, …)` takes an `AgentCard` *object*, not a registry name** —
  the supervisor / runner / blackboard / observability stack is entirely card-agnostic.
- `AgentCard` is `@dataclass(frozen=True)` with **`source_path: Path | None = None`** — i.e. a
  card with no on-disk source is already a legal value.
- Supervisor (`corlinman-subagent`): `ParentContext` + depth/`max_depth`, per-parent/per-tenant
  concurrency, pre-spawn rejection. Blackboard: shared cross-agent state. `AgentExpander`:
  `@agent` mention routing.

### The two precise gaps

| Capability | Status | Gap |
|---|---|---|
| (1) call existing agent | **dispatch works**, but the spawn schemas are **NOT in `_builtin_tool_schemas()`** (dispatch-only) | the main agent never *sees* `subagent.spawn`, so it can't call it. ~5-line fix: advertise the schemas. |
| (2) temporary ad-hoc agent | **missing** | every spawn funnels through `get_or_default(...)` and rejects unknown names (`unknown_subagent_type`); no way to pass an inline `system_prompt`. |

## CC → corlinman mapping

| Claude Code | corlinman today | adaptation |
|---|---|---|
| `Task(subagent_type=name)` | `subagent.spawn(subagent_type=…)` → `get_or_default` → `run_child(card)` | advertise the schema (STEP 0); otherwise unchanged |
| ad-hoc `general-purpose` (no def, freeform prompt, ephemeral) | — (none) | **new** `subagent.spawn_inline` → `build_ephemeral_card()` → same `run_child` |
| `.claude/agents/*.md` def (tools/model/prompt) | `AgentCard` (registry, YAML/markdown) | reuse; ephemeral card is the same dataclass with `source_path=None` |
| context isolation / `isSidechain` / final-msg return | `ParentContext` derivation + `run_child` return envelope | reuse unchanged |
| parallel `Task` calls / depth bound | `spawn_many` + supervisor depth/concurrency | reuse; extend depth-prune to the new tool |

## Design

### Capability (1) — call existing agent (STEP 0, ~5 lines, no logic change)
Add `subagent_spawn_tool_schema()` + `subagent_spawn_many_tool_schema()` to
`_builtin_tool_schemas()` so the main agent is always advertised the existing-agent-call
tools. Dispatch already resolves named cards and reuses their `system_prompt`/`tools`/`model`.

### Capability (2) — temporary ad-hoc agent (`subagent.spawn_inline`)
A new builtin tool that takes a freeform **`goal` + `system_prompt`** (+ optional
`name` / `description` / `tool_allowlist` / `model`), builds an **ephemeral `AgentCard` in
memory** (`source_path=None`, `tools_allowed=["*"]` bounded by the parent's tools), and feeds
it to the **same `run_child` path** — never touching the registry.

Tool schema (OpenAI shape):
```
subagent.spawn_inline {
  goal: str (required)            # one-line purpose, used as the task/first user turn
  system_prompt: str (required)   # the ephemeral agent's persona/instructions (CC's agent body)
  name?: str                      # slugified label for observability (default "inline")
  description?: str
  tool_allowlist?: string[]       # subset of the parent's tools; intersection enforced
  model?: str                     # model override (else inherit)
  max_wall_seconds?: int          # clamped to the ceiling
  extra_context?: str             # optional extra material appended to the child's first turn
}
```

### File-by-file changes
1. `corlinman_agent/subagent/runner.py` — add `SUBAGENT_SPAWN_INLINE_TOOL = "subagent.spawn_inline"`;
   in the depth-(max-1) self-prune block also `discard(SUBAGENT_SPAWN_INLINE_TOOL)`; export in `__all__`.
   `run_child` needs **no change** (already card-object-driven).
2. `corlinman_agent/agents/card.py` — `build_ephemeral_card(*, name, system_prompt, description=None,
   model=None) -> AgentCard` (`source_path=None`, `tools_allowed=["*"]`, empty skills/vars) +
   `_safe_slug()` (CC-style: lowercased `[a-z0-9-]`, 3–50, fallback `"inline"`). Optionally add an
   `"inline"` value to the `AgentSource` literal for observability.
3. `corlinman_agent/subagent/tool_wrapper.py` — `subagent_spawn_inline_tool_schema()`,
   `_parse_inline_args()` (reuse `_parse_args` validation + required `system_prompt`),
   `dispatch_subagent_spawn_inline()` = clone of `dispatch_subagent_spawn` whose only divergence is
   building the ephemeral card instead of `get_or_default`, and `persona_store=None`. Add to `__all__`.
4. `corlinman_agent/subagent/__init__.py` — re-export the new symbols (mirror `dispatch_subagent_spawn`).
5. `corlinman_server/agent_servicer.py` — import the new symbols; add `SUBAGENT_SPAWN_INLINE_TOOL` to
   `BUILTIN_TOOLS`; add a dispatch branch after the `SUBAGENT_SPAWN_TOOL` branch; add the schema to
   `_builtin_tool_schemas()` (alongside STEP 0).
6. `docs/multi-agent.md` — document the third surface (ad-hoc inline agent).
7. `tests/test_subagent_spawn_inline.py` — ephemeral card has `source_path=None`; registry untouched;
   tool intersection bounded by parent tools (escalation rejected); depth-prune strips the tool;
   missing `system_prompt` → `args_invalid`; return-envelope parity with named spawn.

### Lifecycle & safety (all reused from the named-spawn path)
- **Depth**: `run_child` derives `child_ctx.depth = min(parent.depth+1, …)`; supervisor rejects at
  `depth >= max_depth` (`DEPTH_CAPPED`). Extended self-prune stops a max-depth-1 child inline-spawning a grandchild.
- **Concurrency**: same supervisor slot (per-parent / per-tenant caps) held for the child's duration.
- **Wall-clock**: `max_wall_seconds` clamped to the ceiling; cooperative cancel + grace, partial output preserved.
- **Isolation**: fresh `ParentContext` (mangled child id/session, `::` separators), fresh `ReasoningLoop`,
  child sees only `[system = ephemeral prompt] + [goal/extra_context]` — zero parent history.
- **Containment**: inline `tools_allowed=["*"]` is **intersected with the parent's tools** + the caller's
  `tool_allowlist`, so an inline agent can never exceed the parent's authority.

### Recommended hardening (do alongside, flagged by the research)
- **Wire supervisor caps at the servicer.** Today the `dispatch_subagent_spawn*` calls in
  `agent_servicer.py` omit `supervisor_acquire`/`child_seq`/`max_depth`/`max_wall_seconds_ceiling`, so
  depth/concurrency caps aren't enforced at this entry point. Thread them into **all three** spawn
  branches so named + inline spawns share one enforced pool. (Verify current state first.)
- **Prompt-injection containment.** The inline `system_prompt` is model/attacker-controlled; capability
  stays bounded by the parent's tools, but encourage a narrow `tool_allowlist` and allow the existing
  `_permission_gate` to deny `subagent.spawn_inline` per channel/user.
- **Rust-supervisor parity.** Audit `rust/crates/corlinman-subagent` (and any Python supervisor) for
  hardcoded spawn-tool-name literals; add `subagent.spawn_inline` wherever spawn tools are enumerated.
- **Defer inline + `run_in_background`** to a later wave (v1 rejects it with a typed envelope).

### Minimal change path (ordered)
STEP 0 advertise existing spawn schemas → STEP 1 runner constant + prune → STEP 2 `build_ephemeral_card`
→ STEP 3 schema + parse + `dispatch_subagent_spawn_inline` → STEP 4 package re-exports → STEP 5 servicer
wiring → STEP 6 supervisor-caps hardening (+ Rust audit) → STEP 7 tests + docs.

Estimated surface: ~1 new tool, ~1 factory, ~1 dispatcher (clone), + wiring. Reuses runner/supervisor/
blackboard wholesale. Verification: ruff + mypy + new tests + import-linter (no new cross-layer imports).
