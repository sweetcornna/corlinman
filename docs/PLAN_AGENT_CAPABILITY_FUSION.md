# PLAN — Agent Capability Fusion (opencode + Claude Code)

Status: **in progress** · Owner: agent · Created 2026-05-22

Goal: bring the corlinman QQ-bot agent up to opencode / Claude Code
parity as a coding agent. Driven by a 3-agent source study of
`/Users/cornna/project/opencode` and
`/Users/cornna/project/claude-code-sourcemap` (Claude Code v2.1.88
restored source).

## Where the agent stands today

The agent (`corlinman_agent` driven by `CorlinmanAgentServicer`) already
has, as builtin tools advertised to the model:

- file ops — `read_file`, `write_file`, `edit_file`, `list_files`
- search — `search_files` (regex grep + filename glob)
- shell — `run_shell` (workspace cwd, timeout, denylist)
- web — `web_search`, `web_fetch`; `calculator`
- orchestration — `subagent_spawn`, `blackboard`
- automatic per-session conversation memory

It runs the full `ReasoningLoop` (multi-round tool calling) over the
Codex provider, in `grpc_agent` mode.

## Gap analysis vs opencode / Claude Code

| Capability | opencode | Claude Code | corlinman | Decision |
|---|---|---|---|---|
| file read/write/edit | ✅ | ✅ | ✅ | have it |
| grep / glob | ✅ | ✅ | ✅ | have it |
| shell | ✅ | ✅ | ✅ | have it |
| **todo / task tracking** | `todowrite` | `TodoWriteTool` | ❌ | **ADD (Tier 1)** |
| **apply_patch (multi-hunk)** | `apply_patch` | (MultiEdit-ish) | ❌ | **ADD (Tier 1)** |
| **coding system prompt** | `prompt/*.txt` | `constants/prompts.ts` | ❌ none | **ADD (Tier 1)** |
| shell safety depth | AST parse | 23-check validator | regex denylist | **HARDEN (Tier 2)** |
| LSP code intel | ✅ | ✅ | ❌ | skip — huge effort, low value for a chat bot |
| repo_clone / repo_overview | ✅ | — | ❌ | skip — `run_shell` + `git clone` covers it |
| plan mode | ✅ | ✅ | ❌ | skip — mode-switch UX doesn't map to a QQ turn |
| worktree / REPL / voice / vim / teams | — | ✅ | ❌ | skip — out of scope for a QQ bot |

## Tier 1 — capabilities to add

### 1. `todo_write` tool
Session-scoped task list so the agent stays organised on multi-step
work. Data model merges both sources (Claude Code's is richer):

```
TodoItem = { content: str, activeForm: str, status: pending|in_progress|completed }
```

- One `in_progress` at a time (soft-enforced: warn, don't reject).
- Scoped by `session_key`; stored in-process on the servicer (a turn is
  one RPC — no cross-process durability needed for v1).
- The tool returns the full rendered list so the model sees current
  state; the list is also re-injected into context on the next turn of
  the same session.

### 2. `apply_patch` tool
The Codex/opencode textual patch envelope — Codex models are natively
trained on it:

```
*** Begin Patch
*** Add File: <path>
+<line>
*** Update File: <path>
*** Move to: <newpath>          (optional)
@@ <context>
 <unchanged>
-<removed>
+<added>
*** Delete File: <path>
*** End Patch
```

- Workspace-confined (every path through `resolve_in_workspace`).
- Multi-pass fuzzy line matching (exact → rstrip → strip → unicode-
  normalised) for robustness, mirroring opencode `patch/index.ts`.
- Atomic-ish: parse + validate every hunk before writing any file.

### 3. Coding-agent system prompt
Today, with no agent card matched, the agent gets **no system prompt at
all**. Add a baseline `instructions` block injected by the servicer
when the assembled messages carry no system message. Key content
(distilled from both prompts):

- concise, professional tone; no emoji unless asked;
- task discipline — use `todo_write` for 3+ step tasks; mark complete
  only after verification;
- tool discipline — prefer the dedicated file tools over `run_shell`
  for file ops; read before edit; verify with `run_shell`;
- truthful reporting — never claim success without evidence;
- code references as `file:line`.

## Tier 2 — shell hardening

`run_shell` runs as the bot's user on a shared VPS. Proportionate
hardening (not a sandbox — that needs containers):

- widen the denylist: `sudo`/`doas`, `dd if=`, `LD_PRELOAD=`, `mkfs`,
  fork bomb, `> /dev/sd*`;
- split compound commands on `;` `|` `&` `&&` `||` and screen each
  segment, so `ls # rm -rf ~` style smuggling is caught;
- log every command + exit code for audit.

## Out of scope (explicitly)

LSP, repo_clone/overview, plan mode, worktree, REPL, voice, vim, team
tools, AST-grade shell validation. Revisit only if a concrete need
appears.

## Execution order

1. coding-agent system prompt (highest leverage / lowest effort)
2. `todo_write` tool + context re-injection
3. `apply_patch` tool
4. shell hardening
5. tests for each; deploy; verify end-to-end on the VPS
