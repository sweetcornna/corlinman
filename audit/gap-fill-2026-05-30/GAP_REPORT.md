# corlinman Gap-Fill Report — 2026-05-31

Synthesis of a verified ~23-subsystem comparison of corlinman against three references:
**claude-code** (v2.1.88 restored source), **hermes-agent**, and **openclaw**. Only
gaps that survived verification (CONFIRMED + a small UNCERTAIN tail) are included;
"already-covered" and "intentionally-omitted" candidates were refuted and dropped.
Every `path:line` below was carried from the verification pass; the highest-leverage
claims were re-confirmed against the live tree (cache_control = 0 hits, mainline
adapters carry only `_retry_after_ms_from_exc` not `with_retry`, `is_stale` returns
`False` for unrecorded paths, calculator `_BIN_OPS` has no `ast.Call`, `_CONTEXT_BUDGET`
is a flat 120k constant, no `SendMessage`/`coordinator_mode` symbols anywhere).

> Provenance note: the orchestrator's source `confirmed_gaps` array was truncated
> mid-entry at the E5 context-overflow gap. That gap, plus all gaps described only
> inside the per-subsystem summaries, were reconstructed from their fully-specified
> fields and the project's own `docs/RESEARCH_AGENT_PARITY.md` grid (A1–A6, B1–B13,
> C1–C12, D1–D7, E1–E10). `gaps.json` (same directory) is the machine-readable list.

---

## 1. Executive summary

**Total confirmed gaps: 50** across 18 subsystems (5 subsystems with rich summaries
had no discrete-enough delta to score: see §5). After de-duplication (3 gap pairs are
the same work seen from two subsystems — see §4) the count of **distinct work items is
~47**.

**Impact distribution:** 11 high · 22 medium · 14 low (a handful are present-but-weak
ergonomics rather than hard capability gaps).

### Highest-leverage items (by impact ÷ effort)

| # | Gap | Impact | Effort | Why it leads |
|---|-----|--------|--------|--------------|
| 1 | **Model-aware compaction budget** (`model-blind-flat-budget`) | high | small | One small edit: thread `context_length` (already parsed) into the budget instead of the flat 120k. Stops both early-compaction waste on 1M models and 413 overflow on 32k models. |
| 2 | **Read-before-edit guard** (`edit-read-before-edit-guard`) | high | small | One-line contract: reject an edit when there is no `FileState` record for an existing path. Closes a genuine destructive-blind-edit risk; the staleness machinery is already there. |
| 3 | **OAuth identity headers/prompt** (`provider-auth-oauth-identity-headers`) | high | small | Without the `anthropic-beta: oauth-2025-04-20` + `x-app: cli` + "You are Claude Code" prefix, the whole OAuth-subscription chat path likely 500s in prod. Header injection is cheap. |
| 4 | **Anthropic prompt caching** (`prompt-caching-cache-control`) | high | medium | Every Anthropic request currently re-bills the full system+tools prefix; one `cache_control: ephemeral` block + usage plumbing recovers a large recurring cost and unblocks USD cost math. |
| 5 | **Wire retry/backoff into the model call** (`retry-not-wired-mainline-providers`) | high | medium | Primitives exist (`with_retry`, `retry_after_ms` parsed) but are unused on the hot path; today an `Overloaded`/`RateLimit` is a single terminal frame. Pure wiring. |
| 6 | **CJK/multimodal tokenizer** (`chars-div-4-token-estimate`) | medium | medium | The chars//4 heuristic badly mis-counts CJK (the primary user base) and ignores images, making every compaction threshold imprecise. |
| 7 | **`[MSG_BREAK]` splitting** (`channels-msg-break-leak`) | medium | small | The seeded default persona emits `[MSG_BREAK]` but no sender splits on it, so a literal control token is leaking to end-users right now. |
| 8 | **read_file multimodal** (`read-multimodal-image-pdf-notebook` / `read-image-pdf-notebook`) | high | medium | corlinman is a multimodal model whose file-read returns mojibake for images/PDFs/notebooks; unlocks a large class of "look at this screenshot/PDF" requests. |

### One-line parity verdict per reference

- **vs claude-code (coding agent):** strong on the *inner* loop and tool craft (compaction
  A1/A2/A4, fuzzy edit, grep polish, FileState cache all shipped); behind on the
  *reliability/orchestration edges* — no prompt caching, retry not wired to the model
  call, no model-fallback chain, no blocking hooks, no coordinator/SendMessage,
  multimodal read missing. **~80% on craft, ~40% on the reliability/orchestration tier.**
- **vs hermes-agent:** at or above parity on persona-life tools, channels breadth,
  scheduler core, and cross-channel identity *design*; behind on agent-facing *tools*
  (memory/session-search, TTS, code-exec, vision), slash-command/skill bridging, the
  inbound authorization/pairing gate, and the wired-but-dead `run_agent` cron action +
  unwired identity store. **~70%, gaps are agent-tool breadth + a few dead wires.**
- **vs openclaw:** behind primarily on the *semantic memory / dreaming* axis — no live
  vector recall (the `vector` column is never populated), no free-association AgentDream,
  no scientific calculator, no inline HTML preview, plus the `[MSG_BREAK]` multi-bubble
  split. **~65%, the dense-vector and dreaming subsystems are the main deltas.**

---

## 2. Gaps by subsystem

Status legend: **missing** (no implementation) · **partial** (half-built / built-but-inert)
· **present_weak** (works but materially behind the reference).

### 2.1 Agent reasoning loop & orchestration

- **No cross-model fallback chain on overload** — *missing · medium · medium · E4*
  Source: claude-code `query.ts:894` (FallbackTriggeredError → switch model, strip
  thinking sigs, replay), hermes `run_agent.py:253-295`. Reality: `gateway/routes/chat.py:114-147`
  `ModelRedirect` is one-shot alias resolution at request entry; `agent_servicer.py:1262-1266`
  builds ONE loop; `reasoning_loop.py:1046-1078` on exception yields `ErrorEvent` + returns;
  `failover.py` is error taxonomy only. Fix: `fallback_model` on the alias/binding +
  overload counter + turn-replay-with-new-provider branch around `loop.run()`.
  **(Duplicate of `no-model-fallback-chain` in providers-reliability — see §4.)**

- **Lifecycle HookBus is observe-only — no blocking Stop/PreToolUse hooks** —
  *missing · medium · large · D3*
  Source: claude-code `stopHooks.ts:65-332`. Reality: `corlinman-hooks/bus.py:51`
  subscribers return `None`; `event.py:228-229` PreToolDispatch is explicitly
  fire-and-forget; `reasoning_loop.py:1094-1104` turn-end has no veto path;
  `RESEARCH_AGENT_PARITY.md:86` lists D3 as `none`. Fix: decision/collect return on
  select events, threaded into `_run_one_round` + the no-tool-calls exit.
  **(Overlaps `hooks-no-discovery`.)**

- **No persistent re-addressable teammates / coordinator mode with SendMessage** —
  *missing · medium · large · D5*
  Source: claude-code `coordinatorMode.ts:120-168`. Reality: `supervisor.py:638-728`
  awaits one `TaskResult` then releases the slot; `api.py:80-176` `TaskSpec`/`TaskResult`
  have no continuation field; `blackboard.py:60-186` is a scratchpad, not a message bus;
  **zero** `SendMessage`/`coordinator_mode`/`TeammateIdle` symbols in `python/packages`
  (re-verified). MEMORY `agent_worktree_caveats` also flags "SendMessage missing". Fix:
  child registry keyed by agent id + durable child inbox + `SendMessage`/continue tool +
  relaxed join-only lifecycle.

- **No token-budget / diminishing-returns auto-continue for autonomous runs** —
  *missing · low · medium*
  Source: claude-code `tokenBudget.ts:45-93`. Reality: `reasoning_loop.py:1094-1104` only
  non-error terminal is "no tool calls this round"; only guard is `rounds < _MAX_ROUNDS`
  (line 990, default 60); `inject_user_message` (814) exists but nothing auto-nudges. Fix:
  optional `BudgetTracker` that injects "continue, budget remaining" when `turnTokens <
  budget*0.9` and not diminishing; gate behind autonomous/scheduled runs.

- **`session.py` Session bundle and `cancel.combine` are unimplemented stubs** —
  *missing · low · small*
  Reality: `session.py:1-17` is docstring + logger, no `Session` class (TODO M2);
  `cancel.py:23-36` implements only `with_timeout`, `combine()` is a TODO. Low impact
  (single-Event cancel works today). Fix: implement both as documented.

### 2.2 Built-in tool catalog breadth

- **read_file cannot read images, PDFs, or Jupyter notebooks** — *missing · high · medium · B2*
  Source: claude-code `FileReadTool/prompt.ts:40-47`. Reality: `coding/files.py:55-85`
  schema is "Read a UTF-8 text file" (offset/limit only); `files.py:228`
  `path.read_text(utf-8, errors=replace)` unconditional; `search.py:53-56` lists
  `.png/.jpg/.pdf` as skip suffixes; zero `ipynb` handling. Fix: ext-dispatch with
  base64 image blocks, PDF page extraction, notebook cell parsing.
  **(Duplicate of `read-image-pdf-notebook` in tool-craft — see §4. Both require the
  ToolResult content-block plumbing.)**

- **No vision / image-analysis tool (only generation)** — *missing · high · medium*
  Source: hermes `toolsets.py:26-29` (`vision_analyze`), openclaw `ImageProcessor`.
  Reality: `image/` is generation-only (`plain.py`/`generate.py`/`dispatch.py`); zero
  `vision_analyze`/`analyze_image` symbols; `BUILTIN_TOOLS` (`agent_servicer.py:229-246`)
  has no analyze entry; `PLAN_PROVIDER_AUTH.md:258` explicitly defers it. Fix: add a
  `vision_analyze` tool (url/path/attachment → describe/OCR) on the image-module shape.

- **No agent-callable persistent-memory or session-search tool** — *missing · medium · large*
  Source: hermes `toolsets.py:126-141` (`memory`, `session_search`). Reality: server-side
  `_recall_memory`/`_store_memory` (`agent_servicer.py:2889-2970`) are automatic, not
  callable; `memory_write` (`background_review.py:87,692`) is curator-only and not in
  `BUILTIN_TOOLS`; `rag/` is offline `epa_backfill` only. Fix: expose a callable `memory`
  tool (notes + profile) and a `session_search` tool (the `LocalSqliteHost` FTS5 store
  already exists).

- **No text-to-speech / audio tool** — *missing · low · medium*
  Source: hermes `toolsets.py:114-116`. Reality: `agent_servicer.py:367-388` catalog has
  no TTS; no `*tts*` tool module; `routes_voice/persistence.py:24` is server-side only.
  Fix: agent-callable `text_to_speech` emitting an audio attachment.
  **(Shares backend with the voice-multimodal gap — see §4.)**

- **No in-process code-execution / REPL tool** — *missing · low · medium*
  Source: hermes `toolsets.py:150-152` (`execute_code`), claude-code `REPLTool`. Reality:
  `coding/__init__.py:71-98` `CODING_TOOLS` has no `execute_code`; `shell.py:202` `run_shell`
  spawns a fresh subprocess (no session persistence, no in-process tool calls). Lower
  priority — `run_shell` covers the basic `python -c` case. Fix: optional `execute_code`
  with a persistent session that can call other tools.

- **Calculator is arithmetic-only (no scientific/symbolic math)** — *present_weak · medium · small*
  Source: openclaw `SciCalculator/calculator.py:5-15` (sympy/statistics). Reality:
  `web/calculator.py:37-44` `_BIN_OPS` maps only `+,-,*,/,//,%,**`; `_eval_node:91-125`
  raises on `ast.Call`/`Name`/`Attribute`; no sympy/scipy in tree (re-verified). The
  AST-allowlist is an intentional safety choice but is a real breadth gap. Fix: a
  sympy-backed scientific mode behind a safe allowlist, or a separate tool.

- **web_fetch returns stripped HTML with no prompt-driven extraction** — *present_weak · medium · medium*
  Source: claude-code `WebFetchTool/prompt.ts:4-8`. Reality: `web/fetch.py:70-106` schema
  is url + max_chars only; `dispatch:279-302` regex-strips then truncates at 12k chars —
  no prompt param, no markdown, no model pass, so the relevant section of a large page is
  silently dropped. Fix: optional prompt + HTML→markdown + small-model extraction.

> Note: background-shell `run_in_background` was deliberately deprioritized per CHANGELOG
> D4 and is *not* reported here under builtin-tools (it appears once under tool-craft B12
> with that caveat flagged).

### 2.3 Tool implementation craft (edit/read/grep/bash)

- **read_file is text-only — no image/PDF/notebook reads** — *missing · medium · large · B2*
  Same defect as §2.2 item 1; flagged here because the *closing work* lives in
  `coding/files.py` and the reasoning-loop `ToolResult` path (it currently assumes string
  tool content). **De-dup target — implement once.**

- **edit_file does not require the file to have been read first** — *partial · high · small · B5/C5*
  Source: claude-code `FileEditTool.ts:275-287` ("File has not been read yet"). Reality:
  `files.py:433` only checks `state.is_stale(path)`; `_filestate.py:65-81` `is_stale`
  returns **False** when there is no record (re-verified), so an edit to a never-read file
  proceeds blind. Fix: when `state` is supplied and there is no record for an *existing*
  path, reject with a "read it first" error. **One of the highest impact/effort items.**

- **run_shell is synchronous-only — no background task mode** — *missing · medium · large · B12*
  Source: claude-code `BashTool.tsx:14,56-57`. Reality: `shell.py:61-62`
  `_DEFAULT_TIMEOUT=30`/`_MAX_TIMEOUT=60` hard caps; `dispatch_run_shell:280-312`
  `wait_for(communicate())` blocking; SIGKILL at 60s; no task registry. Legitimate
  builds/test-suites are unrunnable. Fix: background spawn + task-handle/poll surface.
  **Conflicts with the deliberate D4 deprioritization — confirm intent before building.**

- **read_file silently truncates instead of an actionable pre-read token gate** —
  *present_weak · low · small · B1*
  Source: claude-code `FileReadTool.ts:175-185,755-772`. Reality: `files.py:243-252`
  truncates at `MAX_READ_CHARS=60000`, sets `truncated:true` with no `next_offset`/guidance;
  `reasoning_loop.py:203` second 8k cap; no token estimate. The model re-reads the same
  head. Fix: surface "file too large — use offset/limit or search_files" + `next_offset`.

- **edit_file does no quote/CRLF/BOM normalization before matching** — *missing · medium · medium · B6*
  Source: claude-code `FileEditTool/utils.ts:27-36` + `FileEditTool.ts:208-214`. Reality:
  `files.py:439` `read_text(utf-8)` with no BOM/CRLF/quote handling; fuzzy tiers
  (`466-469`) only rstrip/strip; write-back (`517`) text-mode silently strips `\r`,
  converting CRLF files. Fix: match-time normalization (curly→straight, CRLF→LF, UTF-16
  BOM) + round-trip the original encoding/EOL on write.

- **Planned tier-4 block-anchor fuzzy matcher was never implemented** — *partial · low · medium · B4*
  Source: opencode-style anchor matcher, designed in `PLAN_AGENT_PARITY_IMPL.md:187-189`.
  Reality: `files.py:466-469` only rstrip/strip tiers; `_fuzzy_line_matches:353-385`
  requires every line equal. Lowest-impact item; tiers 1–3 already recover whitespace
  drift. Fix: add the anchor-first/last-line + interior-drift tier.

### 2.4 Context mgmt: compaction, freezing, caching, dedup

- **No Anthropic prompt caching (`cache_control: ephemeral`) or cache-usage accounting** —
  *missing · high · medium · A3*
  Source: claude-code `microCompact.ts:88-118`. Reality: `anthropic_provider.py:357-364`
  passes plain system/tools/messages; line 420 done chunk emits **no usage at all**; zero
  `cache_control` hits in non-test src (re-verified); `RESEARCH_AGENT_PARITY.md:22` A3 =
  "no cache headers". Fix: `cache_control:{type:ephemeral}` on the stable prefix + plumb
  `cache_read_input_tokens`/`cache_creation_input_tokens` into the done-event usage
  (`base.py` already reserves `cached_input_tokens`) + cache-break detection on compaction.
  **Unblocks the USD cost math gap.**

- **Compaction budget is a flat 120k constant, ignoring each model's context_length** —
  *present_weak · high · small*
  Source: claude-code `autoCompact.ts:33-91` (`getEffectiveContextWindowSize`). Reality:
  `reasoning_loop.py:221-224` flat `_CONTEXT_BUDGET=120000` (re-verified); `:1029` passes it
  unconditionally; `declarative.py:61/395-399` parses `context_length` (marked "advisory")
  but it has **zero** runtime consumers. Fix: `budget = context_length - reserved_output`.
  **Top impact/effort ratio of the whole set.**

- **No conversation history dedup** — *missing · medium · small · A6*
  Source: openclaw/opencode (200-turn window). Reality: only unrelated dedups exist
  (`context_assembler.py:252-276` toolbox tokens; `persona/life.py:717` topics;
  `agent_servicer.py:1174-1183` one-time leading-turn strip). Fix: content-hash dedup over
  recent N turns at history-extend, preserving tool_call/tool_result pairing.

- **Compacted/summarized history is never persisted as a durable boundary** —
  *partial · medium · medium · A5*
  Source: claude-code `autoCompact.ts:294-326` (`setLastSummarizedMessageId`). Reality:
  `reasoning_loop.py:1027-1041` `messages = _compact_history(...)` is a local var, never
  written back; resume (`agent_servicer.py:1112-1184`) replays raw journal turns; the
  journal schema has no boundary column; `PLAN_UI_FIXES.md:189` defers it. Result:
  steady-state context is O(turns), and the expensive summary call repeats every turn.
  Fix: persist summary block + boundary marker; load from boundary on next turn/resume.

- **Token estimation is a chars//4 heuristic, not a real tokenizer** — *present_weak · medium · medium*
  Source: claude-code `microCompact.ts:164-205` (per-block, image=2000), hermes
  `trajectory_compressor.py:362-374` (HF tokenizer). Reality: `reasoning_loop.py:332-338`
  `_estimate_tokens = chars//4`; `_estimate_chars:290-329` ignores images/files; thresholds
  (0.60/0.95) ride this; zero tiktoken/tokenizer deps. Badly mis-counts CJK (primary user
  base) and image-heavy chats. Fix: real tokenizer or per-block estimation with image
  sizing + CJK-aware ratio + padding.

### 2.5 Provider reliability: retry/backoff/fallback/cost/overflow

- **Exponential backoff / Retry-After not wired into the mainline model-call path** —
  *partial · high · medium · E1*
  Source: claude-code `withRetry.ts:170,530`. Reality: `with_retry` is used **only** by
  `codex_provider.py:440` (first-event); `agent_client/retry.py` is gRPC-transport-only;
  all 8 mainline adapters carry only `_retry_after_ms_from_exc` (header parse) — not
  `with_retry` (re-verified: `anthropic:700-787`, `openai:441-527`); `reasoning_loop.py:611,1274`
  call `chat_stream` directly; the error propagates to `_error_frame` and stops. Fix: wrap
  `chat_stream` attempts in a backoff loop consulting `RateLimitError.retry_after_ms` +
  `OverloadedError`; reuse the provider-agnostic `with_retry`. **Wiring, not new primitives.**

- **No model fallback chain on sustained overload** — *missing · high · large · E4*
  Source: claude-code `withRetry.ts:54,160` → `query.ts:894`. Reality: `AliasEntry`
  (`specs.py:173-186`) has no fallback field; `ModelRedirect` (`chat.py:114-145`) is
  alias-resolution only; `_retry.py:154-179` retries the **same** model on 529;
  `RESEARCH_AGENT_PARITY.md:99` E4 = `none`; `ModelRedirect.json` is docstring-only
  aspiration. **Same work as `loop-cross-model-fallback` — see §4.**

- **Context-overflow is terminal — no dynamic max_tokens shrink-and-retry** —
  *missing · medium · medium · E5*
  Source: claude-code `withRetry.ts:550,389-426`. Reality: `anthropic_provider.py:777` /
  `openai_provider.py:517` raise `ContextOverflowError` on 400+'context';
  `agent_servicer.py:3571` `context_overflow` is terminal (not in the retryable set at
  `3557`); no code parses the numeric limit or adjusts max_tokens (re-verified: zero
  shrink-retry symbols). Fix: parse "A + B > C", compute `availableContext = limit - input
  - buffer`, set `maxTokensOverride`, retry. Pre-emptive `_compact_history` reduces but
  does not eliminate this.

- **No per-model USD cost computation** — *present_weak · medium · small · E8*
  Reality: `_CostMeter` aggregates tokens but does "No pricing math"; `estimated_cost_usd`/
  `cost_status` columns written only by tests; `update_turn_cost` has no production caller;
  the sole USD figure is a hardcoded Haiku-rate fallback in `sessions_cost.py`;
  `TurnComplete` is built without cost. Fix: per-model `MODEL_COSTS` + `calculateUSDCost`
  (input/output/cache-read/cache-creation) into `TurnComplete` + `update_turn_cost`.
  **Depends on usage actually being emitted — see the prompt-caching gap.**

### 2.6 Provider auth: OAuth / refresh / per-agent models

- **OAuth-subscription chat omits the Claude Code identity headers/prompt** —
  *missing · high · small*
  Reality: `AnthropicProvider` does not send `anthropic-beta: oauth-2025-04-20`,
  `user-agent: claude-cli/...`, `x-app: cli`, nor the "You are Claude Code" system-prompt
  prefix, so OAuth-bearer chat likely 500s in prod. Fix: inject the four identity headers +
  the prompt prefix when the active credential is an OAuth bearer. **High impact, tiny edit.**

- **Anthropic on-use OAuth refresh is a no-op on the hot path; no 401 recovery** —
  *missing · high · medium · E6*
  Reality: `_refresh_sync` returns `None` inside a running loop; `chat_stream` has no async
  pre-refresh or 401-recovery for Anthropic (Codex has single-flight refresh). Fix: async
  pre-refresh of an expiring token + reactive 401 refresh-and-retry, mirroring Codex.

- **Claude Code credential import misses the macOS Keychain (CC ≥ 2.1.114)** —
  *present_weak · medium · small*
  Reality: import reads only `~/.claude/.credentials.json`; newer Claude Code stores creds
  in the macOS Keychain. Fix: add a `security find-generic-password` read path.

> Per-agent model **and** provider binding are fully wired (`agent_servicer.py:962-976`) —
> not a gap.

### 2.7 Permission model & pre-tool classification

- **Builtin permission gate has no `ask` action** — *missing · medium · medium · D2*
  Reality: `permission.py` is allow/deny/log only; `approval_gate.py` is a pure stub; a
  builtin tool can never trigger an interactive prompt. Fix: an `ask` verdict routed
  through the existing plugin `ApprovalGate` prompt-and-wait; unify the two disjoint gates.

- **Rules are tool-NAME-level only — no per-argument/command-pattern matching** —
  *missing · medium · medium · D2*
  Reality: no `run_shell(rm:*)` / prefix / wildcard / exact-command rules, no env-strip or
  compound-command split; the one `run_shell` guard is a hardcoded regex in `shell.py`.
  Fix: `ruleContent`/pattern matching + command splitting + env stripping, last-match-wins.

- **No pre-tool auto-approve classifier** — *missing · low · medium · D4* — optional, low
  priority for a chat bot.

- **No permission-mode concept or layered/org rule sources** — *missing · low · medium · D2*
  Reality: env-config-only, no `acceptEdits/plan/bypass/dontAsk`, no layered precedence or
  org `policyLimits`. Fix: a mode enum + layered rule sources.

> The context-aware D2 work (gate consulted with full caller context, subagent
> inheritance) is **DONE** (`agent_servicer.py:1993,1940`) — not a gap.

### 2.8 Hook system

- **No user-extensible / discoverable / decision-returning hook layer** —
  *missing · medium · large · D3*
  Source: hermes (`HOOK.yaml`/`handler.py` + `emit_collect`), claude-code (28 lifecycle
  events, blocking/mutating hooks). Reality: bus is observe-only; no `HookRegistry`/
  `HOOK.yaml`/`handler.py` (grep nothing); no `emit_collect`; `SessionStart/End/Reset`,
  `PreCompact/PostCompact`, `Stop` not emitted; `PLAN_AGENT_PARITY_IMPL.md` T3.2
  deliberately deferred the blocking half. Fix: hook discovery + `emit_collect` decision
  path + the missing lifecycle emit points. **Overlaps `loop-blocking-lifecycle-hooks`.**

> The internal hook *bus* (24 wire-stable variants, emitted at real lifecycle points) is
> at or beyond parity — not a gap.

### 2.9 Subagents / Task tool / coordinator-worker

- **Re-addressable teammates / coordinator mode with SendMessage** — see §2.1 (the primary
  entry). Subagents are fire-and-join + blackboard, `max_depth=1`.

- **Background-dispatch infra built but deliberately inert** — *partial · low · medium · D7*
  Reality: production `run_child_factory` raises; `run_in_background` unadvertised; the
  synthetic `<task-notification>` injection has no journal impl so it always skips. Fix:
  wire the factory + a journal-backed notification (partly intentional, pending a use case).

- **Model has no stop/cancel tool for children** — *missing · low · small · D5*
  Reality: only an operator HTTP kill route; no model-callable stop; no periodic in-flight
  child-progress summary. Fix: a `subagent_stop` tool + optional progress summary.

### 2.10 MCP client + server

- **Outbound client can't speak ecosystem Streamable-HTTP/SSE; per-tool manifests required;
  no OAuth / resources-as-tools / reconnect** — *partial · medium · large*
  Reality: `_normalise_ws_url` only rewrites `http→ws` and dials a websocket
  (corlinman-to-corlinman); real external servers (context7, mcp-remote) use
  Streamable-HTTP/SSE; calling an MCP tool requires a hand-authored `mcp`-kind plugin
  manifest per tool; no `ListMcpResources`/`ReadMcpResource` agent tools, no OAuth, no
  reconnection. Fix: add Streamable-HTTP + SSE transports, OAuth, auto `tools/list` →
  surface (drop per-tool manifests), resources/prompts agent tools, reconnection.

### 2.11 Skills system & marketplace/hub

- **Full skill body injected every turn — no progressive disclosure / model-driven
  selection / Skill tool** — *missing · medium · medium*
  Reality: `context_assembler.py:432` injects the full `body_markdown` of every skill_ref
  every turn; no description-in-context + on-demand body/file load; no foreground
  skill-authoring tool; multi-file bundled skills are never told their base dir, so their
  relative paths are unreachable. Fix: description-in-context + a `Skill` tool that loads
  body/files on demand + expose the base dir + foreground `skill_manage`.

- **Downloaded tarball hash never verified; no static security scan; dropped frontmatter** —
  *missing · medium · small*
  Reality: content hash fetched but not verified; no local static scan (openclaw
  `skill-scanner.ts` / hermes `skills_guard.py` have no counterpart); the Skill model drops
  `whenToUse`/`paths`/`platforms`/`model`/`effort`/`hooks`. Fix: verify hash on install + a
  static scan + parse the dropped fields.

### 2.12 Slash commands & user-pull commands

- **No skill-as-slash-command bridge; `register_command` has zero production callers** —
  *missing · medium · medium*
  Reality: hermes `/skill-name` loads skills into a turn; corlinman has none; the runtime
  `register_command()` exists but no production caller, so plugin/skill commands never reach
  the registry. Fix: bridge skills/plugins into the registry at load time.

- **No unknown-command notice** — *missing · low · small* — unrecognized `/foo` is silently
  forwarded to the LLM as chat. Fix: detect leading-slash-not-in-registry, emit a hint.

- **Access control is a single global admin env var** — *present_weak · low · medium*
  No DM-vs-group tier, no non-admin `user_allowed_commands` allowlist. Fix: per-scope tiers
  + allowlist. (Note: corlinman's allow-by-default polarity differs from hermes by design.)

- **No session-control slash commands (`/clear /reset /new /stop /model /retry`)** —
  *missing · medium · medium*
  Reality: the `/clear`/`/reset`/`/model` strings are only Next.js web-composer UI labels +
  i18n, not channel handlers; no destructive-command confirm gate. Fix: channel handlers +
  a confirmation gate.

### 2.13 Plugin lifecycle (skills + hooks + mcp bundles)

- **Plugin manifest `hooks`/`skill_refs` parsed-and-ignored; no persisted enable/disable;
  no marketplace; hot-reload unwired** — *missing · medium · large · D6*
  Reality: `manifest.py:254-255` `hooks`/`skill_refs` have zero consumers; no persisted
  enabled-state across scopes (a skill is on-disk or deleted; bundled = 409); `/admin/plugins`
  toggles only a live `McpAdapter` and writes nothing durable; plugin manifests have no
  watcher despite the doc claim. Fix: wire plugin-contributed skills+hooks; a persisted
  `enabledPlugins` map across user/project/local scopes; a manifest watcher (marketplace
  optional). **Depends on the hook-discovery work (§2.8) for the "hooks" half.**

### 2.14 Memory layers / RAG

- **No live vector/semantic recall — vector column never populated; query falls to BM25** —
  *missing · medium · large*
  Source: openclaw `KnowledgeBaseManager` (dense-vector KNN + RRF + rerank). Reality:
  `LocalSqliteHost.upsert` passes `None` for vector (`local_sqlite.py:688`); episodes write
  `embedding=NULL`; the embed sweep is only wired in tests; the EPA backfill runs on an
  empty column; README/CHANGELOG acknowledge "FTS5 today; HNSW+RRF+rerank on the roadmap".
  Fix: populate embeddings on the live write path + a vector/HNSW query stage with RRF
  fusion over BM25 + optional cross-encoder rerank.

### 2.15 Self-evolution / dreaming / GEPA / Darwin

- **EvolutionApplier never materializes; ShadowTester unscheduled; GEPA score_variants dead;
  no AgentDream** — *partial · low · large*
  Reality: the applier is store-backed only (flips status + audit rows, never mutates
  kb/tags/skills — the real apply lived in the now-deleted Rust crate); `ShadowTester` has
  no entrypoint, so high-risk kinds reach the operator ungated; `score_variants` has zero
  call sites; no free-association AgentDream analogue; Darwin's effectiveness half (40/100) +
  hill-climbing + LLM diff authoring deferred; no LLM TagMaster. Fix: materialize approved
  proposals; schedule `ShadowTester` to gate medium/high-risk; wire `score_variants`.
  AgentDream/Darwin-effectiveness are larger optional follow-ons.

### 2.16 Persona system: life-state, qzone, 格兰 tools

- **persona.* placeholder mirroring is dead; daily_publish injects no life-state** —
  *partial · medium · medium*
  Reality: `set_state` writes `state_json` but no `persona_resolver` is published on gateway
  `AppState` and `_context_metadata` never stamps `agent_id`, so `{{persona.life_*}}`/
  `{{persona.mood}}` resolve empty; the bundled grantley prompt uses none of the
  placeholders; `qzone.daily_publish` injects no life/diary and binds no `persona_id`. Fix:
  publish a `persona_resolver` + stamp `agent_id`; add `persona.*` to the grantley prompt;
  bind `persona_id` + inject life-state into `daily_publish`.

> The runtime tool surface (4 life + 4 qzone tools + 2 authoring tools + seed-pack chain)
> is at or above hermes parity — not a gap.

### 2.17 Messaging channels / platforms breadth

- **Inbound multimodal dropped on all four text channels (`attachments=[]`)** —
  *missing · high · medium*
  Reality: the live long-poll/gateway adapters yield `attachments=[]`, so user images/voice/
  docs are never forwarded (only QQ/OneBot wires inbound); Telegram never parses
  sticker/video/audio/animation; no sticker vision-description. Fix: wire inbound attachment
  extraction on Telegram/Discord/Slack/Feishu + parse Telegram media + sticker vision.
  **Downstream use depends on `vision-analyze` + multimodal read.**

- **`[MSG_BREAK]` multi-bubble token leaks to users** — *missing · medium · small*
  Reality: the seeded grantley persona emits `[MSG_BREAK]` but no sender splits on it, so
  the literal token leaks; both openclaw bots split. Fix: split outbound text on
  `[MSG_BREAK]` in each channel sender. **User-visible bug right now.**

- **No media-group/album buffering or merge-debounce; no ambient auto-chat / per-chat
  binding** — *missing · low · medium*
  Fix: album/merge-debounce buffer; optional ambient participation + per-chat agent binding.

> The 7-platform breadth itself (Telegram/QQ/Discord/Slack/Feishu/QQ-Official/WeChat) is in
> strong shape — not a gap.

### 2.18 Voice / TTS / STT / multimodal

- **No agent TTS tool; no STT of inbound voice; realtime transcript→chat unwired; no mic
  client** — *missing · medium · large*
  Reality: no agent-callable TTS (overlaps the builtin-tools TTS gap); `MessageTranscribed`
  fires an empty stub; the realtime `transcript_sink=None`; no browser-mic/CLI client
  connects to `/v1/voice`; Telegram `send_voice` exists but nothing generates the OGG and
  there is no auto-TTS reply. Fix: STT of inbound voice + wire `transcript_sink` to chat + a
  mic client + an auto-TTS reply path. **Shares the TTS backend with builtin-tools.**

> The server-side realtime voice gateway (`/v1/voice`) and image generation are mature and
> *ahead* of the claude-code reference — not gaps.

### 2.19 Goals / planning / cron / scheduling

- **`run_agent` cron action is wired-but-dead; no channel delivery, catch-up, or rich
  schedules** — *partial · medium · medium*
  Reality: the config loader parses `run_agent` but the dispatcher emits
  `unsupported_action` (`runner.py:523`), so no scheduled job can run the agent — the entire
  point of an LLM cron job; no channel delivery; no missed-run catch-up across restarts;
  only raw cron strings (no `every 30m`); no webhook triggers. Fix: implement the `run_agent`
  branch (run a real agent turn) + channel delivery + catch-up/grace + ergonomic schedules.
  **`run_agent` is the highest-value item here.**

> The subprocess + `run_tool` actions and the cron parser are solid (parity with the Rust
> source) — not gaps.

### 2.20 Identity / user model / pairing / trust

- **corlinman-identity is shipped-but-unwired; and there is no inbound
  authorization/pairing gate** — *missing · medium · medium*
  Reality: `AdminState.identity_store` is `Any | None = None` (`state.py:146`) and never
  assigned; the entrypoint never imports `corlinman_identity`; `resolve_or_create` is never
  called; the redeem path is never wired to a channel; `sweep_expired_phrases` is never
  scheduled — so all four `/admin/identity*` routes 503. Separately, `router.py` does only
  keyword filtering + rate-limiting — no allowlist or pairing, so **any** sender on an
  enabled channel is processed (behind hermes). Fix: (a) open/assign the identity store +
  wire `resolve_or_create` + the redeem path + schedule the sweep; (b) add an
  authorization/pairing gate (allowlist + DM code pairing, `unauthorized_dm_behavior`) in
  the channel router.

### 2.21 Observability: tracing / cost / metrics

- **8 of 10 Prometheus metric families defined-but-unwired; OTel spans don't cover
  loop/tools/subagents** — *partial · low · medium*
  Reality: only `HTTP_REQUESTS` + `LOG_FILES_REMOVED` ever `.inc()`/`.observe()` in prod;
  `AGENT_GRPC_INFLIGHT`/`APPROVALS_TOTAL` left as explicit Python TODOs; OTel spans cover
  only the HTTP/gRPC chat boundary, none inside the loop/tool dispatch/supervisor; no
  Langfuse-style per-call export. (Cost-is-token-only is tracked under §2.5.) Fix: wire the
  8 dormant families at their lifecycle points + add loop/tool/subagent spans.

### 2.22 Config / settings sync / hot-reload

- **Admin config-reload route + variable-cascade hot-reload + ConfigChanged hook are all
  built-but-disconnected** — *partial · low · small*
  Reality: `/admin/config/reload` + `config_swap_fn` are dead (`admin_b` extras never
  populated); the only prod `VariableCascade` is `hot_reload=False` and `start_watching` is
  never called, so prompt-fragment edits never go live mid-session; `ConfigWatcher` is
  constructed without a `hook_emitter`/`validator`, so `HookEvent.ConfigChanged` never fires;
  `/admin/config/schema` is a stub. Fix: populate `admin_b` extras; construct the prod
  cascade with `hot_reload=True` + `start_watching`; pass `hook_emitter`+`validator`;
  implement the schema route. **Pure wiring of existing machinery.**

> The `ConfigWatcher` core (fs-watch, debounce, SIGHUP, atomic swap, section diff) exceeds
> both references — not a gap.

### 2.23 Canvas / advanced rendering / output styles

- **Canvas Host has no web-UI consumer; chat transcript has no syntax/math/mermaid; no
  output-styles feature** — *missing · low · medium*
  Reality: no `/canvas` page; the UI never hits `/v1/canvas` (only tests + a Swift client);
  the chat path (`markdown-message.tsx`/`artifact-panel.tsx`) has no highlighting/KaTeX/
  mermaid (mermaid + markdown previews are "not yet wired" placeholders); no streaming inline
  HTML preview or PNG export; claude-code output styles (`.claude/output-styles/*.md`) are
  entirely absent. Fix: a `/canvas` consumer + upgrade the chat render path; optional inline
  preview/PNG export; optional output-styles loader.

> The backend Canvas `Renderer` (5 artifact kinds, themed HTML, SSE) is mature and at parity
> with its Rust origin — not a gap.

---

## 3. Cross-cutting themes

1. **"Built-but-not-wired" is the dominant failure mode.** A striking share of gaps are
   *not* missing code — they are capable machinery left disconnected: retry primitives
   exist but don't wrap the model call; `context_length` is parsed but never consumed;
   the identity store is implemented but never assigned; the variable-cascade hot-reload is
   `hot_reload=False`; `run_agent` is parsed but the dispatcher refuses it; 8 metric families
   are defined but never incremented; `register_command()` has no callers; the
   EvolutionApplier writes audit rows but never materializes. **These are the cheapest,
   safest, highest-leverage fixes** and they cluster in providers-reliability,
   context-compaction, config-settings, goals-cron, identity-trust, and observability.

2. **Multimodal INPUT is the single biggest *capability* hole**, and it spans three
   subsystems as one logical chain: channels drop inbound attachments → `read_file` can't
   surface images/PDFs → there's no `vision_analyze` tool. Closing any one alone has limited
   value; they pay off together.

3. **The error-classification half is excellent; the error-*action* half is missing.**
   Every adapter maps 429/503/529/overflow into a typed taxonomy and parses `Retry-After`,
   but nothing retries, falls back, shrinks max_tokens, or computes cost from the usage. The
   reliability subsystem is "diagnose perfectly, act not at all".

4. **CJK-first user base meets ASCII-first heuristics.** The chars//4 token estimate and the
   `[MSG_BREAK]` leak both bite hardest for the actual (Chinese QQ/Telegram) users.

5. **Cost depends on caching.** USD cost math (`cost-no-usd-math`) and prompt caching
   (`prompt-caching-cache-control`) are coupled — the Anthropic done-chunk emits no usage at
   all today, so caching must land (or at least usage plumbing) before per-model cost is real.

### De-duplicated gap pairs (count once)
- `loop-cross-model-fallback` (agent-loop) ≡ `no-model-fallback-chain` (providers) → **one** fallback-chain work item.
- `read-multimodal-image-pdf-notebook` (builtin-tools) ≡ `read-image-pdf-notebook` (tool-craft) → **one** multimodal-read work item.
- `text-to-speech-tool` (builtin-tools) shares its backend with `voice-no-tts-stt-tools` (voice) → **one** TTS backend, two surfaces.
- `loop-blocking-lifecycle-hooks` (agent-loop) overlaps `hooks-no-discovery` (hooks) → same blocking-hook substrate.

---

## 4. Prioritized fill plan

Ordered by dependency and impact/effort. Each package lists the corlinman files it touches.
**File-disjoint packages run in parallel** (grouped into waves). Risk tags: **[SAFE/LOW-RISK]**
= additive or pure wiring, no behavior change to existing paths; **[RISKY/LARGE]** = touches
hot paths, schemas, or in-flight areas.

> **Red-CI-gate caveat (per MEMORY `project_audit_loop`):** the CI gate is intentionally red
> from deferred ruff/mypy debt — do **not** mass-sweep lint while making these changes; keep
> each PR's diff tight to the package(s) below so the gate signal stays interpretable.
> **In-flight areas to avoid colliding with:** the Rust→Python migration (`rust/` deletion —
> the EvolutionApplier "real apply" and any cancel/session work touch ex-Rust surfaces) and
> the active multi-agent fix work (`audit/PLAN_multiagent_fix.md`, `multiagent_confirmed.json`
> — WP12 below). Coordinate before touching `supervisor.py`/subagent dispatch.

### Wave A — cheap wiring + user-visible bug (all [SAFE/LOW-RISK], fully parallel)

1. **[SAFE] Model-aware compaction budget** — `corlinman-agent/reasoning_loop.py`
   (+ a catalog lookup to `corlinman-providers/declarative.py` `ModelSpec.context_length`).
   Replace flat `_CONTEXT_BUDGET` with `context_length - reserved_output`. *Highest ratio.*

2. **[SAFE] Read-before-edit guard** — `corlinman-agent/coding/files.py` +
   `coding/_filestate.py` (add `has_record`). Reject edits to unread existing files.

3. **[SAFE] `[MSG_BREAK]` splitting** — `corlinman-channels/` senders (per-platform send
   path). Split outbound text into bubbles. *Fixes a live token leak.*

4. **[SAFE] OAuth identity headers + prompt prefix** — `corlinman-providers/anthropic_provider.py`.
   Inject the four headers + "You are Claude Code" prefix on OAuth-bearer chat.

5. **[SAFE] Config hot-reload wiring** — `corlinman-server/.../gateway/` (`admin_b` state
   extras, the prod `VariableCascade` construction, `ConfigWatcher` `hook_emitter`/`validator`,
   `/admin/config/schema`). Pure wiring of built machinery.

6. **[SAFE] Pre-read token-gate message + `next_offset`** — `corlinman-agent/coding/files.py`
   (read path only; disjoint from WP2's edit path *within* the file — sequence after WP2 if
   the same person, else split by function). Actionable truncation guidance.

These six touch six distinct files/packages and can land as six independent PRs.

### Wave B — reliability actions + cost (mostly [SAFE], one [RISKY])

7. **[SAFE] Wire retry/backoff into the model call** — `corlinman-agent/reasoning_loop.py`
   (wrap `chat_stream` at `:611,:1274` in a backoff loop reusing `corlinman-providers/_retry.py`).
   Honors `retry_after_ms`/`OverloadedError`. No new primitives.

8. **[SAFE] Prompt caching + usage plumbing** — `corlinman-providers/anthropic_provider.py`
   (+ `base.py` `cached_input_tokens` is already reserved). Add `cache_control: ephemeral` to
   the stable prefix; emit `cache_read`/`cache_creation` in the done chunk. *Big cost win,
   unblocks WP9.*

9. **[SAFE] Per-model USD cost** — `corlinman-server/.../sessions_cost.py` + the
   `_CostMeter`/`update_turn_cost`/`TurnComplete` path. Add `MODEL_COSTS` + `calculateUSDCost`.
   **Depends on WP8** (usage must be emitted first).

10. **[RISKY/LARGE] Context-overflow shrink-and-retry** — `corlinman-providers/anthropic_provider.py`
    + `openai_provider.py` + `corlinman-server/.../agent_servicer.py` (`_error_frame` retryable
    set). Parse the limit, set `maxTokensOverride`, retry. Touches the hot error path —
    test against real 400s.

11. **[RISKY/LARGE] Model fallback chain** — `corlinman-providers/specs.py` (alias
    `fallback_model`) + `gateway/routes/chat.py` (`ModelRedirect`) + `agent_servicer.py`
    (overload counter + turn-replay-with-new-provider). Cross-cutting; **de-dup of the
    agent-loop entry.** Sequence after WP7.

WP7/8 are file-disjoint from WP10/11's hot-path edits and can start in parallel; WP9 waits on
WP8; WP11 waits on WP7.

### Wave C — multimodal input chain ([RISKY/LARGE], implement as one coordinated chain)

12. **[RISKY/LARGE] ToolResult content-block plumbing + multimodal `read_file`** —
    `corlinman-agent/coding/files.py` + `corlinman-agent/reasoning_loop.py` (the ToolResult
    path currently assumes string content). Single de-dup of the two read gaps. **Largest
    structural change in the set** — changes a core contract; isolate it.

13. **[SAFE] `vision_analyze` tool** — new module under `corlinman-agent/image/` +
    `BUILTIN_TOOLS`/`_builtin_tool_schemas` in `agent_servicer.py`. Additive.

14. **[SAFE] Inbound attachment extraction on text channels** — `corlinman-channels/`
    (Telegram/Discord/Slack/Feishu adapters). Additive per-adapter; pays off with WP12/13.

WP13 and WP14 are additive and parallel once WP12's content-block path exists.

### Wave D — agent-tool breadth + dead-wire revivals (mixed, mostly parallel)

15. **[SAFE] `run_agent` cron dispatcher** — `corlinman-server/.../scheduler/runner.py:523`
    (implement the branch) + optional channel delivery. *Highest-value goals-cron item.*

16. **[SAFE] Wire the identity store + authorization/pairing gate** —
    `corlinman-server/.../gateway/` (`state.py` store assignment, entrypoint import,
    `resolve_or_create` per message, redeem wiring, sweep schedule) + `channels/router.py`
    (allowlist + DM pairing). Two halves; the store-wiring half is pure revival.

17. **[SAFE] Agent-callable `memory` + `session_search` tools** — new tools over the existing
    `LocalSqliteHost` FTS5 store + `BUILTIN_TOOLS`. Additive.

18. **[SAFE] TTS tool + STT/transcript wiring** — `corlinman-agent` (TTS tool) +
    `corlinman-server/.../routes_voice/` (`transcript_sink`, STT of inbound voice). Shared
    backend, two surfaces.

19. **[SAFE] History dedup** — `corlinman-agent/reasoning_loop.py` / `context_assembler.py`
    (content-hash over recent turns). Small, additive. *Sequence after Wave A's
    reasoning_loop edits to avoid churn in the same file.*

20. **[SAFE] CJK/multimodal tokenizer** — `corlinman-agent/reasoning_loop.py`
    (`_estimate_tokens`). Swap heuristic for a real tokenizer or per-block estimation.
    *Same file as WP1/7/19 — sequence within that file, don't parallelize.*

### Wave E — large optional / lower-priority ([RISKY/LARGE], revisit on concrete need)

21. **[RISKY/LARGE] Blocking/discoverable hooks** — `corlinman-hooks/` + `corlinman-agent`
    loop + `agent_servicer.py`. Deliberately deferred (T3.2) — build on a concrete use case.
22. **[RISKY/LARGE] Coordinator / SendMessage / re-addressable children** — `corlinman-subagent/`
    + `corlinman-agent/subagent/`. **Collides with the in-flight multi-agent fix work —
    coordinate first.**
23. **[RISKY/LARGE] Live vector recall** — `corlinman-tagmemo`/`memory-rag` (`local_sqlite.py`
    embed write path + HNSW/RRF query stage). Large; on the acknowledged roadmap.
24. **[RISKY/LARGE] EvolutionApplier materialization + ShadowTester scheduling** —
    `corlinman-evolution-engine`. **Touches ex-Rust surfaces being deleted — coordinate with
    the migration.**
25. **[SAFE] Smaller tail** — scientific calculator (`web/calculator.py`), web_fetch prompt
    extraction (`web/fetch.py`), edit CRLF/BOM normalization (`coding/files.py` — sequence
    after WP2/12), tier-4 block-anchor matcher, slash session-control commands, skill
    progressive-disclosure + hash verification, plugin enable/disable persistence, observability
    metric wiring, canvas UI consumer + output styles. Mostly additive; schedule opportunistically.

---

## 5. Subsystems judged already at parity (no scored gap, or gaps are edge-only)

None of the 23 subsystems is fully gap-free, but several are *at or beyond* parity on their
core and have only edge/orchestration deltas (already noted inline above as "not a gap"):

- **Agent-loop inner mechanics** — multi-round tool calling, mid-turn injection, cancellation,
  observability stream are at parity; only orchestration edges remain.
- **Context-compaction A1/A2/A4** — token-aware compaction, tool-result freeze, file-read cache
  all shipped.
- **Config-settings ConfigWatcher core** — exceeds both references; only wiring gaps.
- **Canvas backend Renderer** — at parity with its Rust origin.
- **Channels platform breadth** — 7 platforms fully present.
- **Scheduler core** (subprocess/run_tool + cron parser) — at parity with the Rust source.
- **Persona-life tool layer** — at or above hermes.
- **Voice server-side realtime gateway + image generation** — *ahead* of the claude-code reference.
- **Permission context-awareness (D2 caller-context + subagent inheritance)** — done.
- **Internal hook bus** (24 variants, emitted at real points) — at/beyond parity.
- **Provider error classification + per-agent model/provider binding** — done.

---

## 6. Implementation log

### Wave A — SHIPPED 2026-05-31 (4 of 6 items)

| WP | Item | Status | Files | Tests |
|----|------|--------|-------|-------|
| A1 | Model-aware compaction budget | ✅ done | `corlinman-agent/reasoning_loop.py`, `corlinman-providers/declarative.py` | 5 new in `test_reasoning_loop.py` + 1 in `test_declarative.py` |
| A2 | Read-before-edit guard | ✅ done | `corlinman-agent/coding/files.py`, `coding/_filestate.py` | 5 new in `test_coding_tools.py` |
| A6 | Pre-read truncation guidance (`next_offset`/hint) | ✅ done | `corlinman-agent/coding/files.py` | 1 new in `test_coding_tools.py` |
| A3 | `[MSG_BREAK]` bubble splitting | ⏳ deferred | `corlinman-channels/` (7 senders in service.py) | — |
| A4 | OAuth identity headers + prompt prefix | ⏳ deferred | `corlinman-providers/anthropic_provider.py` | — |
| A5 | Config hot-reload wiring | ⏳ deferred | gateway admin_b / VariableCascade / ConfigWatcher | — |

Verification: full `corlinman-agent` + `corlinman-providers` suites **948 passed, 2 skipped**;
server tool-execution subset **99 passed**; ruff clean on all changed files.

Notes:
- A1's model-aware sizing only activates for providers exposing `context_window`
  (today: `DeclarativeProvider` → the `openai_compatible` national clouds).
  Mainline anthropic/openai/codex adapters keep the flat 120k default (no
  behavior change) until a per-model window map is added.
- A2 is a **production behavior change** (gated by `CORLINMAN_REQUIRE_READ_BEFORE_EDIT`,
  default on).
- A4 (OAuth identity headers) deferred pending confirmation — it makes corlinman
  present Claude-Code client identity on OAuth-bearer chat; in-scope per
  `docs/PLAN_PROVIDER_AUTH.md` but worth an explicit go-ahead.
