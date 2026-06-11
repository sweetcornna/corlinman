# PLAN — claude-code 1:1 feature parity (opencode secondary) + cross-channel commands

> Goal (2026-06-11): 完整 1:1 实现 claude-code 所有功能(opencode 为辅),强化系统能力;
> 并让这些指令(slash commands)在所有渠道可用(console / web / QQ / Telegram / Discord / …)。
> Source matrix: `docs/parity-matrix-2026-06-11.json` (5-agent workflow sweep of
> claude-code 2.1.88 restored source + opencode + the live corlinman tree, 19 clusters).
> Foundation: PR #88 (`corlinman console`, docs/PLAN_CLI_CONSOLE.md).

## Wave map (from the matrix)

| Wave | Cluster | Status | Effort | Value |
|---|---|---|---|---|
| 1 | Print mode + structured output (`--output-format text/json/stream-json`, `--max-turns`) | partial | S | high |
| 1 | Todo surfacing (live checklist render, activeForm spinner) | partial | S | high |
| 1 | **Cross-channel commands** (`/new /model /usage /sessions /resume` in ALL channels — user directive) | missing | M | high |
| 1 | Project memory: CORLINMAN.md analog of CLAUDE.md (discovery, @includes, /memory, /init) | missing | M | high |
| 1 | Context compaction parity (`/compact`, threshold config, circuit breaker, compaction event) | partial | M | high |
| 1 | Permission modes + interactive console approval (default/acceptEdits/plan/bypass + /permissions) | partial | M | high |
| 1 | File-snapshot rewind (`/rewind`, message-granular checkpoints) | partial | M | high |
| 2 | Permission rules engine (`Bash(cmd:*)` grammar, multi-source precedence, settings persistence) | partial | L | high |
| 2 | Plan mode (Enter/ExitPlanMode tools, read-only gating, plan-model override) | missing | M | high |
| 2 | Core tool semantic parity (Read offset/limit, Bash run_in_background, Grep modes, NotebookEdit, atomic Write) | partial | L | high |
| 2 | MCP client integration (.mcp.json scopes, tool namespace merge, /mcp) | partial | L | high |
| 2 | Session persistence + resume UX (picker, --continue, --fork-session, history file, retention) | partial | M | medium |
| 2 | Subagent background execution + monitoring (async tasks, TaskStop, output spill) | partial | M | medium |
| 3 | User-configurable hooks (settings-driven command/prompt/agent/http hooks, /hooks) | partial | L | medium |
| 3 | Skills parity (frontmatter contract, context:fork, slash exposure) | partial | M | medium |
| 3 | Model routing chain + ToolSearch deferral | partial | M | medium |
| 3 | Settings system + /config + /doctor | partial | M | medium |
| 3 | opencode: ACP server, session sharing, markdown agent config | partial | L | medium |
| 4 | Console UX long tail (themes, vim, statusLine, /cost breakdown) | partial | M | low |
| 4 | opencode: LSP integration + worktree lifecycle tools | missing | XL | low |

Full gap lists + seams per cluster live in the JSON artifact; they are the
implementation contract for each wave.

## Cross-channel commands design (user directive: 指令在各渠道可用)

`corlinman_channels.commands` is already the shared registry (channels router +
web playground both dispatch through it; console currently has its own). Unify:

1. **One registry, three surfaces.** New *shared* session commands are registered
   as `CommandSpec` rows in `corlinman_channels.commands` (handler-based, async):
   - `/new` — rotate the binding's conversation epoch (fresh context)
   - `/model [name]` — show/set per-binding model override
   - `/usage` — token/turn stats for this binding's session (from journal)
   - `/sessions`, `/resume <key>` — admin-tier, journal-backed
   The console consults the shared registry FIRST (synthetic
   `ChannelBinding(channel="console", …)`), falling back to console-local
   commands (`/progress`, `/verbose`, `/quit` stay local).
2. **Per-binding prefs store** (`binding_prefs_store.py`, sqlite alongside
   `home_channel_store`): `binding_prefs(user_id, channel, account, thread,
   model_override, session_epoch, updated_at_ms)`.
3. **Channel chat path honors prefs** in
   `corlinman_channels/service.py::_build_internal_request`: model_override →
   `InternalChatRequest.model`; session_epoch folded into the session_key
   derivation (`{base}:{epoch}` when epoch > 0). ⚠️ This file is the duck-typed
   contract that once silently killed all channels (lesson
   2026-06-02/persona_id) — every change lands with channel-service tests.
4. **/help** already auto-generates from the registry, and Telegram BotFather
   export (`telegram_bot_commands()`) picks new commands up for free.

## Execution

- Work happens in the isolated worktree `/Users/cornna/project/corlinman-console-wt`
  (branch `feat/cli-console`, on top of PR #88) — another active session owns the
  main checkout (chat UI bug fixes on `feat/chat-enterprise-parity`).
- Wave 1 ships as one PR stacked on #88; waves 2-4 follow in subsequent rounds.
- Multi-agent: self-contained pieces (print-mode formats, todo renderer,
  CORLINMAN.md loader, rewind) are implemented by parallel subagents with
  explicit file ownership; cross-cutting seams (channels service contract,
  permission gate) are implemented in the main loop; everything is triaged,
  linted, and tested centrally before commit (workflow-overreach lesson).
