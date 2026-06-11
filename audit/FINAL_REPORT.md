# corlinman · audit FINAL REPORT (v6 — after Rounds 7-9: backlog clearance + CI greening)

**Last updated**: 2026-05-29 (post-R9).
**Loop**: 6 audit rounds + 1 cleanup pass + a user-directed full-autonomy backlog-clearance arc (R7-9).

## Rounds 7-9 summary — "solve everything" arc

User directed full-autonomy multi-agent clearance of the remaining backlog. **15 commits** (`9b3eca5`..`d953fe7`); final regression **4553 passed / 4 skipped** (from the 4363 baseline; **0 regressions** across the whole arc). **All three Python CI gate jobs are now GREEN** (py-ruff ✓ py-mypy ✓ boundary-check ✓) — the required `gate` aggregator passes on the Python side for the first time in the audit.

- **R7 (10 commits)** — remaining concrete defects: late-tool-call-id (BUG-006), declarative header-auth (all 3 wire formats), memory-recall O(N)→O(limit), auto-resume false-positive, journal correlated-subquery→window (PERF-006) + colliding-turn_id (BUG-010), episode batch-insert, UI perf (reduceEvent + picker), web-fetch DNS-rebind IP-pinning (SEC-012), admin-provider metadata-SSRF guard (SEC-008, surgical — allows self-hosted relays), dead-code deletion.
- **R8 (2 commits)** — **greened the CI gate**: py-ruff 1176→0 (autofix + config-align to the codebase's real conventions + **fixed the real bugs the lint noise hid**: 3 dangling asyncio tasks, a return-in-finally silencing exceptions, 2 loop-closures, 2 dataclass-default calls); py-mypy 166→0 (471 files, root-cause per-package, net-negative type-ignores). Corrected the previously-lying `docs/ci-status.md` (closes R5-Q1's structural half).
- **R9 (3 commits)** — auth hardening (constant-time username SEC-011 + conditional Secure cookie SEC-009), 31 route-coverage tests (TEST-007), and the **durable voice session store** (NEW-fhfunc-4 session half: `SqliteVoiceSessionStore`, R5-B3-concurrency-safe, open-once-cached, real-run verified via the live `/v1/voice` route).

**Headline:** greening the lint gate was not cosmetic — it surfaced and fixed genuine latent bugs (dangling tasks, an exception-silencing finally, loop-closures) that the 1176-error noise had buried.

### Honest terminal state
Every clear-cut audit fix is shipped (3 recon rounds of defects, both R5-introduced regressions, the full CI gate, the safe backlog, + the one unambiguous feature completion). The **residual is product/design-decision feature work** — deliberately aligned + precisely spec'd in `ARCH_DEBT.md` rather than built blind, consistent with the operator's own R5/R6 "align the risky ones" guidance: evolution apply→file-mutation→rollback (agent self-mutation — HIGH-RISK), plugin async-callback (autonomous-agent tool policy), embeddings (provider/cost/RAG choice), MCP `/mcp` server bind (external tool-exposure decision; the cross-tenant IDOR was pre-fixed in R6), persona/user/goals placeholder id-stamping (resolver seam shipped R6-G8), voice transcript→chat bridge (merge semantics), goals + identity-ingest wiring, and the `/nodes` runner endpoint + UI rewrite. Building any of these blind would risk a wrong-shaped or unsafe capability — they need a human product decision.

---
_(v5 report retained below.)_

# corlinman · audit FINAL REPORT (v5 — after Round 6)

**Last updated**: 2026-05-29 (post-Round-6).
**Loop**: 6 rounds + 1 user-directed cleanup pass.

## Round 6 summary (2026-05-29, ultracode dynamic-workflow)

12-scout deep recon → **79 findings**, adding a **regression-hunt of R5's own fixes**. That dimension paid off: it CLEARED R5-B2/B3/P1/P3/C2/C3/S1/S2 as sound but caught **2 regressions R5 shipped** — and surfaced genuine new defects across the under-covered packages. **15 commits** (`1795b35`..`c755a86`); post-merge **4470 passed / 4 skipped** (+107 tests vs 4363 baseline, 0 regressions, import-linter KEPT).

| #ID | sev | fix | commit | verification |
|-----|-----|-----|--------|--------------|
| **R6-REG1** | critical reg | native gateway runnable as unprivileged user (R5-S3 ExecStart→root's uv broke startup) | `1795b35` | static tests; **real-VM required** |
| **R6-REG2** | high reg | coalesce parallel tool_results into one Anthropic/Bedrock user turn (R5-B1 broke multi-tool rounds) | `7d9c4d3` | **real-run** |
| **R6-REG3** | high reg-sec | voice WS token via subprotocol/header, query no longer honored (R5-S1 ?api_key= access-log leak) | `47a40d0` | **real-run** |
| **R6-CONC** | high bug | identity store tx_lock (R5-B3 shared-connection bug recurrence → orphan rows) | `822e37c` | async interleave test |
| **R6-PERF** | high perf | batch streaming-delta journal writes (was a sync sqlite commit per token) | `511957a` | emitter batch test |
| **R6-SEC-brain** | high sec | agent-brain blocks/redacts secrets before the vault write | `ff13621` | secret→BLOCKED test |
| **R6-BUG-wstool** | high bug | malformed frame no longer crashes the reader / leaks the runner | `9c850ef` | malformed-frame test |
| **R6-SEC-mcp** | high sec | MCP per-token tenant scoping (cross-tenant memory IDOR; latent until /mcp bound) | `d0bad74` | cross-tenant denial test |
| **R6-G8** | completeness | persona/user/goals placeholder resolver seam (entrypoint plumbing spec'd) | `c98ac4c` | seam test |
| **R6-BUG-google / -ui** | medium | Gemini multimodal parts; UI GATEWAY_BASE_URL prefix | `0a4cc42`,`a549ddb` | tests |
| **R6-TEST×3** | high test | MCP ACL / home_channel_store + admin_session / voice budget coverage | `1c3a5df`,`e994f23`,`2400625` | new tests |
| **R6-C-align** | docs | evolution apply/rollback, goals, identity-ingest, voice-persistence aligned + specs | `c755a86` | doc + ARCH_DEBT |

**Headline (R6): the audit caught its own regressions.** Adding a regression-hunt phase over R5's commits found that R5-S3 (deploy hardening) had left the gateway unable to start as the new unprivileged user (Critical — prod runs native systemd), and R5-B1 (Anthropic tool-calls) had fixed single-tool rounds while breaking *parallel* tool rounds. Both are now fixed and independently real-run verified.

**Dropped:** SEC-008 (admin-provider SSRF) — the candidate guard would break legitimate self-hosted LLM relays and doesn't close the real exfil vector; reverted + re-spec'd.
**Deferred (ARCH_DEBT):** the evolution apply→rollback chain (HIGH-RISK agent self-mutation — needs a product decision), goals/identity-ingest drivers, voice SqliteVoiceSessionStore, and the persona/user/goals placeholder entrypoint plumbing (seam shipped). Plus the standing R5-Q1 ruff/mypy greening.

---
_(v4 report retained below.)_

# corlinman · audit FINAL REPORT (v4 — after Round 5)

**Last updated**: 2026-05-29 (post-Round-5).
**Loop**: 5 rounds + 1 user-directed cleanup pass.

## Round 5 summary (2026-05-29, ultracode dynamic-workflow)

13-scout recon → **87 findings** (re-verified ~49-item backlog on HEAD + fresh hunt). Triaged to a Tier-0+1 batch + small completeness fixes + honest-align large + a root-cause CI-config repair. **11 commits** (`8017987`..`0c281b6`); post-merge **4363 passed / 4 skipped** (+30 tests vs 4333 baseline, 0 regressions).

| #ID | sev | fix | commit | verification |
|-----|-----|-----|--------|--------------|
| **R5-S1/S2** | critical sec | authenticate `/v1/voice` WS handshake + sanitize tenant audio paths | `8017987` | **real-run** on live ASGI route: unauth/invalid→4401 (no session), valid key (header & query)→started/1000 |
| **R5-S3** | high sec | gateway runs as unprivileged user + root-executed upgrade scripts non-writable (LPE) | `23a424b` | static-source tests; real-VM deferred |
| **R5-B1** | high bug | Anthropic+Bedrock emit tool_use/tool_result on multi-round tool input | `6412213` | **real-run**: both adapters emit correct vendor blocks |
| **R5-B2** | high bug | chat edit-and-rerun sends truncated history, not stale closure | `edcbca4` | vitest |
| **R5-B3** | high bug | serialize shared-connection journal writes (commit() no longer flushes another session's open txn) | `ff8720a` | async interleave test |
| **R5-P1** | high perf | O(K²)→O(K) tool-call arg assembler | `e4f7e54` | bench ~2s→~6ms |
| **R5-P3** | high perf | bound subagent-store terminal retention | `d88a7cb` | growth-bound test |
| **R5-C1** | high | onboard `reuse` image-provider awaits async probe (was always 409) | `6dbb568` | route test 409→200 |
| **R5-C2** | high | wire chat model-picker open (was unreachable) | `233081d` | vitest |
| **R5-C3/C4** | completeness/docs | /nodes honest panel; embeddings/plugin-callback/MCP-server docs aligned to reality + specs | `709a0df`,`1c75fdf` | doc/UI before-after + ARCH_DEBT |
| **R5-Q1** | high ci | re-enable layering guard (phantom pkg) + ruff `external` (1663→1153) + mypy scope + ci-status.md truth | `0c281b6` | `lint-imports` GREEN + now gating |

**Headline correction (R5).** The required CI `gate` check was **red at HEAD** and `docs/ci-status.md` claimed it was nearly green (7 mypy / 17 ruff). Reality: ruff 1663, mypy 155, and import-linter was *silently disabled* (phantom `corlinman_embedding` root package aborted the whole layering contract). R5 fixed the structural breakage (guard re-enabled + actually gating; ruff config aligned, zero churn → 1153; mypy scoped; doc corrected) and surfaced 3 real agent→server layering violations the abort had masked. The residual ruff (1153) + mypy (156) are **genuine debt deferred to a dedicated greening initiative** (`ARCH_DEBT.md` #R5-Q1) — intentionally not a mass-churn sweep, per the user's scope call.

**Deferred this round:** the large completeness features (embeddings/dense-retrieval, plugin async-callback, MCP `/mcp` server bind, `/nodes` runner endpoint) were honestly aligned in docs/UI + given precise wiring specs in `ARCH_DEBT.md`, not built — they need product decisions + multi-part wiring (R4-F3 precedent).

---
_(v3 report retained below.)_

# corlinman · audit FINAL REPORT (v3 — after Round 4)

**Last updated**: 2026-05-29 (post-Round-4).
**Loop**: 4 rounds + 1 user-directed cleanup pass.

## ⚠️ Correction to the v2 report (important)

v2 claimed: *"Default scheduled jobs (update-check, darwin-curate) actually run."*
**That was false.** R3-002 fixed `dispatch()` routing but **nothing ever called
`scheduler.runner.spawn()`** — the tick loops were never created, so the default
cron jobs never fired. This is exactly the "code exists ≠ wired and running"
trap. Round 4 **F1** wired the runtime into the lifespan and **proved it by
booting the gateway** (3 tick tasks spawned, a job fired 3× in 2.6s). The v2
claim is now true for the first time, with real-run evidence.

## Round 4 summary (2026-05-29)

8 commits (`c53b19a`..`0b8befa`): 7 functional fixes + 1 honest doc-alignment.
Post-merge regression **2555 passed / 2 skipped** (+52 tests, 0 regressions).

| #ID | sev | fix | commit | real-run verification |
|-----|-----|-----|--------|----------------------|
| **F1** | critical | scheduler runtime spawned in lifespan (+app_state threading, +SchedulerHandle.trigger) — default cron jobs fire | `c53b19a` | booted gateway: 3 tick tasks, per-second job fired 3×/2.6s, trigger() dispatched |
| **F2** | high | real `PlaceholderEngine` ported + `{{episodes.*}}` resolver wired | `f39b147` | seeded episodes.sqlite → `{{episodes.recent}}` resolves to DB content, drops from unresolved_keys |
| **D1** | high | OAuth callback-state validation across all 4 PKCE flows (constant-time) | `19f39fa` | mismatched/absent state → 400 reject; corrected a test that had encoded the bug |
| **D2** | med | codex `AsyncOpenAI` client closed on all chat_stream paths | `a851c87` | tracking-client test: closed on success/error/cancel + token-recovery |
| **D3** | med | 429 `Retry-After` → `RateLimitError.retry_after_ms` (both providers) | `cc3167e` | strengthened bug-pinning test to assert 7000ms |
| **D4** | high | mtime-cache anthropic OAuth credential reads | `cc3167e` | counter test: 1 read across repeats, re-read on mtime change |
| **D5** | high | `React.memo` MessageBubble — no markdown re-parse storm | `896ee24` | render-count test: settled bubbles don't re-render on pending-only delta |
| **D6** | med | tie-break `list_session_summaries` so same-ms previews don't mix turns | `2e287e2` | same-ms-turn test deterministic over 5 runs |
| **F3** | — | doc-aligned `run_in_background` to its real (not-implemented) behavior | `0b8befa` | existing test confirms the clean rejection sentinel |

**F3 was deliberately not built as a feature**: contract research showed a safe
factory is only a pure-LLM, no-tools MVP that is near-useless without a
journal-notification subsystem (missing from all backends) + `SubagentRequest`
schema changes + a product decision on what tools an autonomous background agent
may use. Shipping that would be a misleading half-feature. The honest audit
action — align the docs, keep the safe rejection, file a precise spec
(`ARCH_DEBT.md`) — was taken instead.

---
_(v2 report retained below.)_

# corlinman · audit FINAL REPORT (v2 — after cleanup pass)

**Last updated**: 2026-05-29 (post-cleanup).
**Loop**: 3 rounds + 1 user-directed cleanup pass.

## Cumulative metrics

| | Round 1 | Round 2 | Round 3 | Cleanup | Total |
|---|---|---|---|---|---|
| Findings filed | 74 | 65 | 61 | — | **200** |
| Selected | 5 | 5 | 5 | 7 | **22** |
| Closed | 5 | 5 | 5 | 7 | **22** |
| Critical closed | 1 | 1 | 2 | 5 | **9** |
| High closed | 4 | 4 | 3 | 1 | **12** |
| New regressions introduced | 0 | 1 (caught + closed in R3-001) | 0 | 0 | net 0 |
| New tests added | 19 | 8 | ~11 | ~119 (98 pytest + 21 vitest) | **~157** across 22 files |

Post-cleanup full regression: **2503 passed / 2 skipped** (Python suite: server + providers + channels + hooks).

## Closed by category

### Critical (9)
- **R1-001** sec — wired `install_api_key_middleware` (was defined but never installed → unauth /v1/* RCE on default 0.0.0.0:8080 bind)
- **R2-001** sec — extended api-key gate to /canvas /memory /channels /plugin-callback legacy aliases
- **R3-002** bug — scheduler.dispatch() now routes `run_tool`/`run_agent` to BUILTIN_ACTIONS (default cron jobs were silently failing)
- **R3-003** sec — retargeted install + release workflow at `sweetcornna/corlinman` (abandoned `ymylive` was a supply-chain hijack waiting to happen)
- **SEC-204** sec — gRPC agent server refuses non-loopback bind without explicit `CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC=1`
- **TEST-001** test — chat_approve handler 12-case branch coverage (was unprotected against regressions)
- **TEST-002** test — chat-stream client-disconnect cancel propagation test (cost-leak surface was unprotected)
- **TEST-003** test — failover error hierarchy + Anthropic vendor mapping (62 tests; the failover contract had 0 direct assertions)
- **TEST-004+005** test — ui/lib SSE wrappers covered (21 vitest tests across 2 files)
- **QUAL-001+004** docs — README no longer sells deleted `newapi` provider or non-existent HNSW+reranker RAG

### High (12)
- **R1-002** bug — preserved `journal_turn_id` in HookEvent emissions across 3 branches of agent_servicer
- **R1-003** bug+perf — close AsyncOpenAI/AsyncAnthropic on every chat_stream path
- **R1-004** sec — sandboxed artifact-panel.tsx SVG/HTML previews
- **R1-005** bug — close prior SSE before opening next live stream
- **R2-002** bug — atomic inbox increment_retry
- **R2-003** bug — strong-ref fire-and-forget tasks
- **R2-004** sec — defusedxml for wechat webhook parse
- **R2-005** sec — canvas session id 32→192 bits
- **R3-001** bug — guard increment_retry against status resurrection (closes R2-002 gap)
- **R3-004** bug — enforce subagent per-tenant cap as documented
- **R3-005** bug — UpgradeStatus.is_terminal() includes "stalled"
- **SEC-007** sec — server-side must_change_password gate (closes first-boot admin/root window)

## Remaining backlog

`audit/ISSUES.md` carries **~49 open items** triaged but not selected this loop:

- **3 critical-leaning sec items** still queued: SEC-209 skill-installer Unicode/TOCTOU, SEC-205 (mostly closed via R3-003 — namespace reservation pending in ARCH_DEBT), and lingering CSP/sandboxing follow-ups
- **~10 high perf items**: PERF-003/004/005/006/009/010/011/012/014/108/202/203/204 (provider caching, skill registry rglob in async, O(K²) tool-arg assembler, UI markdown re-parse storm, UI bundle split, etc.)
- **~10 high quality items**: QUAL-006 phantom embedding layer, QUAL-007/008/009 dead-code modules, QUAL-010/011/013/112 widespread copy-paste, QUAL-101/102/202/203 channels-package dead-code + 4253-LOC SRP violator
- **~5 high test items**: TEST-006 MCP server, TEST-007 untested admin routes (canvas/channels/memory/wechat_webhook/plugin_callback), TEST-008/009/010 etc.
- **Discovered during cleanup (filed, not fixed)**: Anthropic + OpenAI mappers both drop `Retry-After` header on 429 — `RateLimitError.retry_after_ms` is always None. Easy follow-up. See `audit/evidence/cleanup/TEST-003/discovered.md`.

## ARCH_DEBT (operator-action follow-ups)

`audit/ARCH_DEBT.md`:
- **R3-003 follow-up** — manually reserve `ymylive` namespace on github.com + ghcr.io to prevent re-registration. Procedure in `audit/evidence/round-3/R3-003/SQUAT_RESERVE.md`.

## Risk posture (before → after)

- **Network attack surface**: every /v1/* + 4 legacy alias prefixes require api-key auth. Canvas session ids 192-bit. Artifact previews sandboxed. WeChat parser bomb-proof. **Admin/root first-boot window closed** — server-side gate refuses every non-rotation route until password change. gRPC agent refuses public bind unless explicit opt-in.
- **Resource correctness**: providers + UI SSE + inbox retries + hook bus tasks no longer leak. Hook subscribers see real turn_ids. Subagent cap is now per-tenant as documented. Default scheduled jobs (update-check, darwin-curate) actually run. Stalled upgrades exit progress SSE.
- **Test coverage**: chat-approve handler + chat-stream cancel + provider failover error hierarchy + Anthropic vendor error mapping + ui/lib SSE wrappers + must_change_password gate + gRPC bind safety — all now regression-protected. +157 new tests across 22 files.
- **Operator surface**: install.sh + release pipeline point at the live namespace; README accurately describes what ships (BM25 only, no newapi). Documentation no longer over-promises features that don't exist.
- **Supply chain**: install path no longer hijackable by `ymylive` re-registration once the operator completes the SQUAT_RESERVE follow-up.

## Recommended next steps

1. **Operator: reserve `ymylive` namespace** on github.com + ghcr.io (5 minutes, removes the residual supply-chain risk surface in ARCH_DEBT).
2. **Fix the 429 Retry-After drop** discovered during TEST-003 — file in `audit/evidence/cleanup/TEST-003/discovered.md`. Two-line fix in `anthropic_provider.py` + `openai_provider.py`; +1 test asserting the header round-trips into `RateLimitError.retry_after_ms`.
3. **Schedule a "quality cleanup" round** for the `_now_ms`/RFC3339/`DEFAULT_TENANT_ID` deduplication and `corlinman-channels/service.py` 4253-LOC split — large but mechanical.
4. **Cover the remaining untested admin routes** (TEST-006/007/008/009/010 — MCP server, /admin/canvas, /admin/channels, /admin/memory, /admin/wechat_webhook, /admin/plugin_callback, home_channel_store, admin_session TTL, openai_compatible error classification).

## Audit-trail artefacts

- `audit/ISSUES.md` — full triage + open backlog + closed-this-loop tables
- `audit/PROGRESS.md` — round-by-round narrative + metrics
- `audit/ARCH_DEBT.md` — operator follow-up queue
- `audit/evidence/round-{1,2,3}/R*/` + `audit/evidence/cleanup/{TEST,QUAL,SEC}*/` — per-fix before.log + after.log + regression.log + (sec) poc.md + (TEST-003) discovered.md
- `audit/evidence/{round-2,round-3,cleanup}/POST_MERGE_REGRESSION.log` — canonical post-merge full-suite runs (R2: 2397/2; R3: 2405/2; Cleanup: 2503/2)
- Git log — 22 commits between `85b1560` (R1-001) and `2ded420` (TEST-002); each independently reviewable + revertable

22 fixes, 157 new tests, 0 net regressions left open, full 2503-test Python suite green. Cleanup pass complete.
