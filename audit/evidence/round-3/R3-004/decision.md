# R3-004 — Option choice: (a) semantic fix

## TL;DR
Picked **option (a)**: add `tenant_id` to `SubagentRequest` (default
`"default"`) and filter the dispatcher's quota check by it, so the
surface refusal actually agrees with what the supervisor enforces
downstream. Kept `_max_concurrent_per_tenant` / `TenantQuotaExceeded`
names — they were never wrong, only the implementation lied.

## Why (a) and not (b)

1. **Per-tenant is a stated product requirement**. Not just a comment
   in this file — it's in the design docs:
   - `docs/design/phase4-w4-d3-design.md:309` —
     `tenant_quota_caps_across_parents | supervisor | two parents
     collectively cannot exceed per-tenant ceiling`
   - `docs/design/phase4-w4-d3-design.md:122` —
     `SubagentSupervisor — depth cap, per-parent concurrency, per-tenant
     [quota]`
   - `docs/PLAN_MULTI_AGENT.md:17` —
     `max_concurrent_per_parent=3, per_tenant=15, max_depth=2`
   Option (b) would explicitly contradict the design.

2. **The supervisor already enforces per-tenant correctly**. See
   `python/packages/corlinman-subagent/src/corlinman_subagent/supervisor.py:336`
   — `cur_tenant = self._per_tenant.get(tenant_key, 0)` + reject on
   `>= max_concurrent_per_tenant`. The dispatcher's gate exists
   *specifically* to mirror that gate (its own docstring says so:
   "Hard cap that mirrors the Supervisor's per-tenant ceiling. Matched
   default keeps the dispatcher's surface refusal aligned with the
   supervisor's slot refusal"). It was supposed to be per-tenant; the
   implementation was just wrong.

3. **No env-var to break**. `grep -rn CORLINMAN.*SUBAGENT.*MAX
   python/` returns zero hits. The cap is in-code only
   (`DEFAULT_MAX_CONCURRENT_PER_TENANT = 15`). Nothing operator-facing
   to preserve.

4. **`SubagentRequest` has exactly one external caller**:
   `python/packages/corlinman-agent/src/corlinman_agent/subagent/tool_wrapper.py:919`.
   That caller already has `parent_ctx.tenant_id` in scope
   (`ParentContext.tenant_id` is required field, populated in
   `agent_servicer.py:1788`). One-line wiring change.

5. **Backward compatibility is preserved** by giving `tenant_id` a
   default of `"default"`:
   - Existing test callers (`_make_req` in `test_dispatcher.py` and
     `test_store.py`) continue to construct without the field.
   - Persisted state files written before this change hydrate cleanly
     (the store's `_load_from_disk` reads `raw_req.get("tenant_id") or
     "default"`).
   - Existing `test_max_concurrent_per_tenant_enforced` keeps passing
     because all its requests share the default tenant — the
     per-tenant semantic + the global behavior coincide on that path.

## Risk accepted (documented)

**Total dispatcher concurrency ceiling now scales with tenant count.**
The old (buggy) cap was effectively a hard global ceiling of 15
in-flight subagents across the whole process. The new (correct) cap
is 15 *per tenant* — so 5 tenants can collectively have 75 in-flight
rows. This matches the supervisor's behavior and the design spec, but
operators who were implicitly relying on the dispatcher as a global
resource limiter will see higher fan-out. Mitigation: the supervisor's
identical per-tenant gate is still the authoritative quota, so the
total never exceeds what the existing slot accountant would have
allowed anyway. If a single operator-facing global cap is wanted, it
belongs in a separate config field; not in this PR.

## Evidence
- `before.log` — RED: `TypeError: SubagentRequest.__init__() got an
  unexpected keyword argument 'tenant_id'` (the new test fails because
  the dataclass field didn't exist + the dispatcher's check was global).
- `after.log` — GREEN after fix.
- `regression.log` — 23/23 subagent tests pass, 1425/1425 wider
  corlinman-server suite pass, 57/57 subagent-related corlinman-agent
  tests pass.
