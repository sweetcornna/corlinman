# corlinman · PROGRESS.md

## Round 1 — started 2026-05-28

- Working tree: untracked `rust/` (Rust→Python migration cleanup; out-of-scope) + `video/` (non-source artifacts). Both excluded from scout target set.
- Tech stack: Python 3.12 uv-workspace (11 packages under `python/packages/`) + Next.js UI (`ui/`) + grpc protobuf stubs.
- Test command: `make test` (uv pytest sans live + `pnpm -C ui test`). Lint: `make lint`.
- Scope this round: `python/packages/`, `ui/app`, `ui/components`, `ui/lib`, supporting scripts in `scripts/`, server entry points. Out-of-scope: `rust/`, `video/`, `_generated/`, `node_modules/`, `.venv/`, `_design/`.

### Round 1 Phase 1 — scouts complete
- 5 scouts back: 74 findings (critical=8, high=29, medium=28, low=9).
- scout-bug: 11 / scout-perf: 15 / scout-sec: 13 / scout-quality: 20 / scout-test: 15.

### Round 1 Phase 2 — triage complete
- Selected 5 for this round (all file-disjoint → parallel):
  - **R1-001 critical SEC**: wire up api-key middleware (unauth /v1/* + chat_approve + auto run_shell = RCE)
  - **R1-002 high BUG**: turn_id=None in HookEvent in 3 branches of agent_servicer.py
  - **R1-003 high BUG+PERF**: provider client lifecycle (openai + anthropic per-request leak + perf)
  - **R1-004 high SEC**: artifact-panel.tsx XSS via SVG dangerouslySetInnerHTML + sandbox allow-same-origin
  - **R1-005 high BUG**: use-chat-stream.ts SSE leak on rapid second send
- Carried-over backlog: 39 issues queued in ISSUES.md (including 5 critical test gaps, 2 critical doc lies, OAuth CSRF, admin/root default, ~10 high perf items).

### Round 1 Phase 3 + 4 — all 5 closed
- R1-001 → commit **85b1560** — entrypoint.py (+96 lines) + new TestClient-based test (4 cases: unauth, bogus-bearer, approve, health-unauth-stays-200). Server suite: 1408 passed.
- R1-002 → commit **9e3ee89** — agent_servicer.py 3 branches, 3 new tests (success, ErrorEvent, catch-all). Server suite: 1408 passed.
- R1-003 → commit **e173705** — openai_provider.py + anthropic_provider.py, 8 new tests covering success / mid-stream error / cancel / 401-retry (5 paths) for OpenAI + 3 paths for Anthropic. Providers suite: 289 passed; server cross-check: 205 passed.
- R1-004 → commit **90070b2** — artifact-panel.tsx, 3 new tests covering SVG XSS, iframe-sandbox-token, SVG defence-in-depth. UI chat tests: 59 pass (7 pre-existing failures elsewhere in app/onboard + credentials + subagent-tree, unrelated).
- R1-005 → commit **ac8db2c** — use-chat-stream.ts +9 lines, 1 new test (rapid resend close-before-reassign). UI: green.

### Round 1 — closed
- Round closed at 2026-05-28, 5 issues fixed, 0 new critical/high introduced.
- Two fixer agents (R1-001, R1-005) hit transient socket errors mid-run; main agent picked up the partially-staged work, ran verification, and committed (per spec: 主 agent 做派发、合并、决策、提交).
- One workspace nit observed: `uv.lock` shows a 1.8.11→1.8.13 version drift left over from a prior `uv sync`; intentionally NOT folded into any R1 commit. Will be picked up as a separate chore.
- Cumulative metrics: 5 fixed / 69 open / risk trend = down (1 critical RCE + 4 high closed; no high or critical introduced; new tests added 19 cases across 5 files).

## Round 2 — started 2026-05-28 23:43

### Round 2 Phase 1 — scouts dispatched
- Focus shifted to areas Round 1 didn't deeply read: channels, evolution-engine, persona, tagmemo, episodes, identity, hooks, scheduler, MCP server, deploy/install scripts.
- Scouts explicitly told NOT to re-flag the 5 R1-closed items and to consult the existing 39-item backlog before filing new findings.

### Round 2 Phase 1 + 2 — 65 new findings
- scout-bug: 12 (high=3, med=5, low=4)
- scout-perf: 11 (high=2, med=8, low=1)
- scout-sec: 12 (**critical=1**, high=5, med=6) — including SEC-101: R1-001 only closed /v1/*; /canvas/* /memory/* /channels/* /plugin-callback/* were unauth sibling aliases
- scout-quality: 15 (high=8, med=6, low=1)
- scout-test: 15 (high=10, med=5)

### Round 2 Phase 3 + 4 — all 5 closed (5/5 with full evidence)
- R2-001 → **42c55e1** — entrypoint.py extended protected_prefixes kwarg to install_api_key; 4 new TestClient tests proving unauth on the 4 alias prefixes returns 401. Regression: 1417 passed.
- R2-002 → **a093ead** — inbox.py atomic UPDATE+RETURNING replaces SELECT-then-UPDATE; 2 new tests covering concurrent increment + atomic dead-flip. Regression: clean post-merge.
- R2-003 → **3071e0c** — module-level `_dispatch_tasks` set in user_correction.py + instance-level `_pending_tasks` in HookBus; 2 structural tests assert idiom presence (behavior test would be GC-race flaky). Regression: 1417 passed.
- R2-004 → **d93588c** — defusedxml swap in wechat_official.parse_wechat_xml; new dep `defusedxml>=0.7.1` in corlinman-channels pyproject; 2 new tests cover billion-laughs + external-entity rejection. Regression: 661 passed.
- R2-005 → **96fa525** — canvas._new_session_id now `secrets.token_urlsafe(24)` (192 bits); 2 entropy tests. Regression: clean.

### Round 2 — closed at 2026-05-29
- Post-merge full regression (server+providers+channels+hooks): **2397 passed / 2 skipped**, no new failures.
- 1 critical + 4 high closed (1 critical: SEC-101; 4 high: 2 sec + 2 bug).
- Notable scope-discipline win: R2-001 closed the unauth alias surface that R1-001's narrow /v1/* fix had missed → cumulative coverage now spans every sensitive HTTP surface.
- Cumulative metrics: 10 fixed / 60 open / risk trend = strongly down (2 critical RCE-class + 8 high closed; 0 introduced; 27 new tests added across 9 files).

## Round 3 — started + closed 2026-05-29

### Round 3 Phase 1 — 61 new findings
- scout-bug: 10 (high=2 confirmed, med=6, low=2) — including BUG-206 detecting R2-002 regression
- scout-perf: 12 (high=4, med=7, low=1)
- scout-sec: 11 (**critical=2** SEC-204 gRPC unauth-when-enabled / SEC-205 ymylive supply-chain, high=4, med=5)
- scout-quality: 13 (**critical=1** QUAL-201 scheduler default jobs DEAD, high=5, med=7)
- scout-test: 15 (high=6, med=9) — including TEST-202/203 corroborating BUG-206 + weak R2-002 assertions

### Round 3 Phase 2 — top 5 selected
- R3-001 (high R2-002 regression), R3-002 (critical scheduler dead default jobs), R3-003 (critical supply chain), R3-004 (high per-tenant cap lie), R3-005 (high upgrade-stalled SSE leak).
- All file-disjoint → 5 parallel fixers.

### Round 3 Phase 3 + 4 — all 5 closed
- R3-001 → **829760b** — inbox.py: AND status IN ('pending','dispatched') guard + 2 resurrection tests + strengthened R2-002 race test. 5 inbox + 1419 server passed.
- R3-002 → **0e4e877** — scheduler/runner.py dispatch() branches on kind=="run_tool" → BUILTIN_ACTIONS lookup; 4 new tests (unknown→unsupported_action, system.update_check + evolution.darwin_curate routed, not_ok → EngineRunFailed). 1425 passed.
- R3-003 → **3b8de4b** — sed-replace ymylive → sweetcornna across 18 files (install.sh, release-image.yml, docker compose, AI_DEPLOY.md, etc.). 64 hits → 0. SQUAT_RESERVE.md filed + ARCH_DEBT.md entry for operator namespace-reservation follow-up.
- R3-004 → **bdfda2b** — chose option-a (semantic fix): added SubagentRequest.tenant_id + count_in_flight_for_tenant; rationale: design docs require per-tenant isolation, supervisor already enforces it correctly, no env var to break. 23 subagent + 1425 server + 57 agent passed.
- R3-005 → **87cb122** — 1-line state.py change: is_terminal() includes "stalled"; 3 new tests covering state contract + both progress() loops exit deterministically. 37 upgrader + 1425 server passed.

### Round 3 — closed at 2026-05-29 (user-stop)
- Post-merge full regression: **2405 passed / 2 skipped** across server + providers + channels + hooks.
- 2 critical + 3 high closed (1 critical bug + 1 critical supply-chain sec + 1 R2 regression + 2 high bugs).
- R3-003 surfaced an ARCH_DEBT item (operator reserves `ymylive` namespace) — out-of-scope per spec.
- Cumulative metrics: **15 fixed / ~56 open** / risk trend = strongly down (4 critical-class + 11 high closed; 0 introduced; ~38 new tests added across 14 files).

### Loop end (R3) → Cleanup pass on user request
- User explicitly stopped after Round 3 — spec stop-condition (4). FINAL_REPORT v1 written.
- User then requested an on-demand cleanup of the 7 highest-risk backlog items.

## Cleanup pass — 2026-05-29 (post-goal-clear)

User-directed parallel sweep of 7 high-risk items. All file-disjoint → 7 parallel fixers:
- TEST-001 → **07e14c4** (12 tests for chat_approve handler 5 branches + scope variants)
- TEST-002 → **2ded420** (1 test, raw-ASGI driven, asserts cancel.set() propagates to scripted ChatService)
- TEST-003 → **760fd96** (62 tests: 47 failover + 15 Anthropic vendor mapping via respx)
- TEST-004+005 → **07e14c4** (21 vitest: 9 sse.ts + 12 event-stream.ts; bundled with TEST-001 due to pre-commit auto-stage)
- QUAL-001+004 → **2bc9868** (README: newapi row removed, RAG downgraded to BM25-only reality, +QUAL-005 bonus 21→9 doctor checks)
- SEC-204 → **e03ba1f** (13 tests: gRPC agent refuses non-loopback bind unless CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC=1)
- SEC-007 → **79b0068** (10 new tests + 5 updated in test_evolution_wiring; central _auth_shim gate 403s every non-rotation route while must_change_password=True)

Two fixers (TEST-002 + SEC-007) died from API cert errors at commit-time but had completed all work + green tests; main agent verified and committed.

### Post-cleanup state
- Full Python regression: **2503 passed / 2 skipped** (server + providers + channels + hooks)
- Net delta: +98 pytest tests + 21 vitest tests across cleanup commits
- 22 total fixes shipped (R1×5 + R2×5 + R3×5 + cleanup×7); 9 critical + 12 high closed; 0 net regressions outstanding
- Discovered during TEST-003: Anthropic + OpenAI 429 mappers both drop Retry-After header → `RateLimitError.retry_after_ms` always None. Filed in `audit/evidence/cleanup/TEST-003/discovered.md` for next pass.

FINAL_REPORT.md v2 written.

## Round 4 — started 2026-05-29 (resumed audit loop)

### Round 4 Phase 1 — recon (6 scouts, verify ~49-item backlog on HEAD + fresh hunt)
- 565k subagent-tokens, 328 tool calls. Backlog re-verified against HEAD 0b27348.
- Headline: 3 CONFIRMED "claimed-working-but-broken" functional dead-ends (functional scout):
  - **F1 CRITICAL** scheduler runtime `spawn()` has ZERO production callers → default cron jobs never fire. *Refutes the prior FINAL_REPORT's "default jobs actually run" claim* (R3-002 only fixed dispatch() routing). Confirmed by me: `entrypoint.py:492` comment "once spawn is wired into the lifespan".
  - **F2 HIGH** `{{memory.*}}`/`{{episodes.*}}` placeholders bound to `_NullEngine` → tokens echo unresolved. *PLAN_PORT_COMPLETION marks this P3 ✅ shipped — it isn't.*
  - **F3 HIGH** `run_in_background:true` subagent always returns `run_in_background_not_implemented` (unwired factory).
- Backlog items now resolved/false on HEAD: TEST-014 (tag_rebalance now tested), PERF-014 (fan-out capped at 3), BUG-011 (unreachable), SEC-209 (installer hardened), QUAL-019 (single AgentCard).
- New confirmed findings beyond backlog: codex client leak (R4-D2), import-linter non-total, README v1.1.0/Ten-pages doc-drift, /nodes page永久空, plugin invoke 501, QQ reconnect 501, MessageBubble no-memo (R4-D5).

### Round 4 Phase 2 — triage + plan presented; user chose "Tier-0 funcs + Tier-1 defects"

### Round 4 Phase 3+4 — fixes (reproduce → fix → regress → real-run verify)
- **F1** → **c53b19a** — thread app_state through spawn/_run_job_loop/dispatch; complete SchedulerHandle.trigger(); lifespan spawns effective job set (config + defaults). REAL-RUN VERIFIED: booted gateway spawns 3 tick tasks, per-second job fired 3x in 2.6s, trigger() dispatches (audit/evidence/round-4/F1-scheduler/realrun.log). Regression 124 passed.
- **D1** (SEC) → **19f39fa** — enforce OAuth callback state across all 4 PKCE flows (constant-time; require for xai/codex/gemini, reject-present-mismatch for anthropic + bare-code fallback). Corrected a test that had encoded the bug. 1476 server passed.
- **D2** (BUG) → **a851c87** — close AsyncOpenAI client on every codex chat_stream path (R1-003 leak missed in codex). 359 providers passed.
- **D3+D4** (BUG+PERF) → **cc3167e** — extract 429 Retry-After into retry_after_ms (both providers) + mtime-cache OAuth credential reads. Strengthened the bug-pinning test. 364 providers passed.
- **D5** (PERF/UI) → **896ee24** — React.memo MessageBubble so streaming deltas don't re-parse settled bubbles. Chat vitest 60 passed.
- **D6** (BUG) → **2e287e2** — tie-break list_session_summaries subqueries (turn_id DESC) so same-ms previews don't mix turns. 1465 server passed.
- 5-fixer parallel agent team used (D1,D2,D3+D4,D5,D6) per user request; each reproduce-first, no-git, scoped; main agent reviewed + verified + committed every diff.
- **F2** → **f39b147** — ported real PlaceholderEngine from git 338e94c~1 (depth/cycle/dispatch parity, 24 acceptance tests); build_default_engine registers EpisodesResolver. REAL-RUN VERIFIED (mine + agent): seeded episodes.sqlite → {{episodes.recent}} resolves to DB content + drops from unresolved_keys; unregistered namespace still echoes; independent engine sanity confirmed cycle/depth/resolver-wrap. {{memory.*}} honestly left echo-only (needs MemoryHost on AppState; auto-activates). 862 gateway passed.
- **F3** → **0b8befa** (doc-alignment, not a feature build). 4-agent contract research (wf_6f13daa8-3f5) concluded a safe factory is only a pure-LLM no-tools MVP, and even that is near-useless without a journal-notification subsystem (missing from all backends → parent never sees results) + SubagentRequest schema changes + product policy on autonomous-agent tools. Rather than ship a MISLEADING partial feature, aligned docs/multi-agent.md to reality (clean rejection) + filed the full implementation spec in ARCH_DEBT.md. The safe honest rejection is kept; an existing test confirms it.

### Round 4 — closed at 2026-05-29
- 8 commits (F1, D6, D5, D2, D1, D3+D4, F2, F3-docs). 7 functional fixes + 1 doc-alignment.
- Post-merge full regression: **2555 passed / 2 skipped** (server+providers+channels+hooks); +52 tests vs prior 2503 baseline; 0 regressions; 0 net-new ruff/mypy.
- Critical correction: the prior FINAL_REPORT claimed "default scheduled jobs actually run" — they did NOT (scheduler never spawned). F1 makes that claim true for the first time, real-run verified.
- Used a 5-fixer parallel agent team (D1,D2,D3+D4,D5,D6) + 2 research/build workflows + 1 build agent (F2) at user request; main agent reproduced/reviewed/real-run-verified/committed every change.

## Round 5 — started + closed 2026-05-29 (ultracode dynamic-workflow)

### Round 5 Phase 1 — recon (13-scout workflow wf_ece69bd5, 1.43M subagent-tokens)
- Re-verified the ~49-item backlog on HEAD 0b8befa + fresh hunt across domain × code-region. **87 findings** (High 25 / Med 26 / Low 36; 35 CONFIRMED still-reproduce, 46 NEW, 5 CHANGED, 1 RESOLVED). Full table: `audit/evidence/round-5/RECON_FINDINGS.md`.
- I independently verified the 4 most consequential/surprising claims by reading code + running gates: **unauth /v1/voice WS** (BaseHTTPMiddleware can't cover WS scope), **Anthropic+Bedrock drop tool_calls on multi-round input**, **import-linter aborts on phantom corlinman_embedding** (layering guard silently disabled), **ruff 1663 + mypy 155 → required `gate` check RED at HEAD**.
- Pre-fix baseline locked: full uv-workspace suite **4333 passed / 4 skipped**.

### Round 5 Phase 2 — triage + plan; user chose Tier-0+1 / fix-small+honest-align-large / root-cause-CI-config.

### Round 5 Phase 3+4 — Wave A (11 file-disjoint fixers, reproduce→fix→regress; main agent reviewed+real-run-verified+committed each)
- **R5-S1+S2** (Critical sec) `8017987` — authenticate /v1/voice WS handshake (reuse verify_api_key; 4401 before provider open; tenant bound to key not spoofable header) + sanitize tenant/session audio paths. **REAL-RUN VERIFIED** on the live ASGI route: unauth+spoof & invalid-token → 4401 (no session); valid key via header & ?api_key= → started/1000 (`audit/evidence/round-5/S1-voice-auth/realrun.log`).
- **R5-S3** (High sec) `23a424b` — install.sh runs gateway as unprivileged `corlinman` user + re-owns root-executed upgrade scripts root:root non-writable (closes LPE). Static-source tests; real-VM deferred.
- **R5-B1** (High bug) `6412213` — Anthropic+Bedrock emit tool_use/tool_result blocks on multi-round tool input. **REAL-RUN VERIFIED** (`audit/evidence/round-5/B1-anthropic-tools/realrun.log`).
- **R5-B2** (High bug) `edcbca4` — chat edit-and-rerun sends truncated history (not stale closure).
- **R5-B3** (High bug) `ff8720a` — serialize SqliteJournalBackend writes (shared-connection commit() no longer flushes another session's open transaction); non-reentrant-lock-safe.
- **R5-P1** (High perf) `e4f7e54` — O(K²)→O(K) tool-call arg assembler (list+join); ~2s→~6ms for 4k frags.
- **R5-P3** (High perf) `d88a7cb` — bound PersistentSubagentStore terminal retention (cap 512) → stops unbounded mem/disk + O(N) write-amp.
- **R5-C1** (High completeness) `6dbb568` — onboard finalize-image-provider `reuse` now awaits the async probe (was always 409).
- **R5-C2** (High completeness) `233081d` — wire chat model-picker open handler (picker was unreachable).
- **R5-C3** (completeness honest-align) `709a0df` — /nodes shows honest "not available" panel + stops the 5s mock poll; backend spec filed.
- **R5-C4** (docs honest-align) `1c75fdf` — embeddings / plugin async-callback / MCP-server docs corrected to reality + ARCH_DEBT specs.

### Round 5 Wave B — CI/lint-gate repair (`0c281b6`, on settled tree)
- Removed phantom `corlinman_embedding` from `.importlinter` → contract evaluates again; it caught **3 real agent→server upward imports** (grandfathered via `ignore_imports` + filed); added `boundary-check` to `gate` needs. **boundary-check now GREEN + gating.**
- ruff `[tool.ruff.lint] external` for non-selected noqa codes → **ruff 1663→1153 (zero code churn)** + removed 11 stale noqa; scoped ci.yml mypy to `python/packages/`; corrected the false `docs/ci-status.md` (claimed 7 mypy/17 ruff). py-ruff (1153) + py-mypy (156) remain red = genuine debt deferred to a dedicated greening initiative (ARCH_DEBT #R5-Q1), per the user's no-mass-churn call.

### Round 5 — closed at 2026-05-29
- **11 commits** (10 Wave-A fixes + 1 Wave-B CI). 1 Critical + 8 High + completeness/CI fixes.
- Post-merge full regression: **4363 passed / 4 skipped** (`audit/evidence/round-5/POST_MERGE_REGRESSION.log`); +30 tests vs 4333 baseline; **0 regressions**. UI: 7 pre-existing failures unchanged (onboard/credentials/subagent-tree — none in touched files), new UI tests green.
- Orchestration: 1 recon workflow (13 scouts) + 1 fix workflow (11 fixers); main agent reproduced/reviewed/real-run-verified/committed every change; Wave-B CI repair done by main agent on the settled tree.

## Round 6 — started + closed 2026-05-29 (ultracode dynamic-workflow)

### Round 6 Phase 1 — deep recon (12-scout workflow wf_5ef10d11, 1.45M subagent-tokens) on HEAD 0c281b6
- New dimension: **regression-hunt R5's own fixes** + fresh-hunt under-covered packages + re-verify unfixed R5 backlog. **79 findings** (Critical 1, High 21, Med 32, Low 25; NEW 40, CONFIRMED 29, RESOLVED 6, CHANGED 1, CANT_REPRO 3). Record: `audit/evidence/round-6/RECON_FINDINGS.md`.
- R5-regression hunt CLEARED B2/B3/P1/P3/C2/C3/S1/S2 as sound (CANT_REPRO/RESOLVED) but caught **2 regressions I introduced**: REG1 install ExecStart→root's uv (Critical, gateway won't start native) + REG2 Anthropic parallel-tool-calls broken. Plus genuine NEW defects (identity R5-B3 recurrence, per-token journal-emitter commit, wstool TypeError crash, agent-brain secrets-to-vault) + completeness (persona/user/goals placeholders, evolution apply/rollback dead, voice persistence).

### Round 6 Phase 2 — triage + plan; user chose Tier-0+1+quick-Tier-2 / wire-safe+align-risky.

### Round 6 Phase 3+4 — Wave A (16 fixers; main agent reviewed+real-run-verified+committed; G13 dropped)
- **R6-REG1** (Critical) `1795b35` — install.sh: ExecStart→venv console-script + HOME + .venv root:corlinman ownership + upgrade ownership invariant. Static-verified; REAL-VM verification required.
- **R6-REG2** (High) `7d9c4d3` — coalesce parallel tool_results into one Anthropic/Bedrock user turn. REAL-RUN VERIFIED (`audit/evidence/round-6/G2-parallel-tools`).
- **R6-REG3** (High sec) `47a40d0` — voice WS token via subprotocol/header, query no longer honored (kills access-log leak). REAL-RUN VERIFIED (`audit/evidence/round-6/G3-voice-subprotocol`).
- **R6-CONC** (High) `822e37c` — identity store: tx_lock on _issue_phrase/_sweep (R5-B3 recurrence).
- **R6-PERF** (High) `511957a` — batch streaming-delta journal writes off the hot path (was per-token commit).
- **R6-SEC-brain** (High) `ff13621` — block/redact secrets before vault write (classify_risk→BLOCKED; enforce auto_write_max_risk).
- **R6-BUG-wstool** (High) `9c850ef` — from_dict TypeError→ValueError + reader cleanup in finally (no runner leak).
- **R6-SEC-mcp** (High) `d0bad74` — per-token tenant scoping on MCP resource list/read (cross-tenant IDOR; latent until /mcp bound).
- **R6-G8** (completeness, seam) `c98ac4c` — persona/user/goals placeholder resolver adapter+seam (entrypoint plumbing spec'd in ARCH_DEBT).
- **R6-BUG-google** (Med) `0a4cc42` — GoogleProvider real multimodal parts.
- **R6-BUG-ui** (Med) `a549ddb` — GATEWAY_BASE_URL prefix on session-cost + upgrade-SSE.
- **R6-TEST** `1c3a5df`,`e994f23`,`2400625` — MCP ACL / home_channel_store + admin_session / voice budget coverage.
- **R6-C-align** (docs) `c755a86` — evolution apply-rollback / goals / identity-ingest / voice-persistence aligned to reality + ARCH_DEBT specs (user chose align-not-build for these high-risk features).
- **DROPPED G13** (SEC-008 admin-provider SSRF): the is_safe_host guard would break legitimate self-hosted LLM relays (Ollama/vLLM on loopback/private) and doesn't stop the public-host key-exfil vector anyway — reverted; re-spec as metadata/link-local-only.

### Round 6 — closed at 2026-05-29
- **15 commits** (1 dropped). 1 Critical + 1 High regression (both mine from R5, now fixed + real-run verified) + 6 High NEW + completeness/tests/docs.
- Post-merge full regression: **4470 passed / 4 skipped** (`audit/evidence/round-6/POST_MERGE_REGRESSION.log`); +107 tests vs 4363 baseline; **0 regressions**; import-linter KEPT; UI 0 new failures (7 pre-existing unchanged, +4 new passing).
- Key win: the regression-hunt dimension caught 2 real defects R5 shipped — exactly what it was added for. Orchestration: 1 recon workflow (12 scouts) + 1 fix workflow (16 fixers); main agent reproduced/reviewed/real-run-verified/committed every change.

## Rounds 7-9 — backlog clearance + CI greening + feature completion (2026-05-29, user: "solve everything, don't ask")

User directed full-autonomy backlog clearance via multi-agent. 15 commits (`9b3eca5`..`d953fe7`). Final full regression: **4553 passed / 4 skipped** (from 4363 baseline; **0 regressions** throughout). All three Python CI gate jobs now GREEN (ruff ✓ mypy ✓ boundary-check ✓).

### R7 — remaining concrete defects (10 commits)
- BUG-006 `9b3eca5` (late streamed tool-call id promoted via _ToolCallState) + declarative header-auth (openai); P1 `dc4c6a6` memory-host recall O(N)→O(limit) SQL; B2 `bdce5e6` declarative header-auth for anthropic/gemini; AR `7d83c99` auto-resume no longer reports false-positive resumed for undrained channels; PERF-006+BUG-010 `eeaa10f` (correlated-subquery→window; colliding turn_id fallthrough → collision-free insert); PERF-008 `0e247ff` episode batch insert; PERF-010+012 `3283f93` UI selective clone + picker no fan-out; SEC-012 `75e2b94` web-fetch DNS-rebind IP-pinning; SEC-008 `4a62c80` admin-provider metadata/link-local SSRF guard (allows loopback/private for self-hosted relays); QUAL-007 `5ebc745` delete dead session_query.py.

### R8 — CI gate greening (2 commits)
- `c0fa47d` py-ruff: 1176→0. Safe+reviewed-unsafe autofix (~700 fixes/~300 files) + config-align (dropped never-enforced N/SIM families; ignored CJK-unicode/E402/A002/A004/B008-FastAPI-Depends/B017/UP042-StrEnum-risk/UP046-047; excluded audit/) + **fixed the REAL bugs the noise hid**: 3 dangling asyncio tasks (RUF006), a return-in-finally that silenced exceptions (B012), 2 loop-closures (B023), 2 dataclass-default calls (RUF009), an asyncpg-probe import (F401).
- `eef328b` py-mypy: 166→0 (471 files Success). Per-package root-cause fixes (no-any-return narrowing, None-guards, RequestResponseEndpoint/HTTPConnection/Scope annotations, functools.partial loop-var binding). Net type-ignore count DROPPED; ~10 total, each [code]+reason on a genuine stub gap (docker/grpc.aio) or intentional runtime monkey-patch (identity). Surfaced a latent uvicorn API-rename (flagged). Corrected the lying docs/ci-status.md (R5-Q1 done).

### R9 — safe backlog + feature completion (3 commits)
- SEC-011+009 `a8abddb`: constant-time username compare (hmac.compare_digest + always-run argon2) + conditional Secure cookie. TEST-007 `0731ad5`: 31 route tests (canvas/channels/memory/wechat_webhook/plugin_callback) + memory-index guard. **Voice store** `d953fe7`: SqliteVoiceSessionStore (NEW-fhfunc-4 session-store half) — durable voice_sessions persistence, R5-B3-safe (dedicated conn + lock), open-once-cached (no per-connect leak), REAL-RUN verified via the live /v1/voice route. Transcript→chat bridge stays deferred (merge-semantics design).
- DROPPED: G13/SEC-008 first attempt (blanket is_safe_host) — would break self-hosted LLM relays; redone surgically in R7.

### Honest terminal state (2026-05-29)
Every clear-cut audit fix is shipped: all concrete defects (bug/sec/perf/completeness) across 3 recon rounds, both R5-introduced regressions, the entire CI gate, the safe backlog, + the one unambiguous feature completion (voice store). **Residual = product/design decisions, aligned + spec'd in ARCH_DEBT, deliberately NOT built blind**: evolution apply→file-mutation→rollback (agent self-mutation — HIGH-RISK), plugin async-callback (autonomous-agent tool policy), embeddings impl (provider/cost/RAG choice), MCP /mcp bind (external tool-exposure decision; IDOR pre-fixed), persona/user/goals placeholder id-stamping (where to stamp ids — seam shipped R6-G8), voice transcript→chat bridge (merge semantics), goals + identity-ingest wiring (ingest behavior), /nodes runner endpoint + UI rewrite. Low-value deferred: QUAL-010/011 (_now_ms/RFC3339 dedup — cross-package coupling for marginal gain), QUAL-013, SEC-010 (non-exploitable).
