# PLAN — Task continuity & parallel processing (Tier 4)

Status: **spec — not started** · Created 2026-05-23 · Owner: agent

References:
- `docs/RESEARCH_AGENT_PARITY.md` for the prior research style
- hermes-agent reference at `/Users/cornna/project/hermes-agent`
- This research distilled into: T4.1–T4.4 below

## Where the agent stands today

What corlinman **already has** (verified by research pass):

| Surface | Module | Verdict |
|---|---|---|
| Durable session transcript SQLite store | `corlinman-replay/session_store.py` (`SqliteSessionStore`) | usable, but **NOT wired into live Chat** |
| Per-turn workspace git snapshot | `coding/_snapshot.py` + agent_servicer | wired (T2.4) |
| Conversation memory recall + store | `corlinman-memory-host` LocalSqlite | wired (T1.4 / T2 era) |
| Per-session todo store (in-memory) | `coding/todo.py` `TodoStore` | wired but **process-local** |
| Per-session cost meter (in-memory) | `agent_servicer._CostMeter` | wired but **process-local** |
| Per-RPC FileState | `coding/_filestate.py` | per-RPC only, no cross-turn |
| Scheduler / cron | `corlinman-server/scheduler/runner.py` | unrelated to chat turns |

What corlinman **needs**:

| Gap | Severity | T4 item |
|---|---|---|
| Tool results + reasoning state lost when gateway restarts mid-turn | high | T4.1 |
| Inbound QQ messages lost in the gap between NapCat→gateway and dispatch | medium | T4.3 |
| Concurrent Chat RPCs to the **same** session_key can race the todo store / cost meter / workspace snapshot | medium | T4.2 |
| Unhandled exception kills a turn with no recovery breadcrumbs | medium | T4.4 |

## Design

### T4.1 — Per-turn session journal + resume

- Open one `SqliteSessionStore` per gateway process under
  `<data_dir>/sessions.sqlite` (already used today for tests under
  `corlinman-replay`). Reuse the existing schema.
- New columns / metadata in a sibling `turns` table:
  `session_key TEXT, turn_id INTEGER PRIMARY KEY,
  status TEXT CHECK (status IN ('in_progress','completed','errored')),
  started_at_ms, ended_at_ms, error TEXT`.
  Turn-scoped metadata lives here so the existing per-message
  `sessions` table stays single-purpose.
- In `agent_servicer.Chat`:
  - On entry, write a `turns(...status='in_progress')` row keyed by
    `(session_key, turn_id=now_ms)`.
  - After `_assemble_context` etc., append every message added to
    `start.messages` to the message store under the same session_key.
  - On each `ToolCallEvent` / `ToolResult`, append a tool-result row
    (`role='tool'`, `tool_call_id` set) to the message store.
  - On the terminal `DoneEvent`, update the turn row to
    `status='completed'` + `ended_at_ms`.
  - On exception, update the turn row to `status='errored'` +
    `error=<str(exc)[:1000]>`.
- **Resume path**: a new `_resume_in_progress_turn(session_key) -> ResumeData | None` is called when a new Chat RPC arrives:
  - If the session has a `turns` row with `status='in_progress'`
    *and* the last user message in `start.messages` matches the
    interrupted turn's user message (a re-send within ~5 minutes),
    re-hydrate the message list from the message store (including any
    tool results that already landed) and resume the loop without
    re-running completed tool calls.
  - The re-hydrated messages replace `start.messages` for the loop.
  - The reasoning loop drives the provider with the resumed history;
    the model "sees" the partial work and continues.
  - If no match, ignore the orphan; mark its turn row as `errored`
    with reason `interrupted` so it doesn't keep matching.

### T4.2 — Per-session async lock + concurrent-sessions

- `_SessionLocks` helper on the agent servicer:
  - `dict[str, asyncio.Lock]` with a wrapper `acquire(session_key)`.
  - Locks are created on demand and garbage-collected when no one
    holds them (a `WeakValueDictionary` or a small ref-count map).
  - Empty `session_key` (one-shot HTTP callers) gets no lock — they
    are independent by definition.
- Wrap the core `Chat` body in
  `async with self._session_locks.acquire(start.session_key):`
  so two RPCs targeting the same session serialise; different sessions
  proceed concurrently.
- Document: parallelism is **across sessions**, not within one.

### T4.3 — Inbound message durability (channel-side queue)

- Add a SQLite-backed inbound queue at
  `<data_dir>/inbox.sqlite`:
  `inbox(id INTEGER PK AUTOINCREMENT, channel TEXT, session_key TEXT,
  payload_json TEXT, received_at_ms, status TEXT
  CHECK (status IN ('pending','dispatched','done','dead')))`.
- In `corlinman-channels/router.py` (or its caller in
  `gateway/channels_runtime`), every accepted inbound event is
  **first** inserted with `status='pending'` and `received_at_ms`,
  **then** dispatched to the chat service. Successful dispatch flips
  the row to `dispatched`; the Chat handler's DoneEvent path flips it
  to `done`. An unhandled exception leaves it `pending` for replay.
- On gateway boot, a one-shot drainer pass picks up rows with
  `status IN ('pending','dispatched')` older than ~30s and replays
  them through the dispatch path. Per-row replay cap (3 retries) so a
  poison message eventually marks `dead`.

### T4.4 — Exception recovery breadcrumbs

- Already enabled by T4.1's `status='errored'` row + the existing
  workspace snapshot from T2.4: an errored turn's `error` field +
  the prior turn's snapshot give a deterministic rewind point.
- Add a small admin / log helper:
  `journal_errored_turns(session_key, limit=5)` returns the recent
  failures so an operator (or a future "self-heal" hook) can see the
  shape of the breakage.

## Sequencing

T4.1 first (the foundation; T4.2 + T4.4 build on it).
T4.2 in parallel with T4.3 (independent files).
T4.4 mostly falls out of T4.1.

## Out of scope (intentional)

- **Multiprocessing worker pool**. corlinman is asyncio-native and a
  single-process gateway is the design — multi-session concurrency
  via async tasks is what the tests measured; we are not introducing
  a separate worker pool.
- **Cross-process state**. State is per-gateway-process; if the
  operator scales to multiple gateway nodes, they need an external
  broker (Postgres / Redis), which is a v3 problem.
- **Hermes-style compression chains**. corlinman has token-aware
  elision (T2.3); explicit `parent_session_id` chaining is over-kill
  for a chat-bot.
