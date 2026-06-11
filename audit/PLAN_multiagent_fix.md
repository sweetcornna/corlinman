All references confirmed against current source. I have everything needed to produce the plan.

```markdown
# Build-Ready Fix Plan — Subagent / Supervisor / Dispatcher Subsystem

## 1. Executive Summary

**24 raw findings → 13 distinct defects after dedup.** All are adversarially confirmed real. The findings collapse heavily: the four "background dispatch is dead" findings (MA-BG-1, MA-BG-2, MA-DR-2, MA-BG-DEAD-1) are one defect; the four "supervisor `_lock` is dead" findings (MA-LOCK-1, MA-DEAD-LOCK-3, MA-SUP-LOCK-1, plus the lock half of others) are one defect; the five "stale cap/ceiling docstring" findings (MA-DR-3, MA-DEPTH-2, MA-DOCDRIFT-6, MA-DOC-DRIFT-1, MA-CEILING-1) are one doc-drift cluster.

Severity (deduped, taking the highest confirmed severity per defect):

| Severity | Count | Defects |
|---|---|---|
| **High** | 3 | D1 child-allowlist non-enforcement, D2 drain-task leak, D3 restart-wedge quota starvation |
| **Medium** | 4 | D4 background-dispatch dead surface, D5 slot-counter cancel leak, D6 undeclared cross-package imports, D7 depth-not-threaded / max_depth contract |
| **Low** | 6 | D8 dead `_lock`, D9 wall-clock ceiling==default, D10 synth overrun, D11 spawn_many schema strictness, D12 dispatcher no-shutdown + dead `_snapshot`, D13 doc-drift cluster |

**Dominant theme: advertise-vs-enforce drift.** The system repeatedly *advertises* a contract (filtered tool schema, `run_in_background`, `max_depth=2`, `max_wall_seconds` hard cap, locked counters, declared deps) that the *execution boundary* does not honor. Two of the three High defects (D1, D3) are genuine Claude-Code parity / containment violations; the rest are dead code, leaks, and stale docs. Only D1, D2, D3 carry runtime risk; everything else is correctness/hygiene.

---

## 2. Defect Table

| ID | Sev | file:lines | Defect (one line) | Fix (one line) |
|---|---|---|---|---|
| **D1** | High | runner.py:586-661 | Child tool-allowlist filters *schema* only; executor runs any tool the parent holds | Gate execution on `child_tool_names` in `_drain_events`; return `tool_not_in_allowlist` envelope |
| **D2** | High | runner.py:412-462 | `asyncio.shield`'d drain task leaks (loop/stream/executor) on parent cancel | Add `finally:` that cancels+awaits `drain_task` on every exit path |
| **D3** | High | store.py:80-83,267-296 | Orphaned in-flight rows wedge forever after restart; permanently eat 15-slot tenant quota | Boot reconcile queued/running→`stalled`; make `stalled` terminal so it's pruned & uncounted |
| **D4** | Med | agent_servicer.py:2045-2126; entrypoint.py:1874-1908 | `run_in_background` advertised but unreachable: dispatcher never threaded, factory always raises, no `replace_factory` | Either remove the schema field + dead `_dispatch_via_background`, or fully wire dispatcher + add `replace_factory` |
| **D5** | Med | tool_wrapper.py:550-585 | Slot counters leak if cancelled at `await _emit_subagent_spawned` (before `with slot_cm`) | Move `with slot_cm:` up to wrap the emit await |
| **D6** | Med | supervisor.py:535,560,627-629; pyproject.toml | Lazy imports of corlinman_agent/corlinman_server undeclared; server import = cycle | Delete dead `child_emitter`; guard `corlinman_agent.events` imports with `try/except ImportError` |
| **D7** | Med | agent_servicer.py:1937-1944,1967-1974; runner.py:822 | Depth hardcoded 0; `max_depth=2` grandchild unreachable; advertise≠usable for max_depth≥3 | Prune spawn tools at `child_depth >= 1`; set DEFAULT_MAX_DEPTH=1 + doc to `parent→child` |
| **D8** | Low | supervisor.py:223,275,34-38 | `_lock` created, never acquired; docstrings claim it guards counter RMW | Delete `_lock` from `__slots__`+init; rewrite docstring to "atomic via await-free RMW" |
| **D9** | Low | types.py:41; supervisor.py:109 | `max_wall_seconds` default==ceiling (60==60); docs claim 300 ceiling | Add `DEFAULT_MAX_WALL_SECONDS_CEILING=300`; point ceiling at it; fix docs |
| **D10** | Low | runner.py:474-493,510,569 | Synthesis adds up to 30s after `max_wall_seconds`; "hard budget" exceeded | Pass `min(30, remaining_budget)` as synth timeout from a shared budget |
| **D11** | Low | tool_wrapper.py:1179 | spawn_many per-task schema requires `agent`; dispatch defaults missing agent to general-purpose | Change per-task `required` to `["goal"]`; align properties with single-spawn |
| **D12** | Low | dispatcher.py:205,317-321,595-603,643-644 | No `shutdown()`/cancel-all for in-flight tasks; dead `_snapshot`/`_asdict` | Add `async def shutdown()`; call from lifespan; delete `_snapshot`/`_asdict` |
| **D13** | Low | agent_servicer.py:2615; runner.py:95,428; tool_wrapper.py:1242,1245; supervisor.py:12 | Stale "3 per-parent" / "300 ceiling" comments vs live 10 / 60 | Update literals (3→10, 300→60); prefer interpolating policy fields |

---

## 3. Per-Defect Detail

### D1 — Child tool-allowlist is advisory only (privilege non-containment) [High]
**Root cause:** `_filter_tools_for_child` (runner.py:268) computes `child_tool_names` but it is consumed *only* by `_project_tool_schemas` (runner.py:295) to build the advertised schema. The frozenset is never passed into `_drive_and_collect`→`_drain_events`, which execute every emitted `ToolCallEvent` via `_execute_child_tool(tool_executor, event)` (runner.py:652) with no membership check. The gateway executor (agent_servicer.py:1937-1944) only blocks the 3 spawn tools and applies the *parent's* permission gate. A child whose schema hides `run_shell` but whose model emits it anyway runs it with full parent authority.

**Minimal patch (runner-local, covers all 3 spawn paths):**
- runner.py:351 — add `allowed_tools=child_tool_names` to the `_drive_and_collect(...)` call.
- runner.py:372 (`_drive_and_collect`) — add param `allowed_tools: frozenset[str] | None = None`; forward it into the `_drain_events(...)` call at runner.py:413.
- runner.py:586 (`_drain_events`) — add param `allowed_tools: frozenset[str] | None = None`.
- runner.py:~652 — before `_execute_child_tool`, insert: `if allowed_tools is not None and event.tool not in allowed_tools: loop.feed_tool_result(ToolResult(call_id=event.call_id, content=json.dumps({"error":"tool_not_in_allowlist","tool":event.tool}), is_error=True)); continue`.

**Claude-Code semantic restored:** rule #5 — subagents get a *controlled, enforced* tool subset; advertised toolset == usable toolset at the execution gate, not just by hiding schemas.

**Test:** `tests/test_subagent_tool_execution.py` — executor that *would* run `run_shell`; child allowlist `{web_search}`; assert a `run_shell` emission returns the `tool_not_in_allowlist` envelope and the executor is never invoked for it.

---

### D2 — Child drain task leaks on parent cancellation [High]
**Root cause:** `_drive_and_collect` creates `drain_task = asyncio.ensure_future(_drain_events(...))` (runner.py:412) and awaits it only via `await asyncio.wait_for(asyncio.shield(drain_task), ...)` (runner.py:429). `drain_task.cancel()` exists *only* in the timeout-grace branch (runner.py:448). There is no `finally`. On **parent/gather cancel** (e.g. spawn_many cancels its coros), `CancelledError` exits `wait_for` but `asyncio.shield` keeps `drain_task` running detached, holding the live `ReasoningLoop`, provider stream, and `tool_executor`.

**Minimal patch:** Append a `finally:` to the existing `try` ending at runner.py:462 (both `asyncio` and `contextlib` already imported, lines 41-42):
```python
finally:
    if not drain_task.done():
        drain_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await drain_task
```
Timeout path is unaffected (it already cancels at 448, so `done()` is True → no-op).

**Claude-Code semantic restored:** N/A (resource-leak hazard) — a dropped/cancelled delegation must release its resources, not orphan a live stream.

**Test:** `tests/test_subagent_runner.py` — wire a `_drain_events` whose loop blocks; cancel the `run_child`/`_drive_and_collect` task; assert `drain_task.cancelled()` (or `done()`) and that the provider stream/executor handles were released.

---

### D3 — Orphaned in-flight rows wedge & starve tenant quota after restart [High]
**Root cause:** `stalled` is in `_IN_FLIGHT_STATES` (store.py:83), so it's never terminal and `_prune_terminal_locked` (store.py:267-296) never evicts it. `_load_from_disk` rehydrates `queued`/`running` verbatim; the fresh `AsyncSubagentDispatcher._tasks` (dispatcher.py:205) is empty at boot, so no live task maps to any hydrated row. No code ever sets `stalled` (the recovery sweep is explicitly deferred, dispatcher.py:121-124). Each orphan is counted forever by `count_in_flight_for_tenant` (store.py:420), so after 15 orphans `dispatch_async` raises `TenantQuotaExceeded` for every future background spawn from that tenant.

**Minimal patch (two coordinated edits):**
- store.py:80-83 — move `"stalled"` from `_IN_FLIGHT_STATES` into `_TERMINAL_STATES`.
- `SubagentTaskStore.__init__` (store.py:~182-190) — after `_load_from_disk()`, before `_prune_terminal_locked()`, add a sync loop: for each hydrated status in `("queued","running")` set `state="stalled"`, `finished_at=_now_ms()`, `finish_reason="stalled_on_restart"`, then flush. The subsequent `_prune_terminal_locked()` now bounds them.
- Update dispatcher.py:121-124 docstring (sweep now exists).

**Claude-Code semantic restored:** a delegation never stays permanently "in progress" across a host restart; a dropped background task resolves to terminal/failed.

**Test:** `tests/system/subagent/test_store.py` — persist a `running` row, re-open the store, assert the row is `stalled`, is terminal, and is **not** counted by `count_in_flight_for_tenant` (quota freed).

---

### D4 — Background-dispatch surface advertised but structurally dead [Medium]
*(Dedup of MA-BG-1, MA-BG-2, MA-DR-2, MA-BG-DEAD-1)*
**Root cause:** Three independent breaks. (1) agent_servicer.py:2045/2080/2108 never pass `subagent_dispatcher=`, so it defaults `None` (tool_wrapper.py:300) and the model path always hits `BACKGROUND_NOT_IMPLEMENTED_ERROR` (tool_wrapper.py:392-405). (2) The published dispatcher's factory `_unwired_run_child_factory` (entrypoint.py:1874-1885) unconditionally raises and references a `replace_factory()` that exists nowhere. (3) Only `subagent_spawn` advertises `run_in_background`; inline/spawn_many schemas omit it yet inline still parses+rejects it. The model-facing schema now carries an honest "NOT YET IMPLEMENTED" disclaimer, so the *user-facing* behavior (clean rejection) is correct — hence Medium, not High.

**Minimal patch (honest-first, recommended):**
- tool_wrapper.py:188-200 — remove the `run_in_background` property from `subagent_spawn_tool_schema`. Keep the defensive reject branches (392-405, 1583-1590).
- entrypoint.py:1874-1908 — either skip constructing/publishing the hard-raising dispatcher, or fix the comment that names the nonexistent `replace_factory`.
- dispatcher.py:12-13 — correct the module docstring (drop the "spawns a task that invokes `Supervisor.spawn_child_to_result`" claim).

**If background is actually wanted (defer to a later milestone):** add `AsyncSubagentDispatcher.replace_factory(self, factory)` (slot `_run_child_factory` already exists), build a real supervisor/registry/provider-bound factory at boot, thread `subagent_dispatcher=` into all three servicer calls, and make the schema consistent across the three sibling tools.

**Claude-Code semantic restored:** the advertised tool grammar must match deliverable capability — don't advertise a Task-tool background mode the wiring can't fire.

**Test:** keep `test_run_in_background_rejected_as_not_implemented`; add a schema assertion that `subagent_spawn_tool_schema` no longer exposes `run_in_background` (honesty path).

---

### D5 — Slot counters leak on cancel in acquire→guard window [Medium]
**Root cause:** In `_run_child_under_slot`, `try_acquire` increments both counters synchronously (supervisor.py:339-351), but `with slot_cm:` is only entered at tool_wrapper.py:571. Between acquisition (`slot_cm = outcome`, :550) and the guard sits `await _emit_subagent_spawned(...)` (:553-561) — a suspension point. `_emit_subagent_spawned`'s `except Exception` does not catch `CancelledError`, so a cancel there propagates before the guard; `Slot.release()` never runs; counters stay incremented until non-deterministic `Slot.__del__` GC.

**Minimal patch:** Move the guard up so every post-acquisition await runs inside it. Wrap from tool_wrapper.py:553 (`child_ctx_preview = ...`) — including the `await _emit_subagent_spawned`, `_make_bubble_emitter`, and the existing `try/run_child` — inside `with slot_cm:`. `nullcontext()` (the `supervisor_acquire is None` test path) is re-entrant, so this is safe.

**Claude-Code semantic restored:** N/A (counter-leak hazard) — concurrency accounting must be exact under cancellation.

**Test:** `tests/test_subagent_spawn_inline.py` — emitter whose `emit_event` blocks; cancel the dispatch task at that await; assert `supervisor.parent_count(...) == 0` and `tenant_count(...) == 0`.

---

### D6 — Undeclared cross-package imports + import cycle [Medium]
**Root cause:** supervisor.py imports `corlinman_agent.events` (535, 560) and `corlinman_server.gateway.observability.emitter` (627-629) with no `try/except`; `corlinman-subagent/pyproject.toml` declares only `corlinman-hooks`. The server import creates a server→subagent→server cycle (corlinman-server already depends on corlinman-subagent). `child_emitter` (605-636) has zero call sites; `test_subagent_events.py` imports both undeclared packages at module top, so the package can't pass its own tests standalone.

**Minimal patch:**
- supervisor.py:605-636 — **delete `child_emitter`** (dead; production uses tool_wrapper's guarded `_make_bubble_emitter`). This removes the `corlinman_server` import and the cycle outright.
- supervisor.py:535,560 — wrap the `corlinman_agent.events` imports in `try/except ImportError: return` (matches `_make_bubble_emitter`'s pattern).
- Optionally gate `test_subagent_events.py` with `pytest.importorskip`.

**Claude-Code semantic restored:** N/A (packaging hygiene).

**Test:** add a packaging/import test that imports `corlinman_subagent.supervisor` and constructs `Supervisor(SupervisorPolicy())` with neither corlinman_agent nor corlinman_server importable (monkeypatch `sys.modules`) and asserts no ImportError.

---

### D7 — Depth never threaded; max_depth contract unreachable + advertise≠usable [Medium]
*(Dedup of both MA-DEPTH-1 variants)*
**Root cause:** `_dispatch_builtin` rebuilds `ParentContext(... depth=0 ...)` for every tool call (agent_servicer.py:1968-1974), and the child executor reuses the parent's depth-0 `start` (agent_servicer.py:1937-1942). Depth is never +1'd for children, so supervisor's `depth >= max_depth` (supervisor.py:331) never sees depth>0 and the documented `parent→child→grandchild` (max_depth=2) is unreachable — the live ceiling is depth 1, enforced by *two* independent mechanisms (schema prune at runner.py:822 `child_depth >= max_depth - 1`, and the blanket `subagent_no_recursive_spawn` reject). Separately, for operator-set `max_depth≥3`, runner.py:822 leaves spawn tools in a depth-1 child's advertised schema while the executor still hard-rejects them → advertise≠usable. **Not a security hole** (children are correctly capped at depth 1).

**Minimal patch (Option B — accept single-level nesting, recommended, near-zero risk):**
- types.py:50 + api.py:194 — `DEFAULT_MAX_DEPTH = 1`.
- supervisor.py:104 — `max_depth: int = 1`.
- runner.py:822 — change gate to `if child_depth >= 1:` (every spawned child; matches the executor's blanket refusal for all max_depth values).
- Update `parent→child→grandchild` docstrings (types.py:48-50, api.py:193, runner.py docstrings) to `parent→child`.

*(Option A — make grandchild real — only if delegation depth is a genuine product requirement: thread the child's incremented `ParentContext` into a child-scoped executor, drop the hard reject, let supervisor.py:331 + runner.py:822 gate. Larger, deferred.)*

**Claude-Code semantic restored:** rule #3 — advertised toolset == usable toolset; subagents cannot spawn subagents (effective depth 1), and the stated contract matches the wiring.

**Test:** `tests/test_subagent_runner.py` — at `max_depth=1`, assert a depth-1 child's `ChatStart.tools` contains no spawn tools; add a config test asserting `SupervisorPolicy().max_depth == DEFAULT_MAX_DEPTH`.

---

### D8 — Dead `_lock`; docstrings claim a guard that doesn't exist [Low]
*(Dedup of MA-LOCK-1, MA-DEAD-LOCK-3, MA-SUP-LOCK-1)*
**Root cause:** `self._lock = asyncio.Lock()` (supervisor.py:275) is never acquired (`grep` finds no `async with self._lock`). `try_acquire` is sync (supervisor.py:316) and its RMW is atomic only because it's await-free. Docstrings (34-38, 271-274) falsely claim the lock guards the counter RMW.

**Minimal patch:** supervisor.py — delete `"_lock",` (223) and `self._lock = asyncio.Lock()` + its comment (271-275); rewrite the docstring (34-38) to "the counter read-modify-write is atomic by virtue of being await-free on a single-threaded loop." Keep `import asyncio` (used elsewhere). Add a one-line warning at `try_acquire` that no `await` may be introduced into the acquire path.

**Claude-Code semantic restored:** N/A (internal correctness/maintainability).

**Test:** lightweight — assert `not hasattr(Supervisor(SupervisorPolicy()), "_lock")` (or that no lock attr exists) to prevent re-introduction.

---

### D9 — Wall-clock ceiling aliases the default (60==60); docs claim 300 [Low]
**Root cause:** `DEFAULT_MAX_WALL_SECONDS=60` (types.py:41) is reused as both per-task default (types.py:123) and policy ceiling (supervisor.py:109), so the clamp `60 > 60` (tool_wrapper.py:518-521) never engages on the default path. runner.py:428 + tool_wrapper.py:347-348 docstrings claim a 300s ceiling that exists nowhere.

**Minimal patch (Option A — decouple, recommended):** types.py:41-area — add `DEFAULT_MAX_WALL_SECONDS_CEILING: int = 300`; supervisor.py:109 — point `max_wall_seconds_ceiling` at it; supervisor.py:97 docstring `=60`→`=300`. This gives the documented "enforced from above" headroom (child defaults 60s, may request up to 300s). *(Option B if 60 is the intended hard cap: leave code, fix the 300 docstrings to 60.)*

**Claude-Code semantic restored:** N/A — operator-facing budget docs must match enforced behavior.

**Test:** `tests/test_subagent_*` — with default policy, a task requesting `max_wall_seconds=120` is clamped to 300 (not 60); a request of 400 clamps to 300.

---

### D10 — Synthesis fallback exceeds the advertised hard budget by up to 30s [Low]
**Root cause:** `wait_for` bounds only the drain by `max_wall_seconds` (runner.py:429). The success-but-empty synthesis net (runner.py:474-493) is bounded by a *separate* `_SYNTH_FALLBACK_TIMEOUT_SECONDS=30.0` (runner.py:510,569), added on top. No outer supervisor `wait_for` clamps the live spawn path (only `try_acquire` is used). Total = drain(≤budget) + synth(≤30).

**Minimal patch:** runner.py:~480 — compute `remaining = max(0.0, float(task.max_wall_seconds) - (_now_ms() - started_ms)/1000.0)` (started_ms already in scope) and pass `min(_SYNTH_FALLBACK_TIMEOUT_SECONDS, remaining)` as the synthesis timeout (replacing the hardcoded value at :569); skip synthesis if `remaining <= 0`.

**Claude-Code semantic restored:** the `max_wall_seconds` "hard budget" is honored as a true ceiling.

**Test:** drive a child that exhausts its budget in drain, then assert total elapsed ≤ `max_wall_seconds` (+ small epsilon) on the success-but-empty path.

---

### D11 — spawn_many per-task schema stricter than behavior [Low]
**Root cause:** per-task schema `required: ["agent","goal"]` (tool_wrapper.py:1179) with `additionalProperties:False`, but `_parse_args` only hard-requires `goal` (821-823) and a missing agent defaults to general-purpose (423). Single-spawn requires only `["goal"]` (259) → sibling tools disagree. Lax providers run agent-less tasks as general-purpose; strict providers 400 the whole call.

**Minimal patch:** tool_wrapper.py:1179 — change per-task `required` to `["goal"]`. Optionally extend per_task.properties (1130-1177) with `subagent_type`, `model`, `description` to match single-spawn (keep `additionalProperties:False`). Do **not** add `run_in_background` to per_task.

**Claude-Code semantic restored:** fan-out task spec shape consistent with single-task spawn.

**Test:** `tests/test_subagent_spawn_many.py` — a per-task dict with only `goal` validates and dispatches as general-purpose (no schema rejection).

---

### D12 — Dispatcher has no shutdown; dead `_snapshot`/`_asdict` [Low]
**Root cause:** `_tasks` registers every background task (dispatcher.py:205,317-321); only single-request `kill()` cancels (331-342); no `shutdown`/`aclose`/`cancel_all` exists, and lifespan teardown never references the dispatcher. `_snapshot` (595-603) has zero call sites and its docstring claims a coherence model the live `count_in_flight_for_tenant` quota path doesn't use; `_asdict = asdict` (643-644) is also unused.

**Minimal patch:**
- dispatcher.py:~369 — add `async def shutdown(self)`: snapshot outcomes under `self._lock`, cancel undone tasks, `await asyncio.gather(*tasks, return_exceptions=True)` (safe — `_run` folds `CancelledError` into a clean exit, 388-394).
- entrypoint.py lifespan teardown (~2380) — call `dispatcher.shutdown()` alongside existing `aclose()`s.
- dispatcher.py:595-603, 643-644 — delete `_snapshot`, `_asdict`, and the now-unused `from dataclasses import asdict` (line 39, verify no other use).

**Claude-Code semantic restored:** N/A (lifecycle robustness). Ship with D4's wiring milestone since the path is dead until then.

**Test:** `tests/system/subagent/test_dispatcher.py` — schedule a long fake task via a real factory, call `shutdown()`, assert the task is cancelled and the row is terminal.

---

### D13 — Stale cap/ceiling comment cluster [Low]
*(Dedup of MA-DR-3, MA-DEPTH-2, MA-DOCDRIFT-6, MA-DOC-DRIFT-1, and the doc half of MA-CEILING-1)*
**Root cause:** Live `max_concurrent_per_parent=10` (supervisor.py:102) and ceiling=60, but comments say "3 per-parent" / "300 ceiling". Confirmed stale sites: agent_servicer.py:2615; runner.py:95 ("default 3"), runner.py:428 ("default 300"); tool_wrapper.py:1242 ("default 3"), :1245 ("N-3"); supervisor.py:12 ("default 3"). Note `supervisor.py:97/102` and `types.py:41` are **already correct** — do not touch. `dispatcher.py:10-11` "default 15" is correct.

**Minimal patch:** Comment-only edits: 3→10 at agent_servicer.py:2615, runner.py:95, tool_wrapper.py:1242, supervisor.py:12; "N-3"→"N-10" at tool_wrapper.py:1245; "300"→"60" at runner.py:428. Prefer phrasing as `SupervisorPolicy.max_concurrent_per_parent (default 10)` / `DEFAULT_MAX_WALL_SECONDS` to stop future drift.

**Claude-Code semantic restored:** N/A (operator-facing doc accuracy on fan-out ceilings).

**Test:** add an assertion pinning `SUBAGENT_SPAWN_MANY_MAX_TASKS == SupervisorPolicy().max_concurrent_per_parent` to catch future divergence.

---

## 4. Recommended Fix Order

Ordered by dependency, then severity, **batched per file** to avoid churn. D9 should land before/with D13 (D13's runner.py:428 "300→60" assumes the D9 decision; if D9 Option A picks 300, then runner.py:428 documents the *ceiling* 300, not 60 — resolve D9 first).

**Phase 0 — Decisions to lock first (no code):**
- D9: pick ceiling = 300 (Option A) vs 60 (Option B). *Recommend 300.*
- D7: pick max_depth=1 (Option B) vs real grandchild (Option A). *Recommend 1.*
- D4: pick remove-schema (honesty) vs full-wire. *Recommend remove now, wire later.*

**Phase 1 — High-severity runtime fixes (independent, ship first):**
1. **D2** runner.py — `finally` cancel (1 edit). *No dependency.*
2. **D1** runner.py — allowlist execution gate (4 edits, same file as D2 → batch). Touches `_drive_and_collect`/`_drain_events` signatures, as does D2's `finally` and D10's synth-budget and D7's prune; **do all runner.py work in one pass.**
3. **D3** store.py — terminal-state + boot reconcile (2 edits). *No dependency.*

**Phase 2 — Medium fixes:**
4. **D7** runner.py (prune `>= 1`, batched with Phase-1 runner.py pass) + types.py/api.py/supervisor.py (depth defaults + docstrings).
5. **D5** tool_wrapper.py — move slot guard up.
6. **D6** supervisor.py — delete `child_emitter`, guard agent imports (batch with D8 supervisor.py edits).
7. **D4** tool_wrapper.py (drop schema field) + entrypoint.py + dispatcher.py docstring (batch dispatcher.py with D12).

**Phase 3 — Low fixes (batched by file):**
8. **D8 + D13(supervisor.py:12) + D6** → single supervisor.py pass: remove `_lock`, fix docstrings, delete `child_emitter`, guard imports.
9. **D9** types.py + supervisor.py:97/109 (batch supervisor.py with #8).
10. **D10** runner.py (batch with Phase-1 runner.py pass).
11. **D11** tool_wrapper.py (batch with D4/D5 tool_wrapper.py).
12. **D12** dispatcher.py (`shutdown` + delete `_snapshot`/`_asdict`) + entrypoint.py (lifespan call; batch with D4 entrypoint.py).
13. **D13** remaining comment edits: agent_servicer.py:2615, runner.py:95/428, tool_wrapper.py:1242/1245 (each batched into that file's pass).

**File-batch summary (one editing pass each):**
- **runner.py:** D2, D1, D7(prune), D10, D13:95/428
- **supervisor.py:** D8, D6, D9(ceiling+doc), D7(max_depth), D13:12
- **tool_wrapper.py:** D1(uses `tool_not_in_allowlist`), D4(schema), D5(guard), D11(required), D13:1242/1245
- **store.py:** D3
- **dispatcher.py:** D4(docstring), D12(shutdown + dead-code)
- **entrypoint.py:** D4, D12(lifespan call)
- **types.py / api.py:** D7(DEFAULT_MAX_DEPTH), D9(new ceiling constant)
- **agent_servicer.py:** D13:2615 (+ D4/D7 only if full-wire Option A chosen)
- **pyproject.toml:** D6 (only if extras route chosen; not needed if `child_emitter` deleted)

---

## 5. Parity Scorecard vs Claude Code Subagent Model

| Claude-Code semantic | Status today | After this plan |
|---|---|---|
| Subagents cannot spawn subagents (effective depth 1) | ✅ holds (by hard-reject, but contract says depth 2) | ✅ holds **and** contract matches (D7) |
| Advertised toolset == usable toolset | ❌ schema-filter not enforced at exec (D1); spawn tools advertised to mid-depth children (D7) | ✅ enforced gate + matched prune |
| Child tool subset cannot escalate past parent | ❌ child runs any parent tool (D1) | ✅ `tool_not_in_allowlist` gate |
| `run_in_background` returns request_id + later notifies | ❌ dead surface; clean reject only (D4) | ⚠️ honestly *not advertised* (removed), or fully wired later |
| Delegation never stuck "in progress" across restart | ❌ wedges + starves quota (D3) | ✅ boot-reconcile to `stalled` |
| Cancelled delegation releases resources | ❌ drain task + slot counters leak (D2, D5) | ✅ `finally`-cancel + guard-up |
| Hard wall-clock budget is a true ceiling | ⚠️ soft by up to 30s (D10); ceiling==default (D9) | ✅ shared budget + decoupled ceiling |
| Per-task fan-out spec consistent with single spawn | ⚠️ stricter `required`, narrower fields (D11) | ✅ aligned |
| Internal accounting / docs honest | ❌ dead `_lock`, stale caps, undeclared deps, dead helpers (D6,D8,D12,D13) | ✅ cleaned |

**Net:** The system currently *behaves* like Claude Code at the user boundary in two cases by accident (depth-1 cap via hard-reject; background returns a clean rejection), while genuinely diverging on **tool-subset enforcement (D1)** and **restart durability (D3)** — the two changes that most matter for safety and reliability. After Phase 1, the system matches Claude Code on containment and durability; after Phases 2-3, the advertised contract (depth, background, budget, caps, deps) stops lying about the wiring.
```