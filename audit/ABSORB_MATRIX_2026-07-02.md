# ABSORB MATRIX — 2026-07-02

> Phase 2 of the work order: benchmark corlinman against **hermes-agent** (fresh
> clone, read-only reference) and **claude-code** (baseline =
> `docs/parity-matrix-2026-06-11.json`, 19 clusters from the 2.1.88 restored
> source). Each dimension re-verified against corlinman's **current** tree
> (methodology: 读两边源码,写实证结论,不信"文件存在即完成"). Docs found stale
> where noted. Decisions: **ADOPT** / **ADAPT-ADOPT** (absorb the mechanism,
> re-implement — no code copied, license-clean) / **REJECT**.
>
> Landing order (value/cost, per the work order): MCP tool-face ✅ already
> shipped v1.22.0 → compaction hardening → permission grammar → error-recovery
> → hooks → rest.

| # | Dimension | corlinman now | Decision | Value/Cost |
|---|---|---|---|---|
| 1 | Agent loop & error recovery | PARTIAL | ADAPT-ADOPT | M / M |
| 2 | Context compaction | DONE (2 surfaces) | ADAPT-ADOPT (harden) | H / S |
| 3 | Permission system | PARTIAL (engine DONE) | ADAPT-ADOPT | H / M |
| 4 | Tools & sandbox | PARTIAL | ADOPT + ADAPT-ADOPT | H / S+L |
| 5 | MCP | mostly DONE (v1.22.0) | ADAPT-ADOPT | H / M |
| 6 | Memory layering | PARTIAL (strong substrate) | ADOPT (selective) | M / M |
| 7 | Subagent orchestration | DONE (≥ both) | REJECT | L / S |
| 8 | Project memory (CORLINMAN.md) | PARTIAL (near-complete) | ADAPT-ADOPT (`/init`) | M / S |
| 9 | Hooks lifecycle | PARTIAL (rich bus) | ADAPT-ADOPT | M / L |
| 10 | Output & scriptability | DONE (≥ both) | REJECT (opt `--system-prompt`) | L / S |
| 11 | Session management | PARTIAL | ADAPT-ADOPT | H / M |
| 12 | Observability | PARTIAL (cost computed, unsurfaced) | ADAPT-ADOPT | M / S+M |

## Landing plan (value/cost ranked)

**Batch 1 — cheap high-value (S-cost, land now, TDD + `make ci` each):**
1. **Dim 2** compaction hardening — `window − buffer` threshold + env/pct overrides + per-user disable + saved-token feedback + informative elision (H/S).
2. **Dim 4-core** — atomic `Write` (tmp→fsync→replace + cap), shell `run_in_background`, `NotebookEdit` (H/S).
3. **Dim 5-slices** — `{server}_{tool}` namespacing (fixes the v1.22.0 collision drop) + `deniedMcpServers`/allow policy (H/S).
4. **Dim 8** — `/init` codebase-analysis → `CORLINMAN.md` (M/S).
5. **Dim 1-slice** — jittered backoff in `_loop_retryable` (M/S).
6. **Dim 12-console** — `/cost` + live token/context/cost status bar on the existing `sessions_cost` aggregate (M/S).

**Batch 2+ — larger, own PRs (M–L cost):** Dim 3 permission console surface (`/permissions`,`/plan`, interactive resolver, `Enter/ExitPlanMode`, settings persistence) H/M; Dim 11 message-id soft-delete rewind + `--continue`/fuzzy resume H/M; Dim 6 provider-fan-out `MemoryManager` + background prefetch M/M; Dim 9 declarative `hooks` settings + `/hooks` M/L; Dim 4 sandbox-backend abstraction H/L; Dim 12 per-tool OTel spans M.

**REJECT:** Dim 7 (subagents — corlinman ≥ both baselines); Dim 10 (structured output — already ahead of both).

**Stale-doc corrections (verified):** compaction `/compact`+breaker+event (matrix said partial), CORLINMAN.md discovery/include (matrix said missing), permission arg-pattern engine (matrix said partial), MCP advertise+route (matrix said partial) — all **shipped**. `docs/PLAN_CLAUDECODE_PARITY.md` wave table refreshed accordingly.

---

## Dim 1 — Agent main loop & error recovery — ADAPT-ADOPT (value=M cost=M)
- **claude-code**: one streaming loop; 429/overload → exp backoff honoring `retry-after` + model fallback (Opus→Sonnet); context-overflow → auto-compact + re-issue; tool failure → `is_error` tool_result for self-correction; ends on `end_turn`/max-turn/interrupt.
- **hermes**: centralized `classify_api_error` (`agent/error_classifier.py:478`) → ~25-member `FailoverReason` taxonomy with retryable/compress/rotate/fallback hints; inner retry loop (`conversation_loop.py:1013`) with **jittered** exp backoff (`retry_utils.py:28`, base 5s/cap 120s/jitter 0.5); refundable `IterationBudget` (parent 90 / subagent 50); ~16 one-shot recovery guards in `TurnRetryState` (OAuth refresh, credential-pool 429 rotation, thinking-sig strip, image-shrink, multimodal downgrade, 1M-beta strip, `restart_with_compressed_messages`).
- **corlinman now**: PARTIAL — `reasoning_loop.py`: `_loop_retryable` (`:242`) honors `retry_after_ms`/`reset_at_ms`; per-round `while True` (`:1612`) does context-overflow shrink-once (`:1683`), model fallback (`:1729`, `_fallback_models=[sonnet-4-6, haiku-4-5]`), transient retry cap 3 backoff `0.5*2^(n-1)` cap 16s (`:1751`), sustained-overload cross-model fallback (`:1781`); outer cap `_MAX_ROUNDS=60` (`:424`); tool-failure → `is_error` (`:2754`), dedup + spill-to-disk.
- **gap**: **no jitter** (thundering-herd); retry is **pre-stream only** (`_streaming_started` `:1610`) → mid-stream 5xx unrecoverable; narrow taxonomy (no credential rotation / OAuth-refresh / thinking-sig / image-shrink branches); flat round cap vs refundable subagent-scoped budget.
- **decision**: ADAPT-ADOPT — port **jittered backoff** + a small recovery subset (e.g. thinking-signature strip) into `_loop_retryable`/`run`; skip the provider-zoo long tail. Highest-signal, lowest-risk slice = jitter.

## Dim 2 — Context compaction — ADAPT-ADOPT / harden (value=H cost=S)
- **claude-code**: auto-compact at `effective_window − AUTOCOMPACT_BUFFER(~13k)` with pct/absolute env overrides + per-user `autoCompactEnabled`; manual `/compact`; **microcompact** elides old tool results without reordering (cache-stable), feeding saved tokens back into the threshold; ~20k summary reserve; circuit breaker at 3 fails; emits a compacted event.
- **hermes**: `ContextCompressor` (`agent/context_compressor.py`) at `threshold_percent` 0.50 of `context_length − max_tokens`; anti-thrash (skip if last 2 passes saved <10%) + summary-LLM cooldown; LLM-free `_prune_old_tool_results` (`:1139`) writes **informative** 1-line tool summaries + dedups + truncates args; consecutive-failure breaker (`conversation_compression.py:96`).
- **corlinman now**: **DONE** on two surfaces. In-loop `_compact_history` (`reasoning_loop.py:723`) = cache-stable elide (`:833`, 60% threshold) + provider-summarize (`:931`, 95% threshold); model-aware budget `_resolve_context_budget` (`:501`, window−15% cap 48k). Console `Compactor` (`console/compaction.py`) = threshold 150k / keep 6 / **circuit breaker 3** (`:200`) / manual `/compact` (`commands.py:145`) / emits "⛁ context compacted". Parity matrix's compaction gaps (/compact, breaker, compacted-event) are **STALE — already shipped**.
- **gap** (genuine): (a) fixed `150k`/`window−15%` threshold, not `window − buffer` with env/pct overrides + per-user disable; (b) no saved-token feedback → elide can false-re-trigger; (c) generic `[older tool output elided]` sentinel vs informative per-tool summaries; (d) no summary-LLM cooldown/anti-thrash on the in-loop summarizer; (e) no breaker on the *in-loop* summarizer (only the console one).
- **decision**: ADAPT-ADOPT / harden — add `window − buffer` + env-override threshold + saved-token feedback to `_compact_history`, and informative elision summaries. REJECT a rebuild (two-tier + console breaker already exist). **Top landing candidate** (H/S).

## Dim 5 — MCP client integration — ADAPT-ADOPT (value=H cost=M)
- **claude-code**: `.mcp.json` declarative config, 4 scopes (enterprise/user/project/local) + precedence dedup; `/mcp` command (add/remove/list/test, stdio+SSE/HTTP, OAuth); `{server}_{tool}` namespace; resources; server-initiated **sampling**; `allowed`/`deniedMcpServers`; `--mcp-config`/`--strict-mcp-config` (cluster #13).
- **hermes**: `tools/mcp_tool.py` stdio/HTTP/SSE; full **`SamplingHandler`** for `sampling/createMessage` w/ rate-limit + model-whitelist (`:857`); dynamic **`tools/list_changed` auto-refresh** (`_schedule_tools_refresh :1522`); `hermes mcp add/remove/list/test` CLI + OAuth (`hermes_cli/subcommands/mcp.py:41`). Resource `list_changed` observed, `resources/read` ignored.
- **corlinman now**: PARTIAL. Client stdio+ws/http (`client_manager.py:489`); **advertise+route NOW yes** (v1.22.0, `gateway/mcp/advertise.py:144`, `entrypoint.py:1070`); hot-plug `add/remove/restart/enable/disable` + `mcp_servers.sqlite` + `/admin/mcp/*`.
- **gap**: (1) **sampling** missing (empty client caps `client_manager.py:545`); (2) client **resources** missing (`resources/list`/`read`); (3) no `.mcp.json` **scopes/precedence** + `allowed`/`deniedMcpServers` (flat map `client_manager.py:154`); (4) no **`/mcp` console command** (admin-HTTP only); (5) no **`tools/list_changed`** listener (hot-plug is admin-driven, not server-pushed); (6) tools advertised **bare + first-wins dedup** (`advertise.py:59`) → cross-server name collisions silently drop tools (a limitation my v1.22.0 introduced — namespacing is the fix).
- **decision**: ADAPT-ADOPT onto the existing spine. Best slices by value/cost: `deniedMcpServers`/allow policy (safety, S) + `{server}_{tool}` namespacing (correctness, S) first; sampling + `list_changed` (M) next.

## Dim 6 — Memory layering — ADOPT selective (value=M cost=M)
- **claude-code**: hierarchical `CLAUDE.md` (mirrored by corlinman `CORLINMAN.md`, see Dim 8).
- **hermes**: `MemoryManager` fan-out over pluggable `MemoryProvider`s (`agent/memory_manager.py`) — 4 layers: `MEMORY.md`/`USER.md` notes, FTS5 session search (trigram CJK), Honcho dialectic user-model, skills self-improvement. Timing: labeled blocks at prompt build (`:456`), sync pre-turn `prefetch_all` (`:495`), post-turn `sync_all` + **background** `queue_prefetch_all` for the next turn (`:558`); one external provider at a time (`:374`).
- **corlinman now**: PARTIAL. `corlinman-memory-host` FTS5 (`local_sqlite.py:91`, bm25 `:341`) + remote_http/federation/read_only; tools `memory_search`/`session_search`/`memory_write`/`memory_read` (`memory/tools.py:226`); recall = recency (`_recall_memory` last-8-turns `agent_servicer.py:4373`) + relevance (`_recall_relevant_notes` bm25 `:4419`) folded into system prompt `_inject_memory_note` (`:4631`), **inline/synchronous** pre-answer (`:1610`); files = hierarchical `CORLINMAN.md` + per-profile `MEMORY.md`.
- **gap**: no pluggable multi-provider **fan-out manager** (+ one-external-provider guard); no **dialectic user-model** layer (Honcho analog); recall is **inline each turn** (latency) vs hermes background next-turn prefetch; skills-as-memory lives under `gateway/evolution/`, not unified.
- **decision**: ADOPT selective — a provider-fan-out `MemoryManager` + **background next-turn prefetch** (cut inline recall latency); optional dialectic user-model provider; REJECT a hard Honcho dependency.

## Dim 3 — Permission system — ADAPT-ADOPT (value=H cost=M)
- **claude-code**: persisted per-session **mode state machine** (default/acceptEdits/plan/bypass/dontAsk); interactive allow/deny/always-allow dialog blocking on `ask`; **rules engine** `Bash(cmd:*)`/`Write(*.ts)` content-glob, deterministic order (deny→allow→hooks→mode), multi-source precedence + settings persistence of "always allow"; denial tracking; **Plan mode** via `Enter/ExitPlanMode` tools + read-only gating + plan journal + opusplan override.
- **hermes**: `~/.hermes/config.yaml` `approvals.mode ∈ {off,manual,smart,cron}`+YOLO, live re-read (`tools/approval.py:213`); command-content gate (hardline `rm`/`dd` `:450`, sudo-stdin `:431`, de-obfuscation `:746`, aux-LLM risk scoring `:1861`); file-write staging + atomic `os.replace` (`write_approval.py:114`); workspace confinement (`path_security.py:15`).
- **corlinman now**: PARTIAL — **engine largely DONE**: `PermissionMode` default/acceptEdits/plan/bypass (`permission.py:80`, no `dontAsk`); `Bash(rm:*)` arg patterns + **compound-command decomposition** closing the first-token bypass (`permission.py:216-332`) + file path-globs; first/last-match order (`:494`) + `from_layered_sources` (`:569`); `ApprovalGate` ask→resolver, **fail-closed** (`approval_gate.py:145`); wired at dispatch (`agent_servicer.py:2567`). Separate gateway plugin-runtime gate (`middleware/approval.py`).
- **gap**: (1) **no console resolver wired** → every `ask` fail-closes to deny; (2) no `/permissions` or `/plan` command, no `--permission-mode` flag; (3) no settings-file persistence (env-only `CORLINMAN_AGENT_PERMISSIONS`); (4) no denial tracking; (5) Plan mode is only a gate enum — no `Enter/ExitPlanMode` tools, no plan-approval dialog/journal/opusplan.
- **decision**: ADAPT-ADOPT the **console surface** on the existing engine — interactive dialog on the `ApprovalGate` resolver hook, `/permissions`+`/plan`, `--permission-mode`, settings.json persistence, `Enter/ExitPlanMode` tools.

## Dim 4 — Tools & sandbox — ADOPT core (H/S) + ADAPT-ADOPT sandbox (H/L)
- **claude-code**: Read offset/limit/pages/ipynb + image resize; Bash `run_in_background` (task id, disk spill, offset polling) + default-on sandbox + per-call timeout; Grep ripgrep `output_mode`+`-A/-B/-C`+head_limit+multiline; Glob truncation+metadata; **atomic Write** (tmp→fsync→replace) + git diff + 1GiB cap; dedicated `NotebookEdit`.
- **hermes**: `ToolRegistry` (`tools/registry.py:208`) ~90 modules / 67+ tools; OpenAI schemas **sanitized for strict backends** (`schema_sanitizer.py:46`); standout = **7 interchangeable sandbox backends** (`tools/environments/{local,docker,modal,managed_modal,daytona,singularity,ssh}.py`) behind an `Environment` ABC + `file_sync.py`; output spill (`tool_output_limits.py`).
- **corlinman now**: PARTIAL — Read **DONE** (offset/limit `files.py:555`, PDF pages `:294`, ipynb `:403`); Edit **DONE** (`old/new/replace_all` + read-before-edit `_filestate.py:73`); Grep **PARTIAL** (output-mode trio + context + glob/type prefilter `search.py:97`, but pure-Python `re`, no head_limit/multiline); Bash **PARTIAL** (timeout + setsid `shell.py:282`, **no run_in_background**, **no sandbox** `repl.py:34`); Write **PARTIAL** (mkdir + diff `files.py:656`, **not atomic**, no cap); **NotebookEdit MISSING**; errors = `{"error":"args_invalid:…"}` envelopes, never raises.
- **gap**: no sandboxing (0 of hermes' 7 backends, none of claude-code's default sandbox); no shell `run_in_background`; non-atomic Write; no `NotebookEdit`; grep is `re` not ripgrep.
- **decision**: ADOPT the core sub-items (**atomic Write**, **shell `run_in_background`**, **`NotebookEdit`**) — H/S, self-contained; ADAPT-ADOPT hermes' `environments/` **sandbox-backend abstraction** for shell (largest structural gap) — H/L, own project.

## Dim 10 — Output & scriptability — REJECT (corlinman ahead) (value=L cost=S)
- **claude-code**: `--output-format text|json|stream-json` (result carries cost/usage/session_id; stream-json = realtime JSONL), `--max-turns`, skip onboarding in print, exit codes, `--system-prompt`/`--append-system-prompt(-file)` (cluster #9).
- **hermes**: only quiet one-shot (`-q`) printing final text + `session_id` to stderr — no structured envelope, no per-event JSONL (`cli.py:15871`); `max_turns` loop cap (`cli.py:15550`); JSONL only in offline `batch_runner.py:487`.
- **corlinman now**: DONE — `OUTPUT_FORMATS=(text,json,stream-json)`, `_stream_payload` JSONL, `_result_envelope` `{type:result,subtype,result,session_id,model,usage,num_turns,is_error,error}`, `--max-turns` gating (`console/app.py:78-162,432-522`); tested (`test_print_mode.py`). Residual: no `--system-prompt`/`--append-system-prompt` flags; stream-json omits hook-event lines.
- **decision**: REJECT vs hermes (corlinman ahead; cluster #9 effectively done). Optional S: add `--system-prompt`/`--append-system-prompt`.

## Dim 11 — Session management (rewind + resume) — ADAPT-ADOPT (value=H cost=M)
- **claude-code**: `/rewind` restores files + conversation to a message-id checkpoint at every user boundary; per-edit file history; `--rewind-files`; resume picker w/ fuzzy search/preview, `--continue`, `--resume`, `--fork-session`, retention, cost-on-resume (clusters #8+#10).
- **hermes**: message-granular rewind in SQLite — `rewind_to_message` soft-deletes rows `id>=target` keeping audit rows + bumps `rewind_count` (`hermes_state.py:3870`), `restore_rewound` undoes; separate fs checkpoints (`checkpoint_manager`, max 20); resume by id/title (`cli.py:12694`).
- **corlinman now**: PARTIAL — `/rewind` over **per-turn git snapshots** labelled by user text, `revert_last` (`console/rewind.py`); window truncation is a best-effort **label match** that degrades to "files restored; window unchanged" on ambiguity (`rewind.py:192`). `/resume`+`/sessions` list-and-pick by **exact key** + journal replay (`commands.py:97`); no fuzzy picker/`--continue`/`--fork-session`/cost-on-resume/retention.
- **gap**: rewind is turn-granular + fragile label heuristic (vs hermes message-id soft-delete w/ audit trail); resume key-exact only.
- **decision**: ADAPT-ADOPT — port message-id-keyed soft-delete rewind onto corlinman's journal (replace label-match), add `--continue`/fuzzy resume ergonomics.

## Dim 12 — Observability (tracing + cost) — ADAPT-ADOPT (value=M cost=S console / M spans)
- **claude-code**: `/cost` breakdown (USD, input/output/cached tokens, API+tool duration, lines changed), shell-driven `statusLine` bottom bar (cluster #17).
- **hermes**: **no** OTel (stdlib logging only); strong console surfacing — live status bar `context_tokens`/`context_percent`/session token counts + bar (`cli.py:4450-4560`), cost via `estimate_usage_cost` (`cli.py:99`), account billing, exit summary (`cli.py:12644`).
- **corlinman now**: PARTIAL — OTel real but **coarse** (opt-in OTLP + structlog trace binding `telemetry.py:52`, only ~3 request-level spans: `chat.completions`/`chat.service`, no per-tool/step spans). Cost accounting **exists deep**: `_estimate_turn_cost_usd`/`session_cost_usd` (`reasoning_loop.py:202,1273`), per-turn journal `estimated_cost_usd`+`cost_status`, admin rollup `GET /admin/sessions/{key}/cost` → totals/breakdown feeding admin UI. BUT the **console surfaces none**: `/usage` = tokens only, no `/cost`, toolbar = `model · session` (`console/app.py:404`).
- **gap**: tracing exists (hermes lacks) but request-level only; no live cost/token/context status line or `/cost` in the console — the surface hermes polishes + CC cluster #17 targets.
- **decision**: ADAPT-ADOPT — real-time token/context/cost status bar + `/cost` console command onto the already-computed `sessions_cost` aggregate (S); separately deepen OTel to per-tool spans (M).

## Dim 7 — Subagent orchestration — REJECT (corlinman ≥ both) (value=L cost=S)
- **claude-code**: `Task` tool forks a fresh sub-conversation (own window, isolated tools, single-level nesting); background variant returns a task id, spills output, re-invokes the model on completion (Monitor/TaskStop).
- **hermes**: no in-process fork — delegation is a durable SQLite **kanban board** (`plugins/kanban/dashboard/plugin_api.py:597` create/assign/link), a dispatcher runs `ready` tasks as separate worker processes with `task_runs` rows + psutil inspection (`:1435`) + terminate/reclaim (`:1507`); parent→child gating via `task_links` (`:1005`). Cross-process queue delegation, not context-fork.
- **corlinman now**: DONE (exceeds both). Caps accountant `supervisor.try_acquire` depth→per-parent→per-tenant (`corlinman-subagent/.../supervisor.py:322`, defaults 10/15/depth-1/300s); spawn trio + inline (`corlinman-agent/.../subagent/__init__.py:50`); trace-scoped `blackboard` (`subagent/blackboard.py`); **async background** post-dating the matrix (`_dispatch_via_background`, `system/subagent/{dispatcher,store}.py`, persisted state); live registry + panel + `POST /admin/subagents/{id}/kill` (`infra/subagents.py`); agent-type registry (`agents/registry.py`).
- **gap**: residual vs cluster #16 only — child-transcript **spill-to-disk + `outputOffset` incremental reads**, and **auto-rewake the parent model** on completion (corlinman surfaces via SSE/poll, not a model re-invocation). Hermes' durable multi-process board is orthogonal (heavier), not Task parity.
- **decision**: REJECT — at/above both; log the two residual sub-items only.

## Dim 8 — Project memory (CORLINMAN.md) — ADAPT-ADOPT `/init` (value=M cost=S)
- **claude-code**: discovers `CLAUDE.md` global→`~/.claude`→repo-root-down-to-cwd (closer overrides) + gitignored `CLAUDE.local.md`; `@path` includes; `/memory` list/edit; `/init` bootstraps `CLAUDE.md` from codebase analysis.
- **hermes**: single `AGENTS.md` at repo root (71KB); no multi-file walk / `@include` / `/init` analog.
- **corlinman now**: PARTIAL (near-complete). Full discovery/include in `console/project_memory.py`: global→root→cwd `.git`-bounded walk (`:41-88`), `CORLINMAN.md`+`.local.md` (`:23`), `@path`/`@~`/`@/abs` includes w/ cycle-break + depth-5 + missing-marker (`:91-139`), 64KB cap; wired into every mode incl. `--print` (`app.py:584`); `/memory` lists loaded files (`commands.py:127`); tested. Matrix said "missing" — **shipped**.
- **gap**: **`/init` absent** (registry has `/memory`, no `/init`; the `init` CLI commands are data-dir/config scaffolding, not codebase-analysis CORLINMAN.md gen). Minor: `/memory` lists only.
- **decision**: ADAPT-ADOPT — add a `/init` slash command running a one-shot codebase-analysis turn that writes `CORLINMAN.md` (the discovery/include pipeline already consumes it).

## Dim 9 — Hooks lifecycle — ADAPT-ADOPT (value=M cost=L)
- **claude-code**: `settings.json` `hooks` map event→matcher-arrays→hook defs of type **command/prompt/agent/http**; blocking vs async w/ exit-2 rewake + timeout; `if` permission-rule matchers; `/hooks` to view/edit/test. User-configurable, no code.
- **hermes**: file-discovered only — `HookRegistry` loads `~/.hermes/hooks/<name>/{HOOK.yaml,handler.py}` (`gateway/hooks.py:81`) firing **8** points (`gateway:startup`, `session:{start,end,reset}`, `agent:{start,step,end}`, `command:*`) with emit/emit_collect fan-out. `builtin_hooks/` ships **zero** hooks (empty extension point) — the "6 points" is off; real = 8, all discovery-driven.
- **corlinman now**: PARTIAL — richer bus than hermes but not settings-declarative. (1) typed event bus `corlinman-hooks` **31** `HookEvent` variants incl. `UserPromptSubmit`/`PreToolDispatch`/`Stop`/`Pre/PostCompact`/`Session*`/`Subagent*` (`event.py`), 3 priority tiers + cancel. (2) `HookRunner` shell hooks (`pre_tool`/`post_tool`/`stop`) w/ blocking deny + `mutated_args`/`inject_message` (`runner.py:447`) + file-discovered `HOOK.yaml`+`handler.py`; config from a config-file `hooks` section (`main.py:94`) + `CORLINMAN_HOOKS_DIR`; read-only `GET /admin/hooks`.
- **gap**: no claude-code `settings.json` shape (event→matcher-array→def); only shell + in-process Python executors (no `prompt`/`agent`/`http` kinds); no `if` permission matcher; no `/hooks` command.
- **decision**: ADAPT-ADOPT — layer a declarative `hooks` settings block (matchers + command/prompt/agent/http kinds) over the existing bus/`HookRunner` (the blocking `HookDecision` path already exists) + add `/hooks`. Batch 2 (L).
