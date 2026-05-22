# RESEARCH — opencode & Claude Code advantages, and what corlinman should borrow

Created 2026-05-23 · Method: 5 parallel research agents over the locally
cloned `opencode` and `claude-code-sourcemap` (Claude Code v2.1.88
restored source) trees.

This catalogs, in full, the concrete advantages each agent has over
corlinman's current agent, then gives a prioritized borrow roadmap.

corlinman today: a `ReasoningLoop` (multi-round tool calling, round cap
60), 14 builtin tools, automatic per-session memory, a coding system
prompt, the Codex provider over the Responses API.

---

## A. Context management

| # | Advantage | Who | corlinman gap |
|---|-----------|-----|---------------|
| A1 | **Token-aware compaction** — when near the window, an offline summarization turn replaces old history. Post-compact budget: 50K tokens, ≤5 files re-injected at 5K each. | Claude Code | none — history grows unbounded to round 60 |
| A2 | **Tool-result freezing/truncation** — a tool result is truncated once, cached by `tool_use_id`, and never re-sent verbatim; unseen results are dropped. | Claude Code | every tool result kept verbatim every round |
| A3 | **Prompt caching** — system prompt marked `cache_control: ephemeral, ttl 300`; static prefix / dynamic suffix split; cache-break detection. | Claude Code | no cache headers |
| A4 | **File-read cache** — reads memoized by `(path, mtime)`; re-reading an unchanged file is free. | Claude Code | re-reads every time |
| A5 | **Resume / transcript** — compact-boundary markers let a session resume without re-sending pre-boundary history. | Claude Code, opencode | single in-memory run, no resume |
| A6 | **History dedup** — duplicate turns filtered within a 200-turn window. | opencode | none |

Steady-state context growth: Claude Code O(1) (~50K post-compact) vs
corlinman O(rounds × message size).

## B. Tool implementation craft

| # | Advantage | Who | corlinman gap |
|---|-----------|-----|---------------|
| B1 | **Pre-read token gate** — rejects a file whose estimated tokens exceed a cap (25K) *before* reading, with an actionable error. | Claude Code | `read_file` caps chars post-read only |
| B2 | **Image / PDF / notebook reads** — returns a discriminated union (text/image/notebook/pdf) so the model reasons over visual content inline. | Claude Code | text only |
| B3 | **Blocked device paths** — refuses `/dev/zero`, `/dev/tty`, `/proc/self/fd` to avoid hangs. | Claude Code | none |
| B4 | **9-tier fuzzy edit matcher** — SimpleReplacer → LineTrimmed → BlockAnchor (first/last-line anchors + Levenshtein on the middle) → indentation-flexible → escape-normalized → … Recovers from ~30% whitespace/indent drift in the model's `old_string`. | opencode | `edit_file` is exact-match + uniqueness check only |
| B5 | **Edit staleness guard** — a file must be read before it is edited; an edit is rejected if the file changed since that read. | Claude Code | none |
| B6 | **Quote / BOM / CRLF normalization** before matching. | both | none |
| B7 | **Per-file semaphore lock** — serializes concurrent edits to the same file. | opencode | none |
| B8 | **mtime-sorted grep results** — most-recently-edited files surface first. | opencode | `search_files` returns scan order |
| B9 | **VCS noise auto-exclusion** — `.git`/`.svn`/`.hg`/`.bzr` excluded from grep. | Claude Code | partial (`_SKIP_DIRS` exists) |
| B10 | **grep pagination** — `offset` + `limit`, `mode` = content/files/count. | Claude Code | `search_files` caps at 200, no offset |
| B11 | **Shell output tail-truncation + file spill** — keeps the *tail* (where the error is), spills full output to a temp file, returns a path. | both | `run_shell` head-truncates at 30K chars |
| B12 | **Background processes** — long commands run async with a task id. | both | synchronous only |
| B13 | **`<system-reminder>` / structured tool output** — tags, diff stats, LSP diagnostics appended to edit results. | both | plain JSON envelopes |

## C. System prompt & behavior engineering

opencode ships per-provider prompts (`anthropic.txt`, `codex.txt`,
`gpt.txt`, `beast.txt`, …); Claude Code's `constants/prompts.ts` is ~800
lines of assembly. The behavioral rules worth copying:

- **C1 Truthful reporting** — never claim success without verification;
  report real test output; never suppress a failing check.
- **C2 Verify before "done"** — run the test / execute the script /
  check output before reporting completion.
- **C3 Todo discipline** — 3+ steps → `todo_write` first; exactly one
  `in_progress`; mark `completed` immediately, never batched.
- **C4 No speculative code** — no error handling for impossible cases,
  no one-use helpers, no designing for hypothetical futures.
- **C5 Read before edit** — never propose changes to unread code.
- **C6 Tool hierarchy** — dedicated tools over shell for file work.
- **C7 Destructive-action calibration** — local reversible actions are
  free; hard-to-reverse ones (delete, force-push, `reset --hard`) ask
  first; never use a destructive op as a shortcut.
- **C8 Respect user changes** — never revert edits you did not make.
- **C9 Security default** — catch injection / XSS / OWASP top 10.
- **C10 Conciseness** — skip preamble; lead with the answer; `path:line`
  refs; no emoji unless asked.
- **C11 Ask only when blocked** — not permission questions.
- **C12 Minimal comments** — only the non-obvious WHY.
- **Dynamic env block** — cwd, git status, platform, shell, OS, model
  id, knowledge cutoff injected each session.
- **Output styles** — `.claude/output-styles/*.md` reshape behavior.

corlinman's current prompt (~30 lines) covers C1/C3/C5/C6/C10 lightly;
it is missing C2/C4/C7/C8/C9/C11/C12 and the dynamic env block.

## D. Sessions, permissions, hooks, plugins, subagents

| # | Advantage | Who | corlinman gap |
|---|-----------|-----|---------------|
| D1 | **File-change snapshots + revert/undo** — git-worktree snapshots; revert to any message restores the exact filesystem state. | opencode | none — file ops are forward-only |
| D2 | **Permission ruleset** — declarative `(permission, pattern) → allow/deny/ask`, last-match-wins, default `ask`; subagents inherit parent denies. | opencode | ad-hoc denylist on `run_shell` only |
| D3 | **Hook system** — 15+ points (`PreToolUse`, `PostToolUse`, `SessionStart`, `PreCompact`, …); a hook can block, mutate input/output, or prompt. | Claude Code | none |
| D4 | **Pre-tool classification** — a speculative classifier auto-approves safe commands so the user is not interrupted. | Claude Code | none |
| D5 | **Coordinator mode** — a parent orchestrates worker subagents with isolated tool sets + a shared scratch dir. | Claude Code | `subagent_spawn` exists; no coordinator |
| D6 | **Plugin lifecycle** — plugins contribute skills + hooks + MCP servers; enable/disable persisted. | Claude Code | bundled skills only |
| D7 | **Remote / background task persistence** — long tasks survive a dropped connection; polled on resume. | Claude Code | none |

## E. Provider, streaming, reliability, cost

| # | Advantage | Who | corlinman gap |
|---|-----------|-----|---------------|
| E1 | **Exponential backoff** — 500ms→32s, `Retry-After` header honored. | Claude Code | no retry in the Python layer |
| E2 | **Persistent retry for unattended runs** — survives 6h rate-limit windows, 30s keep-alive heartbeat. | Claude Code | none |
| E3 | **Foreground/background 529 gate** — background calls bail immediately during overload to avoid amplification. | Claude Code | none |
| E4 | **Model fallback chain** — 3+ overloads on the primary model → fall back to a cheaper one. | Claude Code | none |
| E5 | **Dynamic max-token adjustment** on context overflow, then retry. | Claude Code | none |
| E6 | **Credential refresh mid-retry** on 401. | Claude Code | Codex token refresh exists; not retry-integrated |
| E7 | **20+ vendor overflow-regex patterns** — detects context overflow across Anthropic/OpenAI/Bedrock/Gemini/Grok. | opencode | basic |
| E8 | **Cost / token tracking** — per-model input/output/cache tokens, session aggregation, cache-hit rate. | Claude Code | none — usage discarded |
| E9 | **SSE per-chunk timeout + mid-stream cancel** — `AbortController` propagates a cancel reason. | both | basic exception propagation |
| E10 | **Multi-provider capability schema** — pick a model by required capabilities. | opencode | partial |

---

## Borrow roadmap (prioritized for a QQ-bot coding agent)

### Tier 1 — high value, bounded effort
1. **Tool-result truncation + freeze** (A2/B11) — cap each tool result,
   tail-truncate `run_shell`, never re-send verbatim. Stops 60-round
   context blow-up. ~½ day.
2. **Provider retry/backoff** (E1/E3) — exponential backoff + retry-after
   on 429/5xx around the Codex call; background calls fail fast. ~1 day.
3. **Enriched system prompt** (C1–C12 + env block) — adopt the missing
   behavioral rules and inject a dynamic environment block. ~½ day.
4. **Cost/token tracking** (E8) — accumulate `usage` per session, expose
   it. ~½ day.
5. **grep polish** (B8/B9/B10) — mtime sort, firm VCS exclusion,
   `offset` paging. ~½ day.

### Tier 2 — medium value / effort
6. **File-read cache** (A4) — memoize `(path, mtime)` within a turn.
7. **Fuzzy edit matcher** (B4/B6) — whitespace/indent-tolerant fallback
   chain for `edit_file`; cuts edit-failure rate.
8. **Token-aware compaction** (A1) — summarize old turns near the limit.
9. **File-change snapshot + revert** (D1) — per-turn snapshot, an `undo`.

### Tier 3 — large / lower value for a chat bot
10. Permission ruleset + hooks (D2/D3), coordinator mode (D5), plugin
    lifecycle (D6), prompt caching (A3), LSP. Revisit on concrete need.

Recommended first batch: **Tier 1** — five items, ~3 days, each
independently shippable and verifiable.
