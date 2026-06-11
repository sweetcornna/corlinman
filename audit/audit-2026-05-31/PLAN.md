# corlinman v1.15.0 — Whole-Project Audit Synthesis & Fix Plan

Date: 2026-05-31
Inputs: 16 read-only auditors, ~50 raw findings, deduplicated below.
Verification: high-severity structural claims were re-checked against HEAD source (file:line confirmed for the skill-state, hook-runner, tenant-scope, upgrade single-flight, feishu open_id, web_fetch fencing, calculator, onboarding-allowlist, and auto-rollback baseline items).

## Executive summary

The audited surface is broadly well-hardened. The highest-impact problems cluster in three themes:

1. **Process-global state on the shared singleton servicer.** `_active_skills` (skill allowed-tools) leaks across all sessions/tenants and never resets, and `subagent_stop`/`cancel_session` can abort *any* session by key with no ownership check. Both are reachable from the model and are the top-priority security items.
2. **C2/C3 wiring gaps in the split (gateway ⇄ standalone-agent) topology.** Several "implemented but unreachable in production" features: the PreToolDispatch blocking hook gate never runs in the standalone server (no HookRunner passed; `_app_state` never set), the `ask` approval verdict always fail-closes (no resolver wirable), `set_persona_stores` is never called, and `autonomous`/`turn_token_budget` auto-continue is never enabled by any caller.
3. **First-run / onboarding breakage.** The `must_change_password` gate 403s the real `finalize-*` onboarding-wizard steps because the allowlist is stale, blocking fresh installs end-to-end.

Performance hot-spots concentrate in the memory-host recall path (per-turn full-namespace `IN(...)` materialization that can crash on SQLite's variable limit, plus full-namespace JSON-decode for graph back-links).

Multi-tenant scoping is the recurring latent risk (tenant_scope middleware never installed; api-key revoke not tenant-scoped; api-key `scope` never enforced; evolution-store/engine drop tenant_id) — low blast radius today (single-operator prod) but activates the moment multi-tenant is turned on.

## Counts

| Severity | Defects | Completeness | Total |
|----------|---------|--------------|-------|
| Critical | 0 | 0 | 0 |
| High     | 4 | 3 | 7 |
| Medium   | 8 | 5 | 13 |
| Low      | 9 | 3 | 12 |
| **Total**| **21** | **11** | **32** |

(After dedup of the two `_active_skills` reports into one, and merging the two memory-host recall-path performance reports per concern.)

## Defect table (bug / security / performance)

| ID | Sev | Conf | Title | Files |
|----|-----|------|-------|-------|
| SEC-01 | High | high | Skill `_active_skills` is process-global on the shared singleton servicer — cross-session/tenant leakage, never reset | agent_servicer.py |
| SEC-02 | High | high | `subagent_stop`/`cancel_session` aborts ANY session by key with no ownership/tenant authz | agent_servicer.py |
| BUG-01 | High | high | C3 PreToolDispatch blocking hook gate never runs in standalone topology (no HookRunner wired; `_app_state` dead) | agent_servicer.py, main.py, gateway/grpc/agent_server.py |
| SEC-03 | High | high | calculator nested-pow bignum bomb blocks the event loop (synchronous, no timeout, exponent-only guard) | web/calculator.py, agent_servicer.py |
| PERF-01 | High | high | Namespace recall materializes whole-namespace id set as `IN(...)` bind list — O(namespace) per turn, crashes on SQLite var limit | memory_host/local_sqlite.py |
| BUG-02 | High | high | Upgrade single-flight: `stalled` (and orphaned `running`) counts as in-flight forever → permanent upgrade lockout | upgrader/state.py, native_upgrader.py, docker_upgrader.py |
| SEC-04 | Med | high | web_fetch advertises untrusted-content fencing but never wraps the body (dead `wrap_external_content` import) | web/fetch.py |
| SEC-05 | Med | high | Per-arg run_shell deny rules bypassable via shell-shape (only first shlex token matched) | permission.py, coding/shell.py |
| SEC-06 | Med | med | API-key routes trust `?tenant=`; revoke not tenant-scoped; tenant_scope middleware never installed | routes_admin_a/api_keys.py, tenancy/admin_schema.py, middleware/tenant_scope.py, lifecycle/entrypoint.py |
| SEC-07 | Med | high | `GET /admin/config` returns channel bot tokens / NapCat token in cleartext despite "redacted" contract | routes_admin_b/config.py, routes_admin_b/napcat.py, channels_runtime/__init__.py |
| BUG-03 | Med | high | Async pre-tool path drops tool-specific handler's mutated_args/inject_message (diverges from sync path) — production path | corlinman-hooks/runner.py |
| BUG-04 | Med | high | Subagent spawns never thread child_seq in prod → same-card children collide on session_key/agent_id/persona/mailbox | agent_servicer.py, subagent/tool_wrapper.py |
| BUG-05 | Med | high | read_file: a single line > MAX_READ_CHARS yields next_offset == offset → infinite re-read loop | coding/files.py |
| BUG-06 | Med | high | Boot teardown reads `state.extras` unguarded → AttributeError aborts all shutdown cleanup in degraded boot (leaks C2 stores) | lifecycle/entrypoint.py |
| PERF-02 | Med | high | Graph back-link expansion JSON-decodes EVERY metadata row in namespace on every successful recall | memory_host/local_sqlite.py |
| PERF-03 | Med | high | `GET /admin/curator/profiles` re-walks every profile's skills dir + YAML-parses every SKILL.md on every poll | routes_admin_b/curator.py, lifecycle/entrypoint.py, skills-registry/registry.py |
| SEC-08 | Med | high | vision_analyze forwards `url` with no SSRF check and accepts http:// despite https-only schema | image/analyze.py |
| BUG-07 | Med | high | OneBot parse_event raises on malformed numeric fields → tears down QQ WS (reconnect churn / message loss) | channels/onebot.py |
| BUG-08 | Med | high | Auto-rollback applier writes empty `metrics_baseline={}` which monitor's strict decoder always rejects → feature dead | auto-rollback/applier.py, monitor.py, metrics.py |
| BUG-09 | Med | high | evolution-store ProposalsRepo never persists tenant_id; meta cooldown hardcodes DEFAULT_TENANT_ID | evolution-store/repo.py, types.py |
| BUG-10 | Med | high | agent-brain IndexSync upsert hardcodes namespace "agent-brain" but query uses configured namespace → no hits under custom ns | agent-brain/index_sync.py |
| BUG-11 | Low | high | Assistant text reply never appended to history → auto-continue/Stop-hook re-runs lose prior output | reasoning_loop.py |
| BUG-12 | Low | high | `_truncate_tool_result` can return MORE than CORLINMAN_TOOL_RESULT_CAP when cap set low | reasoning_loop.py |
| BUG-13 | Low | med | apply_patch violates all-or-nothing (partial writes on commit failure; Add-then-Update of same path fails) | coding/patch.py |
| BUG-14 | Low | med | read_file: no size guard before reading whole image/PDF/notebook into RAM + base64 into model turn | coding/files.py |
| SEC-09 | Med | high | API-key `scope` stored + documented as enforced, but no scope check performed (declared-but-unwired authz) | middleware/auth.py, tenancy/admin_schema.py |
| BUG-15 | Low | high | ConfigWatcher debounce loop leaks the loser of its asyncio.wait pair each iteration; SIGHUP handler never removed on stop | core/config_watcher.py |
| SEC-10 | Low | med | Unescaped attachment filename interpolated into multipart Content-Disposition (form-field injection) | telegram_send.py, discord.py |
| SEC-11 | Low | med | Codex token-refresh failure embeds up to 400 chars of raw HTTP body into logged exception (token leak risk) | codex_provider.py, _codex_oauth.py |
| SEC-12 | Low | med | FileFetcher http branch lacks the SSRF guard the agent WebFetch has (latent; no untrusted caller today) | wstool/file_fetcher.py |
| PERF-04 | Low | med | MCP frame-size gate counts code points (len(str)) against a byte cap → multibyte frames bypass limit 3-4x | mcp-server/transport.py |
| BUG-16 | Low | med | Anthropic reactive 401 recovery replays whole stream with no "already-yielded" guard → token duplication in mid-stream 401 | providers/anthropic_provider.py |

## Completeness table (functional gaps)

| ID | Sev | Conf | Title | Files |
|----|-----|------|-------|-------|
| CMP-01 | High | high | `must_change_password` gate blocks real `finalize-*` onboarding-wizard endpoints (stale allowlist) → fresh install can't onboard | routes_admin_a/_auth_shim.py, routes_admin_b/onboard.py |
| CMP-02 | High | high | allowed-tools unenforced for card/always-on injected skills — only on-demand `Skill()` pulls are gated | context_assembler.py, agent_servicer.py |
| CMP-03 | High | high | Episodes ONBOARDING kind unreachable — runner never computes is_onboarding; no config knob; dead weight/prompt/filter | episodes/runner.py, classifier.py, config.py |
| CMP-04 | Med | high | Permission `ask` verdict always fail-closes to deny — no approval resolver wirable | agent_servicer.py |
| CMP-05 | Med | high | Strict mode does not deny memory_write/send_attachment/text_to_speech despite "denies every mutating tool" claim | permission.py |
| CMP-06 | Med | high | SlashAccessPolicy (DM_ONLY/ALLOWLIST/default_tier) declared but never enforced — no caller | channels/commands.py, service.py, router.py |
| CMP-07 | Med | high | Commands-dir *.md loader + skill→command bridge + $ARGUMENTS substitution all unwired (no prod caller) | channels/commands.py, router.py |
| CMP-08 | Med | high | Feishu bot open_id never resolved → group @mention gate always fails, all group messages dropped by default | channels/feishu.py |
| CMP-09 | Low | high | autonomous/turn_token_budget auto-continue implemented but never wired by any caller (dead in prod) | reasoning_loop.py, agent_servicer.py, subagent/runner.py |
| CMP-10 | Low | high | `set_persona_stores()` declared but never called → agent lazy-opens a 2nd sqlite handle | agent_servicer.py |
| CMP-11 | Low | high | MCP advertises listChanged:true on every capability but never emits any list_changed notification | mcp-server/dispatch.py |

## Prioritization rationale

Ordered by severity × confidence × blast-radius:

1. **SEC-01, SEC-02, BUG-01** — model-reachable, cross-session/tenant impact or a silently-defeated security gate, all on the shared singleton servicer. Top of the list.
2. **SEC-03, PERF-01, BUG-02** — event-loop / DB crash / permanent lockout DoS classes; high confidence; clear repros.
3. **CMP-01** — blocks every fresh install from onboarding; trivially fixed; high confidence.
4. **CMP-02, SEC-04, SEC-05** — security contract gaps where the advertised guarantee is silently void.
5. Medium DB/integration bugs and the multi-tenant latent set (SEC-06/07/09, BUG-08/09/10).
6. Low items kept only where confidence is high or the fix is one-line; low-confidence items (BUG-16) flagged reproduction-first.

## Reproduction-first / low-confidence items

- **BUG-16** (Anthropic 401 replay) — confidence low; the common 401-at-`__aenter__` case is already safe. Reproduce a mid-stream 401 *with a rotatable cred* before adding a `saw_output` guard.
- **BUG-13** (apply_patch all-or-nothing) — medium; confirm the O_NOFOLLOW partial-write and the Add-then-Update rejection with a focused test before reworking the commit loop.
- **SEC-05** (run_shell per-arg bypass) — high that the bypass exists; medium on the *right* fix. Decide policy (tokenize all segments vs. downgrade documented guarantee) before editing the gate.
- **SEC-06** (tenant authz) — exploitability is medium and gated on multi-tenant being enabled; treat as latent-hardening.

## Fix lanes (strictly file-disjoint — safe to run in parallel)

See the structured output `fix_lanes` for the authoritative file ownership. Summary:

- **LANE-A (agent_servicer core):** SEC-01, SEC-02, BUG-01, BUG-04, CMP-04, CMP-10 + the agent_servicer-side of CMP-02. Owns `agent_servicer.py`, `main.py`, `gateway/grpc/agent_server.py`. Highest-traffic file — serialize these onto one lane to avoid conflicts. Note CMP-09 and SEC-03 also touch agent_servicer.py; they are folded into this lane.
- **LANE-B (permission/skills/context):** SEC-05 (permission.py + shell.py), CMP-05 (permission.py), and the context_assembler side of CMP-02. NOTE: CMP-02 and SEC-01 both conceptually involve skill activation but touch different files; the agent_servicer.py portion belongs to LANE-A. To keep lanes file-disjoint, **context_assembler.py + permission.py + coding/shell.py** form LANE-B; the agent_servicer.py edits for CMP-02/SEC-01 are owned solely by LANE-A.
- **LANE-C (agent coding/web/image tools):** SEC-03(calculator.py), SEC-04(fetch.py), SEC-08(analyze.py), BUG-05+BUG-14(files.py), BUG-13(patch.py). NOTE: SEC-03 also needs an agent_servicer.py edit (thread offload) — that edit is owned by LANE-A; LANE-C owns only calculator.py's magnitude guard.
- **LANE-D (reasoning loop):** BUG-11, BUG-12, CMP-09(loop side). Owns `reasoning_loop.py`.
- **LANE-E (memory-host):** PERF-01, PERF-02. Owns `memory_host/local_sqlite.py`.
- **LANE-F (gateway lifecycle/config):** BUG-06, BUG-15. Owns `lifecycle/entrypoint.py`, `core/config_watcher.py`. NOTE: SEC-06 and PERF-03 also touch entrypoint.py — folded here.
- **LANE-G (admin config redaction):** SEC-07. Owns `routes_admin_b/config.py`, `routes_admin_b/napcat.py`, `channels_runtime/__init__.py`.
- **LANE-H (onboarding gate):** CMP-01. Owns `routes_admin_a/_auth_shim.py`, `routes_admin_b/onboard.py`.
- **LANE-I (upgrader):** BUG-02. Owns `upgrader/state.py`, `native_upgrader.py`, `docker_upgrader.py`.
- **LANE-J (hooks runner):** BUG-03. Owns `corlinman-hooks/runner.py`.
- **LANE-K (channels):** CMP-06, CMP-07, CMP-08, BUG-07, SEC-10. Owns `channels/commands.py`, `service.py`, `router.py`, `feishu.py`, `onebot.py`, `telegram_send.py`, `discord.py`, `common.py`.
- **LANE-L (auto-rollback):** BUG-08. Owns `auto-rollback/applier.py`, `monitor.py`, `metrics.py`.
- **LANE-M (evolution-store):** BUG-09. Owns `evolution-store/repo.py`, `types.py`.
- **LANE-N (agent-brain):** BUG-10. Owns `agent-brain/index_sync.py`.
- **LANE-O (episodes):** CMP-03. Owns `episodes/runner.py`, `classifier.py`, `config.py`.
- **LANE-P (mcp-server):** PERF-04, CMP-11. Owns `mcp-server/transport.py`, `dispatch.py`.
- **LANE-Q (providers):** SEC-11, BUG-16. Owns `providers/codex_provider.py`, `_codex_oauth.py`, `anthropic_provider.py`.

Cross-cutting note: SEC-06 / SEC-09 (tenant/scope authz) touch `tenancy/admin_schema.py`, `middleware/auth.py`, `middleware/tenant_scope.py`, `routes_admin_a/api_keys.py` and `entrypoint.py`. Because entrypoint.py is owned by LANE-F, the tenant authz work is co-located there to preserve disjointness — but it is a distinct, *latent* (multi-tenant-only) hardening effort that should be sequenced after the model-reachable lanes.

## Dropped / known-deferred

- Duplicate of SEC-01: the second auditor's "process-global _active_skills" report (agent_servicer @1133/3215/3334) — merged into SEC-01.
- Duplicate of PERF-01/PERF-02: the memory-host "namespace pushdown" and "graph back-link full-scan" appeared in both the memory-host auditor and the whole-repo perf auditor — merged.
- reasoning_loop budget double-track over-count (perf, Low) — dropped: non-correctness accounting nit, premature-spill only; low signal.
- reasoning_loop context_budget/compaction model not updated after cross-model fallback (Low, med) — dropped from active plan: narrow trigger (fallback to a smaller-window model + summary threshold), low blast radius; revisit if fallback usage grows.
- reasoning_loop `_maybe_auto_continue` usage=0 guard defeat (Low) — dropped: only matters once CMP-09 wires autonomous mode; fold into CMP-09 when that lands.
- cancel.py `combine()` no-strong-ref GC footgun (Low, med) — dropped from active plan: theoretical under aggressive GC; flag for opportunistic fix.
- Anthropic prompt-cache marker burned on tool_result-only turns (Low, perf) — dropped: cache-effectiveness regression only, never over-spends the ≤4 invariant.
- Codex tool-mapping KeyError on malformed tool (Low) — dropped: internal registry always supplies name; trigger requires malformed plugin tool.
- skill config-requirement env oracle (Low, med) — dropped from active plan: presence-only oracle for authenticated skill authors; note for the skills-config hardening pass.
- split_on_msg_break echoes literal [MSG_BREAK] when all segments empty (Low) — folded into LANE-K (common.py) as a cheap add-on, not separately tracked.
- config POST secret-merge writes None on redacted-with-no-base (Low, med) — folded into LANE-G (config.py) as an add-on to SEC-07.
- scheduler sub-minute cron catch-up under-walk (Low, med) — dropped: best-effort, non-load-bearing.
- goals N+1 list_evaluations (Low, med) — dropped: small goal trees, indexed; opportunistic.
- session-listing full-scan before LIMIT (Low, med) — dropped: admin listing path, bounded by history not page; opportunistic.
- HOOK.yaml exec_module RCE at boot (Low, med) — dropped from active plan: by-design claude-code parity, operator-controlled trust boundary; recommend an opt-in flag + dir-ownership check as a separate hardening ticket, not a bug fix.
- CORS allow_credentials footgun (Low) — dropped: not exploitable (browsers reject `*`+credentials); add a boot-time validation warning opportunistically.
- /plugin-callback auth-model doc inconsistency (Low) — dropped: not an auth bypass (both paths gated); doc reconciliation only.
- evolution-engine `_emit_budget_signal` omits tenant_id (Low, med) — folded conceptually with BUG-09's multi-tenant theme but lives in a different package/file; tracked as a follow-on to LANE-M, not separately laned.
- canvas LaTeX substring blacklist (Low, med) — dropped: pure text converter, no execution; cosmetic false-positive risk only.
- subagent mailbox unbounded/global (Low, med) — dropped from active plan: mitigated once BUG-04 fixes child_seq collisions; recommend bounded queue + tenant scoping as a follow-on.
