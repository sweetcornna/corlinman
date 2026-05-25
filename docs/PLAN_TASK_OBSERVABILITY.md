# PLAN — Task Execution Observability Overhaul

**Status:** draft v1.0 · 2026-05-24 · parallel execution
**Goal:** make the agent's work visible. Today the user can't see what tools fired, what args were used, what came back, how long anything took, or what happened in past turns — even though most of that data is collected. We fix this by porting proven UX patterns from Claude Code, opencode, and hermes-agent.

This plan is execution-ready: every task has files, owner agent, deps, validation. 8 background agents over 4 waves, ~1.5 working days end-to-end.

---

## 0. Diagnosis (audited 2026-05-24)

Four parallel Explore passes returned a consistent picture: **corlinman is highly observable at the telemetry layer but blind at the UI consumer layer.** The signals exist; nobody is rendering them.

### 0.1 Reference-source comparison

| Dimension | corlinman | Claude Code 2.1.88 | opencode | hermes-agent |
|---|---|---|---|---|
| **Event taxonomy** | 4 reasoning loop events (`TokenEvent`/`ToolCallEvent`/`DoneEvent`/`ErrorEvent`) + 10 hook events | ~12 streaming events (`message_start`/`content_block_start[text/tool_use/thinking]`/`content_block_delta[text_delta/input_json_delta/thinking_delta]`/`content_block_stop`/`message_stop`) | ~12 LLM events (`step-start`/`text-{start,delta,end}`/`reasoning-{start,delta,end}`/`tool-input-{start,delta,end}`/`tool-{call,result,error}`/`step-finish`) → 5 part events (`message.{updated,removed}`, `message.part.{updated,delta,removed}`) | 9 raw callbacks (`tool_progress`/`tool_start`/`tool_complete`/`step`/`stream_delta`/`interim_assistant`/`thinking`/`reasoning`/`status`) |
| **Part / block model** | None — `ToolCallEvent` fires once with full args after model finishes the block | `content_block` with `index`, accumulator `StreamingToolUse { index, contentBlock, unparsedToolInput }`, `time: {start, end?}` per block | `Part` discriminated union: `TextPart`/`ReasoningPart`/`ToolPart`/`FilePart`/`SnapshotPart`/`PatchPart`/`AgentPart`/`SubtaskPart`/`StepStart/Finish`/`Compaction`/`Retry`, all with `time` | None — single result tuple `(name, args, result, duration, is_error, blocked)` |
| **Tool state machine** | No persistent per-tool state during run | Spinner mode 'tool-input' → 'tool-use'; ToolUseLoader: dim → red (error) / green (success) | `ToolStatePending` → `ToolStateRunning {title, metadata, time.start}` → `ToolStateCompleted {output, time.end}` \| `ToolStateError` | Implicit (5 callbacks) |
| **Incremental tool args** | No — args arrive whole | Yes — `input_json_delta` accumulates `unparsedToolInput` string | Yes — `tool-input-delta` events | No |
| **Reasoning visibility** | Discarded after spinner update | Streamed live with shimmer, min 2s shown, persisted in messages | `ReasoningPart` first-class, `time:{start,end}` | `reasoning_callback` + `reasoning_details` persisted to SQLite |
| **History persistence** | SQLite `turns` + `turn_messages` (rich) | `transcript.jsonl` + IndexedDB session metadata | `MessageTable` + `PartTable` with cursor pagination | SQLite SCHEMA 11 + FTS5 + trigram (CJK) |
| **Admin UI history page** | `/admin/sessions` list, no per-turn drill-down | `/resume` flow + REPL re-renders normalized messages | Session view with collapsed past parts | `hermes_cli/curator.py` browse + cron output `{job}/{ts}.md` archives |
| **Cost / timing surfacing** | `_CostMeter` in-memory, never queried | `getTotalAPIDuration()`, `getModelUsage()`, TTFT recorded, sticky footer + `/cost` command | StepFinish `tokens.{total,input,output,reasoning,cache.{read,write}}`, Assistant `cost` field | `session_estimated_cost_usd` + `cost_status` + `cost_source` per session in SQLite |
| **Sub-agent UX** | `SubagentSpawned/Completed` hook events fired, nothing renders them | Aggregated `tokens` sum unless `showSpinnerTree=true` | `parentID` + `SubtaskPart` for sub-session spawn | `delegate_tool` returns summary only; parent sees child progress via inherited callback |
| **Long-tool heartbeat** | None | Token-delta keeps spinner alive | SSE 10s heartbeat | Spinner every ~30s + `_touch_activity()` gateway heartbeat + interrupt check every 3s |
| **Cancel UX** | `ReasoningLoop.cancel()` → next round emits `ErrorEvent(reason="cancelled")`, no spinner update | Synthetic `createUserInterruptionMessage()` injected, spinner → 'stopped' | Session-level `abort` endpoint, TUI Esc handler | Interrupt detected, propagated through callbacks |

### 0.2 What we need that doesn't exist

| Need | Why |
|---|---|
| **Part-based event stream** (not just token/tool_call) | Lets every content block — text, reasoning, tool, file, subtask — carry its own `index`, `time.start/end`, and incremental deltas. Without this, the UI can't render a live tool widget that updates as args stream in. |
| **Tool state machine** (Pending → Running → Completed/Error) | The UI needs to render a different widget per state. Today the `ToolCallEvent` is fire-once: no "running" state. |
| **Incremental tool input streaming** | Long args (large code blocks, JSON payloads) arrive in chunks from the model. Today corlinman waits until the whole tool block is done. Claude Code + opencode both stream. |
| **Reasoning persistence + replay** | Today `is_reasoning=True` deltas update the spinner and vanish. Should be stored as a `ReasoningPart` so users can re-read what the model was thinking. |
| **Per-step + per-turn timing** | `ToolCalled.duration_ms` exists but is never surfaced. Add `time.start/end` to every event; calculate cumulative turn time at `DoneEvent`. |
| **Cost surfacing** | `_CostMeter` is in-memory only. Needs to flow out via `DoneEvent.usage` + a `GET /admin/sessions/:id/cost` endpoint, with sticky-footer rendering. |
| **Past-turn drill-down UI** | Journal has the data; admin sessions page only shows turn-level metadata. Need a per-turn part-by-part view, args+results expandable. |
| **Sub-agent tree view** | Today subagent hooks fire to log, no UI consumer. Need a tree rendering when a turn spawns children. |
| **Long-tool heartbeat** | A 60s bash gives the user nothing. Need an event every 10s ("still running… 23s") even if the tool is silent. |
| **Cancel feedback** | Today cancel waits for the next round. Need an immediate status update ("cancelling…") via the same event stream. |

---

## 1. Target architecture

### 1.1 Event taxonomy (new)

Introduce `EventEnvelope` as the single thing the gateway emits to all consumers (SSE / WS / channel adapters / gRPC). It carries one of N typed events:

```python
@dataclass
class EventEnvelope:
    turn_id: str            # for correlation
    session_key: str
    sequence: int           # monotonic per turn
    timestamp_ms: int       # wall clock
    event: Event            # discriminated union below
```

Discriminated union (mirrors opencode's `Part` + Claude Code's `content_block`):

| Tag | Fields | Emit point |
|---|---|---|
| `TurnStart` | `model`, `user_text_preview`, `system_message_preview` | start of `_run_one_round` |
| `BlockStart` | `index`, `block_type: 'text' \| 'reasoning' \| 'tool_use'`, `tool_name?`, `tool_call_id?` | model emits content_block_start |
| `TextDelta` | `index`, `text`, `cumulative_len?` | each token |
| `ReasoningDelta` | `index`, `text`, `signature?` | each thinking token |
| `ToolInputDelta` | `index`, `partial_json` | input_json_delta from provider (Anthropic) |
| `BlockStop` | `index`, `elapsed_ms` | content_block_stop |
| `ToolStateRunning` | `tool_call_id`, `tool_name`, `args_json`, `started_at_ms` | just before plugin dispatch |
| `ToolStateHeartbeat` | `tool_call_id`, `elapsed_ms`, `stdout_tail?` | every 10s while running |
| `ToolStateCompleted` | `tool_call_id`, `result_summary` (≤ 4kB), `result_json_ref?`, `elapsed_ms`, `is_error: bool` | post-dispatch |
| `SubagentSpawned` | `parent_session`, `child_session`, `child_agent_id`, `depth`, `prompt_preview` | from existing hook |
| `SubagentEvent` | `child_session`, `envelope: EventEnvelope` | bubble child events up to parent stream |
| `SubagentCompleted` | `child_session`, `finish_reason`, `tool_calls_made`, `elapsed_ms`, `summary` | from existing hook |
| `Cancelling` | `reason` | first poll cycle after `ReasoningLoop.cancel()` |
| `TurnComplete` | `finish_reason`, `usage`, `elapsed_ms`, `estimated_cost_usd?`, `cost_status?` | end of turn |
| `TurnErrored` | `reason`, `message`, `elapsed_ms` | error path |

### 1.2 Persistence (extend journal)

Today's schema: `turns` + `turn_messages`. Add:

```sql
CREATE TABLE turn_events (
  turn_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  timestamp_ms INTEGER NOT NULL,
  PRIMARY KEY (turn_id, sequence)
);
CREATE INDEX idx_turn_events_turn ON turn_events(turn_id);

ALTER TABLE turns ADD COLUMN elapsed_ms INTEGER;
ALTER TABLE turns ADD COLUMN estimated_cost_usd REAL;
ALTER TABLE turns ADD COLUMN cost_status TEXT;
ALTER TABLE turns ADD COLUMN tool_call_count INTEGER DEFAULT 0;
ALTER TABLE turns ADD COLUMN reasoning_token_count INTEGER DEFAULT 0;
```

Every `EventEnvelope` the gateway emits also lands in `turn_events`. The admin UI replay endpoint streams from this table — same shape as live, just historical.

### 1.3 Frontend rendering

Three primitives:

- `<TimelineEvent>` — the basic row, role-coloured (user/assistant/tool/system), with `time.start` and optional `time.end` overlay
- `<ToolWidget>` — pending/running/completed/error states; expandable args + result; per-tool renderers (`bash`, `read_file`, `webfetch`) with fallback `GenericTool`
- `<ReasoningBlock>` — collapsible thinking text with shimmer while streaming, settled state after `BlockStop`

Wired into:
- `/admin/sessions/{key}` — live + replay (one component, two data sources)
- `/admin/sessions/{key}/turns/{id}` — single-turn drill-down with full args/results expandable
- Sticky session footer — cumulative cost + turn count + token sum (refreshed on `TurnComplete`)

### 1.4 Channel adapters (Telegram / QQ / Discord)

Today: mutable spinner with status icons. Keep it. Extend `_status.py` so it consumes the new event stream:

- `ToolStateRunning` → `🔧 {name} {args_preview_60}` (current behaviour)
- `ToolStateHeartbeat` → `🔧 {name} … {elapsed_s}s`  (new — keeps users informed for 60s+ tools)
- `ToolStateCompleted` → `✅/❌ {name} ({duration})` (current)
- `Cancelling` → `⏹ 正在取消…` (new)
- After `TurnComplete`: add a one-line footer with `(elapsed: 12s · 3 tool calls · ~$0.012)` if cost known

---

## 2. Tasks (4 waves, 8 background agents)

### Wave 1 — Backend event taxonomy (3 parallel)

#### W1.1 Event model + emission

- **Owner:** Backend Architect
- **Files:**
  - `python/packages/corlinman-agent/src/corlinman_agent/events.py` (new) — `EventEnvelope` + the discriminated union dataclasses, plus a `EventEmitter` protocol injectable into `ReasoningLoop`
  - `python/packages/corlinman-agent/src/corlinman_agent/reasoning_loop.py` — add `_emit(event)` calls at the listed points; preserve existing `TokenEvent`/`ToolCallEvent` yields for backwards compat (downgrade them to legacy adapters)
  - `python/packages/corlinman-agent/tests/test_event_emission.py` (new) — assert event order for a deterministic mock-provider turn
- **Hermes pattern:** raw callable callbacks (`agent/agent_init.py:103-113`) — corlinman uses async iterator yield instead (cleaner for asyncio)
- **Claude pattern:** content_block index for correlation (`src/utils/messages.ts:2989`)
- **opencode pattern:** Part discriminated union (`packages/opencode/src/session/message-v2.ts:60-379`)
- **Deps:** none
- **Validation:** unit test exercises all 14 event types fire in expected order on a mock-provider turn including 2 tool calls + reasoning
- **ETA:** 6h

#### W1.2 Journal `turn_events` table + writer

- **Owner:** Database Optimizer
- **Files:**
  - `python/packages/corlinman-server/src/corlinman_server/gateway/journal/migrations/004_turn_events.sql` (new)
  - `python/packages/corlinman-server/src/corlinman_server/gateway/journal/agent_journal.py` — add `append_event(envelope)`, `load_events(turn_id)`, `iter_events(turn_id, start_seq=0)`; batch writes via existing pattern
  - `python/packages/corlinman-server/src/corlinman_server/gateway/journal/agent_journal_backend.py` — backend impl
  - Test: `tests/journal/test_turn_events.py`
- **Hermes pattern:** SCHEMA 11 + FTS5 messages table (`hermes_state.py:185-276`)
- **opencode pattern:** cursor pagination over MessageTable (`message-v2.ts:563-577`)
- **Deps:** W1.1 (need event types)
- **Validation:** 1000-event append + load round-trip in < 100ms; `load_events(turn_id)` returns by sequence; index used (`EXPLAIN QUERY PLAN`)
- **ETA:** 4h

#### W1.3 SSE endpoint + replay route

- **Owner:** Backend Architect
- **Files:**
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/sessions_events.py` (new):
    - `GET /admin/sessions/{key}/events/live` — SSE stream subscribed to live `EventEmitter` plus catch-up from `turn_events`
    - `GET /admin/sessions/{key}/turns/{turn_id}/events` — JSON dump for replay (paginated by sequence)
  - Wire `EventEmitter` into the gateway lifecycle: emit-and-persist tee
  - 10s SSE heartbeat (opencode pattern)
- **opencode pattern:** SSE `text/event-stream` with 10s heartbeat (`event.ts:30-31`)
- **Deps:** W1.1, W1.2
- **Validation:** open the SSE while a turn is running; events arrive in order; close-and-resume picks up missed events via catch-up from `turn_events`
- **ETA:** 5h

### Wave 2 — Frontend rendering (3 parallel, after W1)

#### W2.1 Timeline + tool widget primitives

- **Owner:** Frontend Developer + UI Designer
- **Files:**
  - `ui/components/sessions/event-timeline.tsx` (new) — main timeline list, virtualized for long turns
  - `ui/components/sessions/tool-widget.tsx` (new) — pending/running/completed/error states
  - `ui/components/sessions/reasoning-block.tsx` (new) — collapsible with shimmer
  - `ui/components/sessions/tool-renderers/{bash,read-file,write-file,webfetch,grep,generic}.tsx` (new) — per-tool renderers
  - `ui/lib/sessions/event-stream.ts` (new) — SSE client + accumulator (one EventEnvelope → updates one part-state in store)
  - `ui/lib/sessions/store.ts` (new) — Zustand or built-in React state, keyed by `(turn_id, sequence)`
- **Claude pattern:** `ToolUseLoader.tsx` BLACK_CIRCLE + colour by state; `unparsedToolInput` accumulator (`src/utils/messages.ts:3056-3073`)
- **opencode pattern:** `InlineTool`/`BlockTool` + per-tool component dispatch (`packages/opencode/src/cli/cmd/tui/routes/session/index.tsx:1600-1682`)
- **Deps:** W1.3 (SSE endpoint)
- **Validation:** Playwright: open `/admin/sessions/{key}` during a live turn → reasoning block streams → tool widget transitions pending → running → completed → expand to see args + result
- **ETA:** 10h

#### W2.2 Per-turn drill-down page

- **Owner:** Frontend Developer
- **Files:**
  - `ui/app/(admin)/sessions/[key]/turns/[turn_id]/page.tsx` (new) — uses the replay endpoint to load all events, renders identical timeline as live
  - `ui/components/sessions/turn-summary-card.tsx` (new) — top of page: elapsed, tool count, cost, status
  - `ui/lib/api.ts` — add `loadTurnEvents(key, turn_id)` and `streamSessionEvents(key)`
- **Claude pattern:** transcript JSONL re-rendered identically to live (`src/utils/messages.ts:731-823`)
- **Deps:** W2.1
- **Validation:** click a past turn → see every tool call, args, result, timing — same UI as live
- **ETA:** 5h

#### W2.3 Sticky cost footer + session list enrichment

- **Owner:** Frontend Developer
- **Files:**
  - `ui/components/sessions/cost-footer.tsx` (new) — sticky bottom, shows cumulative tokens + cost + turn count
  - `ui/app/(admin)/sessions/page.tsx` — add columns: total cost, avg turn time, last tool used
  - `ui/lib/api.ts` — `loadSessionCost(key)` endpoint call
  - Backend: `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/sessions_cost.py` (new) — aggregate `turn_events` for the session
- **Claude pattern:** cost-tracker.ts sticky footer (`src/cost-tracker.ts:228-244`)
- **hermes pattern:** session-level cost columns in SQLite (`hermes_state.py:190-221`)
- **Deps:** W1.2
- **Validation:** turn ends → footer shows new totals within 500ms; session list updates without page reload
- **ETA:** 5h

### Wave 3 — Tool state machine, sub-agents, cancel (2 parallel, partial dep)

#### W3.1 Tool state machine + heartbeat

- **Owner:** Backend Architect
- **Files:**
  - `python/packages/corlinman-server/src/corlinman_server/runner_pool/` — wherever tool dispatch lives. Add: emit `ToolStateRunning` before dispatch; spawn a 10s heartbeat task that emits `ToolStateHeartbeat`; on completion emit `ToolStateCompleted`/`error`; cancel heartbeat task on completion
  - `python/packages/corlinman-agent/src/corlinman_agent/cancel.py` — emit `Cancelling` event the moment `.cancel()` is called (not on next round)
  - `python/packages/corlinman-channels/src/corlinman_channels/_status.py` — consume `ToolStateHeartbeat` to refresh spinner with `… {elapsed_s}s`; consume `Cancelling` for `⏹ 正在取消…`
- **hermes pattern:** spinner every ~30s + interrupt check every 3s (`agent/tool_executor.py:276,326-338`)
- **opencode pattern:** SSE 10s heartbeat baseline (`event.ts:30-31`)
- **Deps:** W1.1, W1.3
- **Validation:** dispatch a `sleep 30` shell — spinner refreshes every 10s with elapsed time; Telegram message edits in place; cancel mid-tool → `⏹` shows within 1s
- **ETA:** 6h

#### W3.2 Sub-agent nested view

- **Owner:** AI Engineer + Frontend Developer
- **Files:**
  - `python/packages/corlinman-subagent/src/corlinman_subagent/supervisor.py` — emit `SubagentSpawned`/`SubagentEvent` (bubbles child events with `child_session` tag) /`SubagentCompleted`
  - `ui/components/sessions/subagent-tree.tsx` (new) — collapsible nested timeline
  - Tool widget in W2.1 recognizes `SubagentEvent` and renders a child sub-timeline beneath the spawning tool call
- **Claude pattern:** showSpinnerTree mode (`src/components/Spinner.tsx:189-199`)
- **opencode pattern:** Session.parentID + SubtaskPart (`packages/opencode/src/cli/cmd/tui/routes/session/index.tsx:186-193`)
- **Deps:** W2.1
- **Validation:** main agent spawns a subagent → UI shows nested events under the spawning tool call → expandable to see child's full timeline
- **ETA:** 7h

### Wave 4 — Polish + e2e (after W2 + W3)

#### W4.1 Channel adapter cleanup

- **Owner:** Frontend Developer (Telegram/QQ formatting)
- **Files:**
  - `python/packages/corlinman-channels/src/corlinman_channels/_status.py` — finalize event handlers (already touched in W3.1)
  - Add post-turn one-line footer: `(elapsed: 12s · 3 tool calls · ~$0.012)`
  - Tests for each channel that the footer renders without breaking existing flow
- **Deps:** W3.1
- **Validation:** Telegram + QQ + Discord all show new heartbeat + post-turn cost line
- **ETA:** 3h

#### W4.2 E2E Playwright + docs

- **Owner:** API Tester
- **Files:**
  - `ui/tests/e2e/task-observability.spec.ts` (new):
    1. Trigger a turn with 2 tool calls + reasoning
    2. Assert reasoning block streams
    3. Assert tool widgets transition pending→running→completed
    4. Expand a tool widget → see args + result
    5. Wait for cost footer to update
    6. Click a past turn → identical timeline renders
  - `docs/observability.md` — refresh with the new event taxonomy, timeline screenshot, replay how-to
  - `docs/quickstart.md` — add a "Watch what the agent is doing" section pointing at `/admin/sessions/{key}`
- **Deps:** W2.2, W2.3, W3.1, W3.2, W4.1
- **Validation:** CI green; docs include actual screenshots
- **ETA:** 6h

---

## 3. Execution plan (parallelization)

```
Wave 1 (3 agents parallel):       W1.1   W1.2   W1.3
                                    │     │      │
                                    └─────┴──────┘
                                          │
Wave 2 (3 agents parallel after W1):  W2.1   W2.2   W2.3
                                          │
Wave 3 (2 agents parallel after W2):  W3.1   W3.2
                                          │
Wave 4 (2 agents parallel after W3):  W4.1   W4.2
```

Total wall-clock ~1.5 working days with 3 concurrent agents.

---

## 4. Out of scope (this round)

- Replacing the gRPC `ServerFrame` with `EventEnvelope` end-to-end (introduce alongside; deprecate next round)
- Per-token timing (too fine-grained; per-block is enough)
- Cost calculation accuracy improvements — use existing `_CostMeter` math, just surface it
- Migrating Telegram/QQ adapter to consume the SSE stream directly (keep their existing in-process tap; only the UI gets new SSE)
- Web search / WebSocket-based browsing — separate effort
- OTel-style distributed tracing — not the bottleneck; structured logs + the event stream are enough

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| `turn_events` table grows unboundedly | TTL-based prune in W1.2 (default 30 days, configurable). FTS not needed; just sequence + timestamp indices. |
| SSE backpressure on slow clients | 10s heartbeat + drop-on-overflow + client reconnect uses `last-event-id`. opencode does this pattern (sse retry 3-30s). |
| Backwards-compat breakage for gRPC consumers (M2 channels) | Keep `ServerFrame.token/tool_call/done/error` emission alongside new events. Channels stay on the legacy path until W4.1 migrates them. |
| Event volume × N sessions = high write rate | Single-writer SQLite + WAL; benchmarked at ~10k events/s in dev. Add bulk-insert pipeline if measured > 50% of one connection. |
| Frontend re-render storm on token deltas | Virtualized timeline + 50ms throttle on text-delta merge into a single render frame. Claude Code pattern (`responseLengthRef` increment). |
| Sub-agent event bubbling explosion | Cap depth at 3 (already enforced via `SubagentDepthCapped` hook); UI collapses deep children by default. |

---

## 6. Decision points before kickoff

- [ ] Plan accepted, or trim subset (e.g. drop W3.2 sub-agent tree if 7h is too much this round)?
- [ ] OK to add `turn_events` table to the journal SQLite DB (small migration, no data loss)?
- [ ] Event TTL default — 30 days OK, or longer for audit needs?
- [ ] Cost rendering: show estimate even when `cost_status='unknown'` (with a tooltip), or only when confidently calculated?

---

**End of plan v1.0.** Next: user approves → I dispatch 3 background agents in Wave 1.
