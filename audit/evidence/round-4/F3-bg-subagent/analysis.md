# R4-F3 — background subagent dispatch: analysis & why it's a pause-point

## Finding (confirmed)
`subagent.spawn` with `run_in_background:true` always returns the sentinel
`run_in_background_not_implemented`. Two gaps:

1. **`agent_servicer.py:1865`** calls `dispatch_subagent_spawn(...)` WITHOUT
   passing `subagent_dispatcher=`, so even though a dispatcher exists on
   `app.state.corlinman_subagent_dispatcher`, the tool path defaults it to
   `None` and rejects (`tool_wrapper.py:382`).
2. **`entrypoint.py:1649`** builds the dispatcher with
   `_unwired_run_child_factory`, which **raises** `RuntimeError`, and the
   comment points to `AdminState.subagent_dispatcher.replace_factory()` — a
   method that **does not exist** and is never called.

`docs/multi-agent.md:169-189` documents `run_in_background:true` as working.

## Why this is materially different from F1/F2
- **F1 (scheduler)** and **F2 (placeholders)** were *silently broken* —
  jobs never fired; tokens echoed garbage. Those are correctness/UX failures.
- **F3 fails HONESTLY and SAFELY**: it returns a clean, documented sentinel
  (`run_in_background_not_implemented`) and the parent loop continues. There
  is no data corruption, no security hole, no resource leak. It is an
  *unfinished feature*, not a malfunctioning one.

## Why a correct fix needs product/policy decisions (not just wiring)
The background `SubagentRequest` (`tool_wrapper.py:_dispatch_via_background`,
~919) carries only metadata: `request_id, parent_session_key,
parent_agent_id, subagent_type, goal, description, tenant_id`. It deliberately
does NOT carry the per-turn execution context the *foreground* path uses
(`agent_servicer.py:1865-1874`): `provider`, `parent_tools`, the live
`ParentContext`, `supervisor_acquire`, `event_emitter`.

A real `run_child_factory(req)` must therefore decide, at execution time:
- **Which provider/model** a detached child uses. Foreground inherits the
  parent turn's selected model; a boot-time factory has no turn → must pick a
  default. Wrong/expensive model = real $ cost.
- **Which tool allowlist** the child gets. A background child with
  `run_shell` is a cost/security concern; the foreground path scopes tools to
  the parent's per-turn allowlist, which the request doesn't carry.
- **Detached lifecycle + result surfacing**: the design (Claude-Code parity)
  injects a synthetic user-role notification into the parent journal on
  terminal state — another subsystem to build and verify.

Building this hastily risks orphaned child tasks, wrong-provider spend, or an
over-privileged detached agent — i.e. exactly the blast-radius expansion the
audit spec forbids ("最小改动 / 不扩大爆炸半径", "不确定就停下").

## Note: gap #1 alone is NOT a safe partial fix
Threading the dispatcher through (gap #1) WITHOUT a real factory (gap #2)
would change behavior from an honest rejection to: dispatch → factory raises
RuntimeError → child flips to `failed` + emits failure obs/journal noise.
That is strictly worse than the current clean rejection. So #1 must not ship
without #2.

## Recommendation
Treat F3 as a scoped feature-completion task requiring an explicit
provider/model + tool-allowlist policy decision, OR (the low-risk honest
alternative) align `docs/multi-agent.md` with reality and keep the safe
rejection until the policy is decided. Surfaced to the user for the call.
