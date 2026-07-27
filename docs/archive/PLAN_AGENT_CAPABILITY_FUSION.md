# PLAN — Agent Capability Fusion (opencode + Claude Code)

Status: **Tier 1 + Tier 2 done, verified on VPS** · Owner: agent · Created 2026-05-22

> Tier 1 (todo_write, apply_patch, system prompt) and Tier 2 (shell
> hardening) are implemented, tested, deployed, and verified end-to-end:
> the agent plans with todos, reads before editing, applies patches, and
> verifies with run_shell. Reasoning-loop round cap raised 8 → 60 so
> multi-step tasks finish. See the closing "Parity assessment" section.

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

1. coding-agent system prompt (highest leverage / lowest effort) — done
2. `todo_write` tool + context re-injection — done
3. `apply_patch` tool — done
4. shell hardening — done
5. tests for each; deploy; verify end-to-end on the VPS — done

## Parity assessment (post-Tier-1/2)

The agent now runs the **core Claude Code / opencode coding loop**:
plan with `todo_write` → `read_file` → `apply_patch` / `edit_file` /
`write_file` → verify with `run_shell` → report. Tool surface:
`read_file`, `write_file`, `edit_file`, `apply_patch`, `list_files`,
`search_files` (grep+glob), `run_shell`, `todo_write`, `web_search`,
`web_fetch`, `calculator`, `subagent_spawn`, `blackboard`, plus
automatic per-session memory and a coding system prompt.

What is **deliberately not ported** from Claude Code's ~44-tool surface,
and why — these are terminal-CLI / desktop features with no meaning for
a QQ chat bot, or large subsystems with poor effort/value:

- LSP code intelligence — large; `search_files` + `run_shell` cover the
  practical need.
- plan mode / worktree / REPL / Vim / voice — terminal-UX features.
- PowerShell / NotebookEdit — niche.
- team / remote-session / cron tools — corlinman already has its own
  scheduler + evolution subsystems; not part of the coding loop.
- AST-grade shell sandboxing — needs containerisation, not a tool.

Future candidates if a concrete need appears: parallel read-only tool
execution in the reasoning loop, an on-demand `skill` tool.
