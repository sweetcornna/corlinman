# BUG LEDGER — 2026-07-02

> Phase 1 of the "zero-bug + parity" work order. Methodology: **逐项实测代码,
> 不信任"文件存在即完成"**. Every "known gap" from
> `docs/GAP_ANALYSIS_HERMES_OPENCLAW.md` was **re-verified against the live tree**
> (branch `feat/multi-agent-live-panel`, HEAD `7f57fbd5`) — the gap doc predates
> several shipped waves and is partly stale (lesson `lesson_gap_audit_vs_live_release`).
>
> Working branch for fixes: `fix/zero-bug-parity`.

## 0. Baseline (先测,再改)

| Check | Result |
| --- | --- |
| `ruff check .` | ❌→✅ 1 error (I001 import sort in a test) — FIXED `aab59f86` |
| `mypy python/packages/` | ❌→✅ 1 error (`console/render.py:139` arg-type) — FIXED `bba18e31` |
| `pytest -m "not live_*"` | ✅ 6316 passed, 4 skipped (212s) |
| `pnpm ui typecheck / lint / vitest` | ✅ pass |
| `gen-proto` diff | ✅ clean |
| `lint-imports` | ✅ pass |
| `corlinman doctor` | ✅ 0 fail, 0 warn, 9 ok |
| Static scan (prod) | 31 `NotImplementedError` (see B1 — mostly legit: gen'd gRPC stubs + `embed()` routing); 71 TODO/FIXME markers; UI clean of `@ts-ignore`/`@ts-expect-error` |

**Net:** the local CI gate had exactly two red items (both trivial, now green). The
tree is far healthier than the work-order framing implies; the real work is
*verifying* the known gaps and finding *latent* bugs, not mass-repair.

---

## 1. Fixed this session (batch 1 — green the gate)

### L-001 — ruff I001 import block un-sorted (P3)
- **Repro:** `uv run ruff check .` → `I001` at `corlinman-agent/tests/test_tool_aliases.py:10`.
- **Root cause:** file committed (`e3c2e5d2`) with a stray blank line splitting the pytest / corlinman import groups the isort profile keeps contiguous.
- **Impact:** `make ci` red at the ruff step (whole gate blocked). No runtime effect.
- **Fix / regression:** `aab59f86` (autofix; ruff is the gate).

### L-002 — mypy arg-type: Spinner frame → `Text.append_text` (P3)
- **Repro:** `uv run mypy python/packages/` → arg-type at `console/render.py:139`.
- **Root cause:** rich-REPL work (`d6734e8c`) passed `Spinner.render()` (typed `RenderableType`) straight into `Text.append_text` (needs `Text`). Correct at runtime (a text-less `Spinner("dots")` always renders `Text`) but unsound to the checker.
- **Impact:** `make ci` red at mypy. No runtime effect.
- **Fix / regression:** `bba18e31` — isinstance-narrow to `Text`; new `test_working_spinner_renders_frame_label_and_elapsed` drives `_Working.__rich_console__` end-to-end. mypy is the primary gate.

---

## 2. Known-gap re-verification (vs live tree)

| Gap (doc claim) | Verified state | Verdict |
| --- | --- | --- |
| Voice = MockVoiceProvider only | Real `OpenAIRealtimeProvider` (`routes_voice/provider_openai.py:456`), auto-selected when an OpenAI key resolves (`mod.py:338`); mock is the no-key fallback | **CLOSED** (doc stale) |
| Bedrock/Azure = `NotImplementedError` | Real chat paths: Bedrock SigV4 stream (`bedrock_provider.py:210,276`), Azure deployment routing (`azure_provider.py:53,128`); only `embed()` raises (expected) | **CLOSED** (doc stale) |
| `service`/`mcp` executor kinds unsupported | Both handled: `plugin_invoker.py:186` (service→supervisor), `:206` (mcp→`McpToolBridge`); `unsupported_plugin_type` only for genuinely-unknown kinds | **CLOSED** (doc stale) |
| ConfigWatcher not wired at startup | Fully wired (`lifecycle/config_loading.py:405/423`, `entrypoint.py:1164`), rebuilds `provider_registry` on change — **but OPT-IN, default OFF** (`config_loading.py:263-306`; needs `CORLINMAN_CONFIG_HOT_RELOAD=1` or `[server].config_hot_reload=true`). Documented in-code. | **BY DESIGN** (see L-010) |
| MCP not wired into agent tool face | Client/connect/execute all wired, but the **discovery→advertisement seam is NEVER wired** (`discovered_tools()` has zero prod callers; `agent_servicer` never touches the mcp manager) → model never sees external MCP tools | **REAL BUG → L-003** |
| Evolution apply/rollback = 记账 only; `metrics_baseline={}` deadens auto-rollback | `metrics_baseline` **FIXED** (real snapshot; `from_dict({})` no longer raises). Applier still mutates no artifact; real-mutation applier (`gateway/evolution/applier.py`) never instantiated; no `AutoRollbackMonitor` runtime driver (`rollback run-once` = clean `todo_stub`) | **PARTLY FIXED; rest product-gated → L-004 (exempt)** |

---

## 3. Open findings

### L-003 — MCP tools never advertised to the model (P1, actionable)
- **Repro (static, confirmed):** `grep -rn discovered_tools python/packages --include=*.py | grep -v /tests/` → only the definition (`client_manager.py:281`). `agent_servicer.py` has **zero** `mcp_manager`/`discovered_tools` refs. `_inject_builtin_tools` (`agent_servicer.py:659`) merges only `_CACHED_BUILTIN_TOOL_SCHEMAS`.
- **Root cause:** external MCP servers are connected at boot (`entrypoint.py:1021,1055`) and are *executable* if a call is routed to an `mcp`-kind plugin (`plugin_invoker.py:206`), but their live-discovered tools are never merged into the model-facing catalog. The model can only emit calls for advertised tools → it never calls any external MCP tool. The end-to-end "agent discovers + uses an external MCP tool" loop is dead.
- **Impact:** MCP-as-primary-extension-path (the #1 parity priority) is non-functional despite all supporting plumbing existing. High value, but a **feature-grade, cross-process, hot-path** change.
- **Design (proposed, for its own focused batch):** at advertisement time in `agent_servicer` (guarded on `self._app_state`), pull `mcp_manager` (from `app_state`/extras), call `discovered_tools()`, convert each `ToolDescriptor`→OpenAI schema under a stable namespace (e.g. `mcp__<server>__<tool>`), merge after builtins (gateway/plugin tools still win on name clash), and add an execution route so an `mcp__server__tool` name dispatches to `McpToolBridge` without requiring a hand-authored registry entry. Cross-process caveat: in grpc_agent mode the manager lives in the gateway process — pass discovered **schemas** to the servicer and keep **execution** where the live connections are (as the invoker already does via `extras`). Needs: unit test (discovery→advertise), routing test (advertised name→bridge), and a channels-service regression (duck-typed request contract).
- **Status:** CONFIRMED + located; **not yet implemented** (deserves a dedicated PR, not a rushed bundle).

### L-004 — Evolution apply/rollback chain dead end-to-end (P2, EXEMPT — product-gated)
- **Verified:** apply (`routes_admin_b/infra/evolution.py:503`) drives the store-backed `corlinman_auto_rollback.EvolutionApplier`, which writes a history row + intent-log + a real metrics baseline but **mutates no skill/prompt/kb artifact** (`applier.py` docstring 6-10; `_commit_apply` 284-324). The real-mutation applier at `gateway/evolution/applier.py:239` is never instantiated in prod. No `AutoRollbackMonitor` runtime driver — `rollback run-once` is a clean `todo_stub` (message + `exit 2`, not a crash).
- **Why exempt:** the repo's own doc flags agent self-mutation of skill/prompt files as **high-risk, requires a product decision** (`GAP_ANALYSIS` §3 note). `metrics_baseline={}` (the actual latent *bug*) is already fixed. The apply route returning `status:"applied"` for a bookkeeping-only op is a minor honesty gap (candidate P3) but changing its semantics is product territory.
- **Status:** LEDGERED + EXEMPT with reason. Recommend a product decision before wiring artifact mutation + a monitor driver.

### L-010 — ConfigWatcher hot-reload off by default (P3, BY DESIGN)
- Wired but opt-in (`config_loading.py:263-306`). Documented in-code (`:298-305`). Not a bug; noted so smoke tests set the flag before asserting hot-reload.

---

## 4. New-bug hunt (recent-branch code)

Targeted read-only audit of the last ~15 commits, then triaged + confirmed on
mainline. **The critical duck-typed-SimpleNamespace contract is NOT reintroduced**
(`_build_chat_start` reads new fields via tolerant `getattr`; no recent commit
added a hard field access) — verified clean.

### L-101 — openai_compatible `/openai` base-url mounts 404 every chat (P2) — FIXED `cce28525`
- See §3 root cause. Confirmed regression from adaptive base-url completion (`b3f60428`).
- **Fix:** both mirror normalizers treat a `/openai`-ending path as an API root. Regression tests added (chat + probe). `make ci`-affected slices green.

### L-102 — live subagent `tool_calls_made` inflated by cross-process poll feed (P3) — LEDGERED, deferred
- **Confirmed:** `_apply_child_event` (`live_subagents.py:264`) does a non-idempotent `row.tool_calls_made += 1` on `ToolStateRunning`, while `_apply_spawned`/`_apply_completed` are idempotent. The shared registry is fed once per open SSE client (`sessions_events.py:403/468`) **and** (single-process) also via the emitter observer, so each tool-start is counted `(1+N_clients)×`.
- **Impact:** the live multi-agent panel shows an inflated tool count **during** a run. `_apply_completed` (`:304`) reconciles the final count from the authoritative `SubagentCompleted.tool_calls_made`, so the number self-corrects on completion **unless** the completed child reports 0. No crash, no race (all mutators await-free).
- **Why deferred:** a correct idempotent fix needs a stable per-event identity present in **both** the envelope and journal-poll paths (the envelope path has no journal seq/id; `timestamp_ms` isn't a reliable dedup key), or feeding the registry from a single authoritative pump instead of per-client replay — an architectural change disproportionate to a self-healing cosmetic P3.
- **Recommended fix:** give `SubagentEvent` journal rows a monotonic `seq`, track a per-child high-water mark in the registry, count a tool-start only when its `seq` advances; keep the envelope path activity-only.

### L-103 — stale/orphaned `in_progress` turn dropped from replay transcript (P3, SUSPECTED) — LEDGERED, deferred
- **Suspected:** `_sessions_lib.py:617` skips any journal turn with `status == "in_progress"`. The comment assumes only the latest turn is ever in-progress; a turn whose process crashed before `finalizeJournalTurn` stays `in_progress` forever and is then silently dropped from `_replay_from_journal` on every future reload (its messages/tool calls become invisible).
- **Why deferred:** this skip is the deliberate fix for the frozen-duplicate-bubble on resume (`739f5eb7`, memory `project_chat_resume_streaming`) — narrowing it needs a "stale in-progress" heuristic (in-progress **and** not actually live **and** older than a threshold → include) plus a resume-path test, and touches a path that was just stabilized. Needs a decision on the staleness signal before changing.

### Notes (evidence-backed, not ranked)
- Auth-gated absolute download links (`8f3e1b6c`) are unusable for non-admin channel users (401) — **pre-existing** limitation acknowledged in the commit, not a new regression. P3.
- `_resolve_public_base_url()` re-reads env + py-config JSON + `<data_dir>/public_origin` on **every** `_register_tool_media` call (uncached). Minor perf, no leak. P3.
- `approvals.py` "200 [] when unwired" (`e1dac0de`) keeps the route's admin-auth dependency; not an auth gap.

**No P0/P1 crash or auth bypass** found in the reviewed hot paths.

---

## 5. Runtime smoke (Phase 1.1-4)

| Surface | Result |
| --- | --- |
| In-process brain boot (`corlinman console`, production wiring) | ✅ boots, no crash |
| Real agent turn + provider dispatch (`-p … --output-format stream-json`) | ✅ reasoning loop ran, dispatched a well-formed request to the resolved provider (`gpt-5.5`); got `401 Invalid API key` — **expected: no live credential in this dev env** (keys are out-of-band by policy). Error handled gracefully: clean `{"type":"result","subtype":"error","is_error":true,"error":{"reason":"auth",…}}`, exit 0. No crash, no silent-empty. |
| stream-json output contract | ✅ proper single `result` envelope |
| Actual successful tool call | ⚠️ **not exercisable here** — requires a valid provider key. Not faked. Builtin tool advertisement + calculator are covered by pytest. |
| Admin UI pages / channel dry-run / scheduler / config hot-reload | ⚠️ not exercised end-to-end (need a running gateway + live channel tokens + browser). Covered indirectly: 6316 pytest tests include route-auth, scheduler, channel-service, and config-reload suites; UI vitest/typecheck/lint green. |

**Honest position:** the agent loop, provider routing, and error/stream contracts are verified live; the surfaces needing live infra (valid keys, channel tokens, a browser-driven gateway) are not fully exercised in this dev environment and are flagged, not claimed.

## Grading key
- **P0** crash / data loss / startup fail / security. **P1** core feature broken/wrong.
- **P2** degradation / misleading / doc-vs-behavior. **P3** UX / log noise / cosmetic.
