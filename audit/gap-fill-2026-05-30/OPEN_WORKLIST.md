# OPEN Worklist — Agent-Parity Gap Fill (synthesis)

Date: 2026-05-31
Source: verification verdicts for ~57 documented gaps + 13 D-defects + newly discovered gaps.

## Executive summary

All 13 subagent/supervisor D-defects (D1–D13) verified **FIXED** — no work remains there (only two optional cosmetic doc-drift cleanups in `dispatcher.py:12-16` and `runner.py` docstrings, plus an optional `test_dispatcher.py` shutdown test). They are **not** in this worklist.

The remaining open surface is **gap work**: items verified OPEN or PARTIAL plus the newly discovered gaps. After dedup (see merges below) there are **~41 distinct open items**.

The dominant constraint is file contention. Two files — `agent_servicer.py` and `gateway/lifecycle/entrypoint.py` — are the wiring spine for the majority of OPEN/PARTIAL "code-exists-but-never-threaded" gaps (memory host, persona resolver, identity store, hook runner, agent_runner_fn, config watcher, subagent dispatcher). To keep lanes strictly file-disjoint, **all edits to those two files (plus `gateway/core/state.py` and `routes_admin_a/state.py`) are consolidated into a single Boot/Servicer Wiring lane (lane-wire)**, and leaf lanes that own their own package-internal files depend on it.

Likewise `reasoning_loop.py`, `anthropic_provider.py`, `coding/files.py`, `coding/search.py`, `local_sqlite.py`, `commands.py`, and `channels/service.py` are each owned by exactly one lane, with all touching work sequenced internally.

### Dedup / merges applied
- `loop-cross-model-fallback` == `no-model-fallback-chain` → merged (sustained-overload escalation, fallback-chain config). Also merged with new_gap "Sustained provider overload (529/503) never escalates" (same branch).
- `read-multimodal-image-pdf-notebook` == `read-image-pdf-notebook` → merged (PDF + ipynb branches in `coding/files.py`).
- `text-to-speech-tool` shares its backend with `voice-no-tts-stt-tools` → TTS tool implemented once; STT/transcript-sink wiring tracked together in the voice lane.
- `loop-blocking-lifecycle-hooks` overlaps `hooks-no-discovery` → merged into one hooks lane.
- New_gap "Skill allowed-tools never enforced" appears 3×(permissions/skills) → single item in lane-wire (servicer tool-catalog narrowing).
- New_gap "USD cost ignores cache_creation" + `cost-no-usd-math` → both land in the reasoning-loop lane.

## Lane table

| Lane | Title | Risk | Decision | Depends on | Effort |
|------|-------|------|----------|------------|--------|
| lane-search | search_files quality (content/glob modes) | SAFE | BUILD | — | M |
| lane-edit-files | read/edit file fidelity (pdf/ipynb, CRLF/BOM, anchor tier, diff) | MEDIUM | BUILD | — | L |
| lane-anthropic | Anthropic provider: caching, reliability, OAuth | MEDIUM | BUILD | — | M |
| lane-calc-web | Calculator + web-fetch + untrusted-content wrapper | MEDIUM | BUILD | — | M |
| lane-reasoning-loop | Reasoning loop: fallback, budget, dedup, cost, spill, est | MEDIUM | BUILD | lane-anthropic(soft) | L |
| lane-new-tools | New agent tools: TTS, memory_write, code REPL (packages only) | SAFE | BUILD | — | M |
| lane-channels-inbound | Channels inbound multimodal + attribution + reply-to | MEDIUM | BUILD | — | L |
| lane-commands | Slash commands: dir loader, unknown notice, ACL | MEDIUM | BUILD | — | M |
| lane-memory-rag | Memory recall: decay, residual-pyramid, relevance-auto | MEDIUM | BUILD | — | L |
| lane-hooks | Hook discovery + decision bus + lifecycle events | RISKY | BUILD | — | L |
| lane-mcp | MCP inbound notifications + tool annotations | SAFE | BUILD | — | M |
| lane-skills-meta | Skill model fields (disable-model-invocation, trust-scan, frontmatter) | SAFE | BUILD | — | M |
| lane-wire | Boot/servicer wiring spine (entrypoint + agent_servicer + state) | MEDIUM | BUILD | leaf lanes (soft) | L |
| lane-session-prims | Session/cancel primitives | SAFE | BUILD | — | S |
| lane-coordinator | Coordinator SendMessage / durable mailbox | RISKY | DEFER | — | L |
| lane-evolution | Evolution applier materialize | RISKY | DEFER | — | L |
| lane-memory-vector | Vector recall on memory write/query hot path | RISKY | DEFER | — | L |
| lane-mcp-outbound | Streamable-HTTP/SSE outbound transports | RISKY | DEFER | — | L |
| lane-session-control | /clear /reset /stop /model /retry session commands | RISKY | DEFER | lane-commands | M |

## Per-lane detail

### lane-search (SAFE, BUILD)
Owns `coding/search.py`. Closes the 4 search_files new_gaps: add `output_mode` (files_with_matches default), `-i`/case-insensitive, `-A/-B/-C` context, glob/type filter; stop stripping indentation + cap-truncate (return verbatim line, explicit truncation marker); sort name/glob mode by mtime desc + `truncated` flag. Test: unit tests on a tmp tree for each mode/flag; assert indentation preserved.

### lane-edit-files (MEDIUM, BUILD)
Owns `coding/files.py`. PDF (+optional `pages`) and `.ipynb` read branches (merged `read-image-pdf-notebook`); CRLF/BOM/curly-quote normalization at match time + round-trip encoding on write-back; tier-4 block-anchor matcher (first+last line, unique span); compact unified-diff/snippet in edit/write result. Test: mixed-EOL/BOM fixtures, anchored-edit fixture, pdf/ipynb fixtures.

### lane-anthropic (MEDIUM, BUILD)
Owns `anthropic_provider.py`. Emit `is_error:true` on `tool_result` blocks (needs is_error carried from loop — coordinate marker key with lane-reasoning-loop, but the provider edit is self-contained); add `cache_control` on trailing tools block (stay ≤4 markers); parse `anthropic-ratelimit-unified-reset` into `RateLimitError.reset_at_ms`; add `_ensure_fresh()` single-flight OAuth refresh + reactive 401 retry (provider-auth-anthropic-refresh-noop). Test: provider unit tests with stubbed responses/headers.

### lane-calc-web (MEDIUM, BUILD)
Owns `web/calculator.py`, `web/fetch.py`, `web/search.py`, `web/_common.py`, and a NEW `web/external_content.py`. Scientific calculator allowlist (sqrt/sin/log/pi/e via AST whitelist); web_fetch `prompt` param + HTML→markdown + `next_offset` paging; untrusted-content wrapper (randomized markers + security notice + suspicious-pattern detector) wrapping fetch/search output. Test: AST safety tests, fetch envelope shape, wrapper round-trip + marker-spoof sanitize.

### lane-reasoning-loop (MEDIUM, BUILD)
Owns `reasoning_loop.py`. Sequence internally: (1) sustained-overload→fallback escalation + thread `fallback_models` (needs `specs.py` AliasEntry field — owns `specs.py` too); (2) `is_error` carried through `_extend_with_tool_round`; (3) duplicate user/assistant turn dedup; (4) multimodal token-estimate charge; (5) cost: persist to DoneEvent + cache_creation rate; (6) generalized tool-result spill-to-disk handle + per-turn budget; (7) optional BudgetTracker auto-continue (gated). Also owns `specs.py`. Test: loop unit tests for fallback-on-overload, dedup, estimate, cost math.

### lane-new-tools (SAFE, BUILD)
Owns NEW `image/tts.py` (text_to_speech tool), `memory/tools.py` (add memory_write tool), NEW `coding/repl.py` (opt-in execute_code), and `coding/__init__.py`. Build the tool modules/schemas only; registration into BUILTIN_TOOLS + dispatch lives in lane-wire (agent_servicer.py). Closes text-to-speech-tool (TTS half of voice gap), memory-write-tool, code-execution-repl, calculator stays in lane-calc-web. Test: dispatch unit tests per tool.

### lane-channels-inbound (MEDIUM, BUILD)
Owns `channels/telegram.py`, `discord.py`, `slack.py`, `feishu.py`, `qq_official.py`, `telegram_media.py`, `common.py`, `router.py`. Populate `InboundEvent.attachments` per platform (relax text-required guard); media-group/album debounce; carry sender display-name + reply-to-text onto RoutedRequest. NOTE: `channels/service.py` is contended and owned here too (album flush, attribution prefix). Downstream usefulness depends on lane-edit-files (read multimodal) but wiring is independent. Test: per-adapter parse fixtures; debounce timing test.

### lane-commands (MEDIUM, BUILD)
Owns `channels/commands.py`. Commands-dir loader (`*.md` + frontmatter, `$ARGUMENTS`/`$N` substitution); unknown-command notice helper; SlashAccessPolicy (admin/DM/allowlist tiers); skills→command bridge (register_command from skill frontmatter). NOTE `router.py` is owned by lane-channels-inbound — keep the unknown-notice hook in commands.py and have lane-channels-inbound call it, OR sequence: this lane lands the helper, lane-channels-inbound wires the call. Test: loader fixture, ACL matrix, $ARGUMENTS substitution.

### lane-memory-rag (MEDIUM, BUILD)
Owns `corlinman-memory-host/local_sqlite.py`, `corlinman-memory-host/types.py`, `corlinman-tagmemo/boost.py`, `corlinman-agent/rag/epa_backfill.py`. Query-time exponential time-decay re-rank (opt-in on MemoryQuery); wire residual-pyramid `dynamic_boost`/`build_pyramid` into query reading `chunk_epa`. Auto per-turn relevance recall is a servicer change → lives in lane-wire. Excludes vector recall (deferred). Test: query ranking tests with synthetic dated chunks.

### lane-hooks (RISKY, BUILD)
Owns `corlinman-hooks/bus.py`, `corlinman-hooks/event.py`, `corlinman-hooks/runner.py`. `emit_collect` decision path; new lifecycle events (SessionStart/End/Reset, PreCompact/PostCompact, Stop); file-based HOOK.yaml/handler.py discovery. The dispatch-side wiring (PreToolDispatch decision in `_dispatch_builtin`, turn-end Stop veto in reasoning_loop) is cross-file → the bus/runner/event work is here; the dispatch hook-calls land in lane-wire + lane-reasoning-loop. RISKY but does NOT collide with Rust migration or live subagent subsystem, so BUILD. Test: emit_collect aggregation unit tests, discovery loader test.

### lane-mcp (SAFE, BUILD)
Owns `corlinman-mcp-server/dispatch.py`, `corlinman-mcp-server/types.py`, `corlinman-mcp-server/tools.py`, `corlinman-providers/plugins/manifest.py`. Inbound `*/list_changed` notifications + server→client send channel in connection_loop; ToolAnnotations (readOnlyHint/destructiveHint/title/outputSchema) on ToolDescriptor + manifest Tool. (NOTE: `transport.py` also owned here.) Excludes outbound transports (deferred). Test: capability serialization + notification emit tests.

### lane-skills-meta (SAFE, BUILD)
Owns `corlinman-skills-registry/skill.py`, `corlinman-skills-registry/parse.py`, `corlinman-agent/skills/card.py`, `corlinman-agent/skills/registry.py`, `system/skill_hub/installer.py`, `system/skill_hub/client.py`. Add `disable_model_invocation` field; carry dropped frontmatter (whenToUse/paths/platforms/model/effort/hooks); trust-scan (sha256 content_hash verify + static scan). allowed-tools enforcement is a servicer/context change → lane-wire. Progressive-disclosure body-on-demand needs context_assembler+servicer → lane-wire. Test: parse round-trip, hash mismatch raise.

### lane-wire (MEDIUM, BUILD)
Owns `gateway/lifecycle/entrypoint.py`, `agent_servicer.py`, `gateway/core/state.py`, `routes_admin_a/state.py`, `routes_admin_b/config.py`, `scheduler/runner.py`, `scheduler/cron.py`, `gateway/grpc/placeholder.py`, `persona/default_grantley.md`, `scheduler/builtins/qzone_daily.py`, `gateway/services/chat_bootstrap.py`. The wiring spine — sequence internally:
- memory_host on AppState + servicer self._app_state (memory-and-session-search) + register memory_write/TTS/REPL tools into BUILTIN_TOOLS + dispatch.
- persona_resolver on AppState + agent_id in _context_metadata + grantley placeholders + qzone life block (persona-life-resolver-dead).
- identity_store assign + un-503 routes + resolve_or_create on inbound (identity-unwired).
- agent_runner_fn in lifespan + channel delivery + cron grammar/catch-up (goals-cron-run-agent-dead).
- config_watcher hook_emitter+validator + VariableCascade hot_reload + /admin/config/schema (config-admin-reload-dead).
- HookRunner construction + run_post_tool/run_notification at lifecycle points; PreToolDispatch decision call (depends lane-hooks).
- subagent_stop tool + dispatcher shutdown wiring (subagents-no-stop-tool).
- permissions: ASK action + per-arg rules + classifier + PermissionMode (owns `permission.py`? — no, permission.py is corlinman-agent: see note) ; group-sender + reply-to prefix in agent-facing content; web-untrusted wrap call; skill allowed-tools tool-catalog narrowing; progressive-disclosure Skill tool + catalog-only inject; auto relevance recall; metrics inc() at dispatch/approval points.
- **NOTE on permission.py**: `permission.py` lives in corlinman-agent and is disjoint from agent_servicer.py — assign it to lane-wire as a co-owned file so the ASK/per-arg/mode logic and its dispatch consumption stay in one lane (they are tightly coupled). Also co-owns `context_assembler.py` (progressive-disclosure inject + skill allowed-tools) and `approval_gate.py`.
Test: boot smoke test (lifespan up/down), servicer dispatch tests per newly-wired tool, permission ASK→approval bridge test.

### lane-session-prims (SAFE, BUILD)
Owns `corlinman-agent/session.py`, `corlinman-agent/cancel.py`. Implement Session dataclass + `cancel.combine`. Purely additive new primitives. Test: combine() unit test (two events → one signal); Session construction.

## DEFERRED (in-flight collision)

These are RISKY AND collide with the live subagent/supervisor subsystem or the Rust→Python migration. Hold until those settle (per memory: subagent subsystem in-flight; Rust→Python full port active through rust/ deletion).

- **lane-coordinator** (orchestration-coordinator-sendmessage): durable mailbox + relaxed join-only spawn lifecycle + child registry. Changes the spawn lifecycle contract — collides head-on with the in-flight subagent/supervisor subsystem.
- **lane-evolution** (evolution-applier-no-materialize): real kb/filesystem mutators on a path that must stay reversible (AutoRollback). Collides with the in-flight Rust→Python migration (logic lived in the deleted ~6600 LoC Rust evolution_applier crate).
- **lane-memory-vector** (memory-no-vector-recall): embedding step on the memory read/write hot path. New network dep inside upsert/query; overlaps the Rust corlinman-vector SqliteStore migration boundary. (Distinct from lane-memory-rag, which is BM25-side decay/boost only — keep them separate so the SAFE re-rank work isn't blocked.)
- **lane-mcp-outbound** (mcp-no-streamable-http-sse): Streamable-HTTP/SSE outbound transports + manifest-optional tool resolution. Changes the plugin-invoker tool-resolution contract on a path the Rust→Python migration also touches. (Inbound notification/annotation half is SAFE → lane-mcp.)
- **lane-session-control** (slash-no-session-control): /clear /reset /new /stop /model /retry reach into agent session/journal, cancellation, and model-binding — the same surfaces touched by the in-flight migration + subagent work. Depends lane-commands. Destructive; needs confirm gate.

### Notes on still-OPEN PARTIALs not separately laned
- `compaction-not-persisted-boundary`: touches `agent_journal_backend.py`/`agent_journal.py`/`agent_servicer.py`/`reasoning_loop.py` — schema migration + loop↔servicer summary handoff. Folded into lane-wire (servicer/journal) + lane-reasoning-loop (compaction surface). Sequence after the cheaper loop items; MEDIUM.
- `provider-auth-keychain-import`: SAFE, owns `gateway/oauth/claude_code_import.py` (disjoint) — can be a trivial addendum to lane-anthropic (different package, but auth-themed) or its own micro-lane; assigned to lane-anthropic's owns set is NOT possible (different package file) so tracked under lane-skills-meta? No — keep it in lane-wire's adjacent oauth file is also wrong. It is fully file-disjoint; folded into lane-anthropic as a co-owned file since both are provider-auth.
- `plugins-skills-hooks-fields-ignored`: RISKY, depends on lane-hooks + progressive-disclosure; the enabledPlugins persistence + manifest watcher portion is in `routes_admin_b/plugins.py` + `plugins/lifecycle.py`. Folded conditionally into lane-mcp/lane-skills-meta where file-disjoint; the hook-contribution half waits on lane-hooks. Treated as a follow-on, not a first-wave lane.
- `canvas-no-ui-consumer`: SAFE, UI-only (`ui/components/chat/markdown-message.tsx`, `artifact-panel.tsx`, `ui/package.json`) — fully disjoint from all Python lanes; can be its own UI lane built anytime.
