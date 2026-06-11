# R8 open questions — answered (2026-06-04, code-verified)

## Q1 — HookBus ordering + who emits hook events  ⚠️ biggest finding
- **Emit API:** `await bus.emit(event)` / `bus.emit_nonblocking(event)` (`corlinman-hooks/bus.py:360,388`).
- **Hoist is safe:** both existing gateway blocks already do `bus = getattr(app.state,"hook_bus",None); if None: HookBus(); app.state.hook_bus = bus` (`entrypoint.py:1185-1191` user-correction, `:1393-1396` scheduler). Hoisting one construction above the sibling-bootstrap loop and having both reuse it is the pattern already in code.
- **Producers on the gateway `app.state.hook_bus` today:** only the **scheduler runner** (`scheduler/runner.py:531,674,740 await bus.emit(...)`). The entrypoint comment (`:1172-1175`) explicitly says *no other gateway component constructs/feeds a shared bus yet*. Darwin signals are written **directly to evolution.sqlite** (bus-independent), so they work regardless.
- **GAP:** `agent_servicer` emits tool/agent hooks on its **own** `self._hook_bus` (set at construction `:1140`; `emit` at `:2172,2197,2225`) inside the **agent process** — a *different* bus from the gateway's `app.state.hook_bus`. So **interactive (channel/web) tool-call hooks do NOT reach a gateway-side observer.**
- **Answer:** L1 (observer bootstrap) is feasible and safe, but as designed it would capture **scheduler-turn + user-correction events only**, not interactive tool calls. Full coverage needs the agent-process hook stream bridged to the gateway (separate, larger work) or the observer co-located in the agent runtime. **Recommendation:** ship L1 for scheduler/correction signals + darwin (already direct), and treat "interactive-hook bridge" as its own follow-up. Confidence: high.

## Q2 — in-process builtins vs example subprocess crons
- `docs/config.example.toml` ships **subprocess** `[[scheduler.jobs]]` examples: evolution-engine `run-once` (`:358`), shadow-tester (`:368`), auto-rollback (`:379`), `consolidate-once` (`:399`).
- **Answer/recommendation:** implement **in-process `run_tool` builtins** as the stock default (mirrors `evolution.darwin_curate`; no PATH dependency; shares the open store). The default-job registrar already **skips when the operator declares the same job** (`_config_has_scheduler_job` override-guard), so there is **no double-run** if someone also keeps the example cron. Leave the example crons as a documented alternative (optionally mark superseded). Confidence: high.

## Q3 — which applier becomes canonical
- Confirmed: the admin `/apply` route **hard-imports the store-only** `corlinman_auto_rollback.EvolutionApplier` (`routes_admin_b/infra/evolution.py:517-534`) — mutates no file.
- **Answer/recommendation:** **flag-selected**, default = store-only (stock behavior unchanged). When `[evolution.apply].real_mutations` is on, dispatch `/apply` to the KindHandler-based `gateway/evolution/applier.py`. Do **not** replace outright. Confidence: high.

## Q4 — shadow eval-set dir + default simulators + no-eval-set behavior
- `eval_set_dir` comes from `[evolution.shadow].eval_set_dir`; `ShadowRunner.register_simulator` registers per kind; **no eval set → records a `no-eval-set` marker and the proposal stays gated (never auto-approved)** (`shadow-tester/runner.py:112,187-213`).
- **Answer/recommendation:** default `eval_set_dir = <data_dir>/evolution/eval_sets/<kind>/`; ship the existing simulators (memory_op / tag_rebalance / skill_update) registered. "No eval set ⇒ shadow-skipped, proposal waits for explicit approval" is acceptable **safe** stock behavior. Confidence: high.

## Q5 — `watched_event_kinds` target normalization  ⚠️ must-fix for L5
- **Mismatch confirmed.** Signals carry **bare** `target=<skill_name>` (`darwin.py:526`, `skill_update.py:35`). Proposals carry `target=skills/<name>.md` (`darwin.py:716`, `skill_update.py:121` via `_skill_path`). `capture_snapshot` runs `WHERE target = ?` (`auto-rollback/metrics.py:~184`) using the **proposal** target → it would **never match** the bare-name regression signals → the monitor never detects a breach → never auto-reverts.
- **Answer/required fix for L5:** normalize in the monitor's snapshot capture — derive the signal-target form (strip `skills/` prefix + `.md` suffix) from the proposal target before querying, OR have post-apply regression signals carry the proposal-target form. Pick one convention and assert it in a test. Confidence: high.

## Net effect on the plan
- **L1** is safe to build but only partially useful until the agent→gateway hook bridge exists (flag that scope). **L2/L3** unaffected. **L4** = flag-selected applier (Q3). **L5** must implement the Q5 target normalization or auto-rollback is inert. Q2/Q4 are settled defaults.
