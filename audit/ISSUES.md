# corlinman · ISSUES.md

> Open issue queue. Sorted by `severity × cost × blocking`.
> Format: `#ID | severity | category | file:line | confidence | est_cost | summary`

## Rounds 7-9 — closed (backlog clearance + CI greening + feature completion)

| #ID | sev | summary | commit |
|-----|-----|---------|--------|
| R7-B1 | med (bug) | promote late streamed OpenAI tool-call id (BUG-006) | 9b3eca5 |
| R7-B1/B2 | med (bug) | declarative header-auth honored (openai + anthropic/gemini); query_param explicit-reject | 9b3eca5 bdce5e6 |
| R7-P1 | med (perf) | memory-host recall O(N)→O(limit) SQL | dc4c6a6 |
| R7-AR | med (completeness) | auto-resume stops false-positive 'resumed' for undrained channels | 7d83c99 |
| R7-PERF006 | high (perf) | list_session_summaries correlated-subquery → window function | eeaa10f |
| R7-BUG010 | med (bug) | begin_turn colliding turn_id fallthrough → collision-free insert | eeaa10f |
| R7-PERF008 | med (perf) | episode batch insert (one tx/commit) | 0e247ff |
| R7-PERF010/012 | med (perf) | UI selective reduceEvent clone + model-picker no fan-out | 3283f93 |
| R7-SEC012 | med (sec) | web-fetch DNS-rebind TOCTOU → pin validated IP | 75e2b94 |
| R7-SEC008 | med (sec) | admin-provider probe blocks metadata/link-local (allows loopback/private) | 4a62c80 |
| R7-QUAL007 | med (qual) | delete dead session_query.py | 5ebc745 |
| R8-ruff | high (ci) | py-ruff 1176→0 (autofix + config-align + fixed RUF006/B012/B023/RUF009/F401 real bugs) | c0fa47d |
| R8-mypy | high (ci) | py-mypy 166→0 (471 files; root-cause, net-negative type-ignores) | eef328b |
| R9-SEC011 | low (sec) | constant-time username compare (timing oracle) | a8abddb |
| R9-SEC009 | low (sec) | conditional Secure cookie on https | a8abddb |
| R9-TEST007 | high (test) | 31 route tests (canvas/channels/memory/wechat/plugin_callback) | 0731ad5 |
| R9-voice-store | — (feature) | durable SqliteVoiceSessionStore (NEW-fhfunc-4 session half), real-run verified | d953fe7 |

Final full regression: **4553 passed / 4 skipped** (from 4363 baseline; 0 regressions). All 3 Python CI gate jobs GREEN (ruff ✓ mypy ✓ boundary-check ✓).

**Dropped:** SEC-008 first attempt (blanket SSRF guard broke self-hosted relays) → redone surgically (R7-SEC008).
**Residual (product/design decisions — aligned + spec'd in ARCH_DEBT, NOT built blind):** evolution apply→rollback (agent self-mutation, HIGH-RISK), plugin async-callback (autonomous tool policy), embeddings impl (provider/cost), MCP /mcp bind (tool-exposure; IDOR pre-fixed R6), placeholder id-stamping (seam shipped R6-G8), voice transcript→chat bridge, goals/identity ingest, /nodes endpoint+UI. Low-value: QUAL-010/011/013, SEC-010.

## Round 6 — closed (commits below table)

| #ID | severity | summary | commit | verify |
|-----|----------|---------|--------|--------|
| R6-REG1 | **critical** (reg) | native gateway runnable as unprivileged user — ExecStart→venv console-script + HOME + .venv ownership (R5-S3 broke startup) | 1795b35 | static tests; real-VM required |
| R6-REG2 | high (reg) | coalesce parallel tool_results into one Anthropic/Bedrock user turn (R5-B1 broke multi-tool rounds) | 7d9c4d3 | real-run (G2-parallel-tools) |
| R6-REG3 | high (reg-sec) | voice WS token via subprotocol/header; query no longer honored → kills access-log key leak (R5-S1) | 47a40d0 | real-run (G3-voice-subprotocol) |
| R6-CONC | high (bug) | identity store tx_lock on single-statement writes (R5-B3 recurrence → orphan rows) | 822e37c | async interleave test |
| R6-PERF | high (perf) | batch streaming-delta journal writes off the hot path (was per-token INSERT+commit) | 511957a | emitter batch/flush test |
| R6-SEC-brain | high (sec) | agent-brain blocks/redacts secrets before vault write (BLOCKED + enforce auto_write_max_risk) | ff13621 | secret→BLOCKED test |
| R6-BUG-wstool | high (bug) | wstool from_dict TypeError→ValueError + reader cleanup in finally (no runner leak/hang) | 9c850ef | malformed-frame test |
| R6-SEC-mcp | high (sec) | MCP per-token tenant scoping on resource list/read (cross-tenant IDOR; latent) | d0bad74 | cross-tenant denial test |
| R6-G8 | completeness | persona/user/goals placeholder resolver adapter+seam (activation spec'd ARCH_DEBT #R6-G8) | c98ac4c | seam test + boot-once warning |
| R6-BUG-google | medium (bug) | GoogleProvider real multimodal parts (was list-repr flattening) | 0a4cc42 | multimodal request test |
| R6-BUG-ui | medium (bug) | GATEWAY_BASE_URL prefix on session-cost + upgrade-SSE fetchers | a549ddb | vitest base-url |
| R6-TEST-mcp | high (test) | token_config_to_acl + MCP server build coverage | 1c3a5df | new tests green |
| R6-TEST-stores | high (test) | home_channel_store + admin_session TTL/gc coverage | e994f23 | new tests green |
| R6-TEST-voice | high (test) | voice money/quota (cost.py + budget.py) coverage | 2400625 | new tests green |
| R6-C-align | docs | evolution apply/rollback, goals, identity-ingest, voice-persistence aligned + specs | c755a86 | doc before/after + ARCH_DEBT |

Post-merge regression: **4470 passed / 4 skipped** (`audit/evidence/round-6/POST_MERGE_REGRESSION.log`). +107 tests vs 4363 baseline; 0 regressions; import-linter KEPT.

**Dropped this round:** SEC-008 (admin-provider base_url SSRF) — the candidate `is_safe_host` guard breaks legitimate self-hosted LLM relays (Ollama/vLLM on loopback/private) and doesn't stop the public-host key-exfil vector; reverted, re-spec as metadata/link-local-only.
**Deferred (ARCH_DEBT specs filed):** evolution apply→rollback chain (HIGH-RISK self-mutation, needs product decision), goals/identity-ingest drivers, voice SqliteVoiceSessionStore, persona/user/goals placeholder entrypoint plumbing (#R6-G8 seam ready). Still-open Mediums: SEC-012 DNS-rebind, BUG-006/010, auto_resume dispatch, declarative auth_kind, memory-recall full-scan, PERF-006/008/010/012, dead code (QUAL-007/010/011/013).

## Round 5 — closed (commits below table)

| #ID | severity | summary | commit | verify |
|-----|----------|---------|--------|--------|
| R5-S1 | **critical** (sec) | authenticate /v1/voice WS handshake (BaseHTTPMiddleware can't cover WS scope) — unauth → 4401 before provider open; tenant bound to verified key, not spoofable X-Tenant-Id | 8017987 | real-run (ASGI route): unauth/invalid→4401, valid→started |
| R5-S2 | high (sec) | sanitize tenant_id/session_id in voice audio paths (`../` traversal on retained-audio write) | 8017987 | unit: traversal raises VoicePathError |
| R5-S3 | high (sec) | native install runs gateway as unprivileged user + root-executed upgrade scripts re-owned root:root (closes LPE) | 23a424b | static-source tests (real-VM deferred) |
| R5-B1 | high (bug) | Anthropic+Bedrock emit tool_use/tool_result blocks on multi-round tool input (was dropping tool_calls → every post-first-tool turn failed) | 6412213 | real-run: both adapters emit correct blocks |
| R5-B2 | high (bug) | chat edit-and-rerun sends truncated history instead of stale closure | edcbca4 | vitest: request body == truncated |
| R5-B3 | high (bug) | serialize SqliteJournalBackend writes so a bare commit() can't flush another session's open BEGIN IMMEDIATE txn | ff8720a | async interleave test |
| R5-P1 | high (perf) | O(K²)→O(K) streaming tool-call arg assembler (list+join) | e4f7e54 | bench ~2s→~6ms (4k frags) |
| R5-P3 | high (perf) | bound PersistentSubagentStore terminal retention (cap 512) — unbounded mem/disk + O(N) write-amp | d88a7cb | growth-bound test |
| R5-C1 | high (completeness) | onboard finalize-image-provider `reuse` awaits async probe (was always 409) | 6dbb568 | route test 409→200 |
| R5-C2 | high (completeness) | wire chat model-picker open handler (picker was unreachable) | 233081d | vitest |
| R5-C3 | completeness | /nodes honest "not available" panel + stop 5s mock poll (no backend endpoint exists) | 709a0df | vitest + ARCH_DEBT spec |
| R5-C4 | docs | embeddings / plugin async-callback / MCP-server docs aligned to reality + ARCH_DEBT specs | 1c75fdf | doc before/after quotes |
| R5-Q1 | high (ci) | re-enable layering guard (phantom pkg removed; boundary-check now gates; 3 upward imports grandfathered) + ruff `external` (1663→1153, 0 churn) + mypy scope + ci-status.md corrected | 0c281b6 | lint-imports GREEN; ruff/mypy residual filed |

Post-merge regression: **4363 passed / 4 skipped** (`audit/evidence/round-5/POST_MERGE_REGRESSION.log`). +30 tests vs 4333 baseline; 0 regressions.

**Deferred (filed in ARCH_DEBT #R5-Q1):** py-ruff (1153 genuine — autofixable import churn + CJK-unicode rule policy call) and py-mypy (156 genuine type errors) remain red; a dedicated greening initiative, intentionally not a mass-churn sweep this round. The 3 grandfathered agent→server layering violations also tracked there.

## Round 4 — closed (commits below table)

| #ID | severity | summary | commit |
|-----|----------|---------|--------|
| R4-F1 | **critical** | scheduler runtime spawned in lifespan → default cron jobs actually fire (+app_state threading +SchedulerHandle.trigger). Refutes prior "default jobs run" claim. | c53b19a |
| R4-D1 | high (sec) | OAuth callback-state validation enforced across all 4 PKCE submit flows (constant-time) | 19f39fa |
| R4-F2 | high | real PlaceholderEngine ported + {{episodes.*}} resolver wired (memory auto-activates when a MemoryHost is published) | f39b147 |
| R4-D2 | medium (bug) | close AsyncOpenAI client on every codex chat_stream path (R1-003 leak missed in codex) | a851c87 |
| R4-D3 | medium (bug) | extract 429 Retry-After into RateLimitError.retry_after_ms (both providers) | cc3167e |
| R4-D4 | high (perf) | cache anthropic OAuth credential reads (mtime-keyed; was sync read+parse/request) | cc3167e |
| R4-D5 | high (perf) | React.memo MessageBubble — no markdown re-parse storm on streaming deltas | 896ee24 |
| R4-D6 | medium (bug) | tie-break list_session_summaries subqueries so previews don't mix same-ms turns | 2e287e2 |

Post-merge regression: **2555 passed / 2 skipped** across server + providers + channels + hooks (`audit/evidence/round-4/POST_MERGE_REGRESSION.log`). +52 tests vs prior 2503 baseline; 0 regressions.

**R4-F3 (background subagent dispatch)** — under evaluation; fails honestly today (clean `run_in_background_not_implemented`), requires provider/model + tool-allowlist policy. See `audit/evidence/round-4/F3-bg-subagent/analysis.md`.

### Backlog items found RESOLVED / not-reproducible on HEAD during R4 recon
- TEST-014 → FIXED (test_tag_rebalance.py shipped, commit 8226451)
- PERF-014 → CANT_REPRO (subagent fan-out capped at SUBAGENT_SPAWN_MANY_MAX_TASKS=3)
- BUG-011 → CANT_REPRO (min() branch unreachable; counters move in lockstep + >0 guard)
- SEC-209 → CANT_REPRO (skill installer hardened: symlink/traversal/size guards + atomic replace)
- QUAL-019 → CANT_REPRO (only one `class AgentCard`; others are differently-named classes)

### Still-open backlog confirmed on HEAD (not selected R4)
SEC-008 (provider base_url unguarded, admin-config), SEC-009 (admin cookie no Secure — documented design choice), SEC-010 (bot_name reflected, text/plain), SEC-011 (+auth.py:263 username-compare timing oracle), SEC-012 (DNS-rebind TOCTOU in web fetch), SEC-013 (no /admin/login rate-limit); BUG-006 (tool-call late-id), BUG-008 (CancelledError into retry classifier — latent), BUG-010 (begin_turn fabricates turn_id after 20 collisions); PERF-005/006/007/008/009/010/011/012/013/015 (+new: MessageBubble done as D5, context_assembler re-scan); QUAL-006 (phantom embedding layer) +new non-total import-linter, QUAL-007/008/009 (dead modules) +new session.py/cancel.py stubs, QUAL-010 (29 _now_ms), QUAL-011/012/013/015, doc-drift (README v1.1.0/Ten-pages, AgentCard Rust-engine mention); TEST-006/007/008/009/010 (+new: token_config_to_acl cross-tenant ACL untested, Telegram webhook _verify_secret untested); functional: /nodes page永久空 (fetchRunnersMock), plugin invoke 501 (sandbox symbol mismatch), QQ reconnect 501.

## Cleanup pass — closed (post-goal-clear, on-demand)

| #ID | severity | summary | commit |
|-----|----------|---------|--------|
| TEST-001 | critical | chat_approve handler 12 branch-coverage tests | 07e14c4 |
| TEST-002 | critical | chat-stream client-disconnect cancel propagation test | 2ded420 |
| TEST-003 | critical | failover error hierarchy + Anthropic vendor mapping (62 tests) | 760fd96 |
| TEST-004+005 | critical | ui/lib/sse.ts + ui/lib/sessions/event-stream.ts (21 vitest) | 07e14c4 |
| QUAL-001+004 | critical | README newapi row removed + RAG downgraded to BM25-reality (+QUAL-005 bonus) | 2bc9868 |
| SEC-007 | high | server-side must_change_password gate (13 tests) | 79b0068 |
| SEC-204 | critical | gRPC agent refuses non-loopback bind unless ALLOW_PUBLIC=1 (13 tests) | e03ba1f |

Post-cleanup regression: **2503 passed / 2 skipped** across server + providers + channels + hooks (`audit/evidence/cleanup/POST_MERGE_REGRESSION.log`).

Discovered during cleanup (filed, not fixed): Anthropic 429 mapper drops Retry-After header → `RateLimitError.retry_after_ms` is always None. Same pattern in openai_provider. Details in `audit/evidence/cleanup/TEST-003/discovered.md`.

## Round 3 — closed (commits below table)

| #ID | severity | summary | commit |
|-----|----------|---------|--------|
| R3-001 | high | inbox.increment_retry guard against status resurrection (closes R2-002 regression) | 829760b |
| R3-002 | critical | scheduler dispatch() now routes run_tool/run_agent to BUILTIN_ACTIONS | 0e4e877 |
| R3-003 | critical | retarget install + release workflow at sweetcornna/corlinman (supply-chain) | 3b8de4b |
| R3-004 | high | enforce subagent per-tenant cap as documented (option-a semantic fix) | bdfda2b |
| R3-005 | high | upgrader treats 'stalled' as terminal so progress SSE exits | 87cb122 |

Post-merge regression: **2405 passed / 2 skipped** across corlinman-server + providers + channels + hooks (see `audit/evidence/round-3/POST_MERGE_REGRESSION.log`).

## Round 3 — selected for fix (5, file-disjoint → parallel)

| #ID | severity | category | file:line | conf | est | summary | merged-from |
|-----|----------|----------|-----------|------|-----|---------|-------------|
| R3-001 | high | bug+regression | corlinman-server/.../inbox.py:172 | confirmed | XS | **R2-002 regression**: atomic `UPDATE … SET status=CASE … END WHERE id=?` has no status guard → a stray increment_retry against a `done`/`dead` row resurrects it back to `pending`. Strengthen R2-002's race test to also assert `error`/`updated_at_ms` survive. | BUG-206 + TEST-202/203 |
| R3-002 | critical | bug | corlinman-server/.../scheduler/runner.py:438-446 | confirmed | S | `dispatch()` routes `kind=="run_tool"/"run_agent"` to `_emit_failed("unsupported_action")` instead of `BUILTIN_ACTIONS`. The two default cron jobs (`system.update_check`, `evolution.darwin_curate`) silently never run — admin "fire now" works so it shipped green. | QUAL-201 |
| R3-003 | critical | sec | deploy/install.sh + .github/workflows/release-image.yml + corlinman-upgrader.sh + AI_DEPLOY.md | confirmed | S | install.sh + release workflow still reference `ymylive/corlinman` namespace (repo transferred to `sweetcornna`; GH redirect masks it today). Re-registration of `ymylive` on github.com or ghcr.io = supply-chain hijack of every install + every documented upgrade path. | SEC-205 |
| R3-004 | high | bug | corlinman-server/.../system/subagent/dispatcher.py:280-301 | confirmed | S | `_max_concurrent_per_tenant` claim is a lie — the snapshot counts every in-flight subagent across ALL tenants (no `tenant_id` field on SubagentRequest). One noisy tenant starves the rest. **Decision needed**: semantic fix (add tenant_id) vs doc fix (rename `_global`). Prefer doc fix to avoid public API/env-var churn unless tenant isolation is required this round. | BUG-202 + QUAL-204 |
| R3-005 | high | bug | corlinman-server/.../system/upgrader/state.py:107 + native_upgrader.py:285 + docker_upgrader.py:218 | confirmed | XS | `UpgradeStatus.is_terminal()` excludes `"stalled"` while `is_in_flight()` includes it. SSE `progress()` loop checks only `is_terminal()` → polls forever (500ms tick) for any stalled upgrade. | BUG-201 + BUG-210 |

### Conflict graph
| Fix | Files |
|-----|-------|
| R3-001 | corlinman-server/inbox.py + tests/test_inbox.py |
| R3-002 | corlinman-server/scheduler/runner.py + tests/scheduler/ |
| R3-003 | deploy/install.sh + .github/workflows/release-image.yml + deploy/corlinman-upgrader.sh + deploy/AI_DEPLOY.md |
| R3-004 | corlinman-server/system/subagent/dispatcher.py + tests/system/subagent/ |
| R3-005 | corlinman-server/system/upgrader/state.py + native_upgrader.py + docker_upgrader.py + tests/system/upgrader/ |

All file-disjoint → all parallel.

## Round 2 — closed (commits below table)

| #ID | severity | summary | commit |
|-----|----------|---------|--------|
| R2-001 | critical | extend api-key gate to /canvas, /memory, /channels, /plugin-callback | 42c55e1 |
| R2-002 | high | atomic increment_retry closes inbox concurrent-call race | a093ead |
| R2-003 | high | strong-ref fire-and-forget tasks (user_correction + hook bus) | 3071e0c |
| R2-004 | high | defusedxml against billion-laughs in wechat webhook | d93588c |
| R2-005 | high | canvas session id 32→192 bits | 96fa525 |

Post-merge regression: **2397 passed / 2 skipped** across corlinman-server + providers + channels + hooks (see `audit/evidence/round-2/POST_MERGE_REGRESSION.log`).

## Round 2 — selected for fix (5, file-disjoint → all parallel)

| #ID | severity | category | file:line | conf | est | summary | merged-from |
|-----|----------|----------|-----------|------|-----|---------|-------------|
| R2-001 | critical | sec | gateway/middleware/auth.py:53 (DEFAULT_PROTECTED_PREFIXES) | confirmed | S | R1-001 only closed `/v1/*`; the legacy aliases `/canvas/*`, `/memory/*`, `/channels/*`, `/plugin-callback/*` are the same routes under different prefixes and remain unauth → unauth memory wipe, unauth plugin-callback poisoning of in-flight agent loops, unauth canvas SSE subscribe | SEC-101 |
| R2-002 | high | bug | corlinman-server/.../inbox.py:164-189 | confirmed | S | `increment_retry` does SELECT-then-UPDATE in Python; two concurrent calls race → lost increment → message retries forever instead of being marked dead | BUG-102 |
| R2-003 | high | bug | gateway/evolution/signals/user_correction.py:444-460 + corlinman-hooks/bus.py:463 + .../user_correction.py:361 | likely | S | `asyncio.create_task(_handle_event(...))` fire-and-forget with no strong ref → CPython GC can reap mid-flight per the asyncio docs footgun → silent signal drop under load | BUG-101 |
| R2-004 | high | sec | corlinman-channels/.../wechat_official.py:204-217 | confirmed | S | `xml.etree.ElementTree.fromstring` on attacker-controlled webhook body → billion-laughs / entity-expansion DoS | SEC-104 / BUG-108 |
| R2-005 | high | sec | gateway/routes/canvas.py:71-72 | confirmed | XS | `_new_session_id() = "cs_" + uuid4().hex[:8]` → 32-bit session token guarding canvas SSE + frame write; with TTL=600s and >1 active session, scanner finds a live id in ~minutes | SEC-105 |

### Conflict graph (Phase 3 concurrency)

| Fix | Files |
|-----|-------|
| R2-001 | gateway/middleware/auth.py + tests/gateway/routes/test_chat_requires_auth.py |
| R2-002 | corlinman-server/inbox.py + its tests |
| R2-003 | gateway/evolution/signals/user_correction.py + corlinman-hooks/bus.py + their tests |
| R2-004 | corlinman-channels/wechat_official.py + pyproject.toml (new dep: defusedxml) + tests |
| R2-005 | gateway/routes/canvas.py + tests |

All file-disjoint → all parallel.

### Micro fix plans

**R2-001 (critical SEC) — complete the protected-prefix list**
- Add `/canvas/`, `/memory/`, `/channels/`, `/plugin-callback/` to `DEFAULT_PROTECTED_PREFIXES`.
- Extend `test_chat_requires_auth.py` with one assertion per new prefix: unauth POST → 401.
- Risk: any internal cron/init that hits these endpoints without auth will break. Look for in-process callers first.
- Rollback: single commit revert.

**R2-002 (high BUG) — atomic increment_retry**
- Replace SELECT + Python-side `+1` + UPDATE with single `UPDATE inbox SET retries = retries + 1, status = CASE WHEN retries + 1 >= ? THEN 'dead' ELSE 'pending' END ... RETURNING retries`.
- Test: spawn two `asyncio.gather(increment_retry, increment_retry)` calls; assert final retries == 2 (today: 1).
- Risk: low — same row schema, atomic SQL.

**R2-003 (high BUG) — strong-ref fire-and-forget tasks**
- In `user_correction.py:444-460,361` and `corlinman-hooks/bus.py:463`: hold a module-level `set[asyncio.Task]`, `task.add_done_callback(_set.discard)` on each task spawn. Pattern already exists in `channels/service.py:802-803`.
- Test: subscribe to hook bus, force GC mid-emit, assert all spawned tasks observed by the subscriber.
- Risk: low — additive.

**R2-004 (high SEC) — defusedxml**
- Add `defusedxml>=0.7.1` to `corlinman-channels` package deps.
- Swap `from xml.etree.ElementTree import fromstring` → `from defusedxml.ElementTree import fromstring`.
- Test: malicious entity-expansion payload should now raise `EntitiesForbidden`; existing happy-path tests stay green.
- Risk: depends on whether existing valid payloads use entities (rare in WeChat callbacks).

**R2-005 (high SEC) — wider canvas session id**
- `_new_session_id()` → `"cs_" + secrets.token_urlsafe(24)` (192 bits).
- Test: assert returned id length ≥ 32 chars + entropy spot-check (Distinct over 10k generations).
- Risk: any persisted/leaked id changes shape — but ids are ephemeral (TTL=600s) so no migration needed.

## Round 1 — closed (commits listed below table)

| #ID | severity | summary | commit |
|-----|----------|---------|--------|
| R1-001 | critical | unauth /v1/* RCE — wired api-key middleware | 85b1560 |
| R1-002 | high | turn_id=None in HookEvent across 3 branches | 9e3ee89 |
| R1-003 | high | provider AsyncOpenAI/Anthropic client leak | e173705 |
| R1-004 | high | artifact-panel.tsx stored XSS (SVG + iframe sandbox) | 90070b2 |
| R1-005 | high | use-chat-stream.ts SSE leak on rapid resend | ac8db2c |

(Detailed fix plans + conflict graph retained below for audit-trail reference.)

## Round 1 — selected for fix (top 5, file-disjoint → all parallel)

| #ID | severity | category | file:line | conf | est | summary | merged-from |
|-----|----------|----------|-----------|------|-----|---------|-------------|
| R1-001 | critical | sec | gateway/lifecycle/entrypoint.py:2138-2147 + gateway/middleware/__init__.py | confirmed | M | `install_api_key_middleware` defined but never installed → /v1/* (chat, approve) is unauth; combined with auto-injected run_shell tool = unauth RCE on default 0.0.0.0:8080 bind | SEC-001 + SEC-004 |
| R1-002 | high | bug | corlinman-server/.../agent_servicer.py:1356-1476 | confirmed | S | `journal_turn_id = None` cleared **before** being passed into `HookEvent.TurnComplete/TurnErrored(turn_id=…)` in 3 branches → every hook subscriber sees `turn_id=None`, losing journal correlation | BUG-001 + BUG-002 + BUG-003 |
| R1-003 | high | bug+perf | corlinman-providers/.../{openai,anthropic}_provider.py | confirmed | M | `AsyncOpenAI`/`AsyncAnthropic` constructed per chat call, never `await client.close()` → leaks httpx pool every turn + rebuilds TLS per turn (30-80ms first-byte tax) | BUG-004 + BUG-005 + PERF-001 + PERF-002 |
| R1-004 | high | sec | ui/components/chat/artifact-panel.tsx:162-175 | confirmed | S | (a) SVG artifact rendered with `dangerouslySetInnerHTML` → script execution in admin origin; (b) HTML artifact iframe uses `allow-scripts allow-same-origin` srcDoc → script can call admin APIs with operator cookies. Stored XSS triggerable by any malicious LLM output. | SEC-002 + SEC-003 |
| R1-005 | high | bug | ui/lib/chat/use-chat-stream.ts:267-274,378-382 | likely | S | `closeLiveRef.current = openLiveEventStream(...)` overwrites prior close fn without invoking it → second send while first turn streams leaks the prior EventSource; multiple concurrent SSE consumers reduce into stale pending message | BUG-007 |

### Conflict graph (Phase 3 concurrency)

| Fix | Files | Conflicts with |
|-----|-------|----------------|
| R1-001 | `corlinman-server/.../gateway/{lifecycle/entrypoint.py, middleware/__init__.py, routes/chat.py?, routes/chat_approve.py?}` | none |
| R1-002 | `corlinman-server/.../agent_servicer.py` | none |
| R1-003 | `corlinman-providers/.../openai_provider.py, anthropic_provider.py` | none |
| R1-004 | `ui/components/chat/artifact-panel.tsx` | none |
| R1-005 | `ui/lib/chat/use-chat-stream.ts` | none |

All 5 file-disjoint → dispatch in parallel.

### Micro fix plans (Phase 3 briefs)

**R1-001 (critical SEC) — wire up api-key middleware**
- Root: `install_api_key_middleware(app, admin_db)` lives in `gateway/middleware/auth.py` but is not exported from `gateway/middleware/__init__.py`; `entrypoint.py:2140` does `getattr(mw, "install_api_key_middleware", None)` and silently no-ops. Result: every `/v1/*` route accepts unauth.
- Files to change: `gateway/middleware/__init__.py` (export), `gateway/lifecycle/entrypoint.py` (call after `admin_db` is ready). Also: bind `Depends(require_api_key())` to `chat_approve.handle_approve` if the middleware path-prefix doesn't already cover it.
- Risk: tests / dev workflows that hit `/v1/chat/completions` without auth. Mitigation: middleware must allow loopback or `/healthz` unauth as today; verify which tests POST without auth. If many tests POST without auth, add a test fixture that mints an api-key and stamps it.
- Verification: PoC `curl -s -X POST http://127.0.0.1:8080/v1/chat/completions -d '{...}'` returns 401 (before: 200 SSE). Full pytest run shows no new failures.
- Rollback: `git revert` — single commit.

**R1-002 (high BUG) — preserve turn_id in hook events**
- Root: `journal_turn_id = None  # consumed` runs before `HookEvent.Turn{Complete,Errored}(turn_id=journal_turn_id, ...)` in 3 branches.
- Files to change: `corlinman-server/src/corlinman_server/agent_servicer.py` lines 1356-1476.
- Risk: low — pure local refactor (capture into a local var before nulling).
- Verification: new unit test stubs the hook bus, runs one chat turn (success + error + catch-all), asserts `turn_id` propagated. Existing tests untouched.
- Rollback: single commit revert.

**R1-003 (high BUG+PERF) — fix provider client lifecycle**
- Root: `AsyncOpenAI(...)` / `AsyncAnthropic(...)` instantiated per chat call, no `.close()`, no `async with`. Leaks httpx pool per turn, also re-runs TLS per turn.
- Files to change: `openai_provider.py:166-251` (`_open` + `chat_stream` wrapper) and `anthropic_provider.py:241-317`.
- Approach: minimal-diff option — wrap stream iteration in `try/finally: await client.close()`. Larger refactor (cache client on instance) is the perf-001/002 fix; that's a follow-up.
- Risk: client lifetime tied to request — closing too early would kill in-flight streaming. Must close only after iteration completes / errors / cancels.
- Verification: new test using `respx` confirms `.close()` is called on success, exception, and cancel paths. Run existing provider tests to confirm no regression.
- Rollback: single commit revert.

**R1-004 (high SEC) — artifact-panel XSS hardening**
- Root: `artifact-panel.tsx:162-175` renders SVG with `dangerouslySetInnerHTML` and HTML iframe with `sandbox="allow-scripts allow-same-origin"`.
- Files to change: `ui/components/chat/artifact-panel.tsx`.
- Approach: (a) drop `allow-same-origin` from the iframe — `srcDoc` script can still run, just not in our origin. (b) SVG: render inside the same neutered iframe instead of `dangerouslySetInnerHTML` (one path, no new deps), OR sanitize with DOMPurify if already in tree. Check `package.json` for existing sanitizer first.
- Risk: legitimate SVG/HTML artifacts that linked to same-origin assets will lose that. Acceptable — artifacts are model output, treat as untrusted.
- Verification: vitest renders malicious payload, asserts `fetch('/admin/...')` is NOT callable from artifact (window.parent unreachable from `sandbox=""` iframe).
- Rollback: single commit revert.

**R1-005 (high BUG) — close prior SSE before reopening**
- Root: `closeLiveRef.current = openLiveEventStream(...)` overwrites without calling the previous close fn.
- Files to change: `ui/lib/chat/use-chat-stream.ts`.
- Approach: `closeLiveRef.current?.(); closeLiveRef.current = openLiveEventStream(...)`. Also add a guard on `runTurn` so a click during streaming doesn't fan out two turns.
- Risk: low — pure additive cleanup.
- Verification: vitest with two consecutive `runTurn()` calls confirms only one EventSource alive at a time; harness asserts `EventSource.close()` called on the prior handle.
- Rollback: single commit revert.

## Round 1 — open backlog (not selected this round; carried to Round 2)

### Critical / high (queued)

| #ID | severity | category | file:line | conf | est | summary |
|-----|----------|----------|-----------|------|-----|---------|
| QUAL-001 | critical | doc | README.md:422 | confirmed | XS | newapi listed as production provider but whole surface removed |
| QUAL-004 | critical | doc | README.md:58-60,339-345 | confirmed | XS | RAG claims usearch HNSW + cross-encoder; only BM25 exists |
| TEST-001 | critical | test | gateway/routes/chat_approve.py:142 | confirmed | S | approve endpoint has zero tests |
| TEST-002 | critical | test | gateway/routes/chat.py:493 | confirmed | M | SSE disconnect branch never tested |
| TEST-003 | critical | test | corlinman-providers/.../failover.py | confirmed | M | failover error mapping (Anthropic 401/429/529) untested |
| TEST-004 | critical | test | ui/lib/sse.ts | confirmed | M | core SSE wrapper untested |
| TEST-005 | critical | test | ui/lib/sessions/event-stream.ts | confirmed | M | live event-stream wrapper untested |
| SEC-005 | high | sec | gateway/oauth/anthropic_pkce.py:132-190 | confirmed | S | exchange_code never compares state → OAuth CSRF |
| SEC-006 | high | sec | gateway/routes_admin_b/oauth.py:372,491 | confirmed | S | xai/codex/gemini submit state check is conditional |
| SEC-007 | high | sec | gateway/lifecycle/admin_seed.py:38-39 | confirmed | M | admin/root default + must_change_password not server-enforced |
| BUG-006 | medium | bug | openai_provider.py:212-228 | confirmed | S | tool-call late real id silently dropped (elif unreachable) |
| BUG-008 | medium | bug | corlinman-providers/.../_retry.py:86-105 | likely | S | except BaseException catches CancelledError → cancel-eating retry |
| BUG-009 | medium | bug | agent_journal_backend.py:1306-1330 | confirmed | M | list_session_summaries mixes columns from two turns (no tie-breaker) |
| BUG-010 | medium | bug | agent_journal_backend.py:810-840 | confirmed | M | begin_turn retry exhaustion silently fabricates turn_id |
| PERF-003 | high | perf | anthropic_provider.py:136-185 | confirmed | S | OAuth credential file re-read every request |
| PERF-004 | high | perf | gateway/services/chat_bootstrap.py (refresh) + skills/registry.py:440-507 | confirmed | M | sync rglob+stat in async path every chat turn |
| PERF-005 | high | perf | gateway/services/direct_backend.py:294-322 | confirmed | M | O(K²) string `+=` in tool-call arg assembler |
| PERF-006 | high | perf | agent_journal_backend.py:1306-1330 | likely | M | /admin/sessions correlated subquery scales O(sessions × turns × msgs) |
| PERF-009 | medium | perf | ui/components/chat/markdown-message.tsx:86-99 | confirmed | M | full re-parse of markdown on every SSE delta |
| PERF-010 | medium | perf | ui/lib/chat/use-chat-stream.ts:236-250 | confirmed | M | deep-clone whole pendingMessage on every event |
| PERF-011 | medium | perf | ui/lib/api/chat.ts:137-158 | likely | S | O(N²) SSE buffer concat+split |
| PERF-012 | medium | perf | ui/components/chat/chat-model-picker.tsx:93-100 | likely | M | fan-out N parallel provider model probes on picker open |
| PERF-014 | medium | perf | corlinman-agent/.../subagent/tool_wrapper.py:1193-1211 | likely | M | unbounded `asyncio.gather` over child agent spawns |
| QUAL-006 | high | layering | .importlinter:14-18 | confirmed | XS | `corlinman_embedding` phantom layer reference |
| QUAL-007 | high | dead-code | corlinman-agent/.../session_query.py | confirmed | S | 238-line legacy SQLite client points at deleted Rust table |
| QUAL-008 | high | dead-code | corlinman-agent/.../approval_gate.py | confirmed | XS | 17-line stub module, no symbols, confuses contributors |
| QUAL-009 | high | dead-code | agent_journal_backend.py:2021-2080 | likely | S | RedisJournalBackend stub + env-var dispatch raises NotImplementedError |
| QUAL-010 | medium | dup | _now_ms copy-pasted across 15+ files | confirmed | S | one shared helper would replace 15 copies |
| QUAL-012 | high | naming | sessions.py:617-620 + agent_journal.py:234-242 | confirmed | XS | `_load_messages` semi-private exposed across module boundary (caused commit ef4e341) |
| QUAL-013 | medium | naming | 6 sites cross-importing `_foo` private symbols | confirmed | S | leading-underscore convention violated systematically |
| QUAL-015 | medium | doc-drift | docs/{providers,architecture,plugin-authoring,roadmap,milestones}.md | likely | S | live docs link into `rust/crates/...` paths scheduled for deletion |

### Medium / low (queued)

| #ID | severity | category | file:line | summary |
|-----|----------|----------|-----------|---------|
| QUAL-002 | high | doc-drift | README.md:5 | version badge stuck at 1.7.0, actual 1.8.13 |
| QUAL-003 | high | doc-drift | README.md:475 | claims 10 admin pages, actual 26 |
| QUAL-005 | medium | doc-drift | README.md:750 | Chinese 21 项 doctor claim vs actual 9 |
| QUAL-011 | medium | dup | 3+ packages reimplement RFC3339 formatter/parser |
| QUAL-014 | medium | dead | chat_bootstrap.py:48,95 | `MessageLike` "documentation anchor" referenced nowhere |
| QUAL-016 | low | type | claude_code_login.py:38,79,131 | `Dict[…]`/`Optional[…]` survives in py312 project |
| QUAL-017 | low | naming | 2 unrelated `_build_internal_request` funcs |
| QUAL-018 | low | srp | openai_compatible.py:69 | `supports() → False` collapses 2 concerns |
| QUAL-019 | low | naming | two `AgentCard` classes in different packages |
| QUAL-020 | low | dead | corlinman-user-model / corlinman-agent-brain | both packages have zero external consumers |
| SEC-008 | medium | sec | providers.py:551-1147 | provider base_url not SSRF-guarded |
| SEC-009 | medium | sec | gateway/routes_admin_a/auth.py:150 | session cookie missing Secure flag |
| SEC-010 | medium | sec | gateway/routes/wechat_webhook.py:101 | bot_name reflected without validation |
| SEC-011 | low | sec | _auth_shim.py:109 | username compare not constant-time |
| SEC-012 | medium | sec | corlinman-agent/.../web/_common.py:266 | TOCTOU between SSRF guard DNS lookup and httpx connect |
| SEC-013 | low | sec | gateway/routes_admin_a/auth.py:74 | no rate-limit on /admin/login |
| BUG-011 | low | bug | corlinman-evolution-engine/.../engine.py:512 | `min()` on empty dict possible if invariant drifts |
| PERF-007 | medium | perf | reasoning_loop.py:929 | `hash(repr(messages[0]))` per round |
| PERF-008 | medium | perf | corlinman-episodes/.../store.py:351 | 1 commit/row in episode insert batch |
| PERF-013 | medium | perf | gateway/services/chat_service.py:289 | `__import__("json")` + `bytes(...)` per tool done frame |
| PERF-015 | low | perf | reasoning_loop.py:1573 | unnecessary `list(messages)` copy per round |
| TEST-006 | high | test | gateway/mcp/server.py | entire MCP server untested |
| TEST-007 | high | test | gateway/routes/{canvas,channels,memory,wechat_webhook,plugin_callback}.py | 5 production routes untested |
| TEST-008 | high | test | home_channel_store.py | /sethome store untested |
| TEST-009 | high | test | gateway/middleware/admin_session.py | TTL/gc/sliding refresh untested |
| TEST-010 | high | test | openai_compatible.py | error-classification untested |
| TEST-011 | medium | test | ui/components/chat/{approval-prompt,chat-model-picker,chat-area}.tsx | core chat components no vitest |
| TEST-012 | medium | test | runner_pool/test_tool_events.py:138 | wall-clock-based heartbeat assertion → flaky CI |
| TEST-013 | medium | test | gateway/routes/chat.py | SSE wire-format never asserted (only spans) |
| TEST-014 | medium | test | corlinman-evolution-engine/.../tag_rebalance.py | rules untested in isolation |
| TEST-015 | low | test | gateway/services/chat_bootstrap.py | 430 LOC of bootstrap wiring untested |

## Closed

_(empty — first round)_
